"""Hugging Face integration.

Two different APIs for two different jobs (verified against the live APIs):
  - Hub REST API (`huggingface.co/api/datasets`)      -> catalog / discovery
  - datasets-server (`datasets-server.huggingface.co`) -> structure + row sampling

Key cost insight baked in here: `/rows` returns media as a *hosted, signed,
expiring* URL (`Expires=...&Signature=...`). We never store that URL long
term — we fetch it once and cache the bytes ourselves (see ingest_worker).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterator

import requests

from .config import settings
from .hf_cache import TTL_CATALOG, TTL_SCHEMA, cached_call, make_key

HUB_API = "https://huggingface.co/api"
DATASETS_SERVER = "https://datasets-server.huggingface.co"


class HFError(Exception):
    pass


class HFNotSupported(HFError):
    """Dataset viewer doesn't support this dataset (gated/private/script-based)."""


def _headers() -> dict[str, str]:
    h = {"User-Agent": "dataset-curation-platform/0.1"}
    if settings.hf_token:
        h["Authorization"] = f"Bearer {settings.hf_token}"
    return h


def _get(url: str, params: dict | None = None, retries: int = 3) -> dict[str, Any]:
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=_headers(), timeout=settings.http_timeout_seconds)
            if resp.status_code == 429:
                last_err = HFError(f"429 rate-limited by {url.split('/')[2]} (set HF_TOKEN to raise limits)")
                wait = int(resp.headers.get("Retry-After", 2 ** attempt))
                time.sleep(wait)
                continue
            data = resp.json()
            if isinstance(data, dict) and "error" in data:
                raise HFNotSupported(data["error"])
            return data
        except HFNotSupported:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(min(2 ** attempt, 8))
    raise HFError(f"GET {url} failed after {retries} attempts: {last_err}")


@dataclass
class HFDatasetSummary:
    repo_id: str
    author: str | None
    downloads: int
    likes: int
    tags: list[str]
    license: str | None
    modalities: list[str]


def _summary_to_dict(s: HFDatasetSummary) -> dict:
    return {"repo_id": s.repo_id, "author": s.author, "downloads": s.downloads, "likes": s.likes,
            "tags": s.tags, "license": s.license, "modalities": s.modalities}


def _dict_to_summary(d: dict) -> HFDatasetSummary:
    return HFDatasetSummary(**d)


def list_datasets(task_categories: list[str], min_downloads: int = 0, limit: int = 100) -> list[HFDatasetSummary]:
    """Catalog discovery via the Hub REST API. Cached (TTL_CATALOG): this is
    called repeatedly with heavily-overlapping task-category sets by both
    `discover_catalog()` runs and background re-crawls — popularity/download
    counts don't meaningfully change minute to minute."""
    cache_key = make_key("list_datasets", sorted(task_categories), min_downloads, limit)
    cached = cached_call(cache_key, TTL_CATALOG, lambda: [_summary_to_dict(s) for s in _list_datasets_uncached(task_categories, min_downloads, limit)])
    return [_dict_to_summary(d) for d in cached]


def _list_datasets_uncached(task_categories: list[str], min_downloads: int, limit: int) -> list[HFDatasetSummary]:
    out: list[HFDatasetSummary] = []
    for task in task_categories:
        try:
            resp = requests.get(
                f"{HUB_API}/datasets",
                params={"filter": f"task_categories:{task}", "sort": "downloads", "direction": -1, "limit": limit},
                headers=_headers(),
                timeout=settings.http_timeout_seconds,
            )
            resp.raise_for_status()
            for d in resp.json():
                if d.get("downloads", 0) < min_downloads:
                    continue
                tags = d.get("tags", [])
                modalities = [t.split(":", 1)[1] for t in tags if t.startswith("modality:")]
                license_tags = [t.split(":", 1)[1] for t in tags if t.startswith("license:")]
                out.append(HFDatasetSummary(
                    repo_id=d["id"],
                    author=d.get("author"),
                    downloads=d.get("downloads", 0),
                    likes=d.get("likes", 0),
                    tags=tags,
                    license=license_tags[0] if license_tags else None,
                    modalities=modalities,
                ))
        except Exception:  # noqa: BLE001
            continue
    # de-dupe by repo_id, keep highest downloads
    best: dict[str, HFDatasetSummary] = {}
    for d in out:
        if d.repo_id not in best or d.downloads > best[d.repo_id].downloads:
            best[d.repo_id] = d
    return sorted(best.values(), key=lambda d: -d.downloads)


