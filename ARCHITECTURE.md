# How this actually works, end to end

This document is the technical companion to `README.md` (which is about *what
was tested and what broke*) and `BLUEPRINT.md` (which is the original design
doc). This one is about **mechanics**: every moving part, every network call,
every table, in the order things actually happen at runtime. If you're a
backend engineer trying to understand this codebase in one sitting, read this.

---

## 1. The one-sentence version

A user builds a **moodboard** by searching/browsing Hugging Face's public
datasets (images/video/audio, pulled in via the datasets-server API and
cached locally), the platform **expands** that moodboard into a larger
curated set using **Postgres full-text search over captions/labels** (no
embeddings, no vector DB, no GPU) plus perceptual-hash dedup and
source-diversity balancing, and then **exports** the result as a
provenance-complete Parquet dataset (+ manifest + README + optional zip +
optional push back to Hugging Face).

---

## 2. Physical architecture (what's actually running)

```
┌──────────────┐     ┌──────────────────────────────────────────────┐
│   Browser    │────▶│  Next.js frontend (apps/web)  :3001           │
│ (the user)   │◀────│  page.tsx / AssetCard / ExpandPanel /         │
└──────────────┘     │  DatasetPanel / AssetDetailModal              │
                      └───────────────────┬────────────────────────┘
                                           │ fetch() — JSON over HTTP
                                           ▼
                      ┌────────────────────────────────────────────┐
                      │  FastAPI gateway (apps/api)  :8000           │
                      │  routers: search / assets / moodboards /     │
                      │           jobs / datasets / health           │
                      │  THIN by design: validate → read/write       │
                      │  Postgres directly for fast paths, or         │
                      │  enqueue a job for anything slow/async        │
                      └───────┬───────────────────────┬──────────────┘
                              │                        │
                    reads/writes                  enqueue('type', payload)
                              │                        │
                              ▼                        ▼
                 ┌─────────────────────┐   ┌─────────────────────────┐
                 │  Postgres  :5432     │◀─▶│  jobs table (the queue)  │
                 │  (dc_postgres)       │   │  SKIP LOCKED, at-least-  │
                 │  = stand-in for      │   │  once, visibility timeout│
                 │  Supabase in prod    │   └───────────┬─────────────┘
                 └─────────────────────┘               │ polled by
                              ▲                          ▼
                              │                ┌──────────────────────────┐
                              │                │  3 worker processes        │
                              │                │  (apps/workers), each its  │
                              │                │  own container:            │
                              │                │  - ingest_worker            │
                              │                │  - recommend_worker         │
                              │                │  - export_worker            │
                              │                └───────────┬────────────────┘
                              │                             │
                              │ writes assets/hf_datasets    │ HTTP
                              │                             ▼
                              │              ┌───────────────────────────────┐
                              └──────────────│  Hugging Face (the real internet)│
                                             │  huggingface.co/api/datasets     │
                                             │  datasets-server.huggingface.co  │
                                             └───────────────────────────────┘

                 ┌─────────────────────┐
                 │  Azurite  :10000      │  = stand-in for Azure Blob in prod
                 │  (dc_azurite)         │  containers: thumbnails / previews /
                 │  public containers    │  audio-waveforms / cached-assets
                 │  served straight to   │  (all public-read) + exports (private,
                 │  the browser           │  SAS-token URLs only)
                 └─────────────────────┘
```

**Local dev vs production — literally the same code:**

| Concern | Local dev | Production |
|---|---|---|
| Database | Postgres in Docker (`dc_postgres`) | Supabase Postgres |
| Job queue | `jobs` table + `SKIP LOCKED` (`packages/core/datacurate_core/queue.py`) | Same table/SQL — or swap for real `pgmq`/Service Bus by changing only `queue.py` |
| Object storage | Azurite (`dc_azurite`) | Azure Blob + CDN |
| Config | `.env` → `config.py` `Settings` dataclass | Same `Settings` shape, different env var values |

Nothing in `apps/api`, `apps/workers`, or `packages/core` knows or cares
which environment it's in — only `config.py` and `blob.py`'s connection
strings change.

---

## 3. The data model (Postgres schema)

Six tables matter (see `db/migrations/0001_init.sql`, `0002_dataset_kind_and_dedup.sql`,
`0003_caching.sql`):

