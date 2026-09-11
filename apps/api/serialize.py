"""Single shared row->Asset shape used by EVERY router. Previously search.py
had its own `_row_to_asset` while jobs.py and moodboards.py each hand-rolled a
flat (non-nested) shape — a real bug: the frontend's Asset type always
expects `asset.content.thumbnail_uri`, so any endpoint returning flat fields
crashed the UI (moodboard restore-on-load, and recommendation results).

Fix: one function, used everywhere. Every SQL query that feeds this must
select at least: id, modality, thumbnail_uri, caption. Everything else is
optional (falls back to None/[]).
"""
from __future__ import annotations

import json

# CONFIRMED LIVE (user-reported "it's super damn slow"): a handful of real
# datasets store ENORMOUS blobs in their per-row metadata — measured in the
# real corpus: Doub7e/SDv2-Count70-details-appended-motorcycle averages
# **1.48 MB of metadata PER ASSET** (raw CLIP/YOLO embedding arrays),
# jasperai/monet 230 KB each, anaisleila/computer-use-data-psai 145 KB each;
# 169 MB of metadata across the corpus in total. A single grid page that
# happened to include a few of those ballooned to 513 KB for 48 assets
# (measured: 2.7s wall time vs 47ms of actual server-side query time — i.e.
# ~98% of the wait was shipping metadata the grid never even renders).
#
# The grid/feed/search list views only ever show thumbnail + caption +
# labels/tags; the detail panel fetches the full row separately via
# GET /assets/{id} anyway (and the frontend already prefers that response's
# metadata — see `detail?.metadata ?? asset.metadata` in annotation-data.ts).
# So list responses simply must not carry metadata at all.
MAX_METADATA_VALUE_BYTES = 2_000


def summarize_large_metadata(metadata: dict | None) -> dict | None:
    """For the DETAIL view, where metadata genuinely is the point: keep every
    real field, but replace any single value too big to meaningfully display
    (a 384-float embedding, a full tracking trace) with an honest one-line
    summary of what it actually is. No UI can render a 1.5 MB float array
    usefully — shipping it just makes opening an asset slow for no benefit.
    """
    if not metadata or not isinstance(metadata, dict):
        return metadata
    out = {}
    for key, value in metadata.items():
        # `_`-prefixed keys are STRUCTURAL, not display metadata: they carry
        # the row's full frame gallery and its screen-recording URL (see
        # ingest_worker's no-information-loss block). Summarising those away
        # would silently re-introduce exactly the data loss they exist to
        # prevent, so they always pass through intact.
        if key.startswith("_media") or key.startswith("_recording"):
            out[key] = value
            continue
        try:
            encoded = json.dumps(value, default=str)
        except Exception:  # noqa: BLE001
            out[key] = value
            continue
        if len(encoded) <= MAX_METADATA_VALUE_BYTES:
            out[key] = value
        elif isinstance(value, list):
            out[key] = f"[{len(value):,} values, {len(encoded):,} bytes — too large to display]"
        elif isinstance(value, dict):
            out[key] = f"{{{len(value)} keys, {len(encoded):,} bytes — too large to display}}"
        else:
            out[key] = encoded[:MAX_METADATA_VALUE_BYTES] + f"… [truncated, {len(encoded):,} bytes total]"
    return out


def row_to_asset(row: dict, extra: dict | None = None, include_metadata: bool = True) -> dict:
    out = {
        "id": str(row["id"]),
        "modality": row.get("modality"),
        "source": {
            "dataset": row.get("source_dataset"),
            "config": row.get("source_config"),
            "split": row.get("source_split"),
            "row": row.get("source_row"),
        },
        "content": {
            "thumbnail_uri": row.get("thumbnail_uri"),
            "preview_uri": row.get("preview_uri"),
            "uri": row.get("content_uri"),
            "width": row.get("width"),
            "height": row.get("height"),
            "duration": row.get("duration"),
            "fps": row.get("fps"),
        },
        "caption": row.get("caption"),
        "labels": row.get("labels") or [],
        "tags": row.get("tags") or [],
        "license": row.get("license"),
        "dataset_kind": row.get("dataset_kind"),
    }
    if include_metadata:
        out["metadata"] = summarize_large_metadata(row.get("metadata"))
    if extra:
        out.update(extra)
    return out
