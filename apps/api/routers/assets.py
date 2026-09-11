"""Asset details — full metadata + provenance ("why is this here?")."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from datacurate_core.auth import get_optional_user
from datacurate_core.db import execute, fetch_one
from datacurate_core.field_types import collect_frames_from_values
from datacurate_core.hf_client import get_rows
from datacurate_core.trajectory import build_event_trajectory, build_trajectory
from datacurate_core.mediacache import get_or_cache_media

from serialize import summarize_large_metadata

router = APIRouter()
log = logging.getLogger("assets_router")


@router.get("/assets/{asset_id}")
def get_asset(asset_id: str):
    row = fetch_one("select * from assets where id = %s", (asset_id,))
    if not row:
        raise HTTPException(404, "asset not found")
    row["id"] = str(row["id"])

    # Every asset detail view is the one place we actually play/display the
    # full-resolution original (grid/feed views only ever use the already-
    # cached thumbnail). Cache it here, lazily, on first real view — not at
    # ingest time for every one of the corpus's thousands of rows, which
    # would be slow and mostly wasted (most assets are never opened).
    #
    # Fast path: already cached (the common case after the first view) needs
    # zero network calls at all, just the column we already selected.
    if not row.get("cached_content_uri") and row.get("content_uri"):
        try:
            url, _ = get_or_cache_media(row, return_bytes=False)
            row["cached_content_uri"] = url
            row["cached_at"] = "now"  # cosmetic; real value is in the DB row henceforth
        except Exception:  # noqa: BLE001
            # Never let a caching failure break viewing the asset — the
            # frontend already falls back to content_uri (the original,
            # possibly-signed-but-still-currently-valid HF URL) if this is empty.
            log.warning("get_asset(%s): media cache-on-view failed (non-fatal)", asset_id, exc_info=True)

    # CONFIRMED LIVE (measured): this endpoint returns `select *`, so it was
    # shipping raw metadata verbatim — for the worst real asset in the
    # corpus (Doub7e/SDv2-Count70-details-appended-motorcycle, which stores a
    # full T5 hidden-state tensor per row) that's a 1.42 MB response taking
    # **12.3 seconds** to open a single asset. Every real field is still
    # returned; only individual values too large to render at all are
    # replaced with an honest summary of what they are (see
    # serialize.summarize_large_metadata).
    row["metadata"] = summarize_large_metadata(row.get("metadata"))

    return row


@router.post("/assets/{asset_id}/events")
def log_event(asset_id: str, payload: dict, user: dict | None = Depends(get_optional_user)):
    """impression | click | like | save | remove | export.

    Real per-user attribution when logged in (via the verified bearer token
    — NEVER trust a user_id the client claims in the request body, that's
    trivially spoofable), still logged (as anonymous, user_id NULL) for
    logged-out browsing so impression/click volume tracking keeps working
    either way.
    """
    event = payload.get("event", "impression")
    execute(
        "insert into asset_events (user_id, asset_id, event, context) values (%s, %s, %s, %s)",
        (user["id"] if user else None, asset_id, event, __import__("json").dumps(payload.get("context", {}))),
    )
    return {"ok": True}


@router.get("/assets/{asset_id}/episode")
def get_episode(asset_id: str):
    """Fresh, playable episode media for one asset.

    Solves two real, separately-confirmed problems at once:

    1. EXPIRY. The per-frame URLs stored at ingest are Hugging Face
       `datasets-server` SIGNED urls (`?Expires=...`), good for ~48-72h.
       Stored verbatim they go dead, exactly like the main content_uri did
       before refresh_content_uri existed. So we don't trust the stored
       ones: we re-resolve the row live from /rows on every call, which can
       never be stale.

    2. CORS / PLAYBACK. The screen recording lives at
       huggingface.co/datasets/.../resolve/main/... which answers with
       `Access-Control-Allow-Origin: https://huggingface.co` — NOT `*`.
       A <video> element on our own origin is therefore blocked by the
       browser: the poster frame renders but playback silently never
       starts (confirmed live, this is exactly what the user saw). Caching
       the file into OUR blob storage and serving it from there fixes it
       for good, and gets range-request seeking + immutable caching for
       free since that's how every other cached asset already behaves.
    """
    row = fetch_one("select * from assets where id = %s", (asset_id,))
    if not row:
        raise HTTPException(404, "asset not found")

    metadata = row.get("metadata") or {}
    out: dict = {"frames": [], "recording_uri": None, "frame_count": 0,
                 "trajectory": None, "segments": None, "narration": None}

    # 1. Re-resolve every frame live so nothing is ever an expired URL.
    ds_row = fetch_one(
        "select media_column, media_column_type from hf_datasets where repo_id = %s",
        (row["source_dataset"],),
    )
    if ds_row and ds_row.get("media_column"):
        try:
            data = get_rows(row["source_dataset"], row["source_config"], row["source_split"],
                            offset=row["source_row"], length=1)
            rows = data.get("rows", [])
            if rows:
                values = rows[0].get("row", {})
                fresh = collect_frames_from_values(values, ds_row["media_column"])
                out["frames"] = [f["src"] for f in fresh]
        except Exception:  # noqa: BLE001
            log.warning("get_episode(%s): live frame re-resolve failed", asset_id, exc_info=True)

    if not out["frames"]:
        out["frames"] = [f.get("src") for f in metadata.get("_media_frames", []) if f.get("src")]
    out["frame_count"] = len(out["frames"])

    # 1b. Step-indexed trajectory: actions aligned by index with the frames
    # above, so the UI can show the EXACT screen state for a step plus a
    # reticle on the coordinate that step targets.
    out["trajectory"] = build_trajectory(metadata, out["frames"])
    if out["trajectory"] is None:
        # Datasets that ship a raw input-event log instead of a pre-built
        # action array (anaisleila/computer-use-data-psai). Same step model,
        # derived from real events, and every step carries a video timestamp.
        out["trajectory"] = build_event_trajectory(metadata, out["frames"])

    # Narrated screencasts (computer-use-large shape) have no action
    # coordinates — inventing a reticle for them would be a lie. What they do
    # have is timestamped narration segments, which make the video seekable by
    # meaning: click a segment, jump to that second.
    segments = metadata.get("_segments")
    if isinstance(segments, list) and segments:
        out["segments"] = segments
        out["narration"] = {
            "title": metadata.get("title"),
            "channel": metadata.get("channel"),
            "category": metadata.get("category"),
            "domain": metadata.get("domain"),
            "segment_count": metadata.get("_segment_count") or len(segments),
            "duration_seconds": metadata.get("duration_seconds"),
        }

    # 2. Serve the recording from our own storage (CORS-safe + seekable).
    # Fast path: a featured dataset that's already been pre-cached (see
    # ingest_worker.precache_episodes) needs zero network work here.
    precached = metadata.get("_recording_cached_uri")
    if precached:
        out["recording_uri"] = precached
        return out

    recording = metadata.get("_recording_uri")
    # Some real screencasts in computer-use-large are 400 MB. Those are worth
    # showing but not worth pulling into our blob storage on a user's click —
    # stream them straight from the source instead. `_cacheable` is decided at
    # ingest against the file's real published size.
    if recording and metadata.get("_cacheable") is False:
        out["recording_uri"] = recording
        return out

    if recording:
        try:
            cached, _ = get_or_cache_media(
                {"id": asset_id, "content_uri": recording, "mime_type": "video/mp4", "modality": "video"},
                persist=False,
            )
            out["recording_uri"] = cached
        except Exception:  # noqa: BLE001
            log.warning("get_episode(%s): recording cache failed, falling back to source", asset_id, exc_info=True)
            out["recording_uri"] = recording

    return out
