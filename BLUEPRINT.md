# Dataset Curation Platform — Project Blueprint

**Pinterest × Hugging Face × Dataset Builder**
*Text-driven curation with multimodal support (images, video, audio).*

---

## 1. Vision

Building training datasets today is painful: data is scattered across thousands of Hugging Face datasets, schemas are inconsistent, and assembling 1,000 good samples means hours of manual scripting.

This product makes dataset creation feel like Pinterest — but for ML datasets:

1. **Discover** — browse a Pinterest-style masonry grid of assets (images, video previews, audio waveforms) from HF datasets.
2. **Curate** — like / save assets to a **moodboard** (the curation *is* the query).
3. **Expand** — "Find 100 / 1,000 / 10,000 more like these" — the system uses the *text labels* from your selections to find similar assets.
4. **Build** — the selection becomes a versioned dataset with auto-inferred schema.
5. **Export** — Parquet + manifest + dataset card, downloadable or pushed to HF Hub.

### The key insight: text-first, not embedding-first

Pinterest's actual recommendation engine isn't based on image embeddings — it's based on **text signals**:

- Pin titles and descriptions
- Board names and themes
- Tags and categories
- User queries and click patterns

We do the same: HF image/video/audio datasets are **richly labeled** with captions, tags, categories, and metadata. We use those text signals for retrieval — no expensive image embeddings needed at MVP.

```
Traditional approach (expensive):  Image → CLIP embedding → Vector search (300ms)
Our approach (cheap + fast):       Image caption → Text search → Filter (<50ms)
```

> **"Generate N" means *curate N existing assets***, not synthetic generation.

### The core mental model

> **Don't build "a database containing Hugging Face."**
> Build **a lazy, demand-driven index over the world's datasets**, with a **text-first retrieval engine** on top.
>
> Fundamental primitive: **Moodboard → Extract Keywords → Build Text Query → Search → Filter → Diversify → Dataset**

---

## 2. Scope

### MVP (what we build first)

- **Images, Video, and Audio** — fully multimodal.
- Text-based search and filtering (PostgreSQL full-text search).
- Pinterest-style grid (images, video thumbnails, audio waveform previews).
- Moodboard-based expansion using caption/label extraction.
- Auto schema inference, dataset versioning, Parquet export, push-to-HF.
- Auth, projects, async jobs with live progress.
- **No embeddings required** — pure text retrieval.

### When we add embeddings (V2+)

- If text signals prove insufficient, we add CLIP/other multimodal embeddings.
- The architecture is designed to add embeddings seamlessly (same RetrievalPlan, same pipeline) — just another ranking signal.

### Deferred to V2+

- Multimodal embeddings (CLIP, ImageBind, etc.)
- Synthetic generation
- Collaborative filtering from community signals
- NL-to-dataset builder
- Annotation/labeling tools

---

## 3. User Experience Flow

```
┌────────┬────────┬────────┐
│ image  │ video  │ audio  │      1. Search / feed (text search + metadata filters)
│  ❤️ +  │  ❤️ +  │  ❤️ +  │         Latency: <100ms — feels instant
├────────┼────────┼────────┤
│ image  │ image  │ video  │      2. ❤️ = like,  + = add to moodboard
│  ❤️ +  │  ❤️ +  │  ❤️ +  │
├────────┼────────┼────────┤
│ audio  │ image  │ audio  │      3. Moodboard (10 assets selected)
│  ❤️ +  │  ❤️ +  │  ❤️ +  │
└────────┴────────┴────────┘
                │
                ▼
┌───────────────────────────────────────┐
│ Expand my board                       │
│  ( ) Similar   ( ) Diverse            │   4. Extract keywords from selected
│  ( ) High quality  ( ) Same labels    │      assets' captions/labels/tags
│  Count: [100] [1,000] [10,000]        │      Build search query automatically
└───────────────────────────────────────┘
                │
                ▼
   Extract: "horse", "running", "beach", "sunset"
        → Build query: horse & running & (beach | sunset)
        → Execute text search (25×K candidates)
        → Filter (license, resolution, modality)
        → Dedup (pHash + near-duplicate detection)
        → Diversify (source balance, visual variety heuristics)
        → Top-K results                                  5. Results appear
                │                                         progressively (instant waves)
                ▼
   Select 743 → Dataset v1                     6. Auto schema detected
                │
                ▼
   Export → Parquet + manifest + README        7. Download / push to HF
```

