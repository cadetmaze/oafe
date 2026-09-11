# The dataset-request agent

How the `oafe` frontend and the agent behind it actually work, end to end.

`ARCHITECTURE.md` covers the original platform (search → moodboard → expand →
export). This document covers what was added on top: a user describes the data
they need in plain language, approves or rejects a handful of real examples,
and gets a built, exported dataset. The platform underneath is unchanged and
still does the crawling, dedup, diversification and Parquet writing.

---

## 1. The shape of it

```
 oafe (Next.js, :3000)                    apps/api (FastAPI, :8000)
 ─────────────────────                    ─────────────────────────
 home feed / search / moodboards  ──────▶ /feed /search /moodboards /assets
 submit a request                 ──────▶ POST /api/dataset-requests   (its own route,
                                            writes dataset_requests + file bytes)
 workspace /{id}                  ──────▶ POST /requests/{id}/start    (enqueue)
   polls every 1.5s               ──────▶ GET  /requests/{id}          (phase, review batch)
   polls the timeline             ──────▶ GET  /requests/{id}/events?since=N
   approve / reject               ──────▶ POST /requests/{id}/decisions
   show more                      ──────▶ POST /requests/{id}/more
   confirm                        ──────▶ POST /requests/{id}/confirm  (enqueue build)
   rows during + after the build  ──────▶ GET  /jobs/{build}/results, /requests/{id}/results
   export                         ──────▶ POST /requests/{id}/export
                                              │
                                              ▼
                              dc_worker_agent        (agent_request jobs)
                              dc_worker_agent_build  (agent_build jobs)
                              dc_worker_export       (export, agent_export jobs)
```

**Nothing is streamed.** Every phase change, timeline entry, candidate and
verdict is a row in Postgres before the browser hears about it. A refresh, a
second tab or a laptop that went to sleep all resume against the same state.
That is the reason for `agent_runs`, `agent_events` and `agent_candidates`
rather than a websocket.

---

## 2. The two halves, and why they are split

The review and the build have opposite requirements, and conflating them was
the single biggest usability problem during development.

| | Review | Build |
|---|---|---|
| Question it answers | "Is this the right *kind* of data?" | "Give me all of it" |
| User is | waiting, watching | free to leave |
| Cost | must be small and roughly constant | scales with the requested rows |
| Measured | **~57-100s** (corpus hit) · ~160s (must stream from HF) | ~4-10 min for 100-150 rows |

They run in **separate worker processes** (`AGENT_JOB_TYPES`), because a
multi-minute build must never sit in front of somebody waiting for their first
six examples.

### Review sample size

Sublinear on purpose — the point is to catch "wrong kind of data" early, and
that is visible in ten items. `agent_state.review_size_for`:

| requested rows | ≤200 | ≤1,000 | ≤2,500 | ≤5,000 | more |
|---|---|---|---|---|---|
| examples shown | 6 | 10 | 12 | 15 | 18 |

"Show More" advances to the next pre-computed batch (up to four exist), so it
is a cursor move rather than another crawl.

---

## 3. Getting a review on screen fast

`agent_worker._gather_review_pool`. Search first, fetch second, and only then
spend an LLM call:

1. **The corpus we already hold** — `_search_corpus`, the same mechanism
   `/search` uses. `websearch_to_tsquery` understands a real phrase and
   `expand_query_synonyms` already knows "egocentric", "pov" and "first person"
   describe the same footage, so the LLM never has to supply that. Free, and it
   often ends here.
2. **Streaming from Hugging Face**, only when the corpus can't fill a review.
   `_stream_rows_from_repo` pulls a `/rows` page and keeps the text plus the
   `resolve/main` media URL — **no download, no Pillow, no ffmpeg.** Three HTTP
   calls per dataset instead of a download and two ffmpeg passes *per row*.
   Rows land in `assets` like any other, so the next request on the topic is
   served from Postgres with no Hugging Face call at all. This is the trick
   `dataset_large/` uses, applied to the agent.
