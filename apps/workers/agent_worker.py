"""The dataset-request agent.

Turns "I need 1000 first-person images of people cooking in home kitchens"
into a real, exported dataset — via a small review sample the user approves
before anything is built at scale.

Two job types, one per side of that review:

  agent_request  plan -> discover -> screen -> crawl -> judge -> review sample
  agent_build    refine from the verdicts -> build to target -> version -> export

Everything is written to `agent_runs` / `agent_events` / `agent_candidates` as
it happens. The browser polls; nothing is streamed. Refreshing mid-run shows
the same phase and the same verdicts.

Precision comes from a deliberate split of labour: Postgres full-text search
does *recall* (it is free and can scan the whole corpus), and the LLM does
*precision* on the shortlist. The corpus is never ranked by an LLM.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter, defaultdict

import requests

import agent_brain
from datacurate_core import agent_state, blob, hf_client, queue, unify
from datacurate_core.config import settings
from datacurate_core.db import cursor, fetch_all, fetch_one
from datacurate_core.dedup import dedupe_candidates
from datacurate_core.field_types import detect_caption_columns, detect_media_column
from datacurate_core.models import SelectionStrategy
from datacurate_core.rank import diversify_by_source, score_candidate
from datacurate_core.search import build_tsquery, count_matched_keywords, expand_query_synonyms

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("azure").setLevel(logging.WARNING)
log = logging.getLogger("agent_worker")

# The review exists to answer one question fast: "is this the right kind of
# data?" That takes ~10 examples, so the path to it is deliberately cheap —
# look in the corpus we already have first, crawl only if we must, and stop the
# moment there is enough to show. All the heavy fetching belongs in the build,
# after the user has approved something.
REVIEW_POOL_MULTIPLIER = 2        # judge ~2x the review size, then stop
REVIEW_CRAWL_BUDGET_SECONDS = 90  # wall-clock ceiling on fetching for a review
REVIEW_CRAWL_ROWS = 12            # rows per dataset at review time
DISCOVER_MAX_DATASETS = 3         # new datasets pulled in per discovery pass
LOCAL_POOL_LIMIT = 80
CRAWL_DEADLINE_SECONDS = 45.0
REVIEW_BATCHES = 4                # the review UI offers "Show More" three times
BUILD_DISCOVERY_ROUNDS = 3        # extra Hub searches when the build falls short
BUILD_DISCOVERY_BUDGET_SECONDS = 300

_ASSET_COLUMNS = """
    id, modality, source_dataset, source_config, source_split, source_row,
    content_uri, thumbnail_uri, preview_uri, width, height, duration, fps,
    caption, labels, tags, license, phash, quality_score, dataset_kind