---

## 4. Why Text-First Works

### HF datasets are already labeled

| Dataset | Caption Example | Labels/Tags |
|---|---|---|
| LAION-400M | "A horse running on a beach at sunset" | animal, nature, outdoor |
| COCO | "Person riding bicycle in park" | person, bicycle, outdoor |
| AudioSet | "Sound of dog barking" | dog, animal, bark |
| COYO | "Product photo of red sneakers" | product, fashion, shoes |

Every asset already has **explicit text** describing it. We leverage that.

### The speed difference

| Approach | Latency | Cost |
|---|---|---|
| Image embedding + vector search | 200–500ms | GPU required, ~$50–200/mo |
| Text search (Postgres FTS) | 20–80ms | No GPU, ~$0/mo extra |

### When text fails, add embeddings later

Text search might miss visually similar items with different captions. That's fine for MVP — we add embeddings in V2 as an enhancement, not a requirement.

---

## 5. System Architecture

```
                              USER
                               │
                               ▼
                    ┌────────────────────┐
                    │   Next.js          │
                    │   Pinterest UI     │
                    │   Masonry grid     │
                    │   Real-time feel   │
                    └─────────┬──────────┘
                              │
                        REST + SSE
                              │
                              ▼
                    ┌────────────────────┐
                    │   FastAPI          │  Azure Container Apps
                    │   (API gateway)    │  scale-to-zero for workers
                    │                    │
                    │  • Text search     │  Search is synchronous,
                    │    (in-process,    │  always <100ms
                    │    <50ms)          │
                    │  • No ML inference │
                    │    in API layer    │
                    └───┬────┬────┬──────┘
                        │    │    │
           ┌────────────┘    │    └─────────────┐
           ▼                 ▼                  ▼
   ┌───────────────┐ ┌───────────────┐  ┌────────────────┐
   │   SUPABASE    │ │  SUPABASE     │  │  SUPABASE      │
   │   Postgres    │ │  Full-Text    │  │  pgmq          │
   │   + Auth      │ │  Search       │  │  job queues    │
   │   + Realtime  │ │  (GIN index)  │  │                │
   └───────┬───────┘ └───────────────┘  └───────┬────────┘
           │                                    │
           │              ┌─────────────────────┼──────────────┐
           │              ▼                     ▼              ▼
           │      ┌─────────────┐      ┌─────────────┐  ┌─────────────┐
           │      │ Ingest      │      │ Recommend   │  │ Export      │
           │      │ worker      │      │ worker      │  │ worker      │
           │      │ (CPU, ACA)  │      │ (CPU, ACA)  │  │ (CPU, ACA)  │
           │      │             │      │             │  │             │
           │      │ • HF crawl  │      │ • Keyword   │  │ • Parquet   │
           │      │ • Thumbnails│      │   extract   │  │ • Manifest  │
           │      │ • pHash     │      │ • Search    │  │ • Push HF   │
           │      │ • FTS index │      │ • Dedup     │  │             │
           │      └──────┬──────┘      └──────┬──────┘  └──────┬──────┘
           │             │                    │                │
           ▼             ▼                    ▼                ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │                    AZURE BLOB STORAGE (+ Azure CDN)                  │
   │                                                                     │
   │  /thumbnails/         # 512px WebP (high-quality grid)              │
   │  /video-previews/     # Animated WebP or GIF                        │
   │  /audio-waveforms/    # PNG waveform visualization                  │
   │  /cached-assets/      # Original files, lazily fetched             │
   │  /exports/            # Parquet + manifest + README                 │
   └─────────────────────────────────────────────────────────────────────┘
                              ▲
                              │  CDN serves everything
                              │
   External:  Hugging Face  ──┴──  datasets-server API (rows, metadata)
```

### Why this architecture

| Design Decision | Why |
|---|---|
| **No embeddings in API** | Text search is in-process, <50ms, no ML infrastructure |
| **No GPU workers** | pHash is CPU-only; thumbnails are CPU-only; everything is CPU-cheap |
| **High-res thumbnails** | 512px WebP gives Pinterest-quality previews |
| **CDN-first serving** | Thumbnails served from edge, ~10–30ms globally |
| **Supabase only** | Postgres + FTS + pgmq + Auth + Realtime — zero additional infra |

---

## 6. Technology Stack

