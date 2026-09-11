"""Dataset requests — the agent-driven flow behind the newer frontend.

A request is created by the web app (which owns the multipart/seed-file
validation), then handed here to be run by `agent_worker`. Everything after
that is polled, not streamed: `GET /requests/{id}` and
`GET /requests/{id}/events` read durable rows, so a refresh mid-run shows the
same phase, the same timeline and the same approve/reject verdicts.

Thin, like the rest of this gateway: validate, read/write Postgres, enqueue.
The one exception is `POST /requests/{id}/message`, which calls the LLM inline
so chat stays responsive while the single worker is busy in a long phase.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from datacurate_core import agent_state, llm
from datacurate_core.auth import get_actor
from datacurate_core.db import fetch_all, fetch_one
from datacurate_core.queue import enqueue

from serialize import row_to_asset
from user_project import get_or_create_project

log = logging.getLogger(__name__)
router = APIRouter()

MAX_EXAMPLE_COUNT = 10_000


def _load_request(request_id: str, actor: dict) -> dict:
    req = fetch_one("select * from dataset_requests where id = %s", (request_id,))
    if not req:
        raise HTTPException(404, "request not found")
    # Requests created before an owner existed stay readable; anything owned
    # must belong to whoever is asking.
    if req.get("project_id") and str(req["project_id"]) != get_or_create_project(actor):
        raise HTTPException(403, "not your request")
    return req


def _candidate_to_item(row: dict) -> dict:
    """Review-stack shape: a normal asset plus this run's verdict on it."""
    return row_to_asset(
        row,
        extra={
            "score": row["score"],
            "judge": row["judge"],
            "decision": row["decision"],
            "batch": row["batch"],
            "rank": row["rank"],
            # Our own durable copy, when we already hold one. Hugging Face's
            # media URLs cannot be played in a <video> on our origin — it sends
            # Access-Control-Allow-Origin: https://huggingface.co — so playback
            # only ever works from this.
            "cached_uri": row.get("cached_content_uri"),
        },
        include_metadata=False,
    )


@router.post("/requests/{request_id}/start")
def start_request(request_id: str, actor: dict = Depends(get_actor)):
    """Claim the request for this owner and enqueue the agent run."""
    req = _load_request(request_id, actor)
    example_count = max(1, min(int(req.get("example_count") or 100), MAX_EXAMPLE_COUNT))
    review_size = agent_state.review_size_for(example_count)
    project_id = (
        str(req["project_id"]) if req.get("project_id") else get_or_create_project(actor)
    )
    try:
        run, resumed = agent_state.start_run(
            request_id,
            project_id,
            review_size,
            example_count,
        )
    except agent_state.RequestNotFound as e:
        raise HTTPException(404, "request not found") from e
    except agent_state.RequestOwnershipConflict as e:
        raise HTTPException(403, "not your request") from e
    return {
        "run_id": str(run["id"]),
        "phase": run["phase"],
        "review_size": run["review_size"],
        "resumed": resumed,
    }


@router.get("/requests/{request_id}")
def get_request(request_id: str, actor: dict = Depends(get_actor)):
    """One poll returns everything the workspace needs to render itself."""
    _load_request(request_id, actor)
    state = agent_state.get_state(request_id)
    if not state:
        raise HTTPException(404, "request not found")

    req = state["request"]
    run = state["run"]
    payload = {
        "request": {
            "id": str(req["id"]),
            "query": req["query"],
            "example_count": req.get("example_count"),
            "filters": req.get("filters") or {},
            "created_at": req["created_at"],
        },
        "run": None,
        "review": {"items": [], "batch": 0, "size": 0, "has_more": False},
        "counts": state.get("counts") or {},
        "sources": [],
    }
    if not run:
        return payload

    run_id = str(run["id"])
    payload["run"] = {
        "id": run_id,
        "phase": run["phase"],
        "status": run["status"],
        "progress": run["progress"],
        "review_size": run["review_size"],
        "review_batch": run["review_batch"],
        "spec": run["spec"],
        "stats": run["stats"],
        "error": run["error"],
        "job_id": str(run["job_id"]) if run["job_id"] else None,
        "build_job_id": str(run["build_job_id"]) if run["build_job_id"] else None,
        "export_job_id": str(run["export_job_id"]) if run["export_job_id"] else None,
        "dataset_id": str(run["dataset_id"]) if run["dataset_id"] else None,
        "version_id": str(run["version_id"]) if run["version_id"] else None,
    }
    payload["review"] = {
        "items": [_candidate_to_item(c) for c in state["candidates"]],
        "batch": run["review_batch"],
        "size": run["review_size"],
        "has_more": _has_more_batches(run_id, run["review_batch"]),
    }
    payload["sources"] = [
        {
            "repo_id": s["repo_id"],
            "discovered_via": s["discovered_via"],
            "relevance": s["relevance"],
            "reason": s["reason"],
            "rows_indexed": s["rows_indexed"],
            "rows_kept": s["rows_kept"],
        }
        for s in state["sources"]
    ]
    return payload


