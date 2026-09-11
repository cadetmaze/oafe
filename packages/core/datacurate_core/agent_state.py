"""Durable state for a dataset-request agent run.

Everything the agent does is written to Postgres as it happens rather than
streamed to the browser. That is a product requirement, not an optimization:
the user watches a long-running search, and a refresh, a closed laptop or a
second tab must all show the same real progress. The UI polls `get_state()`
and `events_since()`; nothing lives only in memory.

Mirrors `queue.py`'s style — plain SQL, dict rows, no ORM.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .db import cursor, fetch_all, fetch_one
from .queue import enqueue_in_transaction

# Phases, in order. The UI renders these; the worker advances them.
PHASES = (
    "QUEUED",
    "PLANNING",
    "DISCOVERING",
    "SCREENING",
    "SAMPLING",
    "JUDGING",
    "REVIEW_READY",
    "AWAITING_REVIEW",
    "REFINING",
    "BUILDING",
    "EXPORTING",
    "DONE",
)

# How many examples to put in front of the user, by how many rows they asked
# for. Deliberately sublinear: the point of the review is to catch "this is the
# wrong kind of data" early, and that is visible in ~10 items. Asking someone to
# vet 50 images to earn a 5,000-row dataset would just get clicked through.
_REVIEW_SIZE_TABLE = ((200, 6), (1_000, 10), (2_500, 12), (5_000, 15))
_REVIEW_SIZE_MAX = 18


def review_size_for(example_count: int) -> int:
    for threshold, size in _REVIEW_SIZE_TABLE:
        if example_count <= threshold:
            return size
    return _REVIEW_SIZE_MAX


# ── Runs ──────────────────────────────────────────────────────────────


class RequestOwnershipConflict(Exception):
    """The request was claimed by a different project."""


class RequestNotFound(Exception):
    """The request disappeared before its run could be started."""


class ExportJobConflict(Exception):
    """A different export is already running for this agent run."""

    def __init__(self, job: dict):
        super().__init__("a different export is already in progress")
        self.job = job


def _create_run(cur: Any, request_id: str, job_id: str, review_size: int) -> dict:
    cur.execute(
        """
        insert into agent_runs (request_id, job_id, review_size, phase, status)
        values (%s, %s, %s, 'QUEUED', 'RUNNING')
        on conflict (request_id) do update
          set job_id = excluded.job_id,
              review_size = excluded.review_size,
              phase = 'QUEUED',
              status = 'RUNNING',
              progress = 0,
              error = null,
              updated_at = now()
        returning *
        """,
        (request_id, job_id, review_size),
    )
    return cur.fetchone()


def create_run(request_id: str, job_id: str, review_size: int) -> dict:
    """One run per request; re-starting a request reuses (and resets) its run."""
    with cursor() as cur:
        return _create_run(cur, request_id, job_id, review_size)


def start_run(
    request_id: str,
    project_id: str,
    review_size: int,
    example_count: int,
) -> tuple[dict, bool]:
    """Claim a request and create its first job/run exactly once.

    Locking the request row serializes concurrent browser starts, including
    React StrictMode duplicates. The job, run and first event share this
    transaction, so a worker cannot see the job until all run state exists.
    """
    with cursor() as cur:
        cur.execute(
            "select project_id from dataset_requests where id = %s for update",
            (request_id,),
        )
        request = cur.fetchone()
        if not request:
            raise RequestNotFound(request_id)

        owner_id = request.get("project_id")
        if owner_id and str(owner_id) != str(project_id):
            raise RequestOwnershipConflict(request_id)
        if not owner_id:
            cur.execute(
                "update dataset_requests set project_id = %s where id = %s",
                (project_id, request_id),
            )

        cur.execute("select * from agent_runs where request_id = %s", (request_id,))
        existing = cur.fetchone()
        if existing and existing["status"] != "FAILED":
            return existing, True

        job_id = enqueue_in_transaction(
            cur,
            "agent_request",
            {"request_id": request_id},
        )
        run = _create_run(cur, request_id, job_id, review_size)
        _emit_event(
            cur,
            str(run["id"]),
            "explored",
            "Explored your request",
            {"example_count": example_count},
        )
        return run, False


def get_run(run_id: str) -> Optional[dict]:
    return fetch_one("select * from agent_runs where id = %s", (run_id,))


def get_run_for_request(request_id: str) -> Optional[dict]:
    return fetch_one("select * from agent_runs where request_id = %s", (request_id,))


_RUN_UPDATABLE = {
    "phase",
    "status",
    "progress",
    "spec",
    "review_size",
    "review_batch",
    "threshold",
    "stats",
    "error",
    "build_job_id",
    "export_job_id",
    "dataset_id",
    "version_id",
}
_RUN_JSON_FIELDS = {"spec", "stats"}


def update_run(run_id: str, **fields: Any) -> None:
    """Patch any subset of the run's columns. Unknown names are a programming
    error and raise rather than silently doing nothing."""
    unknown = set(fields) - _RUN_UPDATABLE
    if unknown:
        raise ValueError(f"not updatable on agent_runs: {sorted(unknown)}")
    if not fields:
        return
    sets, params = [], []
    for key, value in fields.items():
        sets.append(f"{key} = %s")
        params.append(json.dumps(value) if key in _RUN_JSON_FIELDS else value)
    params.append(run_id)
    with cursor() as cur:
        cur.execute(
            f"update agent_runs set {', '.join(sets)}, updated_at = now() where id = %s",
            tuple(params),
        )


def set_phase(run_id: str, phase: str, progress: float | None = None, **fields: Any) -> None:
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}")
    if progress is not None:
        fields["progress"] = progress
    update_run(run_id, phase=phase, **fields)


def fail_run(run_id: str, error: str) -> None:
    update_run(run_id, status="FAILED", error=error[:2000])


# ── Exports ───────────────────────────────────────────────────


_TERMINAL_JOB_STATUSES = {"CANCELLED", "COMPLETED", "FAILED"}
_EXPORT_IDENTITY_FIELDS = ("version_id", "dataset_id", "version", "mode", "zip", "hf_repo")


def _same_export(left: dict, right: dict) -> bool:
    def normalized(payload: dict, key: str) -> Any:
        value = payload.get(key)
        if key == "mode":
            return value or "references"
        if key == "zip":
            return bool(value)
        if key in {"version_id", "dataset_id"} and value is not None:
            return str(value)
        return value

    return all(normalized(left, key) == normalized(right, key) for key in _EXPORT_IDENTITY_FIELDS)


def ensure_export_job(
    run_id: str,
    payload: dict[str, Any],
    *,
    prefer_current: bool = False,
    dataset_id: str | None = None,
    version_id: str | None = None,
) -> tuple[dict, bool]:
    """Return one durable export job for a run, creating it when needed.

    The run row is the serialization point. The job and the run pointer commit
    together, so neither a concurrent API click nor a fast export worker can
    observe a queued job without its owning run already pointing at it.

    ``prefer_current`` is used by the build worker: if a user-selected export
    somehow already owns the run, the automatic references export must not
    replace it. API callers instead get ``ExportJobConflict`` when another
    export mode is still active.
    """
    with cursor() as cur:
        cur.execute("select * from agent_runs where id = %s for update", (run_id,))
        run = cur.fetchone()
        if not run:
            raise ValueError(f"unknown agent run {run_id}")

        current = None
        if run.get("export_job_id"):
            cur.execute("select * from jobs where id = %s", (run["export_job_id"],))
            current = cur.fetchone()

        if current and current.get("type") == "agent_export":
            status = current.get("status") or "QUEUED"
            same_request = _same_export(current.get("input") or {}, payload)
            active = status not in _TERMINAL_JOB_STATUSES
            completed = status == "COMPLETED"

            if prefer_current and (active or completed):
                return current, True
            if active:
                if same_request:
                    return current, True
                raise ExportJobConflict(current)
            if completed and same_request:
                return current, True

        job_id = enqueue_in_transaction(cur, "agent_export", payload)
        assignments: list[str] = [
            "export_job_id = %s",
            "phase = 'EXPORTING'",
            "status = 'RUNNING'",
            "progress = 0.85",
            "error = null",
            "updated_at = now()",
        ]
        params: list[Any] = [job_id]
        if dataset_id is not None:
            assignments.append("dataset_id = %s")
            params.append(dataset_id)
        if version_id is not None:
            assignments.append("version_id = %s")
            params.append(version_id)
        params.append(run_id)
        cur.execute(
            f"update agent_runs set {', '.join(assignments)} where id = %s",
            tuple(params),
        )
        cur.execute("select * from jobs where id = %s", (job_id,))
        return cur.fetchone(), False


def finish_export_job(
    job_id: str,
    *,
    output: dict[str, Any] | None = None,
    error: str | None = None,
) -> bool:
    """Finish an agent export and its current run in one transaction.

    Returns whether this job still owned a run. A stale job is still made
    terminal, but cannot mark a run ``DONE`` after a newer export replaced it.
    """
    with cursor() as cur:
        cur.execute(
            "select id, phase from agent_runs where export_job_id = %s for update",
            (job_id,),
        )
        run = cur.fetchone()
        cur.execute("select status from jobs where id = %s for update", (job_id,))
        job = cur.fetchone()
        if not job:
            return False
        if job["status"] in _TERMINAL_JOB_STATUSES and (not run or run["phase"] == "DONE"):
            return bool(run)

        if error:
            cur.execute(
                """update jobs
                      set status = 'FAILED', error = %s, updated_at = now()
                    where id = %s""",
                (error, job_id),
            )
        else:
            cur.execute(
                """update jobs
                      set status = 'COMPLETED', progress = 1.0, output = %s,
                          error = null, updated_at = now()
                    where id = %s""",
                (json.dumps(output or {}), job_id),
            )

        if not run:
            return False

        run_id = str(run["id"])
        if error:
            text = (
                "The dataset was built, but packaging failed: "
                f"{error[:400]}. Try again in Export Options."
            )
            kind = "error"
        else:
            text = "Your dataset package is ready to download in Export Options."
            kind = "message"
        _emit_event(
            cur,
            run_id,
            kind,
            text,
            {"export_job_id": job_id},
            role="assistant",
        )
        cur.execute(
            """update agent_runs
                  set phase = 'DONE', status = 'COMPLETED', progress = 1.0,
                      updated_at = now()
                where id = %s and export_job_id = %s""",
            (run_id, job_id),
        )
        return True


# ── Events (the chat timeline) ────────────────────────────────────────


def _emit_event(
    cur: Any,
    run_id: str,
    kind: str,
    text: str,
    data: dict | None = None,
    role: str | None = None,
) -> int:
    # Every event writer first takes the same parent-row lock. That makes the
    # following MAX(seq) allocation atomic per run while preserving a gapless
    # sequence if the insert rolls back.
    cur.execute("select id from agent_runs where id = %s for update", (run_id,))
    if not cur.fetchone():
        raise ValueError(f"unknown agent run {run_id}")
    cur.execute(
        """
        insert into agent_events (run_id, seq, kind, role, text, data)
        select %s, coalesce(max(seq), 0) + 1, %s, %s, %s, %s
          from agent_events where run_id = %s
        returning seq
        """,
        (run_id, kind, role, text, json.dumps(data or {}), run_id),
    )
    return cur.fetchone()["seq"]


def emit_event(
    run_id: str,
    kind: str,
    text: str,
    data: dict | None = None,
    role: str | None = None,
) -> int:
    """Atomically append one gapless timeline entry and return its `seq`."""
    with cursor() as cur:
        return _emit_event(cur, run_id, kind, text, data, role)


def events_since(run_id: str, since_seq: int = 0) -> list[dict]:
    return fetch_all(
        """select seq, kind, role, text, data, created_at
             from agent_events where run_id = %s and seq > %s order by seq""",
        (run_id, since_seq),
    )


# ── Sources (which datasets, and why) ─────────────────────────────────

_SOURCE_FIELDS = (
    "discovered_via",
    "relevance",
    "expected_hit_rate",
    "measured_hit_rate",
    "text_signal",
    "reason",
    "source_multiplier",
    "rows_indexed",
    "rows_kept",
)


def record_source(run_id: str, repo_id: str, **fields: Any) -> None:
    """Upsert one candidate dataset. Only the fields passed are overwritten, so
    discovery, screening and the post-crawl measurement can each write their own
    part without clobbering the others."""
    unknown = set(fields) - set(_SOURCE_FIELDS)
    if unknown:
        raise ValueError(f"not a column on agent_sources: {sorted(unknown)}")
    cols = list(fields)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols) or "repo_id = excluded.repo_id"
    placeholders = ", ".join(["%s"] * len(cols))
    with cursor() as cur:
        cur.execute(
            f"""
            insert into agent_sources (run_id, repo_id{''.join(', ' + c for c in cols)})
            values (%s, %s{', ' + placeholders if cols else ''})
            on conflict (run_id, repo_id) do update set {updates}
            """,
            (run_id, repo_id, *[fields[c] for c in cols]),
        )


def get_sources(run_id: str) -> list[dict]:
    return fetch_all(
        """select * from agent_sources where run_id = %s
            order by (relevance = 'strong') desc, coalesce(measured_hit_rate, expected_hit_rate, 0) desc""",
        (run_id,),
    )


# ── Review candidates and the user's verdicts ─────────────────────────


def record_candidates(run_id: str, batch: int, items: list[dict]) -> int:
    """Persist a review batch. `items` need `asset_id`, and may carry
    `rank`, `score`, `judge`."""
    if not items:
        return 0
    with cursor() as cur:
        for rank, item in enumerate(items):
            cur.execute(
                """
                insert into agent_candidates (run_id, asset_id, batch, rank, score, judge)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (run_id, asset_id) do update
                  set batch = excluded.batch, rank = excluded.rank,
                      score = excluded.score, judge = excluded.judge
                """,
                (
                    run_id,
                    item["asset_id"],
                    batch,
                    item.get("rank", rank),
                    float(item.get("score") or 0.0),
                    json.dumps(item.get("judge") or {}),
                ),
            )
    return len(items)


def get_candidates(run_id: str, batch: int | None = None) -> list[dict]:
    """Review candidates joined to enough of `assets` to render the card."""
    sql = """
        select c.asset_id, c.batch, c.rank, c.score, c.judge, c.decision, c.decided_at,
               a.id, a.modality, a.source_dataset, a.source_config, a.source_split,
               a.source_row, a.thumbnail_uri, a.preview_uri, a.content_uri,
               a.cached_content_uri, a.mime_type,
               a.width, a.height, a.duration, a.fps, a.caption, a.labels, a.tags,
               a.license, a.dataset_kind
          from agent_candidates c join assets a on a.id = c.asset_id
         where c.run_id = %s
    """
    params: tuple = (run_id,)
    if batch is not None:
        sql += " and c.batch = %s"
        params += (batch,)
    return fetch_all(sql + " order by c.batch, c.rank", params)


def set_decisions(run_id: str, decisions: list[dict]) -> int:
    """Persist approve/reject the moment each one happens, so a refresh
    mid-review never loses verdicts the user already gave."""
    valid = {"approved", "rejected", "pending"}
    applied = 0
    with cursor() as cur:
        for d in decisions:
            decision = d.get("decision")
            if decision not in valid:
                raise ValueError(f"invalid decision {decision!r}")
            cur.execute(
                """update agent_candidates
                      set decision = %s, decided_at = now()
                    where run_id = %s and asset_id = %s""",
                (decision, run_id, d["asset_id"]),
            )
            applied += cur.rowcount
    return applied


def decision_counts(run_id: str) -> dict[str, int]:
    rows = fetch_all(
        "select decision, count(*) as n from agent_candidates where run_id = %s group by decision",
        (run_id,),
    )
    counts = {"approved": 0, "rejected": 0, "pending": 0}
    for r in rows:
        counts[r["decision"]] = r["n"]
    return counts


# ── The whole picture, for one poll ───────────────────────────────────


def get_state(request_id: str) -> Optional[dict]:
    request = fetch_one("select * from dataset_requests where id = %s", (request_id,))
    if not request:
        return None
    run = get_run_for_request(request_id)
    if not run:
        return {"request": request, "run": None, "candidates": [], "sources": []}
    return {
        "request": request,
        "run": run,
        "candidates": get_candidates(str(run["id"]), batch=run["review_batch"]),
        "sources": get_sources(str(run["id"])),
        "counts": decision_counts(str(run["id"])),
    }