| Layer | Tech | Why |
|---|---|---|
| **Frontend** | Next.js (TS) | Virtualized masonry grid, CSR, deploy anywhere |
| **API** | FastAPI (Python) | Same language as workers, async support |
| **Database** | Supabase Postgres | Managed, RLS, cheap, proven scale |
| **Full-text search** | PostgreSQL `tsvector` + GIN index | No Elasticsearch needed; fine to millions of rows |
| **Job queue** | pgmq (Supabase extension) | Zero extra infra; workers poll with visibility timeouts |
| **Auth** | Supabase Auth | Email + OAuth, JWT into API |
| **Real-time** | Supabase Realtime | Progress updates, no WebSocket code |
| **Object storage** | Azure Blob + CDN | Edge-cached thumbnails, cheap storage |
| **Compute** | Azure Container Apps | Scale-to-zero, per-service scaling |
| **Thumbnails** | Pillow + ffmpeg (CPU) | WebP encoding, video frame extraction, audio waveforms |
| **Dedup** | pHash (imagehash library) | CPU-only visual fingerprinting |
| **Export** | PyArrow (Parquet) | Industry standard, streaming writes |

**Not in MVP:**
- Vector embeddings or vector DB
- GPU compute
- Redis cache
- Elasticsearch
- Kubernetes

---

## 7. Multimodal Support

Every asset is a `DataAsset` with a `modality` field. The text-first approach works for all modalities because HF datasets have text labels for everything.

### Images

- **Thumbnail**: 512px WebP, 80% quality
- **Search fields**: caption, labels, tags, alt_text, category
- **Preview**: Full-size image served via CDN (lazy-loaded)

### Video

- **Thumbnail**: Extract keyframe at 25% or 50% duration, 512px WebP
- **Preview**: 2–3 second animated WebP loop (or first 3 frames)
- **Search fields**: caption, description, tags, category
- **Metadata**: duration, fps, resolution, codec

### Audio

- **Thumbnail**: Waveform image (PNG, 800×100px)
- **Preview**: Small audio player with waveform visualization
- **Search fields**: caption, tags, category, transcription (if available)
- **Metadata**: duration, sample_rate, channels

---

## 8. Text Search Architecture

### PostgreSQL Full-Text Search

```sql
-- Assets table with FTS column
ALTER TABLE assets ADD COLUMN search_vector tsvector;

CREATE INDEX assets_fts_idx ON assets USING GIN(search_vector);

-- Trigger to keep FTS updated
CREATE OR REPLACE FUNCTION assets_search_trigger() RETURNS trigger AS $$
BEGIN
  NEW.search_vector :=
    setweight(to_tsvector('english', COALESCE(NEW.caption, '')), 'A') ||
    setweight(to_tsvector('english', COALESCE(NEW.labels_text, '')), 'B') ||
    setweight(to_tsvector('english', COALESCE(NEW.tags_text, '')), 'C') ||
    setweight(to_tsvector('english', COALESCE(NEW.alt_text, '')), 'D');
  RETURN NEW;
END
$$ LANGUAGE plpgsql;

CREATE TRIGGER assets_search_update
  BEFORE INSERT OR UPDATE ON assets
  FOR EACH ROW EXECUTE FUNCTION assets_search_trigger();
```

### Search query

```sql
SELECT id, thumbnail_uri, caption, modality, labels,
       ts_rank_cd(search_vector, query) AS score
FROM assets,
     to_tsquery('english', 'horse & running & (beach | sunset)') query
WHERE search_vector @@ query
  AND modality = ANY(ARRAY['image', 'video'])
  AND width >= 512
  AND license = ANY(ARRAY['cc-by-4.0', 'apache-2.0', 'mit'])
ORDER BY score DESC
LIMIT 30;
```

**Latency**: 20–80ms for queries over 1–5M rows with GIN index.

---

## 9. Keyword Extraction for Recommendations

When a user has 10 assets on their moodboard, we extract keywords from those assets:

```python
from collections import Counter
import re

def extract_keywords(assets: list[Asset], top_k: int = 10) -> list[str]:
    """
    Extract top keywords from a list of assets.
    Uses caption, labels, and tags fields.
    """
    all_text = []
    for asset in assets:
        if asset.caption:
            all_text.append(asset.caption.lower())
        if asset.labels:
            all_text.extend([label.lower() for label in asset.labels])
        if asset.tags:
            all_text.extend([tag.lower() for tag in asset.tags])

    # Tokenize and filter
    words = re.findall(r'\b[a-z]{3,}\b', ' '.join(all_text))

    # Count and return top K
    word_counts = Counter(words)

    # Filter stop words
    stop_words = {'the', 'and', 'for', 'this', 'that', 'with', 'from', 'are', 'was', 'were'}
    keywords = [w for w, _ in word_counts.most_common(top_k * 3)
                if w not in stop_words]

    return keywords[:top_k]
```

