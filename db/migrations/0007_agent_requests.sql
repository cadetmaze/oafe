-- Agent-driven dataset requests (the oafe UI's flow).
--
-- A user describes a data need in natural language and picks a target row
-- count. An agent plans, discovers, screens and judges real HF data, shows a
-- SMALL review sample sized by that target (1000 -> 10 items, 5000 -> 15), and
-- only after the user approves/rejects does it build and export the full set.
--
-- Everything the agent does is written here rather than streamed, so a browser
-- refresh mid-run restores the exact phase, timeline and decisions. That is a
-- product requirement, not an implementation detail.

begin;

-- ── The request itself ────────────────────────────────────────────────
-- Originally defined in oafe/db/migrations/0001_create_dataset_requests.sql
-- against a separate database. Recreated here (idempotently) so the platform
-- DB holds the whole flow — the request, the agent's state, and the assets it
-- selects — instead of splitting it across two databases.
create table if not exists dataset_requests (
  id               uuid primary key,
  query            text not null check (length(btrim(query)) > 0),
  filters          jsonb not null default '{}'::jsonb,
  uploads          jsonb not null default '[]'::jsonb,
  request_payload  jsonb not null,
  created_at       timestamptz not null default now()
);
create index if not exists dataset_requests_created_at_idx
  on dataset_requests (created_at desc);

create table if not exists dataset_request_files (
  id               uuid primary key,
  request_id       uuid not null references dataset_requests(id) on delete cascade,
  position         integer not null check (position >= 0),
  file_name        text not null,
  media_type       text not null,
  byte_size        bigint not null check (byte_size >= 0),
  last_modified_ms bigint,
  content          bytea not null,
  created_at       timestamptz not null default now(),
  unique (request_id, position)
);
create index if not exists dataset_request_files_request_id_idx
  on dataset_request_files (request_id);

-- Promoted out of request_payload jsonb because the agent reads them on every
-- poll and they drive real behaviour (example_count sets the review size).
alter table dataset_requests add column if not exists project_id uuid
  references projects(id) on delete set null;
alter table dataset_requests add column if not exists example_count int not null default 100;
alter table dataset_requests add column if not exists seed jsonb not null default '{}'::jsonb;
alter table dataset_requests add column if not exists origin_context jsonb not null default '{}'::jsonb;

-- ── Guest ownership ───────────────────────────────────────────────────
-- The new UI has no sign-in screen, but moodboards/datasets still need a real
-- owner so they persist server-side. A device-scoped guest id gets its own
-- project, mirroring the one-project-per-user rule from migration 0004.
alter table projects add column if not exists guest_id text;
create unique index if not exists projects_one_per_guest_idx
  on projects (guest_id) where guest_id is not null;

-- ── Per-repo column mapping ───────────────────────────────────────────
-- How this dataset's columns map onto the unified export schema. Resolved
-- once per repo (deterministic detectors first, LLM only for genuine
-- ambiguity) and reused by every later build.
alter table hf_datasets add column if not exists column_mapping jsonb;

-- ── The agent run ─────────────────────────────────────────────────────
-- One run per request. `phase` is the durable state machine the UI renders;
-- `status` is the coarse lifecycle the poller branches on.
create table if not exists agent_runs (
  id             uuid primary key default gen_random_uuid(),
  request_id     uuid not null references dataset_requests(id) on delete cascade,
  job_id         uuid references jobs(id) on delete set null,
  build_job_id   uuid references jobs(id) on delete set null,
  export_job_id  uuid references jobs(id) on delete set null,
  dataset_id     uuid references datasets(id) on delete set null,
  version_id     uuid references dataset_versions(id) on delete set null,
  -- QUEUED|PLANNING|DISCOVERING|SCREENING|SAMPLING|JUDGING|REVIEW_READY
  -- |AWAITING_REVIEW|REFINING|BUILDING|EXPORTING|DONE
  phase          text not null default 'QUEUED',
  -- RUNNING|AWAITING_REVIEW|COMPLETED|FAILED
  status         text not null default 'RUNNING',
  progress       float not null default 0,
  spec           jsonb not null default '{}'::jsonb,
  review_size    int not null default 10,
  review_batch   int not null default 0,
  -- Acceptance cut calibrated from the user's approvals (one scalar is all
  -- ~10 binary labels can honestly support).
  threshold      float,
  stats          jsonb not null default '{}'::jsonb,
  error          text,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
create unique index if not exists agent_runs_request_idx on agent_runs (request_id);

-- ── The timeline the chat renders ─────────────────────────────────────
-- Append-only, monotonic `seq` per run so the UI can poll with ?since=N and
-- never miss or duplicate an entry. `kind` maps onto the three marker icons
-- the workspace chat already has.
create table if not exists agent_events (
  id         bigserial primary key,
  run_id     uuid not null references agent_runs(id) on delete cascade,
  seq        int not null,
  kind       text not null,           -- explored|fetching|labeling|message|error
  role       text,                    -- for kind='message': user|assistant
  text       text not null default '',
  data       jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (run_id, seq)
);
create index if not exists agent_events_run_seq_idx on agent_events (run_id, seq);

-- ── Which datasets the agent picked, and why ──────────────────────────
-- Also the home of the one signal ~10 approve/reject labels genuinely
-- support: a per-source multiplier. Each reviewed sample proxies hundreds of
-- rows sharing that repo's caption style, so demoting a whole repo is far
-- better founded than trying to learn per-item weights.
create table if not exists agent_sources (
  run_id            uuid not null references agent_runs(id) on delete cascade,
  repo_id           text not null,
  discovered_via    text,             -- local_fts|catalog|hub_search|web_search
  relevance         text,             -- strong|partial|none
  expected_hit_rate float,            -- LLM guess: rank ordering ONLY, never a probability
  measured_hit_rate float,            -- the real judged rate after a probe crawl
  text_signal       text,             -- caption|labels|repo_only|none
  reason            text,
  source_multiplier float not null default 1.0,
  rows_indexed      int not null default 0,
  rows_kept         int not null default 0,
  created_at        timestamptz not null default now(),
  primary key (run_id, repo_id)
);

-- ── The review sample and the user's verdicts ─────────────────────────
-- Persisted per decision (not at the end) so a mid-review refresh keeps every
-- approval already made.
create table if not exists agent_candidates (
  run_id     uuid not null references agent_runs(id) on delete cascade,
  asset_id   uuid not null references assets(id) on delete cascade,
  batch      int not null default 0,
  rank       int not null default 0,
  score      float not null default 0,
  judge      jsonb not null default '{}'::jsonb,   -- verdict/confidence/missing/evidence
  decision   text not null default 'pending'
             check (decision in ('pending', 'approved', 'rejected')),
  decided_at timestamptz,
  primary key (run_id, asset_id)
);
create index if not exists agent_candidates_run_batch_idx
  on agent_candidates (run_id, batch, rank);

commit;
