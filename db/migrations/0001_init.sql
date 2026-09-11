-- ============================================================================
-- Dataset Curation Platform — Initial Schema
-- Postgres (Supabase-compatible). No pgvector / embeddings in V1 (text-first).
-- ============================================================================

create extension if not exists pgcrypto;   -- gen_random_uuid()
create extension if not exists pg_trgm;    -- fuzzy text matching (typo tolerance)

-- ── Catalog (Tier 0: what datasets exist) ──────────────────────────────────
create table if not exists hf_datasets (
  id                uuid primary key default gen_random_uuid(),
  repo_id           text unique not null,          -- e.g. "uoft-cs/cifar10"
  author            text,
  modalities        text[] not null default '{}',  -- ['image'] | ['audio'] | ['video']
  tags              text[] not null default '{}',
  license           text,
  downloads         int default 0,
  likes             int default 0,
  size_bytes        bigint,
  configs           jsonb not null default '{}',   -- {config: {splits:[...], features:{...}}}
  media_column      text,                          -- detected column holding media
  media_column_type text,                          -- 'native_image' | 'native_audio' | 'native_video' | 'url_string' | 'path_string' | null
  caption_columns   text[] not null default '{}',  -- detected text/caption columns
  viewer_supported  bool default true,
  index_tier        int not null default 0,        -- 0=metadata,1=sampled,2=on-demand,3=hot
  demand_score      int not null default 0,
  rows_failed       bool default false,             -- /rows API failed for this dataset
  last_synced_at    timestamptz,
  created_at        timestamptz not null default now()
);
create index if not exists hf_datasets_modalities_idx on hf_datasets using gin(modalities);
create index if not exists hf_datasets_tier_idx on hf_datasets(index_tier);

-- ── Assets (Tier 1+: actual curatable items) ───────────────────────────────
create table if not exists assets (
  id                 uuid primary key default gen_random_uuid(),
  modality           text not null check (modality in ('image','video','audio')),

  -- Provenance (source of truth traceability)
  source_provider    text not null default 'huggingface',
  source_dataset     text not null,
  source_config      text not null default 'default',
  source_split       text not null default 'train',
  source_revision    text,
  source_row         bigint not null,

  -- Content
  content_uri        text,                 -- original (HF-hosted) URL, may expire
  thumbnail_uri       text,                -- our CDN/blob url, 512px webp — grid display
  preview_uri         text,                -- larger preview / animated (video) / waveform (audio)

  -- Dimensions
  width              int,
  height             int,
  duration           float,                -- seconds, video/audio
  fps                float,                -- video
  sample_rate        int,                  -- audio

  -- Text (this is our retrieval signal — no embeddings in V1)
  caption            text,
  labels             text[] not null default '{}',
  tags               text[] not null default '{}',
  alt_text           text,
  description        text,

  search_vector      tsvector,             -- maintained by trigger below

  -- Metadata / quality / licensing
  metadata           jsonb not null default '{}',
  mime_type          text,
  license            text,
  phash              bigint,               -- perceptual hash (image/video keyframe)
  color_dominant     text,                 -- hex
  aspect_ratio       float,
  file_size          bigint,
  quality_score      float,
  is_nsfw            bool not null default false,

  created_at         timestamptz not null default now(),

  unique (source_dataset, source_config, source_split, source_row)
);

create index if not exists assets_fts_idx on assets using gin(search_vector);
create index if not exists assets_modality_idx on assets(modality);
create index if not exists assets_license_idx on assets(license);
create index if not exists assets_source_dataset_idx on assets(source_dataset);
create index if not exists assets_phash_idx on assets(phash);
create index if not exists assets_caption_trgm_idx on assets using gin (caption gin_trgm_ops);