3. **Judging**, on the shortlist only, followed by vision on what's about to be
   shown.

Three rules were each learned the hard way:

- **Fetch only when the corpus genuinely can't fill a review.** One run spent
  six minutes fetching six rows while 43 corpus matches sat unused. The bar is
  "can I fill a review", not "is the pool full".
- **Every streamed row's media is verified with a one-byte ranged GET.** These
  rows have no thumbnail to fall back on, so an item that won't load is worse
  than no item. `lightly-ai/epic-kitchens-100-clips` stores clip *IDs*
  (`P01_01`) rather than paths, so every URL built from it 404s; one Qualcomm
  set has a first row that resolves and later ones that don't — which is why
  every row is checked, not just the first. A dataset whose media never
  resolves is recorded as `rows_failed` and skipped for free from then on.
- **Interleave candidates across datasets before judging.** Only the top ~12
  rows get judged, so pure ranking let one dataset take every slot: for
  "website screen recordings" all twelve came from `causvid_website` (a paper's
  website assets) and `markov-ai/computer-use-large` — actual screen
  recordings, already in the corpus — was never considered. Round-robin by
  source, the same thing `/search` does in SQL. Fixing this took that review
  from 1 dataset to 3, all vision-confirmed.
- **Run every search attempt and merge them.** Returning on the first query
  that matched anything meant one incidental phrase hit starved the broader
  keyword search — twice: once leaving 33 freshly-streamed rows unexamined, and
  once filling the limit before the second attempt ran at all.
- **The chat says how many shown items the agent is actually confident about**,
  rather than implying all six are equally good.

An earlier version screened every candidate dataset with an LLM and profiled
each over HTTP before crawling. It was slower *and* worse: for "kitchen cooking
egocentric" it correctly found EPIC-KITCHENS, spent its whole budget on repos
whose `/rows` backend is broken, and returned nothing.

---

## 4. Judging: what decides whether a row is a match

Full-text search does **recall**; the LLM does **precision** on the shortlist.
The corpus is never ranked by an LLM.

- **Text judge** (`agent_brain.judge_rows`, batches of 20) returns
  `match | weak | reject` plus `missing` — the required attributes the text
  does not evidence. `missing` is what separates "the caption contradicts this"
  from "the caption is silent", and only the second is worth an image check.
- **Vision** (`qwen3-vl-235b-a22b-instruct`) runs only on shortlisted `weak`
  rows whose open questions are visually decidable. It matters more than it
  sounds: several genuinely on-topic datasets carry **no per-row text at all**,
  so text judging can only ever return `weak` for them. Vision confirms them
  with real evidence ("First-person view showing hands spreading peanut butter
  on a tortilla in a home kitchen") and roughly doubles their score.
- **Order matters, and got it wrong once.** Admission runs *after* vision.
  Gating first threw away exactly the rows vision exists to rescue, and the
  first run of the pipeline kept zero rows from five real cooking datasets.
- **Fusion**: `0.60·llm + 0.25·keyword-overlap + 0.15·score_candidate`, with
  `reject` a hard drop and an admission rule of `≥2 distinct keywords OR a
  positive verdict` to kill the single-coincidental-keyword tail.
- **Dataset-name tags are not evidence.** Ingest derives tags from the repo id
  so a dataset stays findable with zero per-row text — good for recall, and
  actively harmful as proof about a row. Every row of `causvid_website` was
  returned as a confident match for "website recordings" on the strength of its
  dataset's name, with the judge citing *"tag includes 'website'"*. Tags are now
  labelled as such in the prompt, excluded from keyword-overlap scoring, and a
  `match` may never rest on one. Those rows became `weak`, vision looked, and
  five of six were correctly rejected.
- **Vision looks at every unresolved row that has a picture.** An earlier filter
  also required that none of the missing attributes were "not visually
  decidable"; the model marks attributes conservatively, so that skipped exactly
  the rows the image check existed for.

### The review sample is stratified, not top-N

Showing the ten highest-scoring rows yields near-duplicates that all get
approved, which teaches nothing. The sample takes ~2 top, ~4 mid-band, ~2 from
the dominant source and ~2 low-confidence.

### What ~10 binary labels can honestly support

In descending defensibility — and deliberately *not* "train a model":

1. **Source reweighting.** A repo with ≥3 samples and ≥⅔ rejected is dropped
   (multiplier 0.0); ≥⅔ approved is boosted to 1.25. Each sample proxies
   hundreds of rows sharing that dataset's subject and caption style, so a
   verdict about the dataset is far better founded than one about a row.
2. **Spec repair.** One `refine_spec` call over the items, the user's verdicts
   and the judge's prior verdicts — the *disagreements* are the signal.
   Real output: *"Added exclusions for non-home cooking settings (restaurant,
   commercial kitchen) and positive keywords for cooking tools."*