### Building the search query

```python
def build_query(keywords: list[str], strategy: str) -> str:
    """
    Convert extracted keywords into a tsquery string.
    """
    if strategy == 'SIMILAR':
        # All keywords should match (strict)
        return ' & '.join(keywords[:5])
    elif strategy == 'DIVERSE':
        # Top 2 keywords required, rest optional
        required = ' & '.join(keywords[:2])
        optional = ' | '.join(keywords[2:6])
        return f"({required}) & ({optional})"
    elif strategy == 'BROAD':
        # Any keyword can match
        return ' | '.join(keywords[:8])
    else:
        return ' & '.join(keywords[:5])
```

---

## 10. Recommendation Pipeline (Text-First)

```
MOODBOARD (k selected)
   │
   ▼ 1. EXTRACT KEYWORDS
   From all selected assets' captions, labels, tags
   Top 5–10 keywords by frequency
   │
   ▼ 2. BUILD QUERY
   Based on strategy (SIMILAR, DIVERSE, BROAD)
   Convert to tsquery format
   │
   ▼ 3. TEXT SEARCH (Postgres FTS)
   Retrieve 25×K candidates
   Filter by: modality, license, resolution, excluded sources
   Latency: <100ms for 25k candidates
   │
   ▼ 4. DEDUP
   - Exact dup: same source_dataset + source_row
   - Visual dup: pHash hamming distance ≤ 6
   - Near-dup: same dominant color cluster, same aspect ratio
   │
   ▼ 5. DIVERSIFY
   - Max 5% from any single source dataset
   - Mix modalities if requested
   - Vary visual signatures (aspect ratio, color palette)
   │
   ▼ 6. EMIT RESULTS
   Batch insert every 50 results
   Realtime push to UI
```

### Why this is instant

- **Text search**: 20–80ms for any query
- **No embedding computation**: No GPU, no vector ops
- **Progressive results**: First 50 appear in ~500ms, rest streams in

---

## 11. Deduplication Without Embeddings

### pHash (visual hash)

Works on image and video thumbnails:

```python
import imagehash
from PIL import Image

def compute_phash(image_path: str) -> int:
    """Compute perceptual hash, returns 64-bit int."""
    img = Image.open(image_path)
    return imagehash.phash(img).hash  # 64-bit int
```

Dedup check:
```python
def is_duplicate(hash1: int, hash2: int, threshold: int = 6) -> bool:
    """Hamming distance check. 6 bits difference = near-duplicate."""
    return bin(hash1 ^ hash2).count('1') <= threshold
```

### Additional dedup signals

| Signal | Method |
|---|---|
| Same source | `source_dataset + source_config + source_split + source_row` unique constraint |
| Color similarity | Dominant color extraction (k-means on pixels), cluster near-identical colors |
| Aspect ratio | Group by similar aspect ratio (±10%) |
| File size | Similar file sizes often indicate duplicates |

---

## 12. High-Quality Thumbnails

Pinterest grids feel premium because of image quality. We match that:

### Image thumbnails

```python
from PIL import Image
import io

def create_thumbnail(image_path: str, size: int = 512, quality: int = 85) -> bytes:
    """
    Create high-quality WebP thumbnail.
    Size: 512px longest edge (for Pinterest-quality grid)
    Format: WebP for best compression
    """
    img = Image.open(image_path)

    # Preserve aspect ratio
    img.thumbnail((size, size), Image.Resampling.LANCZOS)

    # Convert to RGB if needed
    if img.mode in ('RGBA', 'P'):
        img = img.convert('RGB')

    # Save as WebP
    buffer = io.BytesIO()
    img.save(buffer, format='WEBP', quality=quality, method=4)
    return buffer.getvalue()
```

### Video previews

