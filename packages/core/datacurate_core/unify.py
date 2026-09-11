"""One consistent record shape for a dataset merged from many HF sources.

A single export can span a captioned photo set, a ClassLabel-only classifier
set and an agent-trajectory set — different column names, different label
vocabularies, different notions of what a "caption" even is. Without a
declared contract the result is a Parquet file whose schema depends on which
datasets happened to be selected.

Two things here are bug fixes to the existing exporter, not new features:

* **The schema is declared, not inferred.** `pa.Table.from_pylist(rows)` infers
  types from the data, so a column that happens to be all-None in one build
  becomes `null`-typed and conflicts or silently downcasts in the next. Every
  export now writes the same columns with the same types.
* **`license` gets a real source.** `assets.license` is never populated at
  ingest, so exports emitted NULL licenses that read as "no restrictions".
  The dataset-level Hub license is joined in and its origin recorded, so an
  unknown license is visibly unknown.

Label handling is deliberately lopsided: `labels` stays verbatim and
authoritative, `labels_canonical` is the convenience column. Merging labels
destroys information and is easy to get wrong, so the safety rules in
`safe_label_map` reject far more merges than they accept.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable

import pyarrow as pa

# `image`/`video`/`audio` are kept (both frontends and every previously
# exported dataset read them) even though `media_uri` now carries the same
# value in one place regardless of modality.
UNIFIED_SCHEMA = pa.schema(
    [
        ("asset_id", pa.string()),
        ("modality", pa.string()),
        ("image", pa.string()),
        ("video", pa.string()),
        ("audio", pa.string()),
        ("media_uri", pa.string()),
        # materialized = re-hosted by us and durable; reference = the source's
        # own URL, which for Hugging Face rows is signed and expires in ~48-72h.
        ("media_state", pa.string()),
        ("caption", pa.string()),
        # Which of several very different things the caption actually is. The
        # ingest path joins labels together when a dataset has no caption
        # column, so without this a label list masquerades as a real caption.
        ("caption_source", pa.string()),
        ("labels", pa.list_(pa.string())),
        ("labels_canonical", pa.list_(pa.string())),
        ("tags", pa.list_(pa.string())),
        ("license", pa.string()),
        ("license_source", pa.string()),
        ("width", pa.int64()),
        ("height", pa.int64()),
        ("duration", pa.float64()),
        ("source_dataset", pa.string()),
        ("source_config", pa.string()),
        ("source_split", pa.string()),
        ("source_row", pa.int64()),
        ("source_url", pa.string()),
        ("frames", pa.string()),
        ("frame_count", pa.int64()),
        ("recording_uri", pa.string()),
        ("description", pa.string()),
        ("fields", pa.string()),
        # Why the agent kept this row. Empty for rows curated by hand.
        ("relevance_score", pa.float64()),
        ("judge_verdict", pa.string()),
        ("judge_evidence", pa.string()),
        ("user_verdict", pa.string()),
        ("missing_fields", pa.list_(pa.string())),
        ("retrieved_at", pa.string()),
    ]
)

MANIFEST_SCHEMA = pa.schema(
    [
        ("asset_id", pa.string()),
        ("source_provider", pa.string()),
        ("source_dataset", pa.string()),
        ("source_config", pa.string()),
        ("source_split", pa.string()),
        ("source_row", pa.int64()),
        ("source_revision", pa.string()),
        ("source_url", pa.string()),
        ("license", pa.string()),
        ("license_source", pa.string()),
        ("phash", pa.int64()),
        ("media_state", pa.string()),
        ("retrieved_at", pa.string()),
        ("selection_meta", pa.string()),
    ]
)


def source_url(record: dict) -> str | None:
    """A link a human can open to check the claim this row makes."""
    repo = record.get("source_dataset")
    if not repo:
        return None
    config = record.get("source_config") or "default"
    split = record.get("source_split") or "train"
    row = record.get("source_row")
    return (
        f"https://huggingface.co/datasets/{repo}/viewer/{config}/{split}"
        f"?row={row if row is not None else 0}"
    )


def caption_source_for(record: dict) -> str | None:
    """Say what the caption really is rather than implying it's a caption."""
    caption = (record.get("caption") or "").strip()
    if not caption:
        return None
    labels = [str(x) for x in (record.get("labels") or [])]
    if labels and caption == " ".join(labels).strip():
        return "labels_joined"
    if not (record.get("metadata") or {}).get("_caption_column") and labels and caption in labels:
        return "labels_joined"
    return "source_caption"


def resolve_license(record: dict, dataset_license: str | None) -> tuple[str | None, str]:
    """Per-row licence first, dataset-level Hub tag second, otherwise unknown.

    Never invents a permissive default: a row with no discoverable licence is
    reported as unknown so a downstream user knows to check.
    """
    if record.get("license"):
        return record["license"], "row"
    if dataset_license:
        return dataset_license, "hub_tag"
    return None, "unknown"


# ── Label vocabulary ──────────────────────────────────────────────────

MIN_CLUSTER_CONFIDENCE = 0.8
MAX_CLUSTER_SIZE = 6


def normalize_label(label: str) -> str:
    return " ".join(str(label).replace("_", " ").replace("-", " ").lower().split())