3. **Threshold calibration** of a single scalar, applied only while it still
   leaves a real dataset.
4. **Hard negatives** into `negative_keywords`.

---

## 5. Merging many datasets into one consistent dataset

`packages/core/datacurate_core/unify.py`. A build routinely spans a captioned
photo set, a label-only classifier set and a set with neither.

- **The Parquet schema is declared, not inferred.** This was a real defect:
  `from_pylist(rows)` types an all-None column as `null`, so two exports of the
  "same" dataset had incompatible schemas. Verified before and after — the old
  path produced `video: null, license: null, labels: list<null>`; the declared
  path produces 33 stable columns with none null-typed.
- **Columns are added, never renamed**, so existing consumers keep working.
- **`caption_source`** says what the caption really is. The ingest path sets
  `caption = " ".join(labels)` for label-only datasets, so without this the
  export claims a caption that is actually a label join.
- **`labels` stays verbatim and authoritative**; `labels_canonical` is the
  convenience column. Merges require confidence ≥0.8, cluster ≤6, never across
  different source columns, and **never** between two labels a single source
  defines as distinct classes. `label_map.json` ships in the bundle.
- **Licensing is honest.** `assets.license` is never populated at ingest, so
  exports used to emit NULL licences that read as permissive. The dataset-level
  Hub licence is joined in, its origin recorded in `license_source`, and the
  README prints `Rows with unknown license: N`.
- **Per-source balance**: `max_share = min(0.35, max(0.10, 2/sources))`. The
  platform's fixed 0.25 is unsatisfiable below four sources, which silently
  turns the cap into a no-op exactly when balance matters most.
- **Provenance**: every row carries `source_dataset/config/split/row` plus a
  human-checkable `source_url` into the Hugging Face viewer, and the manifest
  adds `retrieved_at`, `media_state` and the judge fields.

### The build judges too

The build originally skipped judging entirely: it scored rows on keyword
overlap and image quality and took whatever the datasets held. That is how a
request for "website screen recordings" came back as **diamond rings and
elephants** — those rows live in datasets with "website" in the name, nothing
ever looked at them, and no keyword could rule them out because they have no
caption at all. Three things now stand between a dataset and the output:

1. **Only datasets that contributed rows which survived judging** supply rows —
   not every dataset that turned up during discovery.
2. **Confirmed sources first.** A source is `strong` only if at least one of its
   rows was positively confirmed by the text judge or by vision; `partial`
   sources are used only when the strong ones cannot fill the target. This is
   recorded *after* the vision pass — writing it earlier marked every source
   `partial`, because until the images have been looked at every video row is
   still `weak`, which silently disabled the preference entirely. (The third
   time an ordering-vs-vision mistake has bitten this pipeline; if you add a
   step that consumes verdicts, check where it sits relative to vision.)
3. **Rows are judged in the build**, in interleaved order across sources and
   only until there are enough. Review verdicts are reused, never re-requested.

### Video needs a keyframe before it can be judged at all