```python
import subprocess

def create_video_preview(video_path: str, duration: float) -> bytes:
    """
    Extract keyframe at 25% duration for thumbnail.
    Create 2-second animated WebP preview.
    """
    # Thumbnail frame
    thumb_time = duration * 0.25
    subprocess.run([
        'ffmpeg', '-ss', str(thumb_time), '-i', video_path,
        '-vframes', '1', '-vf', 'scale=512:-1',
        '-q:v', '75', 'thumbnail.webp'
    ], check=True)

    # Animated preview (2 seconds, 3 frames)
    subprocess.run([
        'ffmpeg', '-ss', str(thumb_time), '-i', video_path,
        '-t', '2', '-vf', 'fps=1,scale=512:-1',
        '-loop', '0', '-preset', 'drawing',
        'preview.webp'
    ], check=True)

    return open('thumbnail.webp', 'rb').read()
```

### Audio waveform

```python
import numpy as np
from PIL import Image, ImageDraw

def create_waveform(audio_path: str, width: int = 800, height: int = 100) -> bytes:
    """
    Create waveform visualization for audio.
    """
    # Use ffmpeg to get waveform data
    import subprocess
    result = subprocess.run([
        'ffmpeg', '-i', audio_path, '-ac', '1',
        '-filter:a', f'aresample={width}',
        '-f', 's16le', '-'
    ], capture_output=True)

    samples = np.frombuffer(result.stdout, dtype=np.int16)
    samples = samples / np.max(np.abs(samples))

    # Draw waveform
    img = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(img)

    mid = height // 2
    for i, s in enumerate(samples):
        x = int(i * width / len(samples))
        y = int(mid + s * mid * 0.8)
        draw.line([(x, mid), (x, y)], fill='#3b82f6', width=1)

    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    return buffer.getvalue()
```

---

## 13. Database Schema

```sql
-- ── Catalog ───────────────────────────────────────────
hf_datasets(
  id uuid pk,
  repo_id text unique,
  author text,
  modalities text[],           -- ['image', 'video', 'audio']
  tags text[],
  license text,
  downloads int,
  likes int,
  size_bytes bigint,
  configs jsonb,
  last_synced_at timestamptz
);

-- ── Assets (multimodal) ─────────────────────────────
assets(
  id uuid pk,
  modality text,               -- 'image' | 'video' | 'audio'

  -- Source (provenance)
  source_provider text default 'huggingface',
  source_dataset text,
  source_config text,
  source_split text,
  source_revision text,
  source_row bigint,

  -- Content URIs
  content_uri text,
  thumbnail_uri text,           -- CDN URL for grid display
  preview_uri text,             -- Full-size or animated preview

  -- Dimensions (for images/video)
  width int,
  height int,
  duration float,               -- For video/audio (seconds)
  fps float,                    -- For video
  sample_rate int,              -- For audio

  -- Text fields (FTS indexed)
  caption text,
  labels text[],                -- Parsed into labels_text for FTS
  tags text[],                  -- Parsed into tags_text for FTS
  alt_text text,
  description text,

  -- Search vector (auto-maintained)
  search_vector tsvector,

  -- Metadata
  metadata jsonb,
  mime_type text,
  license text,
  phash bigint,                 -- For image/video dedup
  color_dominant text,          -- Hex color for visual dedup
  aspect_ratio float,           -- width/height
  file_size bigint,

  -- Quality signals
  quality_score float,          -- Resolution + metadata richness
  is_nsfw bool default false,

  created_at timestamptz default now(),

  unique(source_dataset, source_config, source_split, source_revision, source_row)
);

-- Full-text search index
CREATE INDEX assets_fts_idx ON assets USING GIN(search_vector);
CREATE INDEX assets_modality_idx ON assets(modality);
CREATE INDEX assets_license_idx ON assets(license);
CREATE INDEX assets_source_dataset_idx ON assets(source_dataset);

-- ── Curation ────────────────────────────────────────
projects(
  id uuid pk,
  user_id uuid references auth.users,
  name text,
  created_at timestamptz
);

moodboards(
  id uuid pk,
  project_id uuid,
  name text,
  extracted_keywords text[],    -- Cached from last extraction
  created_at timestamptz
);

moodboard_assets(
  moodboard_id uuid,
  asset_id uuid,
  position int,
  added_at timestamptz,
  pk(moodboard_id, asset_id)
);

asset_events(
  id bigint pk,
  user_id uuid,
  asset_id uuid,
  event text,                   -- impression | click | like | save | remove | export
  context jsonb,
  created_at timestamptz
);

-- ── Datasets ─────────────────────────────────────────
datasets(
  id uuid pk,
  project_id uuid,
  name text,
  description text
);

dataset_versions(
  id uuid pk,
  dataset_id uuid,
  version int,
  schema jsonb,
  stats jsonb,
  status text,                  -- draft | ready | exported
  created_at timestamptz,
  unique(dataset_id, version)
);

dataset_records(
  version_id uuid,
  asset_id uuid,
  position int,
  annotations jsonb,
  selection_meta jsonb,          -- How/why this was selected
  pk(version_id, asset_id)
);

-- ── Jobs ─────────────────────────────────────────────
jobs(
  id uuid pk,
  type text,
  status text,
  progress float default 0,
  input jsonb,
  output jsonb,
  error text,
  created_by uuid,
  created_at timestamptz,
  updated_at timestamptz
);

recommendation_results(
  job_id uuid,
  asset_id uuid,
  rank int,
  score float,
  batch int,
  pk(job_id, asset_id)
);

-- ── Queues (pgmq) ────────────────────────────────────
SELECT pgmq.create('ingest_jobs');
SELECT pgmq.create('recommend_jobs');
SELECT pgmq.create('export_jobs');
```