def safe_label_map(
    clusters: Iterable[dict],
    mutually_exclusive: Iterable[Iterable[str]] = (),
) -> dict[str, str]:
    """Turn proposed synonym clusters into a map, dropping the unsafe ones.

    `mutually_exclusive` holds label sets that a single source defines as
    distinct classes (a ClassLabel `names` list). If one dataset declares both
    `cook` and `person_cooking` as separate classes, they demonstrably mean
    different things there, and merging them would destroy a real distinction
    no matter how synonymous they look.
    """
    forbidden: set[frozenset[str]] = set()
    for group in mutually_exclusive:
        names = [normalize_label(n) for n in group]
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                if a != b:
                    forbidden.add(frozenset((a, b)))

    mapping: dict[str, str] = {}
    for cluster in clusters:
        members = [normalize_label(m) for m in cluster.get("members") or [] if m]
        canonical = normalize_label(cluster.get("canonical") or "")
        confidence = float(cluster.get("confidence") or 0.0)
        if not canonical or len(members) < 2:
            continue
        if confidence < MIN_CLUSTER_CONFIDENCE or len(members) > MAX_CLUSTER_SIZE:
            continue
        if any(frozenset((a, b)) in forbidden for i, a in enumerate(members) for b in members[i + 1 :]):
            continue
        for member in members:
            mapping[member] = canonical
    return mapping


def canonical_labels(labels: Iterable[str], label_map: dict[str, str]) -> list[str]:
    out: list[str] = []
    for label in labels or []:
        norm = normalize_label(label)
        if not norm:
            continue
        mapped = label_map.get(norm, norm)
        if mapped not in out:
            out.append(mapped)
    return out


# ── Per-source balance ────────────────────────────────────────────────


def compute_max_share(source_count: int) -> float:
    """Cap per source, scaled to how many sources actually exist.

    The platform's fixed 0.25 default is unsatisfiable below four sources —
    3 x 0.25 < 1.0 — so the capped pass runs dry and the uncapped top-up
    quietly fills the rest, making the cap a no-op exactly when balance
    matters most. An agent build routinely draws on two or three datasets.
    """
    if source_count <= 0:
        return 1.0
    return min(0.35, max(0.10, 2.0 / source_count))


# ── Row construction ──────────────────────────────────────────────────


def unified_row(
    record: dict,
    media_uri: str | None,
    media_state: str,
    dataset_license: str | None = None,
    label_map: dict[str, str] | None = None,
) -> dict:
    """Build one row of the export. Every column in UNIFIED_SCHEMA is present.

    Missing values are explicit nulls and lists are empty lists — never the
    other way round — so a consumer can filter on absence without guessing.
    """
    metadata = record.get("metadata") or {}
    selection = record.get("selection_meta") or {}
    modality = record.get("modality")
    labels = [str(x) for x in (record.get("labels") or [])]
    frames = [f.get("src") for f in metadata.get("_media_frames", []) if isinstance(f, dict)]
    license_value, license_source = resolve_license(record, dataset_license)

    return {
        "asset_id": str(record["id"]),
        "modality": modality,
        "image": media_uri if modality == "image" else None,
        "video": media_uri if modality == "video" else None,
        "audio": media_uri if modality == "audio" else None,
        "media_uri": media_uri,
        "media_state": media_state,
        "caption": record.get("caption"),
        "caption_source": caption_source_for(record),
        "labels": labels,
        "labels_canonical": canonical_labels(labels, label_map or {}),
        "tags": [str(t) for t in (record.get("tags") or [])],
        "license": license_value,
        "license_source": license_source,
        "width": record.get("width"),
        "height": record.get("height"),
        "duration": record.get("duration"),
        "source_dataset": record.get("source_dataset"),
        "source_config": record.get("source_config"),
        "source_split": record.get("source_split"),
        "source_row": record.get("source_row"),
        "source_url": source_url(record),
        "frames": json.dumps(frames),
        "frame_count": len(frames),
        "recording_uri": metadata.get("_recording_uri"),
        "description": record.get("description"),
        "fields": json.dumps(
            {k: v for k, v in metadata.items() if not k.startswith("_")}, default=str
        ),
        "relevance_score": _as_float(selection.get("score")),
        "judge_verdict": selection.get("judge_verdict"),
        "judge_evidence": selection.get("judge_evidence"),
        "user_verdict": selection.get("user_verdict"),
        "missing_fields": [str(m) for m in (selection.get("missing") or [])],
        "retrieved_at": _iso(record.get("created_at")),
    }


def manifest_row(record: dict, media_state: str, dataset_license: str | None = None) -> dict:
    license_value, license_source = resolve_license(record, dataset_license)
    return {
        "asset_id": str(record["id"]),
        "source_provider": record.get("source_provider") or "huggingface",
        "source_dataset": record.get("source_dataset"),
        "source_config": record.get("source_config"),
        "source_split": record.get("source_split"),
        "source_row": record.get("source_row"),
        "source_revision": record.get("source_revision"),
        "source_url": source_url(record),
        "license": license_value,
        "license_source": license_source,
        "phash": record.get("phash"),
        "media_state": media_state,
        "retrieved_at": _iso(record.get("created_at")),
        "selection_meta": json.dumps(record.get("selection_meta") or {}, default=str),
    }


def to_parquet_table(rows: list[dict], schema: pa.Schema = UNIFIED_SCHEMA) -> pa.Table:
    return pa.Table.from_pylist(rows, schema=schema)


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)