Streaming skips media processing, so a video row arrives with no still image —
and a vision model cannot watch an MP4. Without a frame every video row stays
`weak` forever, no source is ever confirmed, and the build has nothing to
prefer. So the shortlist about to be reviewed gets one, with ffmpeg pointed at
the **remote** URL (`-ss` before `-i`) so it range-requests only the bytes
around the seek point. Downloading first hit the 25 MB fetch cap on exactly the
datasets that matter here — real screen recordings are routinely far larger.
Same technique as `narrated_video_ingest.generate_thumbnails`.

### Modality is decided by the user's words, not the model

Someone asking for "website recordings **videos**" and getting a grid of still
images is the clearest possible failure, and it is not a judgement call. The UI
filter wins if set; otherwise the query's own words (`video`, `clip`, `footage`,
`recording` / `audio`, `speech` / `photo`, `image`) decide; the model's answer is
only the fallback.

### Verdicts are never invented

The build does not re-judge thousands of rows, so rows it never judged get
`judge_verdict = null` rather than a default. A real export: 2 `match`,
14 `weak`, 84 `null`, with `user_verdict` set only on reviewed items.

---

## 6. Scale: the tiered build

Fully indexing a row (fetch, thumbnail, pHash) costs about a second, so a
10,000-row request cannot materialise everything. Up to
`agent_materialize_cap` (800) rows are crawled properly; beyond that rows are
indexed as **references** — real provenance, text and media URL, no media copy.
The Parquet export is complete either way; `media_state` records which is which.

**Coming up short is not a reason to stop looking.** The datasets that answered
the review are rarely the only ones on the Hub that fit, and "27 rows from 2
source datasets" is a poor answer to a request for 100. If the selection falls
short, the build runs further Hub searches (up to `BUILD_DISCOVERY_ROUNDS`,
inside a wall-clock budget), each of which asks only for datasets we do not
already hold — so every pass genuinely widens the net rather than re-reading the
same repos. It stops early when a round finds nothing new, and says so.

When the honest answer is still "there isn't that much", the agent says so:
*"Built your dataset: 75 rows from 5 source datasets. That's short of the 150
you asked for: this is everything I could find that genuinely matches."*

Rows are published to `recommendation_results` after each dataset is crawled,
so the workspace fills in progressively instead of showing an empty panel for
the whole build.

---

## 7. Orbitrage — the only LLM provider

Verified live; each of these cost a debugging cycle.

| Fact | Consequence |
|---|---|
| `gpt-oss-120b` ignores `response_format: json_schema` (it emitted `Below is a{…}`) | **All** structured output goes through forced function calls (`tool_choice`). Never `response_format`. |
| `gpt-oss-120b` garbles managed tool names (emitted `exa_search_orbitratejson`) | Web search runs on `glm-4.7`, which drives the Tools Gateway correctly |
| The gateway executes search server-side for a bare tool name (`tools: ["tavily_orbitrage"]`) | Web search is a two-model chain: search on `glm-4.7` → extract repo ids on `gpt-oss-120b` → **validate every id with `get_splits()`**, because this path invents them |
| The vision provider rejects HTTP image links ("Only inline image data URLs and S3 URLs are supported") | Thumbnails are fetched and inlined as base64 data URLs |
| No embeddings endpoint exists | Retrieval stays text-first, exactly as `ARCHITECTURE.md` §7 describes |

Every call has a deterministic fallback: `ToolCallRefused` degrades the run to
the keyword-only behaviour the platform had before any LLM existed. Dataset
captions are third-party text — they are delimited and labelled as data, and
only tool-call output is trusted.

---

## 8. The workspace grid

- **Rows only ever append.** The build republishes its whole selection on each
  poll, re-ranked as more rows are judged; rendering that directly made tiles
  jump around and new ones appear in the middle of the grid while the user was
  reading it. The workspace merges by id instead of replacing, so the masonry
  layout is stable and anything new lands at the bottom.
- **Video tiles play on click**, lazily re-hosting the clip through
  `GET /assets/{id}` the same way the review card does.