"""


# ── Phase 1: plan ─────────────────────────────────────────────────────


def _seed_captions(request: dict) -> list[str]:
    """Text describing the examples the user attached, if any."""
    payload = request.get("request_payload") or {}
    seed = payload.get("seedData") or {}
    asset_ids = [a for a in (seed.get("assetIds") or []) if isinstance(a, str)]
    captions: list[str] = []
    if asset_ids:
        rows = fetch_all(
            "select caption, labels from assets where id = any(%s::uuid[])", (asset_ids[:20],)
        )
        captions += [
            r["caption"] or " ".join(r["labels"] or []) for r in rows if r["caption"] or r["labels"]
        ]
    captions += [
        r["alt"] for r in (seed.get("references") or []) if isinstance(r, dict) and r.get("alt")
    ]
    return [c for c in captions if c][:12]


# ── Phase 2: discover candidate datasets ──────────────────────────────






def _search_corpus(spec: dict, query_text: str, limit: int = LOCAL_POOL_LIMIT) -> list[dict]:
    """Find matching rows using the platform's own text search.

    Same mechanism `/search` uses, and for the same reasons: `websearch_to_tsquery`
    understands a real phrase, and `expand_query_synonyms` knows that "egocentric",
    "pov" and "first person" describe the same footage — knowledge an LLM would
    otherwise have to supply on every request. Falls back to an OR of the
    compiled keywords when the phrase itself matches nothing.

    Review candidates must be viewable: rows indexed as references (real
    provenance and text, no media copy) belong in a built dataset but render as
    blank cards.
    """
    attempts: list[str] = []
    if query_text:
        attempts.append(expand_query_synonyms(query_text))
    keywords = spec.get("positive_keywords") or []
    if keywords:
        attempts.append(" or ".join(keywords[:8]))

    # Run every attempt and merge, rather than returning on the first that
    # matches anything. The phrase query is narrow by design, so a single
    # incidental hit used to short-circuit the broader keyword search entirely —
    # which meant 33 rows we had just streamed from Hugging Face were never
    # even looked at, and the review came back empty.
    merged: dict[str, dict] = {}
    for attempt in attempts:
        if not attempt.strip():
            continue
        rows = fetch_all(
            f"""select {_ASSET_COLUMNS},
                       ts_rank_cd(search_vector, websearch_to_tsquery('english', %(q)s)) as text_rank
                  from assets
                 where search_vector @@ websearch_to_tsquery('english', %(q)s)
                   and modality = %(mod)s
                   and (thumbnail_uri is not null or content_uri is not null)
                 order by (thumbnail_uri is not null) desc, text_rank desc
                 limit %(limit)s""",
            {"q": attempt, "mod": spec["modality"], "limit": limit},
        )
        for row in rows:
            merged.setdefault(str(row["id"]), row)
    return _interleave_by_source(list(merged.values()))[:limit]


def _interleave_by_source(rows: list[dict]) -> list[dict]:
    """Round-robin across source datasets, best row of each first.

    The same trick `/search` and `/feed` use in SQL, applied here because it
    decides which rows get judged at all. Ranking alone let one dataset take
    every slot: for "website screen recordings" the top 12 rows were all from
    `causvid_website`, so `markov-ai/computer-use-large` — actual screen
    recordings, sitting in the corpus — was never even considered.
    """
    by_source: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_source[row["source_dataset"]].append(row)

    interleaved: list[dict] = []
    for tier in range(max((len(v) for v in by_source.values()), default=0)):
        for source_rows in by_source.values():
            if tier < len(source_rows):
                interleaved.append(source_rows[tier])
    return interleaved


_MEDIA_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff",
    ".mp4", ".webm", ".mov", ".mkv", ".avi",
    ".wav", ".mp3", ".flac", ".ogg", ".m4a",
)


def _looks_like_media_url(url: str | None) -> bool:
    """Reject values that were never a file in the first place."""
    if not url:
        return False
    path = url.split("?", 1)[0].lower()
    return path.endswith(_MEDIA_EXTENSIONS)


def _media_url_works(url: str) -> bool:
    """Confirm the URL really serves bytes.

    A one-byte ranged GET rather than a HEAD: Hugging Face answers HEAD for
    paths that a real GET then 404s on, and a review item that cannot load is
    worse than no review item at all.
    """
    try:
        resp = requests.get(
            url,
            timeout=settings.http_timeout_seconds,
            stream=True,
            allow_redirects=True,
            headers={"Range": "bytes=0-0"},
        )
        resp.close()
        return resp.status_code < 400
    except Exception:  # noqa: BLE001
        return False


def _stream_rows_from_repo(repo_id: str, spec: dict, limit: int) -> int:
    """Index a dataset's rows straight from `/rows`, without fetching any media.

    This is the fast path, and it is how `dataset_large/` gets away with being
    instant: pull the JSON page, keep the text and the `resolve/main` media URL,
    and let the browser load the media itself. No download, no Pillow, no
    ffmpeg — three HTTP calls per dataset instead of one download plus two
    ffmpeg passes *per row*, which is what made a video review take minutes.

    Rows land in `assets` exactly like any other, so the next request for this
    topic is served from Postgres with no Hugging Face call at all. Thumbnails
    and pHashes get filled in later by the build, for the rows that survive.
    """
    from ingest_worker import _dataset_baseline_tags, _resolve_source

    try:
        splits = hf_client.get_splits(repo_id)
        if not splits:
            return 0
        config, split = splits[0]["config"], splits[0]["split"]
        features = (hf_client.get_info(repo_id, config) or {}).get("features") or {}
    except Exception as e:  # noqa: BLE001 — a broken dataset is not our problem
        log.info("stream: %s unavailable (%s)", repo_id, e)
        return 0

    media_col, media_type = detect_media_column(features)
    if not media_col:
        return 0
    caption_cols = detect_caption_columns(features)
    tags = _dataset_baseline_tags(repo_id)
    modality = spec.get("modality", "image")

    added = 0
    misses = 0
    try:
        for item in hf_client.iter_rows(repo_id, config, split, max_rows=limit):
            values = item.get("row") or {}
            if values.get(media_col) is None:
                continue
            try:
                src_url, revision, width, height = _resolve_source(
                    repo_id, media_type, values[media_col]
                )
            except Exception:  # noqa: BLE001
                continue
            if not _looks_like_media_url(src_url):
                continue
            # Check EVERY row, not just the first. Datasets are inconsistent:
            # `lightly-ai/epic-kitchens-100-clips` stores clip IDs like "P01_01"
            # instead of paths, and one Qualcomm set has a first row that
            # resolves while later ones 404. Since these rows are shown to a
            # person with no thumbnail to fall back on, an item that cannot
            # load is the difference between a good review and a broken one.
            if not _media_url_works(src_url):
                misses += 1
                if misses >= 3 and added == 0:
                    log.info("stream: %s media URLs do not resolve (%s)", repo_id, src_url[:90])
                    _mark_rows_failed(repo_id)
                    return 0
                continue
            caption = next(
                (str(values[c]) for c in caption_cols if isinstance(values.get(c), str)), None
            )
            _upsert_reference_row(
                {
                    "modality": modality,
                    "source_dataset": repo_id,
                    "source_config": config,
                    "source_split": split,
                    "source_revision": revision,
                    "source_row": item["row_idx"],
                    "content_uri": src_url,
                    "caption": caption,
                    "labels": [],
                    "tags": tags,
                    "metadata": {"_reference_only": True},
                    "description": None,
                }
            )
            added += 1
    except Exception as e:  # noqa: BLE001
        log.info("stream: %s row read stopped (%s)", repo_id, e)

    if added == 0:
        _mark_rows_failed(repo_id)
    return added


def _fetch_from_hugging_face(run_id: str, spec: dict, query_text: str) -> dict:
    """Find datasets on the Hub and stream a page of rows from each.

    Discovery reuses the platform's own per-word Hub search (`search_datasets`
    fans out one word at a time because multi-word `search=` returns nothing),
    and ingestion is the no-download path above. Together that turns a
    multi-minute review into seconds.
    """
    terms: list[str] = []
    for word in (spec.get("hub_search_terms") or []) + (spec.get("positive_keywords") or []):
        if word not in terms:
            terms.append(word)
    terms = terms[:4]
    if not terms:
        return {"datasets_found": [], "rows_added": 0}

    agent_state.emit_event(run_id, "explored", f"Searching Hugging Face for {' '.join(terms)}")
    known = {
        r["repo_id"]
        for r in fetch_all("select distinct source_dataset as repo_id from assets", ())
    }
    candidates: list[str] = []
    for term in terms:
        try:
            for summary in hf_client.search_datasets(
                term, limit=6, modalities=[spec.get("modality")]
            ):
                if summary.repo_id not in candidates and summary.repo_id not in known:
                    candidates.append(summary.repo_id)
        except Exception as e:  # noqa: BLE001
            log.warning("hub search failed for %r: %s", term, e)

    deadline = time.monotonic() + REVIEW_CRAWL_BUDGET_SECONDS
    found, rows_added = [], 0
    for repo in candidates:
        if len(found) >= DISCOVER_MAX_DATASETS or time.monotonic() > deadline:
            break
        added = _stream_rows_from_repo(repo, spec, REVIEW_CRAWL_ROWS)
        if added:
            found.append(repo)
            rows_added += added
            agent_state.record_source(run_id, repo, discovered_via="hub_search", rows_indexed=added)

    agent_state.emit_event(
        run_id,
        "fetching",
        f"Streamed {rows_added} rows from {len(found)} new datasets",
        {"datasets": found},
    )
    return {"datasets_found": found, "rows_added": rows_added}


def _crawl(repo_id: str, max_rows: int, modality: str = "image") -> int:
    from ingest_worker import crawl_dataset  # sibling module; imported lazily

    # Video costs a fetch plus two ffmpeg passes per row; the image budget
    # expires before a single clip is done, which reads as "this dataset has
    # nothing" when it actually has plenty.
    deadline = CRAWL_DEADLINE_SECONDS * (3.0 if modality == "video" else 1.0)
    try:
        stats = crawl_dataset(repo_id, max_rows=max_rows, deadline_seconds=deadline)
        indexed = int(stats.get("assets_indexed") or 0)
    except Exception as e:  # noqa: BLE001 — one bad dataset must not end the run
        log.warning("crawl of %s failed: %s", repo_id, e)
        indexed = 0

    if indexed == 0:
        _mark_rows_failed(repo_id)
    return indexed


def _mark_rows_failed(repo_id: str) -> None:
    """Remember that this dataset's rows can't be read.

    Plenty of real, on-topic datasets are simply broken on Hugging Face's side
    — `a1raman/epic_kitchens_100` answers /rows with "Job manager crashed",
    others with "dataset generation failed". Nothing client-side fixes that, so
    record it: screening already rejects `rows_failed` datasets deterministically
    and for free, which means the next request for this topic skips straight
    past them instead of spending a crawl budget rediscovering it.
    """
    try:
        with cursor() as cur:
            cur.execute(
                """insert into hf_datasets (repo_id, rows_failed)
                   values (%s, true)
                   on conflict (repo_id) do update set rows_failed = true""",
                (repo_id,),
            )
    except Exception:  # noqa: BLE001 — bookkeeping must never break the run
        log.warning("could not mark %s as rows_failed", repo_id, exc_info=True)


def _rows_for_repo(
    repo_id: str, spec: dict, tsquery: str, limit: int, viewable_only: bool = False
) -> list[dict]:
    """Indexed rows from one dataset, most textually relevant first.

    `viewable_only` excludes reference-only rows (no media copy). They belong in
    the built dataset but can never be shown as a review example.
    """
    viewable = " and thumbnail_uri is not null" if viewable_only else ""
    if tsquery:
        return fetch_all(
            f"""select {_ASSET_COLUMNS},
                       ts_rank_cd(search_vector, to_tsquery('english', %(q)s)) as text_rank
                  from assets
                 where source_dataset = %(repo)s and modality = %(mod)s{viewable}
                 order by text_rank desc, created_at desc
                 limit %(limit)s""",
            {"q": tsquery, "repo": repo_id, "mod": spec["modality"], "limit": limit},
        )
    return fetch_all(
        f"""select {_ASSET_COLUMNS}, 0.0 as text_rank
              from assets where source_dataset = %s and modality = %s{viewable}
              order by created_at desc limit %s""",
        (repo_id, spec["modality"], limit),
    )


_LLM_WEIGHT = {"match": 1.0, "weak": 0.45, "reject": 0.0}


def _fuse_score(row: dict, judge: dict, keywords: list[str]) -> float:
    """Blend the LLM's judgement with the platform's existing lexical signals.

    The LLM dominates because it is the only part that understands the request,
    but keeping real weight on keyword overlap and resolution/quality stops a
    confidently-wrong verdict from promoting an obviously poor row.
    """
    llm_score = _LLM_WEIGHT.get(judge.get("verdict", "weak"), 0.3) * float(
        judge.get("confidence") or 0.5
    )
    lexical = min(_matched(row, keywords) / max(len(keywords[:5]), 1), 1.0)
    return 0.60 * llm_score + 0.25 * lexical + 0.15 * score_candidate(row, keywords)


def _matched(row: dict, keywords: list[str]) -> int:
    """Keyword overlap in the row's OWN text.

    Tags are excluded on purpose. The ingest path derives them from the repo id
    so that a dataset unambiguously about a topic stays findable with zero
    per-row text — good for recall, useless as evidence about a particular row.
    Counting them here let every row of `causvid_website` clear the admission
    bar for "website recordings" on the strength of its dataset's name.
    """
    return count_matched_keywords(
        [row.get("caption") or "", " ".join(row.get("labels") or [])], keywords
    )


def _admit(row: dict, judge: dict, keywords: list[str]) -> bool:
    """Keep only rows with real evidence behind them.

    A single coincidental keyword is the platform's known precision ceiling —
    "trees" legitimately appears in both an on-theme caption and an unrelated
    LAION one. Requiring either two distinct keywords or a positive LLM verdict
    removes that tail without discarding genuinely good rows.
    """
    if judge.get("verdict") == "reject":
        return False
    return _matched(row, keywords) >= 2 or judge.get("verdict") == "match"


def _judge_repo_rows(spec: dict, rows: list[dict]) -> dict[str, dict]:
    """Judge rows in batches; returns asset_id -> verdict."""
    keyed = {i: row for i, row in enumerate(rows)}
    verdicts: dict[str, dict] = {}
    batch = settings.agent_judge_batch
    for start in range(0, len(rows), batch):
        chunk = [
            {
                "i": i,
                "dataset": keyed[i]["source_dataset"],
                "caption": keyed[i].get("caption"),
                "labels": keyed[i].get("labels"),
                "tags": keyed[i].get("tags"),
            }
            for i in range(start, min(start + batch, len(rows)))
        ]
        for i, verdict in agent_brain.judge_rows(spec, chunk).items():
            verdicts[str(keyed[i]["id"])] = verdict
    return verdicts


def _ensure_thumbnail(row: dict) -> str | None:
    """Make a keyframe for a streamed video row, so it can be judged and shown.

    Streaming deliberately skips media processing, which leaves video rows with
    no still image — and a vision model cannot watch an MP4. Without a keyframe
    every video row stays "weak" forever, no source is ever confirmed, and the
    build has nothing to prefer.

    ffmpeg is pointed at the REMOTE url with `-ss` before `-i`, so it issues
    range requests and pulls only the bytes around the seek point. That matters:
    downloading first hit the 25 MB fetch cap on exactly the datasets we care
    about here — real screen recordings are routinely far bigger. Seeks to 3s
    because the opening frames of a screencast are usually a blank fade-in.
    (Same approach as narrated_video_ingest.generate_thumbnails.)
    """
    import subprocess
    import tempfile
    from pathlib import Path

    from datacurate_core.blob import content_hash_name_from_bytes, upload_bytes
    from datacurate_core.thumbnails import image_pipeline

    if row.get("thumbnail_uri") or not row.get("content_uri"):
        return row.get("thumbnail_uri")
    url = row["content_uri"]

    try:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "frame.jpg"
            for args in (
                ["-ss", "3", "-i", url],
                ["-i", url],  # very short clips have no 3s mark
            ):
                proc = subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", *args,
                     "-frames:v", "1", "-q:v", "3", str(out)],
                    capture_output=True,
                    timeout=90,
                )
                if proc.returncode == 0 and out.exists() and out.stat().st_size:
                    break
            else:
                return None
            raw = out.read_bytes()

        media = image_pipeline(raw)
        thumb = media.get("thumbnail_bytes")
        if not thumb:
            return None
        name = content_hash_name_from_bytes(thumb, ext=".webp")
        url_out = upload_bytes(settings.blob_container_thumbnails, name, thumb, "image/webp")
    except Exception as e:  # noqa: BLE001 — a row we can't thumbnail is still reviewable
        log.info("could not build a keyframe for %s: %s", row["source_dataset"], e)
        return None

    with cursor() as cur:
        cur.execute(
            "update assets set thumbnail_uri = %s, phash = %s where id = %s",
            (url_out, media.get("phash"), row["id"]),
        )
    row["thumbnail_uri"] = url_out
    return url_out


def _vision_source(row: dict) -> str | None:
    """The image a vision model can actually be shown for this row.

    Our own thumbnail when we have one. Otherwise, for a streamed image row,
    the source URL itself — those rows have no thumbnail precisely because we
    skipped the download, and they are the ones most in need of a look, since
    they usually have no caption either. Video has no still to send.
    """
    if row.get("thumbnail_uri"):
        return blob.internal_url(row["thumbnail_uri"])
    if row.get("modality") == "image" and row.get("content_uri"):
        return row["content_uri"]
    return None


def _apply_vision(spec: dict, pool: list[dict], budget: int) -> int:
    """Look at the images for rows whose text was merely silent.

    Only fires where it can actually change the answer: the verdict is 'weak'
    and every attribute the text failed to evidence is one you could settle by
    looking. Text that *contradicts* the request is already a reject and is
    never re-litigated here.
    """
    if budget <= 0:
        return 0

    # Give shortlisted video rows a keyframe first — otherwise there is nothing
    # for the model to look at and they can never be confirmed.
    for row in pool[:budget]:
        if row["judge"].get("verdict") == "weak" and not row.get("thumbnail_uri"):
            _ensure_thumbnail(row)

    # Every unresolved row with a picture gets looked at. The earlier version
    # also required that none of the missing attributes were "not visually
    # decidable", and that filter did more harm than good: the model marks
    # attributes conservatively, so rows it had simply failed to judge from
    # absent text were skipped by the one step that could have judged them.
    # Looking is cheap relative to showing someone the wrong data.
    eligible = [
        row
        for row in pool
        if row["judge"].get("verdict") == "weak" and _vision_source(row)
    ][:budget]
    if not eligible:
        return 0

    checked = 0
    for start in range(0, len(eligible), 4):
        chunk = eligible[start : start + 4]
        # Stored thumbnail URLs point at the browser-facing host; from in here
        # that resolves to this container, not the blob service.
        items = [
            {"i": start + n, "thumbnail_uri": _vision_source(row)}
            for n, row in enumerate(chunk)
        ]
        verdicts = agent_brain.judge_images(spec, items)
        for n, row in enumerate(chunk):
            verdict = verdicts.get(start + n)
            if verdict:
                row["judge"] = {**row["judge"], **verdict, "missing": []}
                checked += 1
    return checked


def _judge_and_keep(spec: dict, rows: list[dict], keywords: list[str]) -> list[dict]:
    """Judge rows and drop only the outright rejects.

    Anything merely unproven stays in — the vision pass and, ultimately, the
    user decide those.
    """
    verdicts = _judge_repo_rows(spec, rows)
    kept: list[dict] = []
    for row in rows:
        judge = verdicts.get(
            str(row["id"]), {"verdict": "weak", "confidence": 0.3, "missing": []}
        )
        if judge.get("verdict") == "reject":
            continue
        row["judge"] = judge
        row["_score"] = _fuse_score(row, judge, keywords)
        kept.append(row)
    return kept


def _absorb_new_rows(
    pool: list[dict], spec: dict, query_text: str, keywords: list[str], want: int
) -> list[dict]:
    """Re-search the corpus after a fetch and judge whatever is new."""
    seen = {str(r["id"]) for r in pool}
    fresh = [r for r in _search_corpus(spec, query_text) if str(r["id"]) not in seen]
    if fresh:
        pool = pool + _judge_and_keep(spec, fresh[: want * 2], keywords)
    return pool


def _gather_review_pool(
    run_id: str, spec: dict, review_size: int, query_text: str
) -> list[dict]:
    """Get just enough real candidates to put a review in front of the user.

    Search first, fetch second, and only then spend an LLM call:

      1. The platform's own full-text search over the corpus we already hold.
      2. If that is thin, the platform's own Hugging Face discovery — a
         per-word Hub search plus a bounded crawl of what it finds. This is the
         path that built the corpus in the first place; it needs no LLM and has
         been exercised far more than anything written here.
      3. Judge only the shortlist that is actually going to be shown.

    An earlier version screened every candidate dataset with an LLM and profiled
    each over HTTP before crawling. It was slower, and worse: for "kitchen
    cooking egocentric" it correctly found EPIC-KITCHENS, spent its whole budget
    on repos whose /rows backend is broken, and returned nothing.
    """
    keywords = spec.get("positive_keywords") or []
    want = review_size * REVIEW_POOL_MULTIPLIER

    rows = _search_corpus(spec, query_text)
    if rows:
        agent_state.emit_event(
            run_id, "fetching", f"Found {len(rows)} matching rows already in the corpus"
        )
    pool = _judge_and_keep(spec, rows[: want * 2], keywords) if rows else []

    # Fetching from Hugging Face is by far the slowest thing here — a video
    # crawl is a download plus two ffmpeg passes per row. It is worth it when
    # we have nothing, and a bad trade when we already have enough to review:
    # one run spent six minutes fetching six rows while 43 corpus matches sat
    # unused. So the bar is "can I fill a review", not "is the pool full".
    if len(pool) < review_size:
        agent_state.set_phase(run_id, "SAMPLING", 0.5)
        _fetch_from_hugging_face(run_id, spec, query_text)
        pool = _absorb_new_rows(pool, spec, query_text, keywords, want)

    pool.sort(key=lambda r: r["_score"], reverse=True)
    pool = dedupe_candidates(pool, threshold=settings.phash_hamming_threshold)

    # Vision is the slowest step per item, so it only settles rows actually in
    # line to be shown — and only where the text was silent rather than wrong.
    checked = _apply_vision(spec, pool[:review_size], review_size)
    if checked:
        agent_state.emit_event(
            run_id, "labeling", f"Looked at {checked} images to settle what the text didn't say"
        )
        pool = [r for r in pool if r["judge"].get("verdict") != "reject"]
        for row in pool:
            row["_score"] = _fuse_score(row, row["judge"], keywords)
        pool.sort(key=lambda r: r["_score"], reverse=True)

    # Recorded only now, after vision: a verdict of "match" is what makes a
    # source confirmed, and until the images have been looked at every video
    # row is still "weak". Doing this earlier marked every source `partial`,
    # which silently disabled the build's preference for confirmed sources.
    kept_by_source = Counter(row["source_dataset"] for row in pool)
    confirmed_sources = {
        row["source_dataset"] for row in pool if row["judge"].get("verdict") == "match"
    }
    for repo, kept in kept_by_source.items():
        agent_state.record_source(
            run_id,
            repo,
            relevance="strong" if repo in confirmed_sources else "partial",
            rows_kept=kept,
            reason=(
                "confirmed matches for this request"
                if repo in confirmed_sources
                else "matched the search but nothing was confirmed"
            ),
        )

    strict = [r for r in pool if _admit(r, r["judge"], keywords)]
    if len(strict) >= review_size:
        return strict
    # Never hand back an empty review just because the evidence was thin —
    # judging an uncertain candidate is exactly what the review is for, and a
    # row we streamed but couldn't confirm is still worth a human glance.
    #
    # An absolute score floor was tried here and was wrong: rows with sparse
    # text (a streamed row often has only repo-derived tags) score low by
    # construction, so the floor emptied precisely the reviews that needed
    # filling. Rank instead, and let the ordering carry the quality signal —
    # the strongest candidates are always shown first.
    chosen = {str(r["id"]) for r in strict}
    padding = [r for r in pool if str(r["id"]) not in chosen]
    return strict + padding[: review_size * REVIEW_BATCHES]



# ── Phase 5: the review sample ────────────────────────────────────────


def _stratified_sample(pool: list[dict], size: int) -> list[dict]:
    """Pick a sample that actually teaches us something.

    The top N by score would be near-duplicates of each other and would all be
    approved, which tells us nothing. A spread across the score band, plus the
    dominant source and the genuinely uncertain rows, is where the user's
    verdicts carry information.
    """
    if len(pool) <= size:
        return list(pool)

    quotas = [
        ("top", max(1, round(size * 0.2))),
        ("mid", max(1, round(size * 0.4))),
        ("dominant", max(1, round(size * 0.2))),
        ("uncertain", max(1, round(size * 0.2))),
    ]
    picked: list[dict] = []
    seen: set[str] = set()

    def take(rows: list[dict], n: int) -> None:
        for row in rows:
            if len(picked) >= size or n <= 0:
                return
            key = str(row["id"])
            if key in seen:
                continue
            seen.add(key)
            picked.append(row)
            n -= 1

    mid_start = len(pool) // 3
    dominant_repo = Counter(r["source_dataset"] for r in pool).most_common(1)[0][0]
    buckets = {
        "top": pool,
        "mid": pool[mid_start : mid_start + size * 3],
        "dominant": [r for r in pool if r["source_dataset"] == dominant_repo],
        "uncertain": sorted(pool, key=lambda r: float(r["judge"].get("confidence") or 0.0)),
    }
    for name, quota in quotas:
        take(buckets[name], quota)
    take(pool, size - len(picked))
    return picked[:size]


def _record_review_batches(run_id: str, pool: list[dict], size: int) -> int:
    """Lay down every batch the review UI can ask for, up front.

    "Show More" then costs a cursor move instead of another crawl, which keeps
    it instant and means the batches are already durable if the page reloads.
    """
    first = _stratified_sample(pool, size)
    chosen = {str(r["id"]) for r in first}
    agent_state.record_candidates(
        run_id, 0, [{"asset_id": str(r["id"]), "score": r["_score"], "judge": r["judge"]} for r in first]
    )

    remaining = [r for r in pool if str(r["id"]) not in chosen]
    batches = 1
    for index in range(1, REVIEW_BATCHES):
        chunk = remaining[(index - 1) * size : index * size]
        if not chunk:
            break
        agent_state.record_candidates(
            run_id,
            index,
            [{"asset_id": str(r["id"]), "score": r["_score"], "judge": r["judge"]} for r in chunk],
        )
        batches += 1
    return batches


def process_request(job: dict) -> dict:
    request_id = job["input"]["request_id"]
    request = fetch_one("select * from dataset_requests where id = %s", (request_id,))
    if not request:
        return {"error": "request not found"}
    run = agent_state.get_run_for_request(request_id)
    if not run:
        return {"error": "no agent run for this request"}

    run_id = str(run["id"])
    target = int(request.get("example_count") or 100)
    review_size = int(run["review_size"])
    filters = request.get("filters") or {}

    # A job whose worker died mid-run is re-delivered once its lease expires,
    # and the whole run replays. Say so, rather than letting a second copy of
    # the timeline appear with no explanation.
    if agent_state.events_since(run_id, 1):
        agent_state.emit_event(run_id, "explored", "Restarting the search")

    agent_state.set_phase(run_id, "PLANNING", 0.05)
    spec = agent_brain.compile_spec(
        request["query"], target, filters, _seed_captions(request)
    )
    agent_state.update_run(run_id, spec=spec)
    agent_state.emit_event(
        run_id,
        "explored",
        f"Planned the search for {spec['subject']}",
        {"keywords": spec["positive_keywords"], "degraded": spec.get("degraded")},
    )

    agent_state.set_phase(run_id, "DISCOVERING", 0.2)
    pool = _gather_review_pool(run_id, spec, review_size, request["query"])
    if not pool:
        # Name names. "Nothing held up" is useless when the real story is that
        # the right datasets exist and Hugging Face can't serve their rows —
        # the user can act on that (pick a mirror, change modality), and it
        # stops the agent looking like it simply failed to search.
        sources = agent_state.get_sources(run_id)
        # Distinguish "couldn't read it" from "read it and it didn't match" —
        # reporting the second as the first is simply untrue, and it was, for
        # every dataset the streaming path had in fact read successfully.
        unreadable = [s["repo_id"] for s in sources if not s["rows_indexed"]][:3]
        fetched = [s["repo_id"] for s in sources if s["rows_indexed"]][:3]
        if fetched:
            detail = (
                f" I did fetch rows from {', '.join(fetched)}, but none of them held up as "
                f"{spec.get('subject')} when I checked them."
            )
        elif unreadable:
            detail = (
                f" I found {', '.join(unreadable)} — which look right — but Hugging Face "
                "couldn't serve their rows."
            )
        else:
            detail = ""
        agent_state.emit_event(
            run_id,
            "message",
            f"I couldn't get any usable examples for this one.{detail} Tell me more about "
            "what you need — or try a different modality — and I'll search again.",
            role="assistant",
        )
        agent_state.set_phase(run_id, "AWAITING_REVIEW", 1.0, status="AWAITING_REVIEW")
        return {"pool": 0}

    agent_state.set_phase(run_id, "REVIEW_READY", 0.92)
    batches = _record_review_batches(run_id, pool, review_size)
    shown = min(review_size, len(pool))

    agent_state.emit_event(run_id, "fetching", f"Fetched {shown} candidate examples")
    confirmed = sum(1 for r in pool[:shown] if (r.get("judge") or {}).get("verdict") == "match")
    # Be explicit about how much of this the agent actually stands behind.
    # "Here are six examples" reads as six equally-good matches; saying two are
    # confirmed and four are guesses is both true and more useful to review.
    confidence = (
        f"{confirmed} of them I'm confident about; the rest I'm unsure of, so your calls "
        "on those matter most."
        if 0 < confirmed < shown
        else (
            "I'm confident about all of them."
            if confirmed == shown
            else "I'm not certain about any of them yet, so your calls will steer this a lot."
        )
    )
    agent_state.emit_event(
        run_id,
        "message",
        f"I found {len(pool)} candidates across "
        f"{len({r['source_dataset'] for r in pool})} datasets. {confidence} "
        f"Approve or reject each one and I'll use your picks to build the "
        f"{target}-row dataset.",
        role="assistant",
    )
    agent_state.update_run(
        run_id,
        stats={
            "pool_size": len(pool),
            "datasets": sorted({r["source_dataset"] for r in pool}),
            "review_batches": batches,
        },
    )
    agent_state.set_phase(run_id, "AWAITING_REVIEW", 1.0, status="AWAITING_REVIEW")
    return {"pool": len(pool), "review_batches": batches}


# ── Phase 6: build from the verdicts ──────────────────────────────────


def _source_multipliers(reviewed: list[dict]) -> dict[str, float]:
    """The one thing ~10 binary labels genuinely support.

    Each reviewed item stands in for hundreds of rows that share its dataset's
    caption style and subject matter, so a verdict about a dataset is far
    better founded than a verdict about an individual row. Anything finer —
    per-attribute weights, a learned scorer — would be over-reading the data.
    """
    by_repo: dict[str, list[str]] = defaultdict(list)
    for item in reviewed:
        by_repo[item["source_dataset"]].append(item["decision"])

    multipliers: dict[str, float] = {}
    for repo, decisions in by_repo.items():
        if len(decisions) < 3:
            continue  # too few to act on without over-reading
        approved = decisions.count("approved") / len(decisions)
        if approved <= 1 / 3:
            multipliers[repo] = 0.0
        elif approved >= 2 / 3:
            multipliers[repo] = 1.25
    return multipliers


def _threshold_from_review(reviewed: list[dict]) -> float | None:
    """Accept at the score of the lowest-scoring row the user approved."""
    approved = [float(r["score"]) for r in reviewed if r["decision"] == "approved"]
    return min(approved) * 0.9 if approved else None


def _upsert_reference_row(row: dict) -> None:
    """Index a row's text and media URL without fetching or thumbnailing it.

    This is the cheap tier that lets a build reach a large target: full
    materialization costs roughly a second per row, so a 10,000-row request
    would otherwise run for hours. `do nothing` on conflict is deliberate —
    a row already crawled properly has a thumbnail and a pHash, and must never
    be downgraded to this.
    """
    with cursor() as cur:
        cur.execute(
            """insert into assets (modality, source_dataset, source_config, source_split,
                                   source_revision, source_row, content_uri, caption, labels,
                                   tags, metadata, description)
               values (%(modality)s, %(source_dataset)s, %(source_config)s, %(source_split)s,
                       %(source_revision)s, %(source_row)s, %(content_uri)s, %(caption)s,
                       %(labels)s, %(tags)s, %(metadata)s, %(description)s)
               on conflict (source_dataset, source_config, source_split, source_row) do nothing""",
            {**row, "metadata": json.dumps(row.get("metadata") or {})},
        )


def _extend_with_references(run_id: str, spec: dict, repos: list[str], wanted: int) -> int:
    """Top up the corpus with text-only rows from datasets already accepted."""
    from ingest_worker import _dataset_baseline_tags, _resolve_source

    added = 0
    for repo in repos:
        if added >= wanted:
            break
        try:
            splits = hf_client.get_splits(repo)
            if not splits:
                continue
            config, split = splits[0]["config"], splits[0]["split"]
            features = (hf_client.get_info(repo, config) or {}).get("features") or {}
            media_col, media_type = detect_media_column(features)
            if not media_col:
                continue
            caption_cols = detect_caption_columns(features)
            tags = _dataset_baseline_tags(repo)

            for item in hf_client.iter_rows(repo, config, split, max_rows=wanted - added):
                values = item.get("row") or {}
                media_value = values.get(media_col)
                if media_value is None:
                    continue
                try:
                    src_url, revision, width, height = _resolve_source(repo, media_type, media_value)
                except Exception:  # noqa: BLE001
                    continue
                caption = next(
                    (str(values[c]) for c in caption_cols if isinstance(values.get(c), str)), None
                )
                _upsert_reference_row(
                    {
                        "modality": spec["modality"],
                        "source_dataset": repo,
                        "source_config": config,
                        "source_split": split,
                        "source_revision": revision,
                        "source_row": item["row_idx"],
                        "content_uri": src_url,
                        "caption": caption,
                        "labels": [],
                        "tags": tags,
                        "metadata": {"_reference_only": True},
                        "description": None,
                    }
                )
                added += 1
                if added >= wanted:
                    break
        except Exception as e:  # noqa: BLE001
            log.warning("reference top-up failed for %s: %s", repo, e)
    if added:
        agent_state.emit_event(
            run_id, "fetching", f"Indexed {added} more rows as references (no media copy)"
        )
    return added


def _select_final(spec: dict, run_id: str, repos: list[str], target: int, threshold: float | None,
                  multipliers: dict[str, float], judge_new: bool = True) -> list[dict]:
    """Score, judge, dedup and balance the rows that will make up the dataset.

    The build used to skip judging entirely — it scored on keyword overlap and
    image quality and took whatever the accepted datasets held. That is how a
    request for "website screen recordings" came back as diamond rings and
    elephants: those rows live in datasets with "website" in the name, nothing
    ever looked at them, and no keyword could rule them out because they have
    no caption at all.

    So rows are judged here too, in interleaved order across sources and only
    until there are enough. Verdicts from the review are reused rather than
    re-requested.
    """
    keywords = spec.get("positive_keywords") or []
    tsquery = build_tsquery(keywords, SelectionStrategy.BROAD)
    rows: list[dict] = []
    per_repo = max(target, 200)
    for repo in repos:
        if multipliers.get(repo) == 0.0:
            continue  # the user rejected everything this dataset offered
        rows.extend(_rows_for_repo(repo, spec, tsquery, per_repo))
    rows = _interleave_by_source(rows)

    # Verdicts already earned during the review are carried over — the build
    # does not re-ask about a row someone already looked at.
    judged = {
        str(c["asset_id"]): c["judge"]
        for c in agent_state.get_candidates(run_id)
        if c.get("judge")
    }

    selected: list[dict] = []
    wanted = int(target * settings.agent_judge_overshoot)
    batch = settings.agent_judge_batch
    for start in range(0, len(rows), batch):
        if len(selected) >= wanted:
            break
        chunk = rows[start : start + batch]
        unjudged = [r for r in chunk if str(r["id"]) not in judged]
        if judge_new and unjudged:
            judged.update(_judge_repo_rows(spec, unjudged))
        for row in chunk:
            judge = judged.get(str(row["id"]), {})
            if judge.get("verdict") == "reject":
                continue
            row["judge"] = judge
            row["_score"] = _fuse_score(row, judge, keywords) * multipliers.get(
                row["source_dataset"], 1.0
            )
            selected.append(row)

    if threshold is not None:
        above = [r for r in selected if r["_score"] >= threshold]
        # Only enforce the user's bar while it still leaves a real dataset —
        # silently returning 40 rows for a 1,000-row request would be worse
        # than including weaker matches and saying so.
        if len(above) >= target * 0.5:
            selected = above

    selected.sort(key=lambda r: r["_score"], reverse=True)
    selected = dedupe_candidates(selected, threshold=settings.phash_hamming_threshold)
    sources = {r["source_dataset"] for r in selected}
    return diversify_by_source(
        selected, limit=target, max_share_per_source=unify.compute_max_share(len(sources))
    )


def _emit_progress_rows(job_id: str, rows: list[dict], batch: int) -> None:
    """Publish what the build has found so far.

    Written to `recommendation_results`, the platform's existing progressive
    channel, so the workspace can render rows as they are selected instead of
    staring at an empty panel for the whole build. Idempotent per (job, asset)
    so re-emitting a row that survived an earlier pass is harmless.
    """
    if not rows:
        return
    with cursor() as cur:
        for rank, row in enumerate(rows):
            cur.execute(
                """insert into recommendation_results (job_id, asset_id, rank, score, batch, reason)
                   values (%s, %s, %s, %s, %s, %s)
                   on conflict (job_id, asset_id) do nothing""",
                (
                    job_id,
                    row["id"],
                    rank,
                    float(row.get("_score") or 0.0),
                    batch,
                    json.dumps({"source": row.get("source_dataset")}),
                ),
            )


def _build_label_map(spec: dict, rows: list[dict]) -> dict[str, str]:
    """Reconcile label vocabularies across sources, conservatively."""
    labels = sorted({label for row in rows for label in (row.get("labels") or [])})
    if len(labels) < 2:
        return {}
    clusters = agent_brain.cluster_labels(labels, spec)
    # A dataset that defines two labels as separate classes has already told us
    # they are not the same thing, whatever they look like.
    class_sets = [
        row["labels"] for row in rows if len(row.get("labels") or []) > 1
    ]
    return unify.safe_label_map(clusters, class_sets)


def _create_version(request: dict, run_id: str, rows: list[dict], reviewed_by_id: dict) -> tuple[str, str, int]:
    """Snapshot the selection into an immutable dataset version."""
    name = (request["query"] or "Curated dataset")[:80]
    with cursor() as cur:
        cur.execute(
            "insert into datasets (project_id, name, description) values (%s, %s, %s) returning id",
            (request.get("project_id"), name, f"Built by the dataset agent from: {request['query']}"),
        )
        dataset_id = str(cur.fetchone()["id"])
        cur.execute(
            """insert into dataset_versions (dataset_id, version, schema, stats, status)
               values (%s, 1, %s, %s, 'ready') returning id""",
            (
                dataset_id,
                json.dumps({"unified": True}),
                json.dumps(
                    {
                        "rows": len(rows),
                        "sources": sorted({r["source_dataset"] for r in rows}),
                    }
                ),
            ),
        )
        version_id = str(cur.fetchone()["id"])
        for position, row in enumerate(rows):
            cur.execute(
                """insert into dataset_records (version_id, asset_id, position, selection_meta)
                   values (%s, %s, %s, %s)
                   on conflict (version_id, asset_id) do nothing""",
                (
                    version_id,
                    row["id"],
                    position,
                    json.dumps(
                        {
                            "picked_by": "agent",
                            "run_id": run_id,
                            "score": row.get("_score"),
                            "judge_verdict": (row.get("judge") or {}).get("verdict"),
                            "judge_evidence": (row.get("judge") or {}).get("evidence"),
                            "user_verdict": reviewed_by_id.get(str(row["id"])),
                            "missing": (row.get("judge") or {}).get("missing") or [],
                        },
                        default=str,
                    ),
                ),
            )
    return dataset_id, version_id, len(rows)


def process_build(job: dict) -> dict:
    request_id = job["input"]["request_id"]
    request = fetch_one("select * from dataset_requests where id = %s", (request_id,))
    run = agent_state.get_run_for_request(request_id)
    if not request or not run:
        return {"error": "request or run not found"}

    run_id = str(run["id"])
    spec = run["spec"] or {}
    target = int(request.get("example_count") or 100)

    # A build that already produced a version has nothing to redo. Jobs are
    # re-delivered when a worker dies mid-run, and replaying would crawl
    # everything again and duplicate the timeline.
    if run["version_id"]:
        return {"already_built": True, "version_id": str(run["version_id"])}

    agent_state.set_phase(run_id, "REFINING", 0.1, status="RUNNING")
    reviewed = [
        r for r in agent_state.get_candidates(run_id) if r["decision"] in ("approved", "rejected")
    ]
    refinement = agent_brain.refine_spec(spec, reviewed)
    spec = refinement["spec"]
    multipliers = _source_multipliers(reviewed)
    threshold = _threshold_from_review(reviewed)
    agent_state.update_run(run_id, spec=spec, threshold=threshold)

    for repo, multiplier in multipliers.items():
        agent_state.record_source(run_id, repo, source_multiplier=multiplier)
    demoted = [repo for repo, m in multipliers.items() if m == 0.0]

    summary = refinement.get("summary") or "Refined the search from your picks."
    if demoted:
        summary += f" Dropping {', '.join(demoted)} — you rejected what it offered."
    agent_state.emit_event(run_id, "labeling", "Applied your review to the search", {"demoted": demoted})
    agent_state.emit_event(run_id, "message", summary, role="assistant")

    agent_state.set_phase(run_id, "BUILDING", 0.3)
    sources = agent_state.get_sources(run_id)
    # "Contributed rows that survived judging", not "was mentioned during the
    # search". A dataset that only ever appeared in discovery has no business
    # supplying rows to the finished dataset.
    usable = [
        s for s in sources if (s["rows_kept"] or 0) > 0 and multipliers.get(s["repo_id"]) != 0.0
    ]
    # Confirmed datasets first, and only widen if they cannot fill the target.
    repos = [s["repo_id"] for s in usable if s["relevance"] == "strong"]
    if len(repos) < 2:
        repos += [s["repo_id"] for s in usable if s["relevance"] != "strong"]
    if not repos:
        repos = [s["repo_id"] for s in sources if s["relevance"] in ("strong", "partial")]

    # Deepen the good datasets first, within the materialization budget, then
    # fall back to text-only reference rows if the target is still out of reach.
    budget = settings.agent_materialize_cap
    per_repo = max(1, budget // max(len(repos), 1))
    build_job_id = str(run["build_job_id"]) if run["build_job_id"] else job["id"]
    for index, repo in enumerate(repos):
        indexed = _crawl(repo, per_repo)
        if indexed:
            agent_state.emit_event(run_id, "fetching", f"Indexed {indexed} more rows from {repo}")
        # Publish after every dataset so rows appear in the workspace while the
        # build is still running, rather than all at once at the end.
        _emit_progress_rows(
            build_job_id,
            # Preview only — reuse existing verdicts rather than paying for a
            # judging pass on every crawl step. The final selection judges.
            _select_final(
                spec, run_id, repos[: index + 1], target, threshold, multipliers, judge_new=False
            ),
            batch=index,
        )
        queue.update_progress(
            build_job_id, 0.3 + 0.4 * (index + 1) / max(len(repos), 1), status="PARTIAL"
        )

    selected = _select_final(spec, run_id, repos, target, threshold, multipliers)
    if len(selected) < target:
        added = _extend_with_references(run_id, spec, repos, target - len(selected))
        if added:
            selected = _select_final(spec, run_id, repos, target, threshold, multipliers)

    # Coming up short is not a reason to stop looking. The datasets that
    # answered the review are rarely the only ones on the Hub that fit, and
    # "27 rows from 2 source datasets" is a poor answer to a request for 100
    # when a few more searches would have found more. Each round asks Hugging
    # Face for datasets we do not already hold, so every pass widens the net
    # rather than re-reading the same repos.
    deadline = time.monotonic() + BUILD_DISCOVERY_BUDGET_SECONDS
    for round_number in range(BUILD_DISCOVERY_ROUNDS):
        if len(selected) >= target or time.monotonic() > deadline:
            break
        agent_state.emit_event(
            run_id,
            "explored",
            f"Only {len(selected)} of {target} so far — looking for more datasets",
            {"round": round_number + 1},
        )
        found = _fetch_from_hugging_face(run_id, spec, request["query"])
        new_repos = [r for r in (found.get("datasets_found") or []) if r not in repos]
        if not new_repos:
            agent_state.emit_event(
                run_id, "explored", "No further datasets on the Hub matched this request"
            )
            break
        repos.extend(new_repos)
        selected = _select_final(spec, run_id, repos, target, threshold, multipliers)
        if len(selected) < target:
            _extend_with_references(run_id, spec, new_repos, target - len(selected))
            selected = _select_final(spec, run_id, repos, target, threshold, multipliers)

    if not selected:
        agent_state.fail_run(run_id, "no rows survived the refined search")
        return {"error": "no rows selected"}

    _emit_progress_rows(build_job_id, selected, batch=len(repos))
    reviewed_by_id = {str(r["asset_id"]): r["decision"] for r in reviewed}
    label_map = _build_label_map(spec, selected)
    dataset_id, version_id, count = _create_version(request, run_id, selected, reviewed_by_id)

    export_job, _ = agent_state.ensure_export_job(
        run_id,
        {
            "version_id": version_id,
            "dataset_id": dataset_id,
            "version": 1,
            "mode": "references",
            "zip": True,
            "label_map": label_map,
        },
        prefer_current=True,
        dataset_id=dataset_id,
        version_id=version_id,
    )
    export_job_id = str(export_job["id"])

    shortfall = ""
    if count < target:
        # Said plainly rather than quietly under-delivering — the platform has
        # been bitten before by jobs that looked complete but weren't.
        shortfall = (
            f" That's short of the {target} you asked for: this is everything I could "
            f"find that genuinely matches."
        )
    agent_state.emit_event(
        run_id,
        "message",
        f"Built your dataset: {count} rows from "
        f"{len({r['source_dataset'] for r in selected})} source datasets.{shortfall} "
        "Packaging has started. Export Options will show the download as soon as it is ready.",
        role="assistant",
    )
    agent_state.update_run(
        run_id,
        stats={
            **(run["stats"] or {}),
            "built_rows": count,
            "requested_rows": target,
            "label_map_size": len(label_map),
            "sources_used": sorted({r["source_dataset"] for r in selected}),
        },
    )
    return {"rows": count, "requested": target, "version_id": version_id, "export_job_id": export_job_id}


# ── Poll loop ─────────────────────────────────────────────────────────


_HANDLERS = {"agent_request": process_request, "agent_build": process_build}


def run_poll():
    # Split across processes in deployment: a build takes minutes, and if one
    # worker served both types a build would sit in front of every new user's
    # review. The review is the interactive step and must not queue behind it.
    job_types = [
        t.strip()
        for t in os.getenv("AGENT_JOB_TYPES", "agent_request,agent_build").split(",")
        if t.strip() in _HANDLERS
    ] or list(_HANDLERS)

    log.info("agent_worker polling for %s jobs...", " and ".join(job_types))
    while True:
        handled = False
        for job_type, handler in ((t, _HANDLERS[t]) for t in job_types):
            # Claiming a job is itself a database call, and the connection
            # budget is shared with everything else. A busy moment must not be
            # fatal: without this, one PoolTimeout here killed every worker at
            # once and the whole pipeline stopped until they were restarted.
            try:
                job = queue.dequeue(job_type, lease_seconds=1800)
            except Exception as e:  # noqa: BLE001
                log.warning("could not poll for %s jobs (%s); retrying", job_type, e)
                time.sleep(5)
                continue
            if not job:
                continue
            handled = True
            job["id"] = str(job["id"])
            log.info("picked up %s job %s", job_type, job["id"])
            try:
                queue.complete(job["id"], handler(job))
            except Exception as e:  # noqa: BLE001
                log.exception("%s job %s failed", job_type, job["id"])
                # Recording the failure needs the database too, and that is
                # exactly what may have just failed — never let the report of
                # an error become a second, fatal error.
                try:
                    queue.fail(job["id"], str(e))
                    run = agent_state.get_run_for_request(job["input"].get("request_id", ""))
                    if run:
                        agent_state.fail_run(str(run["id"]), str(e))
                        agent_state.emit_event(
                            str(run["id"]), "error", f"The search failed: {e}", role="assistant"
                        )
                except Exception:  # noqa: BLE001
                    log.exception("could not record the failure of job %s", job["id"])
            break
        if not handled:
            time.sleep(2)


if __name__ == "__main__":
    run_poll()