def _has_more_batches(run_id: str, batch: int) -> bool:
    row = fetch_one(
        "select 1 as ok from agent_candidates where run_id = %s and batch > %s limit 1",
        (run_id, batch),
    )
    return bool(row)


@router.get("/requests/{request_id}/events")
def get_events(request_id: str, since: int = 0, actor: dict = Depends(get_actor)):
    """The agent's timeline. Polled with the highest `seq` already rendered, so
    the client never re-renders or misses an entry."""
    _load_request(request_id, actor)
    run = agent_state.get_run_for_request(request_id)
    if not run:
        return {"events": [], "latest_seq": since}
    events = agent_state.events_since(str(run["id"]), since)
    return {
        "events": events,
        "latest_seq": events[-1]["seq"] if events else since,
        "phase": run["phase"],
        "status": run["status"],
    }


@router.post("/requests/{request_id}/decisions")
def post_decisions(request_id: str, payload: dict, actor: dict = Depends(get_actor)):
    """Persist approve/reject as it happens, not at the end of the review."""
    _load_request(request_id, actor)
    run = agent_state.get_run_for_request(request_id)
    if not run:
        raise HTTPException(409, "this request has no agent run yet")
    decisions = payload.get("decisions") or []
    if not isinstance(decisions, list) or not decisions:
        raise HTTPException(400, "decisions required")
    try:
        applied = agent_state.set_decisions(str(run["id"]), decisions)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "applied": applied, "counts": agent_state.decision_counts(str(run["id"]))}


@router.post("/requests/{request_id}/more")
def next_batch(request_id: str, actor: dict = Depends(get_actor)):
    """Advance to the next pre-computed review batch.

    The agent lays down every batch it is willing to offer at review time, so
    "Show More" is an instant cursor move rather than another crawl.
    """
    _load_request(request_id, actor)
    run = agent_state.get_run_for_request(request_id)
    if not run:
        raise HTTPException(409, "this request has no agent run yet")
    run_id = str(run["id"])
    next_index = run["review_batch"] + 1
    items = agent_state.get_candidates(run_id, batch=next_index)
    if not items:
        return {"ok": False, "has_more": False, "items": []}

    agent_state.update_run(run_id, review_batch=next_index)
    agent_state.emit_event(
        run_id,
        "fetching",
        f"Fetched {len(items)} more candidate examples",
        {"batch": next_index},
    )
    return {
        "ok": True,
        "batch": next_index,
        "items": [_candidate_to_item(c) for c in items],
        "has_more": _has_more_batches(run_id, next_index),
    }


@router.post("/requests/{request_id}/confirm")
def confirm_review(request_id: str, actor: dict = Depends(get_actor)):
    """Lock in the review and start the full build."""
    _load_request(request_id, actor)
    run = agent_state.get_run_for_request(request_id)
    if not run:
        raise HTTPException(409, "this request has no agent run yet")
    run_id = str(run["id"])
    counts = agent_state.decision_counts(run_id)
    if counts["approved"] + counts["rejected"] == 0:
        raise HTTPException(400, "review at least one example first")
    if run["build_job_id"]:
        return {"build_job_id": str(run["build_job_id"]), "resumed": True, "counts": counts}

    job_id = enqueue("agent_build", {"request_id": request_id, "run_id": run_id})
    agent_state.update_run(run_id, build_job_id=job_id, status="RUNNING")
    agent_state.emit_event(
        run_id,
        "labeling",
        f"Labeled {counts['approved'] + counts['rejected']} reviewed examples",
        counts,
    )
    return {"build_job_id": job_id, "resumed": False, "counts": counts}


@router.get("/requests/{request_id}/results")
def get_results(request_id: str, limit: int = 120, actor: dict = Depends(get_actor)):
    """Rows in the built dataset version, in selection order."""
    _load_request(request_id, actor)
    run = agent_state.get_run_for_request(request_id)
    if not run or not run["version_id"]:
        return {"results": [], "count": 0, "total": 0}

    total = fetch_one(
        "select count(*) as n from dataset_records where version_id = %s", (run["version_id"],)
    )
    rows = fetch_all(
        """select dr.position, dr.selection_meta, a.id, a.modality, a.thumbnail_uri,
                  a.preview_uri, a.content_uri, a.caption, a.labels, a.tags, a.license,
                  a.source_dataset, a.source_config, a.source_split, a.source_row,
                  a.width, a.height, a.duration, a.fps, a.dataset_kind
             from dataset_records dr join assets a on a.id = dr.asset_id
            where dr.version_id = %s
            order by dr.position
            limit %s""",
        (run["version_id"], min(limit, 500)),
    )
    return {
        "results": [
            row_to_asset(
                r,
                extra={"position": r["position"], "selection": r["selection_meta"]},
                include_metadata=False,
            )
            for r in rows
        ],
        "count": len(rows),
        "total": total["n"] if total else 0,
    }


