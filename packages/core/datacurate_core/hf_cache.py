"""Generic TTL cache for Hugging Face *metadata* API responses, backed by
Postgres (`hf_api_cache` table) so it's shared across every worker process
and survives container restarts — a plain in-memory `lru_cache` would not,
since ingest/recommend/export each run in their own container.

Confirmed live (need for this): recommend_worker's Tier-2 densify calls
`get_info(repo, config)` on a dataset that the Tier-1 search path had
*already* profiled moments earlier in the same request; `discover_catalog()`
and `discover_for_query()` both hit `list_datasets`/`search_datasets` with
heavily-overlapping task/modality filters across repeated runs. None of
that data changes on a per-second basis, so re-fetching it from HF every
single time is pure waste (extra latency + counts against HF's rate limit).

Usage:
    data = cached_call("get_info:repo/id:config", ttl_seconds=6*3600, fetch_fn=lambda: _actually_fetch())
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .db import fetch_one, cursor

log = logging.getLogger("hf_cache")

# Dataset *schemas* (get_splits/get_info) essentially never change once
# published — long TTL. Catalog *search/listing* (search_datasets/
# list_datasets) reflects popularity/download counts that drift slowly and
# new datasets that appear over time — shorter TTL so the platform still
# discovers genuinely new things without re-hitting HF every call.
TTL_SCHEMA = 6 * 3600
TTL_CATALOG = 3600


def make_key(*parts: Any) -> str:
    raw = "|".join(str(p) for p in parts)
    # Hash to keep the primary key short & safe regardless of query length
    # (search_datasets keys can otherwise embed arbitrary user text).
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cached_call(cache_key: str, ttl_seconds: int, fetch_fn: Callable[[], Any]) -> Any:
    """Return the cached value if fresh; otherwise call fetch_fn(), store, return."""
    row = fetch_one(
        "select value, expires_at from hf_api_cache where cache_key = %s", (cache_key,)
    )
    now = datetime.now(timezone.utc)
    if row and row["expires_at"] > now:
        return row["value"]

    value = fetch_fn()

    expires_at = now + timedelta(seconds=ttl_seconds)
    try:
        with cursor() as cur:
            cur.execute(
                """insert into hf_api_cache (cache_key, value, created_at, expires_at)
                   values (%s, %s, %s, %s)
                   on conflict (cache_key) do update
                     set value = excluded.value, created_at = excluded.created_at, expires_at = excluded.expires_at""",
                (cache_key, json.dumps(value), now, expires_at),
            )
    except Exception:  # noqa: BLE001
        # Caching is a pure optimization — never let a write-race or a
        # transient DB hiccup break the actual HF call that already succeeded.
        log.warning("hf_api_cache write failed for key=%s (non-fatal)", cache_key, exc_info=True)
    return value