def search_datasets(query: str, limit: int = 10, modalities: list[str] | None = None) -> list[HFDatasetSummary]:
    """Free-text catalog discovery — used when a local search/recommendation
    comes up thin, to find NEW datasets (not yet indexed at all) that might
    satisfy the query. Verified live against the real Hub API.

    IMPORTANT (confirmed live): combining `search=` with a SINGLE
    `filter=modality:X` genuinely narrows to relevant media datasets (e.g.
    search=fort + filter=modality:image -> fortnite/fortress image sets,
    instead of unrelated text corpora that happen to contain "fort").
    Passing MULTIPLE modality filters at once is ANDed by the Hub API (returns
    nothing — no dataset is simultaneously image+video+audio), so we issue one
    request per modality and merge, same de-dup pattern as list_datasets().

    Cached (TTL_CATALOG): the same query string is genuinely re-issued a lot
    — both by repeated user searches for a popular term and by recommend's
    Tier-3 per-keyword fallback trying several single-word queries where some
    overlap across boards/sessions.
    """
    modalities = modalities or ["image", "video", "audio"]
    cache_key = make_key("search_datasets", query, sorted(modalities), limit)
    cached = cached_call(cache_key, TTL_CATALOG, lambda: [_summary_to_dict(s) for s in _search_datasets_uncached(query, limit, modalities)])
    return [_dict_to_summary(d) for d in cached]


def _search_datasets_uncached(query: str, limit: int, modalities: list[str]) -> list[HFDatasetSummary]:
    best: dict[str, HFDatasetSummary] = {}
    for modality in modalities:
        try:
            resp = requests.get(
                f"{HUB_API}/datasets",
                params={"search": query, "filter": f"modality:{modality}",
                        "sort": "downloads", "direction": -1, "limit": limit},
                headers=_headers(), timeout=settings.http_timeout_seconds,
            )
            resp.raise_for_status()
            for d in resp.json():
                tags = d.get("tags", [])
                mods = [t.split(":", 1)[1] for t in tags if t.startswith("modality:")]
                license_tags = [t.split(":", 1)[1] for t in tags if t.startswith("license:")]
                summary = HFDatasetSummary(
                    repo_id=d["id"], author=d.get("author"), downloads=d.get("downloads", 0),
                    likes=d.get("likes", 0), tags=tags,
                    license=license_tags[0] if license_tags else None, modalities=mods,
                )
                if summary.repo_id not in best or summary.downloads > best[summary.repo_id].downloads:
                    best[summary.repo_id] = summary
        except Exception:  # noqa: BLE001
            continue
    return sorted(best.values(), key=lambda d: -d.downloads)[:limit]


def get_dataset_tags(repo_id: str) -> list[str]:
    """Single-repo lookup of the Hub's own tags for this dataset (cached,
    TTL_CATALOG). These carry real semantic signal our own text-extraction
    can never see — e.g. a dataset author tagging their own upload
    "egocentric"/"robotics" is a much stronger, cleaner signal than us
    guessing from a caption. Used to enrich every asset's tags[] at ingest
    time (see ingest_worker._sample_split), on top of the always-on
    repo-id-derived tags.
    """
    cache_key = make_key("get_dataset_tags", repo_id)

    def _fetch() -> list[str]:
        try:
            resp = requests.get(f"{HUB_API}/datasets/{repo_id}", headers=_headers(),
                                 timeout=settings.http_timeout_seconds)
            resp.raise_for_status()
            return resp.json().get("tags", [])
        except Exception:  # noqa: BLE001
            return []

    return cached_call(cache_key, TTL_CATALOG, _fetch)


