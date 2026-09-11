"""Ingest worker — HF catalog crawl + Tier-1 sampling + thumbnail generation.

Two entrypoints:
  python -m ingest_worker --seed          one-shot: crawl the curated seed list
  python -m ingest_worker --poll          long-running: consume 'ingest_dataset'
                                           jobs from the queue (the Tier-2
                                           "demand-driven densify" hook from
                                           the recommend worker lands here)

Everything funnels through `crawl_dataset()` so both entrypoints share the
exact same logic — no duplicated ingestion code.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time

import requests

from datacurate_core import queue
from datacurate_core.config import settings
from datacurate_core.db import cursor, execute
from datacurate_core.field_types import (
    classify_dataset_kind,
    clean_hub_tags,
    detect_caption_columns,
    detect_label_columns,
    detect_media_column,
    detect_nested_text_columns,
    extract_nested_text,
    extract_text_from_json_string,
    build_rich_text,
    collect_frames_from_values,
    find_recording_in_values,
    infer_schema,
    is_safe_scalar,
    looks_like_json_blob,
    parse_prediction_list,
)
from datacurate_core.hf_client import (
    HFError,
    HFNotSupported,
    get_dataset_tags,
    get_info,
    get_splits,
    iter_rows,
    list_datasets,
    resolve_media_url,
    resolve_media_value,
    search_datasets,
)
from datacurate_core.thumbnails import audio_pipeline, fetch_bytes, image_pipeline, video_pipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("azure").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
log = logging.getLogger("ingest_worker")

MODALITY_FOR_MEDIA_TYPE = {
    "native_image": "image", "native_audio": "audio", "native_video": "video",
    "url_string": "image", "path_string": "image",  # refined per-dataset below when known
}

# Curated seed list — a deliberate cross-section of popular, real HF datasets
# spanning image/audio/video, captioned vs label-only, to exercise every
# schema-inference branch we verified against the live API.
SEED_DATASETS: list[tuple[str, str]] = [
    # (repo_id, expected_modality_hint)
    ("uoft-cs/cifar10", "image"),                                   # label-only classification
    ("lmms-lab-encoder/COCO-Caption", "image"),                     # native Image + rich caption
    ("google-research-datasets/conceptual_captions", "image"),      # url_string + caption
    ("laion/220k-GPT4Vision-captions-from-LIVIS", "image"),         # url_string + long caption
    ("shivalikasingh/video-demo", "video"),                         # native Video (tiny, smoke test)
    ("PolyAI/minds14", "audio"),                                    # native Audio + transcription
]


def _upload_media(container: str, blob_prefix: str, ext: str, data: bytes | None, content_type: str) -> str | None:
    if not data:
        return None
    from datacurate_core.blob import upload_bytes
    return upload_bytes(container, f"{blob_prefix}{ext}", data, content_type=content_type)


def crawl_dataset(repo_id: str, max_rows: int | None = None, progress_cb=None, deadline_seconds: float | None = None) -> dict:
    """Profile + Tier-1 sample a single HF dataset. Idempotent: safe to re-run.

    deadline_seconds: wall-clock budget (confirmed live: this matters). Real
    photos (e.g. LAION) take meaningfully longer per row to fetch+thumbnail
    than tiny 32x32 CIFAR-10 images, and there's no per-row timeout that
    bounds *total* crawl duration. Without an overall deadline, a caller doing
    this inline from an interactive job (recommend_worker's Tier-2 densify)
    has no latency guarantee at all. When set, the crawl stops cleanly at the
    next row boundary once the deadline passes, returning whatever was
    indexed so far rather than blocking indefinitely.
    """
    max_rows = max_rows or settings.tier1_max_rows_per_dataset
    stats = {"repo_id": repo_id, "assets_indexed": 0, "assets_failed": 0, "configs_seen": 0}
    deadline = (time.monotonic() + deadline_seconds) if deadline_seconds else None

    try:
        splits = get_splits(repo_id)
    except HFNotSupported as e:
        log.warning("dataset not supported by viewer: %s (%s)", repo_id, e)
        _upsert_dataset_status(repo_id, viewer_supported=False, rows_failed=True)
        return stats
    except HFError as e:
        log.error("failed to get splits for %s: %s", repo_id, e)
        return stats

    if not splits:
        return stats

    configs_done: set[str] = set()
    all_modalities: set[str] = set()
    caption_cols_all: list[str] = []
    media_col_seen: tuple[str | None, str | None] = (None, None)
    dataset_kind_seen: str | None = None

    # NOTE (confirmed live): max_rows means "total rows across this whole
    # crawl_dataset() call", not "per split". A naive per-split application
    # made a 2-split dataset (train+test) fetch up to 2x the intended amount
    # (e.g. asked for +60, a caller saw +318 because of 2 splits) — that's
    # exactly the kind of surprising-latency bug that matters when this is
    # called inline from an interactive recommend job. Track remaining budget
    # across splits and stop the whole crawl once it's spent.
    rows_budget_remaining = max_rows

    for split_entry in splits:
        if rows_budget_remaining <= 0:
            break
        if deadline and time.monotonic() > deadline:
            log.warning("crawl_dataset(%s): deadline reached, stopping early with %d/%d rows indexed",
                        repo_id, stats["assets_indexed"], max_rows)
            break
        config = split_entry["config"]
        split = split_entry["split"]
        if config in configs_done and split != splits[0]["split"]:
            pass  # allow multiple splits per config; just skip re-profiling schema

        try:
            info = get_info(repo_id, config)
            features = info.get("features", {})
        except HFError as e:
            log.warning("skip %s/%s: %s", repo_id, config, e)
            continue

        media_col, media_type = detect_media_column(features)
        caption_cols = detect_caption_columns(features)
        label_cols = detect_label_columns(features)
        # Fallback ONLY when there's no scalar caption column at all (confirmed
        # live: a real "conversations": [{"from":..., "value":...}] chat-format
        # column held genuinely useful descriptive text about the image, but
        # detect_caption_columns() only ever looks at scalar Value columns).
        nested_text_cols = detect_nested_text_columns(features) if not caption_cols else []
        schema = infer_schema(features)

        if config not in configs_done:
            configs_done.add(config)
            stats["configs_seen"] += 1
            caption_cols_all.extend(caption_cols)
            media_col_seen = (media_col, media_type)
            kind = classify_dataset_kind(features, caption_cols, label_cols)
            # detection_segmentation is a strong, sticky signal (don't let a
            # later plain-labeled config downgrade a dataset that has ANY
            # segmentation/detection config)
            if dataset_kind_seen is None or kind == "detection_segmentation":
                dataset_kind_seen = kind

        if not media_col:
            log.info("no media column detected for %s/%s — skipping (metadata-only dataset)", repo_id, config)
            continue

        modality = _infer_modality(media_type, features, media_col)
        all_modalities.add(modality)

        n_indexed = _sample_split(
            repo_id, config, split, media_col, media_type, modality,
            caption_cols, label_cols, features, rows_budget_remaining, deadline=deadline,
            nested_text_cols=nested_text_cols,
        )
        stats["assets_indexed"] += n_indexed
        rows_budget_remaining -= n_indexed
        if progress_cb:
            progress_cb(stats)

    _upsert_dataset_status(
        repo_id,
        modalities=sorted(all_modalities) or None,
        media_column=media_col_seen[0],
        media_column_type=media_col_seen[1],
        caption_columns=sorted(set(caption_cols_all)) or None,
        dataset_kind=dataset_kind_seen,
        viewer_supported=True,
    )
    if dataset_kind_seen:
        with cursor() as cur:
            cur.execute("update assets set dataset_kind = %s where source_dataset = %s", (dataset_kind_seen, repo_id))
    return stats


def _infer_modality(media_type: str | None, features: dict, media_col: str) -> str:
    if media_type == "native_video":
        return "video"
    if media_type == "native_audio":
        return "audio"
    return "image"  # native_image, url_string, path_string default to image in V1


def _repo_id_tags(repo_id: str) -> list[str]:
    """Every asset gets tagged with words derived from its own dataset's name,
    regardless of whether the row itself has any usable caption/label text.

    Confirmed live gap this fixes: a user searched "flowchart" right after the
    platform had genuinely discovered and ingested several real flowchart
    datasets from the Hub (discovery worked!) — but most of those datasets
    are image-only with zero caption/label columns, so none of their rows'
    search_vector matched "flowchart" at all despite being exactly what was
    asked for. A dataset literally named `rukia07/flowchart-detection-dataset`
    is unambiguously about flowcharts even with zero per-row text.
    """
    parts = re.split(r"[/_\-. ]+", repo_id.lower())
    generic = {"dataset", "datasets", "data", "the", "of", "and", "for", "a", "an", "hf"}
    return sorted({p for p in parts if len(p) >= 3 and p not in generic and not p.isdigit()})


def _dataset_baseline_tags(repo_id: str) -> list[str]:
    """repo-id words + the dataset AUTHOR's own Hub tags ("egocentric",
    "robotics", "video-classification"...), cleaned of boilerplate. Real
    author-provided signal is stronger than anything we could guess from a
    caption — e.g. a robot-manipulation dataset's per-row captions are
    usually empty, but its Hub page is tagged "robotics" by the person who
    published it. Applied to EVERY asset from this dataset, same as the
    repo-id tags (see _repo_id_tags).
    """
    tags = set(_repo_id_tags(repo_id))
    try:
        tags.update(clean_hub_tags(get_dataset_tags(repo_id)))
    except Exception:  # noqa: BLE001
        pass  # tagging is a pure enrichment, never worth failing ingestion over
    return sorted(tags)


def _sample_split(
    repo_id: str, config: str, split: str, media_col: str, media_type: str, modality: str,
    caption_cols: list[str], label_cols: list[str], features: dict, max_rows: int,
    deadline: float | None = None, nested_text_cols: list[str] | None = None,
) -> int:
    from datacurate_core.field_types import classlabel_names

    n_indexed = 0
    class_names_cache: dict[str, list[str] | None] = {}
    nested_text_cols = nested_text_cols or []
    dataset_tags = _dataset_baseline_tags(repo_id)

    for row in iter_rows(repo_id, config, split, max_rows=max_rows, page_size=settings.rows_page_size):
        if deadline and time.monotonic() > deadline:
            log.warning("_sample_split(%s/%s/%s): deadline reached mid-split, stopping at %d rows",
                        repo_id, config, split, n_indexed)
            break
        row_idx = row["row_idx"]
        values = row["row"]
        media_val = values.get(media_col)
        if media_val is None:
            continue

        try:
            src_url, revision, width, height = _resolve_source(repo_id, media_type, media_val)
        except Exception as e:  # noqa: BLE001
            log.debug("could not resolve media url for %s row %s: %s", repo_id, row_idx, e)
            continue

        caption = None
        for c in caption_cols:
            v = values.get(c)
            # Defense in depth (confirmed live, see field_types.classify_dataset_kind
            # docstring): even a column whose *declared* schema type looked like
            # scalar text can hand back a dict/list at the row level for some
            # malformed datasets. Never stringify anything but a real scalar
            # into the caption a user sees.
            if not (v and is_safe_scalar(v)):
                continue
            if isinstance(v, str) and looks_like_json_blob(v):
                # CONFIRMED LIVE (gankun/...'s `qa` column, matched once "qa"
                # was added to CAPTION_NAME_HINTS): a "caption" column can
                # ALSO turn out to hold a JSON-encoded string rather than
                # real text — same failure class as the label-side
                # parse_prediction_list bug, different shape. Try to extract
                # something readable; if that fails, this column is empty
                # for this row, try the next caption column instead of ever
                # showing raw JSON as a "caption".
                extracted = extract_text_from_json_string(v)
                if extracted:
                    caption = extracted
                    break
                continue
            caption = str(v)
            break

        if not caption:
            for ntc in nested_text_cols:
                text = extract_nested_text(values.get(ntc))
                if text:
                    caption = text
                    break

        labels: list[str] = []
        for lc in label_cols:
            v = values.get(lc)
            if v is None or not is_safe_scalar(v):
                continue
            if isinstance(v, int):
                if lc not in class_names_cache:
                    class_names_cache[lc] = classlabel_names(features, lc)
                names = class_names_cache[lc]
                labels.append(names[v] if names and v < len(names) else str(v))
            elif isinstance(v, str):
                # CONFIRMED LIVE (jasperai/monet's `classifier_yolo`/
                # `classifier_clip-vit-base-patch32` columns): some datasets
                # store multi-label classifier output as a JSON-ENCODED STRING
                # in an otherwise-plain string column — no native Sequence/
                # struct type at all, so schema-level checks alone can't catch
                # it. Try to parse it into real label names; if it clearly
                # LOOKS like a JSON blob but doesn't parse into the expected
                # shape, drop it entirely rather than storing raw JSON as a
                # "label" (previously this broke to_tsquery() outright once
                # such a value got extracted as a board keyword).
                parsed = parse_prediction_list(v)
                if parsed is not None:
                    labels.extend(parsed)
                elif not v.lstrip()[:1] in ("[", "{"):
                    labels.append(v)
            else:
                labels.append(str(v))

        if not caption and labels:
            caption = " ".join(labels)  # label-only datasets: label becomes the caption

        # Always searchable by dataset name/topic, even for rows with zero
        # per-row text at all (see _repo_id_tags docstring).
        tags = list(dataset_tags)

        try:
            processed = _process_media(modality, src_url)
        except Exception as e:  # noqa: BLE001
            log.debug("thumbnail pipeline failed for %s row %s: %s", repo_id, row_idx, e)
            continue

        asset_id_key = f"{repo_id}:{config}:{split}:{row_idx}"
        thumb_url = _upload_media(
            settings.blob_container_thumbnails, f"{repo_id}/{config}/{split}/{row_idx}",
            ".webp", processed.get("thumbnail_bytes"), "image/webp",
        )
        preview_url = None
        if processed.get("preview_bytes"):
            preview_url = _upload_media(
                settings.blob_container_previews, f"{repo_id}/{config}/{split}/{row_idx}",
                ".webp", processed["preview_bytes"], "image/webp",
            )

        # NO INFORMATION LOSS (user-reported): keep EVERY frame in the row,
        # not just the one we picked as the representative thumbnail, and
        # resolve the row's screen-recording video if it names one. Both go
        # in metadata under `_`-prefixed structural keys the detail view
        # renders as a real frame gallery / video player.
        row_metadata = {
            k: v for k, v in values.items() if k != media_col and not isinstance(v, (dict, bytes))
        }
        frames = collect_frames_from_values(values, media_col)
        if len(frames) > 1:
            row_metadata["_media_frames"] = frames
            row_metadata["_media_frame_count"] = len(frames)
        found_rec = find_recording_in_values(values)
        if found_rec:
            _, rec_path = found_rec
            row_metadata["_recording_uri"] = resolve_media_url(repo_id, revision, rec_path)

        _upsert_asset({
            "modality": modality,
            "source_dataset": repo_id, "source_config": config, "source_split": split,
            "source_revision": revision, "source_row": row_idx,
            "content_uri": src_url, "thumbnail_uri": thumb_url, "preview_uri": preview_url,
            "width": processed.get("width", width), "height": processed.get("height", height),
            "duration": processed.get("duration"), "fps": processed.get("fps"),
            "sample_rate": processed.get("sample_rate"),
            "caption": caption, "labels": labels, "tags": tags,
            # Indexed at weight D by assets_search_trigger — this is what
            # makes the row's REAL text (an agent's `instruction`, a domain,
            # per-step reasoning) searchable at all. Previously none of it
            # was indexed, so searching the words that actually describe
            # these rows matched nothing.
            "description": build_rich_text(values),
            "metadata": row_metadata,
            "phash": processed.get("phash"), "color_dominant": processed.get("color_dominant"),
            "aspect_ratio": processed.get("aspect_ratio"),
        })
        n_indexed += 1

    return n_indexed


def _resolve_source(repo_id: str, media_type: str, media_val) -> tuple[str, str | None, int | None, int | None]:
    # URL-resolution itself lives in datacurate_core.hf_client.resolve_media_value
    # (shared with mediacache.refresh_content_uri, which needs the exact same
    # logic later to re-resolve an expired signed URL) — this wrapper just
    # adds the width/height extraction ingest cares about.
    url = resolve_media_value(repo_id, media_type, media_val)
    if not url:
        raise ValueError(f"could not resolve media value for {repo_id}: {media_val!r}")
    width = height = None
    if media_type in ("native_image", "native_audio", "native_video"):
        v = media_val[0] if isinstance(media_val, list) and media_val else media_val
        if isinstance(v, dict):
            width, height = v.get("width"), v.get("height")
    return url, None, width, height


def _process_media(modality: str, src_url: str) -> dict:
    raw = fetch_bytes(src_url, max_bytes=25_000_000)
    if modality == "image":
        return image_pipeline(raw)
    if modality == "video":
        return video_pipeline(raw)
    if modality == "audio":
        return audio_pipeline(raw)
    return {}


def _upsert_asset(a: dict) -> None:
    with cursor() as cur:
        cur.execute(
            """
            insert into assets (
                modality, source_dataset, source_config, source_split, source_revision, source_row,
                content_uri, thumbnail_uri, preview_uri, width, height, duration, fps, sample_rate,
                caption, labels, tags, metadata, phash, color_dominant, aspect_ratio, description
            ) values (
                %(modality)s, %(source_dataset)s, %(source_config)s, %(source_split)s, %(source_revision)s, %(source_row)s,
                %(content_uri)s, %(thumbnail_uri)s, %(preview_uri)s, %(width)s, %(height)s, %(duration)s, %(fps)s, %(sample_rate)s,
                %(caption)s, %(labels)s, %(tags)s, %(metadata)s, %(phash)s, %(color_dominant)s, %(aspect_ratio)s, %(description)s
            )
            on conflict (source_dataset, source_config, source_split, source_row)
            do update set
                thumbnail_uri = excluded.thumbnail_uri,
                preview_uri = excluded.preview_uri,
                caption = excluded.caption,
                labels = excluded.labels,
                tags = excluded.tags,
                metadata = excluded.metadata,
                phash = excluded.phash,
                width = excluded.width,
                height = excluded.height,
                duration = excluded.duration,
                aspect_ratio = excluded.aspect_ratio,
                description = excluded.description
            """,
            {**a, "metadata": json.dumps(a["metadata"])},
        )


def _upsert_dataset_status(repo_id: str, **fields) -> None:
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        with cursor() as cur:
            cur.execute(
                "insert into hf_datasets (repo_id, last_synced_at) values (%s, now()) "
                "on conflict (repo_id) do update set last_synced_at = now()",
                (repo_id,),
            )
        return
    set_clause = ", ".join(f"{k} = %({k})s" for k in fields)
    with cursor() as cur:
        cur.execute(
            f"""
            insert into hf_datasets (repo_id, last_synced_at, {', '.join(fields.keys())})
            values (%(repo_id)s, now(), {', '.join(f'%({k})s' for k in fields)})
            on conflict (repo_id) do update set last_synced_at = now(), {set_clause}
            """,
            {"repo_id": repo_id, **fields},
        )


def run_seed():
    log.info("seeding %d curated datasets", len(SEED_DATASETS))
    for repo_id, hint in SEED_DATASETS:
        log.info("=== crawling %s (expected modality: %s) ===", repo_id, hint)
        t0 = time.time()
        try:
            stats = crawl_dataset(repo_id)
            log.info("done %s in %.1fs: %s", repo_id, time.time() - t0, stats)
        except Exception as e:  # noqa: BLE001
            log.exception("failed crawling %s: %s", repo_id, e)


def discover_for_query(query: str, max_new_datasets: int = 4, rows_per_dataset: int = 60,
                        deadline_seconds: float = 60.0) -> dict:
    """The search-side equivalent of recommend_worker's densify: when a text
    search comes up thin, look for datasets IN HF'S WHOLE CATALOG (not just
    our ~6-dataset demo corpus) that match the query text, and sample a few
    rows from the most promising ones we haven't indexed yet.
    """
    # CONFIRMED LIVE: the Hub search API treats a multi-word `search=` value
    # as an AND-ish phrase match and returns EMPTY for realistic multi-keyword
    # queries ("motorcycle motocross bike" -> []), even though every one of
    # those words individually returns good, relevant datasets ("motorcycle"
    # -> 4 real hits, "bike" -> 8 real hits). Try the combined query first
    # (cheap, occasionally works for a genuine short phrase), then fall back
    # to searching each word separately and merging/deduping — exactly the
    # same fan-out-and-merge pattern search_datasets() already uses per-modality.
    try:
        candidates = search_datasets(query, limit=8)
        if not candidates:
            words = [w for w in query.split() if len(w) > 2][:4]
            merged: dict[str, object] = {}
            for word in words:
                for c in search_datasets(word, limit=6):
                    merged.setdefault(c.repo_id, c)
            candidates = list(merged.values())[:8]
            if candidates:
                log.info("discover_for_query(%r): combined query returned nothing, "
                          "per-word fallback (%s) found %d datasets", query, words, len(candidates))
    except HFError as e:
        log.warning("discover_for_query(%r): hub search failed: %s", query, e)
        return {"query": query, "datasets_found": [], "rows_added": 0}

    already_deep = {
        r["repo_id"] for r in fetch_all_repo_ids_with_assets()
    }
    fresh = [c for c in candidates if c.repo_id not in already_deep][:max_new_datasets]

    if not fresh:
        log.info("discover_for_query(%r): no new datasets to try (all %d candidates already indexed)",
                  query, len(candidates))
        return {"query": query, "datasets_found": [c.repo_id for c in candidates], "rows_added": 0}

    log.info("discover_for_query(%r): trying new datasets %s", query, [c.repo_id for c in fresh])
    rows_added = 0
    per_dataset_deadline = deadline_seconds / max(len(fresh), 1)
    for c in fresh:
        try:
            stats = crawl_dataset(c.repo_id, max_rows=rows_per_dataset, deadline_seconds=per_dataset_deadline)
            rows_added += stats.get("assets_indexed", 0)
            log.info("discover_for_query(%r): sampled %s -> %s", query, c.repo_id, stats)
        except Exception as e:  # noqa: BLE001
            log.warning("discover_for_query(%r): failed on %s: %s", query, c.repo_id, e)

    return {"query": query, "datasets_found": [c.repo_id for c in fresh], "rows_added": rows_added}


def fetch_all_repo_ids_with_assets() -> list[dict]:
    from datacurate_core.db import fetch_all
    return fetch_all("select distinct source_dataset as repo_id from assets")


def run_poll():
    log.info("ingest_worker polling for 'ingest_dataset', 'discover_query' and 'precache_episodes' jobs...")
    while True:
        job = queue.dequeue("ingest_dataset")
        if job:
            payload = job["input"]
            repo_id = payload.get("repo_id")
            log.info("job %s: crawling %s", job["id"], repo_id)
            try:
                stats = crawl_dataset(repo_id, max_rows=payload.get("max_rows"), deadline_seconds=payload.get("deadline_seconds", 90.0))
                queue.complete(job["id"], stats)
            except Exception as e:  # noqa: BLE001
                log.exception("job %s failed", job["id"])
                queue.fail(job["id"], str(e))
            continue

        job = queue.dequeue("precache_episodes")
        if job:
            repos = job["input"].get("repo_ids", [])
            log.info("job %s: pre-caching episode recordings for %s", job["id"], repos)
            try:
                queue.complete(job["id"], precache_episodes(repos, job["input"].get("max_assets", 500)))
            except Exception as e:  # noqa: BLE001
                log.exception("job %s failed", job["id"])
                queue.fail(job["id"], str(e))
            continue

        job = queue.dequeue("discover_query")
        if job:
            query = job["input"].get("query", "")
            log.info("job %s: discovering datasets for query %r", job["id"], query)
            try:
                result = discover_for_query(query)
                queue.complete(job["id"], result)
            except Exception as e:  # noqa: BLE001
                log.exception("job %s failed", job["id"])
                queue.fail(job["id"], str(e))
            continue

        time.sleep(2)


def discover_catalog(min_downloads: int = 1000, limit: int = 200):
    """Tier-0 catalog crawl (cheap, no row sampling) — populates hf_datasets
    broadly so the admin/demand-driven system has something to pick from."""
    tasks = [
        "image-classification", "image-to-text", "text-to-image",
        "automatic-speech-recognition", "audio-classification", "video-classification",
    ]
    summaries = list_datasets(tasks, min_downloads=min_downloads, limit=limit)
    log.info("discovered %d datasets above %d downloads", len(summaries), min_downloads)
    for s in summaries:
        with cursor() as cur:
            cur.execute(
                """insert into hf_datasets (repo_id, author, modalities, tags, license, downloads, likes)
                   values (%s, %s, %s, %s, %s, %s, %s)
                   on conflict (repo_id) do update set downloads=excluded.downloads, likes=excluded.likes""",
                (s.repo_id, s.author, s.modalities or [], s.tags, s.license, s.downloads, s.likes),
            )
    return summaries


# ── Eager episode pre-caching (curated, high-value datasets only) ──────────
# Recordings are otherwise cached lazily on first view, which costs the first
# viewer several seconds (download + faststart remux). For the handful of
# datasets that are actually featured in the UI's top-nav collections, that
# tradeoff is wrong: they get opened constantly, so it's worth paying the
# cost once, up front, in the background.
#
# Deliberately an ALLOWLIST, not "cache everything": the corpus references
# far more video than we'd ever want to host (some source datasets carry
# 300 MB per clip, others 1 GB tar shards). markov-ai/computer-use's
# recordings, by contrast, average ~3 MB \u2014 cheap and high-value.
PRECACHE_MAX_BYTES_PER_JOB = 3 * 1024 * 1024 * 1024   # 3 GiB
PRECACHE_MAX_FILE_BYTES = 60 * 1024 * 1024            # skip anything huge


def precache_episodes(repo_ids: list[str], max_assets: int = 500) -> dict:
    """Download + faststart-remux + store every episode recording for the
    given datasets, so playback is instant rather than paying a cold
    download/remux on first view."""
    from datacurate_core.db import fetch_all as _fetch_all
    from datacurate_core.mediacache import get_or_cache_media

    rows = _fetch_all(
        """select id, source_dataset, metadata->>'_recording_uri' as rec
           from assets
           where source_dataset = any(%s)
             and metadata ? '_recording_uri'
             and not (metadata ? '_recording_cached_uri')
           limit %s""",
        (repo_ids, max_assets),
    )
    stats = {"considered": len(rows), "cached": 0, "skipped_large": 0, "failed": 0, "bytes": 0}

    for r in rows:
        if stats["bytes"] >= PRECACHE_MAX_BYTES_PER_JOB:
            log.warning("precache_episodes: byte budget reached, stopping at %d cached", stats["cached"])
            break
        try:
            head = requests.head(r["rec"], allow_redirects=True, timeout=30)
            size = int(head.headers.get("content-length") or 0)
            if size > PRECACHE_MAX_FILE_BYTES:
                stats["skipped_large"] += 1
                continue
            url, _ = get_or_cache_media(
                {"id": str(r["id"]), "content_uri": r["rec"], "mime_type": "video/mp4", "modality": "video"},
                persist=False,
            )
            # Store alongside (never overwriting) the asset's own cached
            # screenshot; the episode endpoint reads this and can then skip
            # all network work entirely.
            execute(
                "update assets set metadata = jsonb_set(metadata, '{_recording_cached_uri}', %s::jsonb) where id = %s",
                (json.dumps(url), r["id"]),
            )
            stats["cached"] += 1
            stats["bytes"] += size
        except Exception as e:  # noqa: BLE001
            stats["failed"] += 1
            log.warning("precache_episodes: failed for %s: %s", r["id"], e)

    log.info("precache_episodes(%s) -> %s", repo_ids, stats)
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", action="store_true", help="one-shot crawl of the curated seed list")
    parser.add_argument("--poll", action="store_true", help="long-running queue consumer")
    parser.add_argument("--discover", action="store_true", help="Tier-0 catalog discovery only")
    args = parser.parse_args()

    if args.discover:
        discover_catalog()
    elif args.poll:
        run_poll()
    else:
        run_seed()
