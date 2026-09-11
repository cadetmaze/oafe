-- Migration 0003: real caching everywhere we fetch from Hugging Face.
--
-- Two distinct caches this adds:
--
-- 1. hf_api_cache — a generic TTL cache for HF *metadata* API responses
--    (get_splits, get_info, search_datasets, list_datasets). Before this,
--    every worker process re-hit the same HF endpoints for the same
--    dataset/config on every crawl/discover/densify call, even seconds
--    apart. Confirmed live: recommend_worker's Tier-2/Tier-3 densify calls
--    get_info() on the same repo+config that was JUST profiled moments
--    earlier by the Tier-1 search path.
--
-- 2. assets.cached_content_uri — the actual full-resolution media bytes.
--    Before this, only the EXPORT worker's materialize step ever copied
--    HF's bytes into our own Blob storage; every other consumer (the
--    asset detail modal, in particular) played media directly from HF's
--    signed, EXPIRING datasets-server URL (`?Expires=...&Signature=...`),
--    which (a) never touches our cache at all and (b) goes dead once the
--    signature expires. This column is populated lazily, on first real
--    view, by the same "fetch once, cache forever" helper the export
--    worker already used — now shared, not duplicated.

create table if not exists hf_api_cache (
    cache_key   text primary key,
    value       jsonb not null,
    created_at  timestamptz not null default now(),
    expires_at  timestamptz not null
);
create index if not exists idx_hf_api_cache_expires on hf_api_cache (expires_at);

alter table assets add column if not exists cached_content_uri text;
alter table assets add column if not exists cached_at timestamptz;