-- Auto-maintained full text search vector: caption weighted highest, then labels/tags/alt
create or replace function assets_search_trigger() returns trigger as $$
begin
  new.search_vector :=
    setweight(to_tsvector('english', coalesce(new.caption, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(array_to_string(new.labels, ' '), '')), 'B') ||
    setweight(to_tsvector('english', coalesce(array_to_string(new.tags, ' '), '')), 'C') ||
    setweight(to_tsvector('english', coalesce(new.alt_text, '') || ' ' || coalesce(new.description, '')), 'D');
  return new;
end
$$ language plpgsql;

drop trigger if exists assets_search_update on assets;
create trigger assets_search_update
  before insert or update on assets
  for each row execute function assets_search_trigger();

-- ── Curation ────────────────────────────────────────────────────────────
create table if not exists projects (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid,                      -- nullable in local/no-auth dev mode
  name        text not null,
  created_at  timestamptz not null default now()
);

create table if not exists moodboards (
  id                  uuid primary key default gen_random_uuid(),
  project_id          uuid references projects(id) on delete cascade,
  name                text not null default 'Untitled board',
  extracted_keywords  text[] not null default '{}',   -- cached from last extraction
  created_at          timestamptz not null default now()
);

create table if not exists moodboard_assets (
  moodboard_id  uuid references moodboards(id) on delete cascade,
  asset_id      uuid references assets(id) on delete cascade,
  action        text not null default 'add',    -- 'add' | 'like' (weights recommendation intent)
  position      int,
  added_at      timestamptz not null default now(),
  primary key (moodboard_id, asset_id)
);

create table if not exists asset_events (
  id          bigserial primary key,
  user_id     uuid,
  asset_id    uuid references assets(id) on delete cascade,
  event       text not null,   -- impression | click | like | save | remove | export
  context     jsonb not null default '{}',
  created_at  timestamptz not null default now()
);
create index if not exists asset_events_asset_idx on asset_events(asset_id);
create index if not exists asset_events_created_idx on asset_events(created_at);

-- ── Datasets (the curated output) ──────────────────────────────────────
create table if not exists datasets (
  id            uuid primary key default gen_random_uuid(),
  project_id    uuid references projects(id) on delete cascade,
  name          text not null,
  description   text,
  created_at    timestamptz not null default now()
);

create table if not exists dataset_versions (
  id            uuid primary key default gen_random_uuid(),
  dataset_id    uuid references datasets(id) on delete cascade,
  version       int not null,
  schema        jsonb not null default '{}',   -- inferred FieldType schema
  stats         jsonb not null default '{}',   -- license histogram, source %, counts
  status        text not null default 'draft', -- draft | ready | exported
  created_at    timestamptz not null default now(),
  unique (dataset_id, version)
);

create table if not exists dataset_records (
  version_id      uuid references dataset_versions(id) on delete cascade,
  asset_id        uuid references assets(id) on delete cascade,
  position        int,
  annotations     jsonb not null default '{}',
  selection_meta  jsonb not null default '{}',   -- {picked_by, strategy, score, keywords}
  primary key (version_id, asset_id)
);

-- ── Jobs / Queue ────────────────────────────────────────────────────────
-- Local stand-in for Supabase's `pgmq` extension: a plain Postgres queue using
-- SELECT ... FOR UPDATE SKIP LOCKED. Same semantics (visibility timeout via
-- locked_until, at-least-once delivery, idempotent consumers). Swappable for
-- pgmq/Azure Service Bus later without changing worker logic beyond queue.py.
create table if not exists jobs (
  id             uuid primary key default gen_random_uuid(),
  type           text not null,             -- 'ingest_dataset' | 'recommend' | 'export'
  status         text not null default 'QUEUED', -- QUEUED|RUNNING|PARTIAL|COMPLETED|FAILED|CANCELLED
  progress       float not null default 0,
  input          jsonb not null default '{}',
  output         jsonb,
  error          text,
  locked_by      text,
  locked_until   timestamptz,
  attempts       int not null default 0,
  created_by     uuid,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
create index if not exists jobs_type_status_idx on jobs(type, status);

create table if not exists recommendation_results (
  job_id    uuid references jobs(id) on delete cascade,
  asset_id  uuid references assets(id) on delete cascade,
  rank      int not null,
  score     float not null,
  batch     int not null,
  reason    jsonb not null default '{}',   -- which keywords matched, dedup notes
  primary key (job_id, asset_id)
);
create index if not exists recommendation_results_job_idx on recommendation_results(job_id, batch);
