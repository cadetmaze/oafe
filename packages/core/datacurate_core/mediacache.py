"""Fetch-once-cache-forever for actual media bytes — the ONE place this
logic lives now. Previously only `export_worker._materialize()` did this
(and only at export time); everything else, most importantly the asset
detail view/modal, played media straight off Hugging Face's `/rows`
response: a *signed, expiring* URL (`?Expires=...&Signature=...`). That
means (a) we never actually cached it anywhere and (b) it goes dead once
the signature expires, even though the DB row for that asset lives forever.

This module is safe to call from the API process (lazily, on first real
view) AND from workers (eagerly, e.g. at export time) — same cache, same
Blob container, same content-hash key, so whichever one gets there first
"wins" and the other gets a cache hit.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from .blob import blob_exists, content_hash_name, content_hash_name_from_bytes, download_bytes, get_url, upload_bytes
from .config import settings
from .db import execute, fetch_one
from .hf_client import HFError, get_rows, resolve_media_value
from .thumbnails import fetch_bytes

log = logging.getLogger("mediacache")
_FFMPEG = shutil.which("ffmpeg")


# CONFIRMED LIVE (user-reported: "when video is playing and then I skip to
# the middle, it's not working"): cached videos were being served as
# `Content-Type: video/octet-stream`. That is NOT a real media MIME type —
# browsers can't infer the container/codec from it, so <video> refuses to
# seek (Azure Blob itself is fine: it correctly answers Range requests with
# 206 Partial Content, verified live). The bad value came from upstream
# `mime_type` values like "video/octet-stream" being trusted verbatim.
# Anything that isn't a MIME type a browser can actually play must fall back
# to the real per-modality default.
_PLAYABLE_DEFAULTS = {"image": "image/jpeg", "video": "video/mp4", "audio": "audio/mpeg"}
_UNUSABLE_MIME_SUFFIXES = ("octet-stream", "binary", "unknown")


def faststart_mp4(raw: bytes) -> bytes:
    """Move an mp4's `moov` atom to the front so it can stream.

    CONFIRMED LIVE (user-reported: videos show a poster, 0:00, an empty
    scrubber and never play): real dataset videos are muxed with `mdat`
    BEFORE `moov` — i.e. the duration/seek index lives at the very END of
    the file. Progressive HTTP playback then can't report a duration or
    start playing until the whole file has downloaded (15 MB+ for some of
    these), which looks exactly like a permanently stuck player. Azure Blob
    was innocent: it answers Range requests with 206 correctly.

    `-movflags +faststart` re-orders the atoms without re-encoding (stream
    copy, so it's fast and lossless). If ffmpeg isn't available or the remux
    fails, return the original bytes unchanged — a non-fast-start video is
    still better than no video.
    """
    if not _FFMPEG or not raw[:12].find(b"ftyp") >= 0:
        return raw
    try:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "in.mp4"
            dst = Path(td) / "out.mp4"
            src.write_bytes(raw)
            proc = subprocess.run(
                [_FFMPEG, "-y", "-loglevel", "error", "-i", str(src),
                 "-c", "copy", "-movflags", "+faststart", str(dst)],
                capture_output=True, timeout=180,
            )
            if proc.returncode == 0 and dst.exists() and dst.stat().st_size > 0:
                out = dst.read_bytes()
                log.info("faststart remux ok: %d -> %d bytes", len(raw), len(out))
                return out
            log.warning("faststart remux failed (rc=%s), using original", proc.returncode)
    except Exception:  # noqa: BLE001
        log.warning("faststart remux errored, using original", exc_info=True)
    return raw


def safe_content_type(mime_type: str | None, modality: str) -> str:
    """The Content-Type we actually serve this media as."""
    default = _PLAYABLE_DEFAULTS.get(modality, "application/octet-stream")
    if not mime_type or "/" not in mime_type:
        return default
    if any(mime_type.lower().endswith(s) for s in _UNUSABLE_MIME_SUFFIXES):
        return default
    return mime_type


def guess_ext(mime_type: str | None, modality: str) -> str:
    ext_defaults = {"image": ".jpg", "video": ".mp4", "audio": ".wav"}
    if mime_type and "/" in mime_type and not any(
        mime_type.lower().endswith(s) for s in _UNUSABLE_MIME_SUFFIXES
    ):
        return "." + mime_type.split("/")[-1]
    return ext_defaults.get(modality, ".bin")


def get_or_cache_media(asset: dict, return_bytes: bool = False, persist: bool = True) -> tuple[str, bytes | None]:
    """asset needs: id, content_uri, mime_type, modality.

    Returns (durable_url, raw_bytes_or_None). durable_url points at OUR blob
    storage, never HF's expiring URL, once this has run at least once for
    this asset. persist=True also writes assets.cached_content_uri so future
    callers (including a totally different process) can skip straight to
    the fast path without even checking Blob existence.

    True content-addressable storage: the URL string is only ever used as a
    same-URL fast-path pre-check (cache_key below); the ACTUAL, canonical
    storage location is keyed by a hash of the real downloaded bytes (see
    content_hash_name_from_bytes) — so two different asset rows, or the
    same asset re-fetched via a refreshed signed URL after the old one
    expired, that happen to be byte-identical always land on and reuse the
    exact same blob, never a duplicate upload.
    """
    content_uri = asset["content_uri"]
    ext = guess_ext(asset.get("mime_type"), asset["modality"])
    url_cache_key = content_hash_name(content_uri, ext=ext)

    if blob_exists(settings.blob_container_cache, url_cache_key):
        url = get_url(settings.blob_container_cache, url_cache_key)
        raw = download_bytes(settings.blob_container_cache, url_cache_key) if return_bytes else None
        if persist:
            _persist(asset["id"], url)
        return url, raw

    try:
        raw = fetch_bytes(content_uri, max_bytes=100_000_000)
    except Exception as e:  # noqa: BLE001
        # CONFIRMED LIVE (the real cause of "some images don't show when
        # clicked"): HF's /rows response hands back a *signed, expiring*
        # URL (Expires=...&Signature=...) -- typically good for only
        # ~48-72h. Once the corpus has been sitting around longer than that
        # (very normal for an actively-developed platform), the STORED
        # content_uri is already dead by the time anyone ever opens that
        # asset for the first time -- so the lazy "cache on first view"
        # strategy alone can never succeed for it; there's no valid URL left
        # to fetch bytes from. Re-resolving a FRESH signed URL from HF (the
        # underlying file itself hasn't gone anywhere) and retrying once is
        # the actual fix, not just a fallback.
        log.info("mediacache: fetch failed for asset %s (%s) -- likely an expired HF signed URL, trying to re-resolve a fresh one", asset["id"], e)
        fresh_uri = refresh_content_uri(asset)
        if not fresh_uri:
            log.warning("mediacache: could not re-resolve a fresh URL for asset %s -- leaving uncached, caller falls back to source URI", asset["id"])
            return content_uri, None
        try:
            raw = fetch_bytes(fresh_uri, max_bytes=100_000_000)
            content_uri = fresh_uri
        except Exception as e2:  # noqa: BLE001
            log.warning("mediacache: re-resolved URL also failed to fetch for asset %s: %s", asset["id"], e2)
            return fresh_uri, None

    # Real dedup happens here: check by the ACTUAL bytes' hash, not the URL
    # that produced them. If some OTHER asset (or an earlier, now-expired
    # URL for this SAME asset) already stored this exact content, reuse it
    # — skip the upload entirely, nothing new gets written to Blob.
    content_cache_key = content_hash_name_from_bytes(raw, ext=ext)
    if blob_exists(settings.blob_container_cache, content_cache_key):
        url = get_url(settings.blob_container_cache, content_cache_key)
    else:
        # Videos must be fast-start or the browser can't play/seek them.
        if asset["modality"] == "video":
            remuxed = faststart_mp4(raw)
            if remuxed is not raw:
                raw = remuxed
                content_cache_key = content_hash_name_from_bytes(raw, ext=ext)
                if blob_exists(settings.blob_container_cache, content_cache_key):
                    url = get_url(settings.blob_container_cache, content_cache_key)
                    if persist:
                        _persist(asset["id"], url)
                    return url, (raw if return_bytes else None)
        url = upload_bytes(
            settings.blob_container_cache,
            content_cache_key,
            raw,
            safe_content_type(asset.get("mime_type"), asset["modality"]),
        )
    if persist:
        _persist(asset["id"], url)
    return url, (raw if return_bytes else None)


def _persist(asset_id: str, url: str) -> None:
    try:
        execute("update assets set cached_content_uri = %s, cached_at = now() where id = %s", (url, asset_id))
    except Exception:  # noqa: BLE001
        log.warning("mediacache: failed to persist cached_content_uri for %s (non-fatal)", asset_id, exc_info=True)


def refresh_content_uri(asset: dict) -> str | None:
    """Re-resolve a fresh, currently-valid media URL straight from Hugging
    Face for one specific already-known row -- used when the URL we stored
    at ingest time has expired. Cheap: one /rows call for exactly one row,
    not a re-crawl. Also persists the fresh content_uri so direct (non-
    cached) consumers of this asset get the working URL too, until it
    expires again.
    """
    dataset_row = fetch_one(
        "select media_column, media_column_type from hf_datasets where repo_id = %s", (asset["source_dataset"],)
    )
    if not dataset_row or not dataset_row.get("media_column"):
        log.warning("refresh_content_uri: no known media_column for dataset %s", asset["source_dataset"])
        return None

    try:
        data = get_rows(
            asset["source_dataset"], asset["source_config"], asset["source_split"],
            offset=asset["source_row"], length=1,
        )
    except HFError as e:
        log.warning("refresh_content_uri: /rows failed for %s row %s: %s", asset["source_dataset"], asset["source_row"], e)
        return None

    rows = data.get("rows", [])
    if not rows:
        return None
    media_val = rows[0].get("row", {}).get(dataset_row["media_column"])
    fresh_url = resolve_media_value(asset["source_dataset"], dataset_row.get("media_column_type"), media_val)
    if not fresh_url:
        return None

    try:
        execute("update assets set content_uri = %s where id = %s", (fresh_url, asset["id"]))
    except Exception:  # noqa: BLE001
        log.warning("refresh_content_uri: failed to persist refreshed content_uri for %s (non-fatal)", asset["id"], exc_info=True)

    return fresh_url