---

## 14. API Endpoints

```
# Search (synchronous, <100ms)
POST /search
  {query: string, filters: {modality, license, min_width, ...}, limit: int}
  → {assets: [...], cursor: string}

GET /feed
  ?cursor=...
  → {assets: [...], cursor: string}

# Asset details
GET /assets/{id}
  → {asset: {..., source: {...}, provenance: {...}}}

# Moodboards
POST   /moodboards                    {name, project_id}
GET    /moodboards/{id}
POST   /moodboards/{id}/assets        {asset_ids: [...]}
DELETE /moodboards/{id}/assets/{asset_id}

# Recommendations (async)
POST /moodboards/{id}/recommendations
  {count: int, strategy: string, filters: {...}}
  → {job_id: string}

GET /jobs/{id}
  → {status, progress, counts}

# Real-time results via Supabase Realtime
# Client subscribes to recommendation_results table

# Datasets
POST /datasets                        {project_id, name}
POST /datasets/{id}/versions          {asset_ids: [...], schema_overrides}
GET  /datasets/{id}/versions/{v}      → {schema, stats, records}
POST /datasets/{id}/versions/{v}/exports
  {mode: 'references' | 'materialized' | 'push_hf'}
  → {job_id}
```

---

## 15. Performance Guarantees

| Action | Latency Target | How |
|---|---|---|
| Search query | <100ms p95 | Postgres FTS with GIN index |
| Feed load (30 assets) | <150ms p95 | CDN-served thumbnails, cursor pagination |
| Moodboard add/remove | <50ms p95 | Single-row upsert |
| Recommendation start | <100ms (returns job_id) | Async from there |
| First 50 results | <500ms | Text search is instant, batch emit |
| Full 1,000 results | <10s | Streaming via Realtime |
| Image thumbnail load | <50ms globally | Azure CDN edge cache |
| Video preview load | <100ms globally | CDN + small file sizes |

---

## 16. Cost Model

| Item | Monthly Cost |
|---|---|
| Supabase (Pro) | $25 |
| Azure Container Apps (API + 2 workers) | $20–40 |
| Azure Blob (100GB thumbnails) | $2 |
| Azure CDN (500GB egress) | $10–20 |
| Azure Container Registry | $5 |
| App Insights | $0–5 |
| **GPU** | **$0** |
| **Total** | **$60–100/month** |

### Cost savings vs embedding approach

| Approach | GPU Cost | Latency | Complexity |
|---|---|---|---|
| With embeddings (CLIP) | $50–200/mo | 200–500ms | High |
| **Text-first (ours)** | **$0** | **20–80ms** | Low |

---

## 17. Implementation Phases

### Phase 0 — Foundations (Week 1)

- [ ] Supabase project with pgmq, Auth, Realtime
- [ ] Azure Blob + CDN + Container Apps + ACR
- [ ] Monorepo: api/, workers/, web/, core/
- [ ] FastAPI skeleton + Next.js shell
- [ ] Auth flow working

**Exit:** User can sign in, both apps deploy

### Phase 1 — HF Catalog (Week 1–2)

- [ ] Ingest worker: crawl HF datasets via HfApi
- [ ] Poll datasets-server for schemas, configs, splits
- [ ] Store in `hf_datasets` table
- [ ] Schema inference → canonical FieldTypes

**Exit:** 5,000+ datasets cataloged with schemas