- **Polling adapts to the phase.** Waiting for a review is a user-input state —
  nothing changes until they click — so the 1.5s poll drops to 6s there and
  stops entirely at `DONE`. Asset lookups are memoised client-side because they
  are made per tile.

## 9. Media that actually plays

- Hugging Face serves media with `Access-Control-Allow-Origin:
  https://huggingface.co`, so a `<video>` pointed at a HF URL **cannot** play on
  our origin. Video plays only from our own blob copy, which
  `GET /assets/{id}` creates on first view.
- The review card degrades in steps: our cached clip → the animated WebP
  preview generated at ingest → the keyframe. Each fallback is strictly more
  reliable, which matters because HF's signed URLs expire in 48-72h (a measured
  76% of stored ones were already dead).
- Inside a worker, stored blob URLs point at the browser-facing host, which
  resolves to the container itself. `blob.internal_url()` maps them back —
  without it the vision check silently saw zero images.

---

## 10. Running it locally

```bash
docker compose up -d postgres azurite api worker-agent worker-agent-build worker-export
cd oafe && npm run dev          # http://localhost:3000
```

`worker-ingest` and `worker-recommend` are **not** needed for this flow — the
agent crawls in-process — and leaving them running wastes scarce database
connections.

Required in the root `.env`: `DATABASE_URL`, `HF_TOKEN`, `ORBITRAGE_API_KEY`.
`oafe/.env.local` needs `DATABASE_URL` and `NEXT_PUBLIC_API_URL`.

### Two operational traps

- **Supabase's session pooler allows 15 clients for the whole project**, shared
  by the API, every worker, the web app *and anything already deployed*. Pools
  are sized `min_size=0, max_size=2` for that reason. Exceeding it produces
  `EMAXCONNSESSION`, and a poll loop that treats it as fatal takes every worker
  down at once — both loops now survive it.
- **A deployed worker on the same database will race the local one.** An Azure
  Container Apps `dc-worker-export` pod was claiming `export` jobs and producing
  the old schema. The agent uses its own `agent_export` type so it is only ever
  served by a worker running this code.

### Node vs libpq TLS

`sslmode=require` means "encrypt, don't verify" in libpq (what psycopg does),
but node-postgres treats it as `verify-full` and fails Supabase's chain with
`SELF_SIGNED_CERT_IN_CHAIN`. `oafe/src/lib/postgres.ts` strips the mode from the
URL and sets `ssl` explicitly — a connection string's own `sslmode` wins over
the option, so overriding alone is not enough.

---

## 11. What was verified end to end

Against the real Supabase database, real Hugging Face datasets and real
Orbitrage models:

- Submitted through oafe's own route → workspace page renders the stored query;
  an unknown id 404s.
- Review ready in **57-98s**, all **6/6 items displayable** — verified by
  actually fetching each one (206 for every item, including real EPIC-KITCHENS
  MP4s) — with vision-confirmed matches carrying real evidence.
- Decisions persisted per click and identical after a reload; the timeline
  replays from Postgres.
- Build streamed rows while running (**100 visible mid-build**) and reached the
  full 100-row target from 5 source datasets.
- Export: clean zip, `data.parquet` + `manifest.parquet` + `README.md`, 100
  rows × 33 columns, **no null-typed columns**, provenance URL and source row on
  every row, manifest row count matching, and an accurate unknown-licence count.

### Known limits

- The agent's precision is bounded by what Hugging Face actually hosts; for
  narrow requests it will honestly return fewer rows than asked for.
- Label clustering biases toward leaving labels alone; expect it to under-merge
  rather than over-merge.
- Vision is capped per build, so most rows in a large dataset are selected on
  text and keyword evidence alone — and are reported as unjudged, not as
  weak matches.
- **A strict review can be a thin one.** When vision rejects most of the
  shortlist the agent shows only what survived — one confirmed item rather than
  six plausible-looking wrong ones. That is the intended trade, but there is no
  second fetch round after vision culls the pool, so a topic the corpus covers
  badly yields a small review.