@router.post("/requests/{request_id}/export")
def export_dataset(request_id: str, payload: dict, actor: dict = Depends(get_actor)):
    """Export the built version. Reuses the platform's existing export worker."""
    _load_request(request_id, actor)
    run = agent_state.get_run_for_request(request_id)
    if not run or not run["version_id"]:
        raise HTTPException(409, "nothing built to export yet")

    mode = payload.get("mode", "references")
    if mode not in ("references", "materialized", "push_hf"):
        raise HTTPException(400, f"unknown export mode {mode}")
    want_zip = bool(payload.get("zip", True))

    # The export worker keys its blob paths off dataset_id/version, so both
    # have to travel with the job — not just the version's own id.
    version = fetch_one(
        "select dataset_id, version from dataset_versions where id = %s", (run["version_id"],)
    )
    if not version:
        raise HTTPException(409, "the built version no longer exists")

    export_payload = {
        "version_id": str(run["version_id"]),
        "dataset_id": str(version["dataset_id"]),
        "version": version["version"],
        "mode": mode,
        "zip": want_zip,
        "hf_repo": payload.get("hf_repo"),
        "hf_token": payload.get("hf_token"),
    }
    try:
        job, resumed = agent_state.ensure_export_job(str(run["id"]), export_payload)
    except agent_state.ExportJobConflict as e:
        raise HTTPException(
            409,
            "the current export must finish before starting another mode",
        ) from e
    except ValueError as e:
        raise HTTPException(409, "the agent run no longer exists") from e

    job_input = job.get("input") or {}
    return {
        "job_id": str(job["id"]),
        "mode": job_input.get("mode", "references"),
        "resumed": resumed,
        "status": job.get("status") or "QUEUED",
    }


_REPLY_TOOL = {
    "name": "reply_to_user",
    "description": "Reply to the user about their in-progress dataset request.",
    "parameters": {
        "type": "object",
        "properties": {
            "reply": {
                "type": "string",
                "description": "A short, concrete reply. 1-3 sentences, no bullet lists.",
            },
            "adds_criteria": {
                "type": "array",
                "items": {"type": "string"},
                "description": "New requirements the user just stated, if any.",
            },
            "removes_criteria": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Things the user says they do NOT want.",
            },
        },
        "required": ["reply"],
    },
}


@router.post("/requests/{request_id}/message")
def post_message(request_id: str, payload: dict, actor: dict = Depends(get_actor)):
    """A user message in the workspace chat.

    Answered inline rather than queued: the single agent worker may be in the
    middle of a multi-minute crawl, and a chat reply that waits for that would
    feel broken. Both the message and the reply are persisted as events, so
    they survive a refresh like everything else.
    """
    req = _load_request(request_id, actor)
    run = agent_state.get_run_for_request(request_id)
    if not run:
        raise HTTPException(409, "this request has no agent run yet")
    run_id = str(run["id"])

    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    agent_state.emit_event(run_id, "message", text, role="user")

    spec = run["spec"] or {}
    try:
        result = llm.call_tool(
            _REPLY_TOOL,
            [
                {
                    "role": "system",
                    "content": (
                        "You are a dataset-curation agent. The user asked for: "
                        f"{req['query']!r} ({req.get('example_count')} examples). "
                        f"Current phase: {run['phase']}. Current spec: {spec}. "
                        "Answer their follow-up honestly and briefly. Never promise data "
                        "you have not found; if they are asking for something outside the "
                        "current search, say it will be applied to the next refinement."
                    ),
                },
                {"role": "user", "content": text},
            ],
            cache_ttl=0,  # a chat turn is never a cache hit
        )
    except (llm.ToolCallRefused, llm.LLMUnavailable) as e:
        log.warning("chat reply failed for run %s: %s", run_id, e)
        result = {
            "reply": "Noted — I'll factor that into the next refinement pass.",
        }

    # Criteria the user states mid-run are stored on the spec so the next
    # refinement actually uses them, rather than being lost in the transcript.
    adds = [c for c in (result.get("adds_criteria") or []) if isinstance(c, str)]
    removes = [c for c in (result.get("removes_criteria") or []) if isinstance(c, str)]
    if adds or removes:
        spec = dict(spec)
        spec["user_added_criteria"] = list(spec.get("user_added_criteria", [])) + adds
        spec["user_removed_criteria"] = list(spec.get("user_removed_criteria", [])) + removes
        agent_state.update_run(run_id, spec=spec)

    seq = agent_state.emit_event(
        run_id, "message", result.get("reply") or "", {"adds": adds, "removes": removes}, role="assistant"
    )
    return {"ok": True, "seq": seq, "reply": result.get("reply")}