### Phase 2 — Sampling + Thumbnails (Week 2–3)

- [ ] Sample rows via datasets-server `/rows`
- [ ] Generate thumbnails (512px WebP for images, keyframes for video, waveforms for audio)
- [ ] Compute pHash for dedup
- [ ] Extract caption, labels, tags → FTS indexing
- [ ] Upload to Blob → CDN

**Exit:** 200k+ assets indexed, search works

### Phase 3 — Pinterest UI (Week 3–4)

- [ ] Masonry grid (virtualized, fast scroll)
- [ ] Search bar + filters (modality, license, resolution)
- [ ] Asset cards with hover actions
- [ ] Moodboards CRUD
- [ ] Asset events logging

**Exit:** Feels like Pinterest, all modalities visible

### Phase 4 — Recommendations (Week 4–6)

- [ ] Keyword extraction from moodboard
- [ ] Query building per strategy
- [ ] Text search + filters + dedup
- [ ] Diversification logic
- [ ] Progressive result emission via Realtime

**Exit:** 10 assets → 1,000 relevant results in <10s

### Phase 5 — Datasets + Export (Week 5–7)

- [ ] Dataset creation from selection
- [ ] Auto schema inference
- [ ] Version management + diffs
- [ ] Export worker: Parquet + manifest + README
- [ ] Push to HF (user token)

**Exit:** End-to-end flow complete

### Phase 6 — Polish (Week 7–8) → Beta

- [ ] NSFW filter (API-based, no local ML)
- [ ] Quality scoring (resolution + metadata)
- [ ] Error handling, job dashboard
- [ ] Performance tuning, load testing

**Exit:** Beta users onboarded

---

## 18. What's Different From Original Plan

| Aspect | Original Plan | Revised Plan |
|---|---|---|
| **Search** | CLIP embeddings + pgvector | Text search (FTS) |
| **Recommendations** | Embedding similarity | Keyword extraction + FTS |
| **Latency** | 200–500ms | 20–100ms |
| **GPU** | Required for embeddings | None |
| **Cost** | $100–300/mo | $60–100/mo |
| **Modalities** | Image only | Image + Video + Audio |
| **Thumbnails** | 256px | 512px (higher quality) |
| **Complexity** | Higher (embeddings pipeline) | Lower (just text) |

---

## 19. When to Add Embeddings (V2)

Embeddings become valuable when:

1. **Visual similarity matters** beyond what captions capture
2. **Cross-modal search** (search by image, not just text)
3. **Personalization** based on user behavior patterns
4. **Scale exceeds FTS capability** (tens of millions of assets)

### How to add embeddings later

Same `DataAsset` model, same `RetrievalPlan`, same pipeline — just add an embedding table:

```sql
asset_embeddings(
  asset_id uuid,
  model text,
  version int,
  embedding vector(512),
  pk(asset_id, model, version)
)
```

Add to pipeline:
- New worker for embedding computation
- Hybrid ranking: `score = 0.6 * text_score + 0.4 * embedding_score`

---

## 20. Definition of Done

A user can, in under 5 minutes:

1. **Search** "horses running on beach" and see instant Pinterest-style grid (images, video thumbnails, audio waveforms)
2. **Save** 10 assets to a moodboard
3. **Click** "1,000 similar" and watch relevant results stream in
4. **Create** a versioned dataset with auto-inferred schema
5. **Export** to Parquet with full provenance and licensing

The experience feels like Pinterest — instant, beautiful, trustworthy.

---

## 21. Key Risks

| Risk | Mitigation |
|---|---|
| HF datasets lack good captions | Fallback to labels/tags; de-prioritize datasets with poor metadata |
| pHash misses some duplicates | Add color similarity + aspect ratio as secondary signals |
| Text search irrelevant results | Iterate on keyword extraction; add user feedback loop |
| Video/audio don't have good thumbnails | Keyframe selection heuristics; waveform visualization |
| Scale exceeds Postgres FTS capability | Add dedicated search (Elastic/Meilisearch) or embeddings |

---

## 22. Success Metrics

| Metric | Target |
|---|---|
| Search latency (p95) | <100ms |
| Thumbnail load time (p95) | <50ms |
| Recommendation completion (1k assets) | <10s |
| User retention (7-day) | >30% |
| Export success rate | >99% |
| Monthly cost | <\$100 |

---

**This is the plan: text-first, multimodal, instant, cheap, beautiful.**