def get_splits(repo_id: str) -> list[dict[str, str]]:
    """Cached (TTL_SCHEMA, 6h): a dataset's split list never changes on the
    timescale of a single browsing/curation session, and this is called
    repeatedly — once per crawl_dataset(), once per densify retry, once per
    discover_for_query() candidate check."""
    cache_key = make_key("get_splits", repo_id)
    return cached_call(cache_key, TTL_SCHEMA, lambda: _get(f"{DATASETS_SERVER}/splits", {"dataset": repo_id}).get("splits", []))


def get_info(repo_id: str, config: str) -> dict[str, Any]:
    """Cached (TTL_SCHEMA, 6h) — same rationale as get_splits(): a dataset's
    feature schema is effectively immutable once published."""
    cache_key = make_key("get_info", repo_id, config)

    def _fetch():
        data = _get(f"{DATASETS_SERVER}/info", {"dataset": repo_id, "config": config})
        info = data.get("dataset_info", {})
        # /info without &config= sometimes returns {config: {...}} instead of the config dict directly
        return info[config] if config in info else info

    return cached_call(cache_key, TTL_SCHEMA, _fetch)


def get_rows(repo_id: str, config: str, split: str, offset: int, length: int = 100) -> dict[str, Any]:
    return _get(
        f"{DATASETS_SERVER}/rows",
        {"dataset": repo_id, "config": config, "split": split, "offset": offset, "length": length},
    )


def iter_rows(repo_id: str, config: str, split: str, max_rows: int, page_size: int = 100) -> Iterator[dict[str, Any]]:
    """Yield row dicts, paging through /rows, stopping at max_rows or when exhausted/failing."""
    offset = 0
    fetched = 0
    while fetched < max_rows:
        length = min(page_size, max_rows - fetched)
        try:
            data = get_rows(repo_id, config, split, offset, length)
        except HFError as e:
            import logging
            logging.getLogger(__name__).warning("iter_rows stopping early for %s/%s/%s at offset %d: %s",
                                                  repo_id, config, split, offset, e)
            return  # graceful stop — dataset marked rows_failed by caller
        rows = data.get("rows", [])
        if not rows:
            return
        for r in rows:
            yield r
            fetched += 1
        total = data.get("num_rows_total")
        offset += len(rows)
        if total is not None and offset >= total:
            return


def resolve_media_url(repo_id: str, revision: str | None, value: str) -> str:
    """Turn a plain string column value into a fetchable URL.
    - Already absolute (http...) -> return as-is.
    - Relative path -> resolve against the dataset repo's file resolver.
    """
    if value.startswith("http://") or value.startswith("https://"):
        return value
    rev = revision or "main"
    path = value.lstrip("/")
    return f"https://huggingface.co/datasets/{repo_id}/resolve/{rev}/{path}"


def resolve_media_value(repo_id: str, media_type: str | None, media_val: Any) -> str | None:
    """Shared "what's the actual fetchable URL for this row's media column
    value" logic — used both at first ingest (ingest_worker._sample_split)
    and when re-resolving a fresh URL later because the one we stored has
    expired (mediacache.refresh_content_uri). ONE implementation so the two
    call sites can never quietly drift apart.

    Handles the same real-world quirks confirmed live during ingestion:
    native Image/Audio/Video columns are usually a `{src, width, height}`
    dict, but some datasets hand back a LIST of variants instead of one dict.
    """
    if media_val is None:
        return None
    if media_type in ("native_image", "native_audio", "native_video"):
        if isinstance(media_val, list):
            media_val = media_val[0] if media_val else {}
        if isinstance(media_val, dict):
            return media_val.get("src")
        return None
    # url_string / path_string
    return resolve_media_url(repo_id, None, str(media_val))