### `hf_datasets` — Tier-0 catalog: "what datasets exist"
One row per Hugging Face dataset repo we know about, *not* per asset.
Populated cheaply (metadata only, no row sampling) by `discover_catalog()`.
Columns worth knowing: `repo_id`, `modalities` (`{image,video,audio}`),
`downloads`/`likes` (popularity, used to prioritize), `media_column` +
`media_column_type` (what we detected as the actual image/video/audio
column), `dataset_kind` (`captioned` / `labeled` / `detection_segmentation`
/ `other` — see §6), `viewer_supported`/`rows_failed` (does the
datasets-server `/rows` API actually work for this repo).

### `assets` — Tier-1+: the actual curatable items
One row **per real HF row** we've sampled and indexed. This is the heart of
the whole system. Key columns:
- **Provenance** (unique key): `source_dataset`, `source_config`,
  `source_split`, `source_row` — literally "this exact row, in this exact
  split, of this exact HF dataset". `unique(source_dataset, source_config,
  source_split, source_row)` prevents duplicate ingestion.
- **Content URLs**: `content_uri` (the original HF-hosted URL — for images
  fetched via `/rows`, this is a *signed, expiring* URL), `thumbnail_uri`
  (our own 512px WebP, generated at ingest, lives in Azurite/Blob forever),
  `preview_uri` (bigger preview / animated video hover-preview / audio
  waveform image), `cached_content_uri` (our own durable full-resolution
  copy — populated lazily, see §8).
- **Text** (the retrieval signal): `caption`, `labels[]`, `tags[]`,
  `alt_text`, `description`, and `search_vector` — a `tsvector` column
  **auto-maintained by a Postgres trigger** (`assets_search_trigger()`) that
  combines all of the above with weights (caption=A highest, labels=B,
  tags=C, alt/description=D). GIN-indexed for fast full-text search. This
  is the entire "no embeddings" retrieval mechanism — see §7.
- **Structured metadata**: `metadata` jsonb — literally every other column
  the source HF row had (minus the media blob itself), so nothing is lost
  even if our caption/label extraction guesses wrong.
- **Dedup/quality**: `phash` (perceptual hash, 64-bit, dHash algorithm),
  `dataset_kind` (denormalized from `hf_datasets` for fast filtering),
  `width`/`height`/`duration`/`fps`/`aspect_ratio`, `quality_score`,
  `is_nsfw`.

### `moodboards` / `moodboard_assets` — the curation workspace
A moodboard is just a named bag of asset IDs (`moodboard_assets` is the
join table, `action` = `add`/`like`/etc). `moodboards.extracted_keywords`
caches the last keyword extraction run against this board (so the frontend
can show "searching for: X, Y, Z" without re-running extraction).

### `jobs` — the queue AND the audit log
Every asynchronous operation (recommend, export, ingest, discover) is a row
here: `type`, `status` (`QUEUED`→`RUNNING`→`COMPLETED`/`FAILED`/`PARTIAL`),
`input` (jsonb payload), `output` (jsonb result), `progress` (0.0-1.0),
`locked_by`/`locked_until` (visibility timeout for `SKIP LOCKED`),
`attempts`. The frontend polls `GET /jobs/{id}` for status.

### `recommendation_results` — one recommend job's output, incrementally
`(job_id, asset_id, rank, score, batch, reason)`. Written in *batches* as
the recommend worker produces them (`_emit_results()`), so the frontend can
show results progressively instead of waiting for the whole job.

### `dataset_versions` / `dataset_records` — frozen export snapshots
When a user "builds a version" from a moodboard or a recommend job, we
snapshot the exact asset list into `dataset_records` (position + a copy of
`selection_meta`) under a new `dataset_versions` row. Versions are
immutable — exporting v1 again always gives the same rows, even if the
moodboard changes later.

### `hf_api_cache` — generic TTL cache for HF metadata calls (new, §9)
`(cache_key, value jsonb, created_at, expires_at)`. Not asset data — this
caches responses from `get_splits`/`get_info`/`search_datasets`/
`list_datasets`.

---

## 4. Walkthrough: what happens when you type in the search box

1. **Frontend** (`page.tsx`) debounces keystrokes 350ms, then calls
   `POST /search {query, filters, limit, offset}`.
2. **`apps/api/routers/search.py`** builds a `tsquery` from the raw query
   text (simple tokenize + `to_tsquery('english', ...)`), and runs:
   ```sql
   with ranked as (
     select *, ts_rank_cd(search_vector, to_tsquery(...)) as rank_val,
            row_number() over (partition by source_dataset order by rank_val desc) as rn
     from assets where search_vector @@ to_tsquery(...) and <filters>
   )
   select * from ranked order by rn asc, rank_val desc limit :limit offset :offset
   ```
   The `row_number() ... partition by source_dataset` + `order by rn` is
   what makes results **interleave across datasets** instead of one dataset
   dominating a page (see README "Round 4", bug #19) — it round-robins:
   1st-best from every dataset, then 2nd-best from every dataset, etc.
3. **Thin-results check**: if `len(results) < THIN_RESULTS_THRESHOLD` (8)
   **and** this is the first page (`offset==0`), the endpoint enqueues a
   `discover_query` job (deduped against any identical in-flight job) and
   returns `discovery_job_id` alongside the (possibly few) results.
4. **Frontend** shows a "🔎 searching Hugging Face for more…" banner while
   polling that job, then auto-refreshes the results when it completes with
   `rows_added > 0`.
5. Every result asset is serialized through **`apps/api/serialize.py::row_to_asset()`**
   — the one shared function every router uses, so `search`, `feed`,
   `moodboards`, and `jobs/{id}/results` all return byte-identical shapes
   (`{id, modality, source:{...}, content:{...}, caption, labels, tags,
   license, dataset_kind, metadata, score}`). This used to be hand-rolled
   per-router and inconsistent — see README bug #10.

### What `discover_query` actually does (ingest_worker.py `discover_for_query()`)
This is genuinely searching **all of Hugging Face**, not just our local
corpus:
1. `search_datasets(query)` hits `GET huggingface.co/api/datasets?search=<query>&filter=modality:image` (and again for `video`, `audio` — the Hub API ANDs multiple modality filters together and returns nothing, so we fan out one request per modality and merge results, deduped by repo_id, highest-downloads wins).
2. **Multi-word queries return empty** from the Hub search API (confirmed live — `search="motorcycle motocross bike"` → `[]`, but `search="motorcycle"` → 4 real hits). So: try the combined query first; if empty, split into words and search each individually, merge.
3. Filter out repos we already have deep (row-level) coverage of.
4. For each new candidate (bounded, `max_new_datasets`), call `crawl_dataset()` (see §5) with a small `max_rows` and a wall-clock `deadline_seconds` budget, so this stays bounded even though it's happening inline inside an interactive-ish request.
5. Return `{query, datasets_found, rows_added}` as the job's output.

---

## 5. Walkthrough: how a dataset gets from Hugging Face into our Postgres

This is `apps/workers/ingest_worker.py::crawl_dataset(repo_id, max_rows, deadline_seconds)`,
the single most important function in the codebase. Called from three
places: the seed-list bootstrap (`--seed`), a queued `ingest_dataset` job,
and inline from recommend's Tier-2/Tier-3 densify (§6).

```
crawl_dataset("uoft-cs/cifar10", max_rows=100)
  │
  ├─ 1. get_splits(repo_id)                 GET datasets-server/splits?dataset=...
  │      -> [{"config": "plain_text", "split": "train"}, {"...": "test"}]
  │      (cached 6h, see §9)
  │
  ├─ for each split (stops early if wall-clock deadline or row budget hit):
  │   │
  │   ├─ 2. get_info(repo_id, config)        GET datasets-server/info?dataset=...&config=...
  │   │      -> {"features": {"img": {"_type":"Image"}, "label": {"_type":"ClassLabel", ...}}}
  │   │      (cached 6h, see §9)
  │   │
  │   ├─ 3. field_types.detect_media_column(features)
  │   │      -> which column holds the actual image/video/audio?
  │   │      handles: native HF Image/Audio/Video types, plain url/path
  │   │      strings, even list-wrapped variants (confirmed live: some
  │   │      datasets' native Audio/Video column is a LIST of variants,
  │   │      not a single {src,...} dict)
  │   │
  │   ├─ 4. field_types.detect_caption_columns(features)
  │   │      name+type heuristics: a string/Value column named caption/
  │   │      text/description/alt_text etc, or the sole remaining string
  │   │      column if nothing else looks caption-like
  │   │
  │   ├─ 5. field_types.detect_label_columns(features)
  │   │      REQUIRES genuine ClassLabel or scalar Value type — NEVER
  │   │      Image/Audio/Video/Sequence/List regardless of column name
  │   │      (this exact gap once let an Image-typed segmentation mask
  │   │       column named "label" get str()'d into the caption field,
  │   │       leaking raw signed URLs — see README bug #23). A scalar
  │   │      STRING value can still itself BE a JSON-encoded blob in
  │   │      disguise (confirmed live: `classifier_yolo` held literal
  │   │      text like '[{"name":"turkeys","prob":0.3},...]') —
  │   │      parse_prediction_list()/extract_text_from_json_string()
  │   │      extract clean text from that shape, or drop the value
  │   │      entirely rather than ever storing raw JSON (README #30/#31).
  │   │
  │   ├─ 6. field_types.classify_dataset_kind(features, caption_cols, label_cols)
  │   │      -> "captioned" | "labeled" | "detection_segmentation" | "other"
  │   │      (real feature-type inspection: ≥2 Image-typed columns AND no
  │   │       caption column = segmentation/detection; a genuine caption
  │   │       column always wins over that heuristic — see README bug #24)
  │   │
  │   └─ 7. _sample_split(...) for up to `rows_budget_remaining` rows:
  │        │
  │        ├─ iter_rows(repo_id, config, split, max_rows)
  │        │    pages through GET datasets-server/rows?dataset=...&offset=...&length=100
  │        │    NOT cached (one-shot consumption, huge payload, would bloat
  │        │    the metadata cache for no reuse benefit)
  │        │    -> yields {"row_idx": 7, "row": {"img": {...}, "label": 3}}
  │        │
  │        ├─ for the media column's value:
  │        │    - native Image/Audio/Video dict -> has its own hosted,
  │        │      SIGNED, EXPIRING url (?Expires=...&Signature=...)
  │        │    - plain string -> resolve_media_url() turns a relative
  │        │      path into a full `resolve/main/...` URL
  │        │
  │        ├─ thumbnails.make_thumbnail(url, modality)
  │        │    - image: fetch full bytes, PIL resize to 512px longest
  │        │      edge, re-encode WebP, upload to Blob "thumbnails"
  │        │      container (public-read)
  │        │    - video: ffmpeg extracts a keyframe -> same WebP pipeline,
  │        │      PLUS a short animated preview clip -> "previews" container
  │        │    - audio: ffmpeg/numpy renders a waveform PNG -> "audio-
  │        │      waveforms" container
  │        │    - also computes a 64-bit dHash (custom PIL+numpy
  │        │      implementation — imagehash/scipy hit a local DLL block,
  │        │      see README bug #1) for dedup, and dominant color
  │        │
  │        ├─ is_safe_scalar() guards on caption/label values (defense in
  │        │    depth against the same corruption class as #6 above)
  │        │
  │        ├─ EVERY asset also gets tagged with words derived from its own
  │        │    repo_id (_repo_id_tags()) — confirmed live need: several
  │        │    genuinely relevant "flowchart" datasets had zero per-row
  │        │    text at all; without this they could never surface for that
  │        │    search term no matter how thoroughly they were crawled
  │        │    (README bug #32)
  │        │
  │        └─ INSERT into assets (... on conflict (source_dataset,
  │             source_config, source_split, source_row) do nothing)
  │             -> Postgres trigger auto-populates search_vector
  │
  └─ after all splits: bulk UPDATE assets set dataset_kind=... where
       source_dataset=repo_id (backfills the kind for this whole crawl),
       and upsert hf_datasets status (modalities, media_column, viewer_supported)
```

**Why `max_rows` and `deadline_seconds` both exist**: `max_rows` is a
row-count budget shared across all splits of one dataset (not per-split —
that was a real bug, see README bug from Round 3: a 2-split dataset fetched
~2x the intended amount). `deadline_seconds` is an independent wall-clock
budget, because different datasets take wildly different time per row (a
32×32 CIFAR thumbnail vs a full-resolution LAION photo) and an interactive
request needs a latency guarantee regardless of how slow the per-row cost
turns out to be.

---

## 6. Walkthrough: "Expand this moodboard to N assets" (the flagship feature)

`POST /moodboards/{id}/recommendations {count, strategy, filters}` just
validates and enqueues a `recommend` job, returns `{job_id}` immediately.
Everything below happens in `apps/workers/recommend_worker.py::process_job()`,
polled by the `worker-recommend` container.

```
                     board assets (captions/labels/tags)
                              │
                              ▼
                 ┌─────────────────────────────┐
                 │ 1. extract_keywords()         │  packages/core/.../search.py
                 │    naive frequency-based        │  top_k=15 raw candidates
                 └───────────────┬─────────────┘
                                  ▼
                 ┌─────────────────────────────┐
                 │ 2. rank_keywords_by_corpus_    │  re-rank by CORPUS rarity
                 │    rarity()                     │  (IDF-style, GIN-backed doc-
                 │    drop words in >30% of the    │  freq lookups). Kills generic
                 │    whole corpus; top_k=10        │  VLM-caption filler words
                 └───────────────┬─────────────┘  ("background","other","scene")
                                  ▼
                 ┌─────────────────────────────┐
                 │ 3. build_tsquery(keywords,     │  NEVER ANDs keywords together
                 │    strategy)                    │  (an early bug: AND'ing top-2
                 │    SIMILAR: top 3, OR'd          │  keywords on a heterogeneous
                 │    DIVERSE/HIGH_QUALITY: top 8   │  board returned 0 results,
                 │    BROAD: top 10                 │  twice, for two strategies —
                 │    all OR'd, narrowing via        │  see README bugs). Every
                 │    keyword COUNT not AND-logic    │  keyword is also run through
                 └───────────────┬─────────────┘  sanitize_keyword() first — a
                                  │                  hard structural guarantee that
                                  │                  a malformed/JSON-ish value that
                                  │                  slipped into a caption/label
                                  │                  upstream can never reach
                                  │                  to_tsquery() as invalid syntax
                                  │                  and crash the job (confirmed
                                  │                  live — see README bug #30).
                                  ▼
                 ┌─────────────────────────────┐
                 │ 4. Tier-1: Postgres search     │  select ... from assets
                 │    candidate_limit =            │  where search_vector @@
                 │    count * 25 (capped 20,000)   │  to_tsquery(...) and <filters>
                 │                                  │  order by ts_rank_cd desc
                 └───────────────┬─────────────┘
                                  ▼
                 ┌─────────────────────────────┐
                 │ 5. score_candidate() +         │  count_matched_keywords():
                 │    count_matched_keywords()     │  multi-keyword matches ALWAYS
                 │    _score = matched*10 +        │  outrank single-keyword ones
                 │             score_candidate     │  (mitigates the "trees"/
                 └───────────────┬─────────────┘  "wooden" false-positive class)
                                  ▼
                 ┌─────────────────────────────┐
                 │ 6. dedupe_candidates()         │  perceptual-hash (dHash)
                 │    phash Hamming distance       │  Hamming distance ≤ 6 =
                 │    threshold                     │  "same image", drop the dupe
                 └───────────────┬─────────────┘
                                  ▼
                 ┌─────────────────────────────┐
                 │ 7. diversify_by_source()       │  round-robin cap: no single
                 │    max_share_per_source=0.25    │  source_dataset > 25% of the
                 │                                  │  final result set (stall-
                 │                                  │  counter-terminated + uncapped
                 │                                  │  top-up pass — an earlier
                 │                                  │  version infinite-looped and
                 │                                  │  hung the whole worker forever)
                 └───────────────┬─────────────┘
                                  ▼
                     emit batch -> recommendation_results
                     (frontend polls /jobs/{id}/results?since_batch=N)
                                  │
                        found < count * 0.8 ?
                              │yes
                              ▼
                 ┌─────────────────────────────┐
                 │ TIER 2 — densify locally-       │  _densify_and_retry():
                 │ known datasets                  │  crawl_dataset() AGAIN, live,
                 │                                  │  on the 1 dataset that already
                 │                                  │  contributed the most matches
                 │                                  │  (deadline_seconds=45, one
                 │                                  │  inline + rest backgrounded
                 │                                  │  as queued ingest_dataset jobs)
                 │                                  │  -> re-run steps 4-7
                 └───────────────┬─────────────┘
                                  │
                        STILL found < count * 0.8 ?
                              │yes
                              ▼
                 ┌─────────────────────────────┐
                 │ TIER 3 — discover NEW datasets  │  _discover_new_datasets_and_
                 │ across all of Hugging Face      │  retry(): same mechanism as
                 │                                  │  search's discover_for_query(),
                 │                                  │  triggered from the expand path
                 │                                  │  instead of the search box.
                 │                                  │  Catches the case where NO
                 │                                  │  locally-known dataset covers
                 │                                  │  the topic at all (confirmed
                 │                                  │  live: a motorcycle-themed
                 │                                  │  board went 6/50 -> 46/50
                 │                                  │  after this was added)
                 └───────────────┬─────────────┘
                                  ▼
                     emit final batch, job COMPLETED
                     output: {found, requested, keywords, densify:{...}, discovery:{...}}
```

**Why three tiers instead of one bigger search?** Cost/latency shaping.
Tier-1 is near-free (an indexed Postgres query, <100ms). Tier-2 costs one
real HF crawl (seconds, bounded). Tier-3 costs a Hub-wide search plus a
fresh crawl of a dataset we've never seen before (most expensive, least
often needed) — it only runs if Tier-1+2 genuinely couldn't satisfy the
request. This mirrors how the blueprint's cost model assumes most requests
are cheap and only the long tail pays the expensive path.

---

## 7. Why there are no embeddings anywhere (the core design bet)

Every other "curate images by similarity" system reaches for CLIP
embeddings + a vector DB. This system deliberately doesn't, for V1, because:

1. **HF datasets already come labeled.** Captions/labels/tags are the
   actual creator-provided signal, not a lossy re-derivation of one.
2. **Cost.** No GPU, no embedding inference pipeline, no vector index to
   host/scale (pgvector or otherwise).
3. **Latency.** Postgres GIN-indexed full-text search is sub-100ms;
   embedding similarity search (even with pgvector) plus a model inference
   call is materially slower for the "search-as-you-type" UX this needs.
4. **It's genuinely how Pinterest ranks**, primarily — text/engagement
   signals dominate, visual embeddings are a refinement layer, not the
   foundation.

The honest tradeoff (documented, not hidden — see README "Known V1
limitations"): pure bag-of-words matching has a real precision ceiling.
"trees"/"wooden" are legitimately on-theme for one board's captions but
also appear generically in unrelated LAION captions describing background
scenery. `count_matched_keywords()` mitigates this (multi-keyword matches
always outrank single-keyword ones) but doesn't eliminate it. **This is the
documented, deliberate on-ramp for embeddings in V2** — pgvector slots into
the exact same `assets` table (`add column embedding vector(512)`) without
touching the rest of the architecture.

---

## 8. Every place media is cached, and why

There are actually **three separate caches**, each with a different reason
to exist:

| Cache | Container/table | Written | Read by | Why |
|---|---|---|---|---|
| **Thumbnails** | Blob `thumbnails` (public) | At ingest, for every asset | Grid/feed browsing (`AssetCard`) | Cheap (512px WebP), needed for EVERY asset ever shown, so it's generated eagerly for all of them |
| **Previews/waveforms** | Blob `previews` / `audio-waveforms` (public) | At ingest, for video/audio only | Video hover-preview, audio static waveform | Same reasoning — small, needed broadly |
| **Full-res media** (`cached_content_uri`) | Blob `cached-assets` (public) | **Lazily**, on first `GET /assets/{id}` (detail-view open) or at export-materialize time — whichever happens first | Asset detail modal (`<img>`/`<video>`/`<audio src>`), export `materialized`/`push_hf` modes | Expensive (full original bytes, could be MBs), and **most of the corpus's thousands of rows are never individually opened** — caching eagerly for all of them at ingest time would be pure waste. Caching on first real view is the same "pay only for what's actually used" bet as thumbnails-not-full-res-at-ingest. |
| **HF metadata calls** (`get_splits`/`get_info`/`search_datasets`/`list_datasets`) | Postgres `hf_api_cache` table | On every call (write-through) | Every subsequent identical call, from ANY worker process | These are called repeatedly with heavy overlap — Tier-1 search profiles a dataset's schema, Tier-2 densify profiles it again seconds later, `discover_catalog()` re-runs the same task-category listing periodically. A dataset's *schema* essentially never changes (6h TTL); catalog search/listing drifts slowly (1h TTL). |

**The actual fix this session** (previously a real gap): before this, only
the *export* path ever ran "fetch once, cache forever" — the asset detail
modal played media directly off HF's signed `content_uri`, which (a) never
touched our cache and (b) goes dead once the signature's `Expires=...`
timestamp passes, even though the DB row lives forever. Now
`packages/core/datacurate_core/mediacache.py::get_or_cache_media()` is the
**one shared implementation** both the API (`assets.py::get_asset`, lazy)
and the export worker (`export_worker.py::_materialize`, eager-at-export)
call — whichever runs first for a given asset "wins" and the other gets a
free cache hit via the same content-hash-keyed Blob path.

```
GET /assets/{id}
  │
  ├─ row.cached_content_uri already set?
  │     │yes -> return immediately, ZERO network calls (fast path)
  │     │no
  │     ▼
  ├─ get_or_cache_media(row):
  │     ├─ blob_exists(cached-assets, hash(content_uri))?
  │     │     │yes -> return that URL (another process/asset already cached it)
  │     │     │no
  │     │     ▼
  │     ├─ fetch_bytes(content_uri)          <- the ONE real HF network hit
  │     ├─ upload_bytes(cached-assets, ...)  <- durable, public-read Blob
  │     └─ UPDATE assets SET cached_content_uri=..., cached_at=now()
  │
  └─ frontend's AssetDetailModal prefers cached_content_uri over content_uri
```

---

## 9. Perceptual hashing & dedup (no embeddings needed here either)

`packages/core/datacurate_core/thumbnails.py` computes a 64-bit **dHash**
(difference hash: resize to 9×8 grayscale, compare adjacent pixel
brightness, one bit per comparison) at ingest time for every image/video
keyframe — a custom PIL+numpy implementation (the `imagehash`/`scipy`
libraries hit a local Windows DLL security block, see README bug #1).
`dedup.py::dedupe_candidates()` computes Hamming distance between phashes;
distance ≤ `phash_hamming_threshold` (6) means "visually the same image" —
these commonly appear when the *same* underlying photo has been captioned
by two different models/datasets (confirmed live: `naorm/website-
screenshots-blip-large` and `...-git-large` share identical phashes for
many rows — same screenshots, two different caption generators). Recommend
always dedupes before diversifying, so a curated set never contains the
same photo twice even across different source datasets.

---

## 10. Export: from a moodboard to a real, provenance-complete dataset

`POST /datasets/{id}/versions` snapshots the current asset list (from a
moodboard or a recommend job's results) into an immutable `dataset_versions`
+ `dataset_records` pair. `POST /datasets/{id}/versions/{v}/exports {mode,
zip}` enqueues an `export` job; `export_worker.py::process_job()`:

1. Joins `dataset_records ⋈ assets` for this version, in `position` order.
2. For each row, builds a flat record: `asset_id, modality, image/video/
   audio (whichever matches), caption, labels, license, width, height,
   duration`, plus **provenance columns**: `source_dataset, source_config,
   source_split, source_row` — the literal answer to "why is this asset in
   my dataset, and where did it come from" for every single row.
3. `mode="references"`: `image`/`video`/`audio` columns hold the original
   (possibly-cached) URLs — fast, tiny, no bytes moved.
4. `mode="materialized"`: calls `get_or_cache_media()` for every row (see
   §8) so the export is fully self-contained and never depends on HF's
   URLs staying alive.
5. `mode="push_hf"`: materialized, then `huggingface_hub.HfApi` uploads
   `data.parquet` + `README.md` straight to the user's own HF namespace.
6. Always writes three files to Blob `exports/{dataset_id}/v{version}/`:
   `data.parquet` (the actual rows), `manifest.parquet` (pure provenance:
   asset_id → source_dataset/config/split/row/license/phash/selection_meta
   — this is the audit trail), `README.md` (a generated dataset card:
   modality/license/source-dataset breakdowns + a plain-English explanation
   of the curation method).
7. If `zip=true`: also bundles all of the above (plus, for materialized
   mode, the actual media bytes under `media/`, **reusing the bytes already
   fetched during materialization** — no second HF round-trip) into one
   `bundle.zip`, so the user gets one single download instead of three
   separate links.
8. Every file link returned to the frontend is a **SAS-signed URL** (the
   `exports` container is private, unlike the public thumbnail/cache
   containers) — time-limited, not permanently public.

---

## 11. End-to-end call sequence for the full user journey

```
1. User opens the app
   -> GET /feed?limit=30                              (page.tsx initial load)
   -> interleaved-by-dataset feed, thumbnail_uri only, instant (indexed query)

2. User searches "motorcycle"
   -> POST /search {query:"motorcycle", limit:30}
   -> Postgres tsquery search, interleaved by dataset
   -> if thin: enqueue discover_query job -> ingest_worker searches ALL of
      HF's catalog, crawls new datasets live, indexes new assets
   -> frontend polls job, banner shows, results refresh when done

3. User clicks an asset (NOT auto-added)
   -> GET /assets/{id}                                 (AssetDetailModal)
   -> lazily caches full-res media into our own Blob storage if not already
   -> shows real dataset fields (metadata jsonb), caption, labels, provenance

4. User clicks "+ Add to board" a handful of times
   -> POST /moodboards                                 (creates board once)
   -> POST /moodboards/{id}/assets {asset_ids:[...]}
   -> per-item remove: DELETE /moodboards/{id}/assets/{asset_id}

5. User clicks "Expand to 50, DIVERSE"
   -> POST /moodboards/{id}/recommendations {count:50, strategy:"DIVERSE"}
   -> enqueue recommend job, return job_id immediately
   -> recommend_worker: extract keywords -> tsquery -> search -> dedup ->
      diversify -> (if starved) densify known datasets live -> (if still
      starved) discover + crawl NEW datasets across all of HF live
   -> results written incrementally to recommendation_results
   -> frontend polls GET /jobs/{id} + GET /jobs/{id}/results?since_batch=N

6. User builds a dataset from those results
   -> POST /datasets                                   (creates dataset once)
   -> POST /datasets/{id}/versions {job_id}             (snapshot -> v1)
   -> POST /datasets/{id}/versions/1/exports {mode:"materialized", zip:true}
   -> export_worker: materialize every asset's bytes into our cache,
      write parquet+manifest+README, bundle into bundle.zip
   -> frontend polls job, shows a single "Download everything" button
      pointing at a SAS-signed zip URL
```

---

## 12. File map (where to actually look)

```
db/migrations/                  schema, in order — read these first for ground truth
  0001_init.sql                   hf_datasets, assets (+ FTS trigger), moodboards,
                                   jobs, dataset_versions/records, recommendation_results
  0002_dataset_kind_and_dedup.sql  dataset_kind columns + backfill
  0003_caching.sql                 hf_api_cache table, assets.cached_content_uri

packages/core/datacurate_core/  shared library, imported by both api and workers
  config.py                       env-driven Settings (the ONLY thing that
                                   differs between local dev and production)
  models.py                       DataAsset / RetrievalPlan / SelectionStrategy —
                                   canonical shapes referenced throughout this doc
  field_types.py                  HF schema inference: detect_media_column,
                                   detect_caption_columns, detect_label_columns,
                                   classify_dataset_kind
  hf_client.py                    all raw HTTP calls to Hugging Face (Hub API +
                                   datasets-server), now cache-wrapped (§9)
  hf_cache.py                     the generic Postgres-backed TTL cache (§9)
  mediacache.py                   the shared "fetch once, cache forever" media
                                   byte cache (§8) — used by api AND workers
  db.py                           psycopg connection pool + cursor()/fetch_all()/
                                   fetch_one()/execute() helpers
  blob.py                         Azure Blob (Azurite-compatible) wrapper:
                                   public vs private containers, SAS URLs,
                                   PUBLIC_BLOB_HOST rewrite for local dev
  queue.py                        the Postgres-backed job queue (§2)
  thumbnails.py                   image/video/audio thumbnail + dHash + waveform
                                   generation pipeline (ffmpeg for video/audio)
  dedup.py                        phash Hamming-distance dedup
  search.py                       extract_keywords, rank_keywords_by_corpus_rarity,
                                   build_tsquery, count_matched_keywords
  rank.py                         score_candidate, diversify_by_source

apps/api/                       FastAPI gateway (thin — see §2)
  main.py                         app wiring, CORS, router registration
  serialize.py                     row_to_asset() — the ONE shared Asset shape
  routers/search.py                POST /search, GET /feed (both interleaved)
  routers/assets.py                GET /assets/{id} (+ lazy media cache),
                                    POST /assets/{id}/events
  routers/moodboards.py            create/get/add/remove/recommend
  routers/jobs.py                  GET /jobs/{id}, GET /jobs/{id}/results
  routers/datasets.py              create/version/export

apps/workers/                   three separate long-running poll loops
  ingest_worker.py                 crawl_dataset(), discover_for_query(),
                                    discover_catalog(), the seed list, run_poll()
  recommend_worker.py              process_job() — the full 7-step + 2-tier
                                    pipeline described in §6
  export_worker.py                 process_job() — parquet/manifest/README/zip

apps/web/                       Next.js frontend
  app/page.tsx                     main feed: debounced search, infinite scroll
                                    (IntersectionObserver), dataset_kind + modality
                                    filter chips, moodboard bar (add/remove/clear)
  components/AssetCard.tsx         grid tile: click->detail modal, explicit
                                    +Add button, hover-preview for video only
  components/AssetDetailModal.tsx  full detail: real metadata fields, provenance,
                                    prefers cached_content_uri over content_uri
  components/ExpandPanel.tsx       moodboard expand UI, shows densify/discovery
                                    status live
  components/DatasetPanel.tsx      build version -> export (zip toggle) UI
  lib/api.ts                       typed fetch wrapper, one function per endpoint
```

---

## 13. What's genuinely NOT here (by design, for V1)

- **No embeddings / vector DB** — see §7 for the reasoning and the exact
  place to add pgvector later without touching anything else.
- **No GPU anywhere** — thumbnailing is PIL/ffmpeg (CPU), dHash is
  numpy (CPU), ranking is SQL + Python scoring (CPU).
- **No Kafka/Service Bus** — `queue.py`'s `SKIP LOCKED` Postgres queue is
  the entire async infrastructure; swapping to real pgmq/Service Bus later
  only touches that one file.
- **No auth** — one implicit local moodboard for the demo; the schema
  already has `user_id`/`project_id` columns ready for Supabase Auth + RLS.
- **No Kubernetes** — three worker containers + one API container +
  Postgres + Azurite, via plain `docker-compose`.
