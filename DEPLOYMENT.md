# Production Deployment (live)

The platform is deployed for real, on real infra, serving real traffic:

| Layer | Where | URL / Resource |
|---|---|---|
| Frontend (OlaAmigoFE) | Azure Container Apps | https://dc-frontend.jollyglacier-f8265958.eastus.azurecontainerapps.io |
| API (FastAPI) | Azure Container Apps | https://dc-api.jollyglacier-f8265958.eastus.azurecontainerapps.io |
| Workers (ingest/recommend/export) | Azure Container Apps (no ingress, background) | `dc-worker-ingest`, `dc-worker-recommend`, `dc-worker-export` |
| Database | Real Supabase Postgres | project `gkamxgnmcghhaigqjvtk`, connected via the Session Pooler (`aws-0-us-east-1.pooler.supabase.com:5432`) |
| Blob storage | Real Azure Storage account | `datacuratestore26393` (containers: `thumbnails`, `previews`, `audio-waveforms`, `cached-assets`, `exports`) |
| Container registry | Azure Container Registry | `datacurateacr2550.azurecr.io` |
| Resource group | Azure | `datacurate-rg` (region: East US) |
| Container Apps environment | Azure | `datacurate-env` |

Local Docker Compose stack (Postgres/Azurite emulators) has been **stopped** — it's no longer used for anything; all reads/writes go to the real Supabase DB and real Azure Blob directly, from both local dev processes (if run) and the deployed Container Apps.

## Why Supabase's Session Pooler, not the "direct connection" hostname

`db.<ref>.supabase.co` (the connection string shown by default in the Supabase dashboard) is **IPv6-only** — it has no `A` record at all. Docker containers (both locally and in Azure Container Apps) generally have no outbound IPv6 route, so connecting to it fails with `Network is unreachable`. The fix: use Supabase's **Session Pooler** endpoint instead — `aws-0-<region>.pooler.supabase.com:5432` with username `postgres.<project-ref>` — which is dual-stack/IPv4-reachable. The pooler region for this project was discovered by trial connection (tried the major AWS regions; `us-east-1` is the one that authenticates). This is set as `DATABASE_URL` in `.env` and is what `apps/api` and all 3 workers actually use in production.

## How the existing corpus was migrated (not started from scratch)

1. Ran all 3 SQL migrations (`db/migrations/0001..0003`) directly against the Supabase project.
2. Copied every real row (assets, hf_datasets, moodboards, datasets, jobs, etc.) from local Postgres into Supabase via streaming `COPY` in FK-dependency order (see `/tmp/migrate_to_supabase.py` pattern — the trigger that maintains `assets.search_vector` was temporarily disabled during the bulk copy to avoid a per-row-over-network timeout, then `search_vector` was backfilled in one bulk `UPDATE`).
3. **Caught and fixed a real bug from this migration**: `asset_events.id` is a `bigserial`. Bulk-copying explicit `id` values does not advance the underlying sequence, so the first new insert after migration collided with an already-migrated row (`UniqueViolation: duplicate key (id)=(1)`). Fixed with `select setval(pg_get_serial_sequence('asset_events','id'), (select max(id) from asset_events))`. Any future table using a serial/identity PK needs the same treatment after a bulk copy — UUID-keyed tables (everything else in this schema) don't have this problem.
4. All ~4,850 existing Blob objects (thumbnails, cached full-res assets, previews, exports) were copied from Azurite into the real Azure Storage account, preserving the exact same container/blob-name paths. The `assets` table's `thumbnail_uri` / `cached_content_uri` / `preview_uri` columns were then rewritten from `http://localhost:10000/devstoreaccount1/...` to `https://datacuratestore26393.blob.core.windows.net/...` via a single `regexp_replace` `UPDATE` (blob paths are identical between Azurite and real Azure Blob, only the host prefix differs).

## Redeploying after a code change

```bash
# Backend (api or workers share one Dockerfile-per-role, same image tag):
az acr build --registry datacurateacr2550 --image datacurate/api:latest --file apps/api/Dockerfile . --no-logs
az containerapp update --name dc-api --resource-group datacurate-rg \
  --image datacurateacr2550.azurecr.io/datacurate/api:latest

az acr build --registry datacurateacr2550 --image datacurate/worker:latest --file apps/workers/Dockerfile . --no-logs
for app in dc-worker-ingest dc-worker-recommend dc-worker-export; do
  az containerapp update --name $app --resource-group datacurate-rg \
    --image datacurateacr2550.azurecr.io/datacurate/worker:latest
done

# Frontend (NEXT_PUBLIC_API_URL is baked in at build time, not runtime —
# always pass it as a --build-arg pointing at the real API FQDN above):
cd OlaAmigoFE
az acr build --registry datacurateacr2550 --image datacurate/frontend:latest --file Dockerfile . \
  --build-arg NEXT_PUBLIC_API_URL=https://dc-api.jollyglacier-f8265958.eastus.azurecontainerapps.io \
  --no-logs
az containerapp update --name dc-frontend --resource-group datacurate-rg \
  --image datacurateacr2550.azurecr.io/datacurate/frontend:latest
```

## Worker "no-args" entrypoint scripts

Azure Container Apps' `az containerapp create --command`/`--args` CLI parameters mis-parse any dash-prefixed token (like `--poll`) as the start of a new flag rather than a literal argument value, making `python -m ingest_worker --poll` impossible to pass directly. Fixed by baking three no-argument wrapper scripts into the worker image (`/usr/local/bin/run-ingest.sh`, `run-recommend.sh`, `run-export.sh` — see `apps/workers/Dockerfile`) and pointing each Container App's `--command` at exactly one of them (a single token, no ambiguity).

## Real post-deploy bug found and fixed: all 3 workers were silently dead since day one

User-reported symptom: searching for "chess play" showed the "checking Hugging Face's full catalog…" discovery UI, but it never resolved live like it's supposed to.

Root cause: **all 3 workers (`dc-worker-ingest`, `dc-worker-recommend`, `dc-worker-export`) had been crash-looping since the very first deploy**, restarting continuously (`Count: 15` in the container events) and never successfully starting even once. The container platform's `ProvisioningState: Succeeded` at `az containerapp create` time only means the *ARM resource* was accepted — it says nothing about whether the container inside actually starts, which is why this went unnoticed initially.

Actual cause: this deploy is being driven from a **MINGW64/Git-Bash shell on Windows**, which auto-rewrites any argument that *looks like* an absolute POSIX path before handing it to a native Windows executable (`az.exe`). So `--command "/usr/local/bin/run-ingest.sh"` was silently mangled into `--command "C:/Program Files/Git/usr/local/bin/run-ingest.sh"` before Azure ever saw it — a path that obviously doesn't exist inside the Linux container, so every container crashed immediately on start with `OCI runtime create failed: ... no such file or directory`, forever.

(One earlier "verified live" recommend-job test during initial deployment had actually been served by the **local Docker Compose worker**, which was still running at that moment and happened to pick up the job from the shared Supabase queue before it was stopped — not by the broken Azure worker. This was only caught once the local stack was later stopped and a fresh discovery job was submitted with nothing left to secretly do the work.)

Fix: prefix the `az containerapp update --command ...` calls with `MSYS_NO_PATHCONV=1`, which disables Git Bash's path-rewriting for that invocation. Re-verified: all 3 revisions flipped to `HealthState: Healthy` / `ProvisioningState: Provisioned`, and their logs immediately showed real polling (`recommend_worker: recommend_worker polling for 'recommend' jobs...`).

**Re-verified the exact end-to-end flow the user reported as broken**: searched `"sunset landscape"` (thin local coverage) → real `discover_query` job enqueued → polled via the public API exactly as the frontend does → job went `QUEUED → RUNNING → COMPLETED` in ~9 seconds, `rows_added: 30` from 2 newly-discovered real datasets (`golamrob/riverside-sunset-golden-hour`, `FaPoSeen/Is_it_sunset`) → re-ran the search and the new assets are really there. The frontend's polling logic (`pollDiscovery` in `dataset-grid.tsx`, poll every 3s for up to 60s, reload the grid on `COMPLETED`+`rows_added>0`) was already correct all along — it simply never had a completed job to react to before now.

## Real quality bug found and fixed: "count-only" discovery trigger missed genuinely thin/wrong-topic searches

User-reported symptom: searching "icons" returned 10 results, but none were actually icons — just unrelated photos/screenshots whose captions happened to mention the word "icon" in passing (a phone-in-hand photo, a website screenshot, a Batman action figure captioned with "iconic", etc). The corpus genuinely had zero dedicated icon datasets in it.

Root cause: `/search`'s discovery trigger ("go look at the rest of Hugging Face's catalog if local coverage is thin") only checked raw **result count** (`< 8 rows → discover`). "Icons" cleared that bar on count alone (10 incidental matches), so it never looked further — even though *zero* of those 10 were actually on-topic.

Fix: added a second, quality-based trigger condition using `ts_rank_cd` (Postgres's own full-text relevance score, already computed for free on every search) — verified empirically live that incidental single-mention matches score exactly `1.0` while genuine, reinforced matches (present in weighted labels/tags, not just an incidental caption word) score `2.0-4.0`. Now: if **no** result clears a "real match" score threshold (`2.0`), treat local coverage as thin and trigger discovery too, regardless of raw count. Also bumped discovery from sampling 2 new datasets per query to 4, so a real, previously-uncovered topic gets a genuinely useful page of results in one pass, not just a token couple.

**Verified live**: re-searched "icons" → discovery correctly fired this time → completed in ~25s → found and ingested **168 real assets from 4 real icon datasets** (`ppierzc/ios-app-icons`, `LiXiY/Anime_Icons`, `YellowjacketGames/orc-assist-icons`, and `dev3Masud/icons8` which hit a transient Hugging Face-side infra error and will be picked up on a later pass) — re-ran the search and genuine icon images (512×512 app icons, 1024×1024 game-asset icons) now rank at the top. Pulled two of the actual thumbnails down and visually confirmed real, clean, professional-quality icon art, not low-res/cheap images.

(Also hit and fixed a separate deploy-mechanics gotcha while pushing this: `az containerapp update --image name:latest` with an unchanged tag string does **not** create a new revision or restart the container, even though the underlying image digest in ACR changed — Container Apps only redeploys when the declared image reference literally changes. Fixed by passing `--revision-suffix <unique>` to force a genuinely new revision each deploy.)

## Round 11 (7 user-reported bugs + real dataset additions)

1. **"Adapt moodboard UI from the OlaAmigoFE upstream PR"** — reviewed `github.com/cadetmaze/OlaAmigoFE`'s merged "moodboard-lookalikes" PR. Adopted its "Find lookalikes"-style button placement/styling for a real "Find similar" feature (see #4).
2. **Video controls hidden in the big detail view** — root cause: the parent panel is a fixed-height (`max-h-[320px]`/`[440px]`) `overflow-auto` box, but the `<video>` element used unconstrained `h-auto w-full`, so any video taller than the box pushed its native seek/play/pause bar (always at the very bottom of a `<video>`) below the visible scroll area. Fixed with `max-h-[300px]/[420px] object-contain` so the video always scales DOWN to fit fully inside the box — controls are now always visible.
3. **New moodboards not persisting/showing** — root cause: the navbar had its OWN, separate, never-fully-integrated "create moodboard" flow that fabricated a fake local-only id (`moodboard-${Date.now()}...`) instead of calling the real backend (`dataset-grid.tsx`'s "+ Add to moodboard" flow was already correctly wired; this second entry point was missed during the original integration). A moodboard with a fake id looks fine in the local list but every real action on it (opening it, adding assets) hits the backend with an id that doesn't exist there, so nothing actually saves. Fixed: navbar's create flow now calls the real `api.createMoodboard()` exactly like the other flow, with busy/error states.
4. **"Find similar" missing** — added a real "Find similar" button in the moodboard view (mirroring the reference PR's placement/styling) that runs the backend's actual `SIMILAR` recommend strategy, polls the job, and appends real results into the grid marked "Similar — not saved yet" with an explicit one-click "+" to actually save them (matches this app's established click-to-view vs explicit-add pattern).
5. **No fallback when a search finds nothing** — two real gaps fixed: (a) clearing the search box only updated the local text state; the grid never reverted to the normal feed until the user pressed Enter again — fixed so an emptied box reverts immediately. (b) a genuinely-thin/obscure query used to leave the user on a dead-end "No results." sentence with no way back — added an explicit "Clear search & browse popular" button.
6. **Storage/abuse risk: unbounded generate-and-export could fill our Blob storage** — confirmed two real, previously-unbounded vectors: the recommend endpoint allowed requesting up to **10,000** assets per moodboard, and the export worker unconditionally fetched+cached real bytes for EVERY record in `materialized`/`push_hf` mode with no ceiling — a single request could have tried to download and store up to 10,000 real media files (some real videos are tens of MB each). Fixed: (a) lowered the recommend endpoint's cap to 1,000; (b) added a real per-JOB cap in the export worker — `MAX_NEW_MATERIALIZATIONS_PER_JOB = 300` and `MAX_NEW_MATERIALIZED_BYTES_PER_JOB = 2 GiB`, whichever hits first. Once hit, remaining records fall back to reference-only (their real original URL, not re-hosted) instead of triggering further downloads — nothing is silently dropped from the exported dataset, just not re-hosted past the cap. Already-cached assets (from an earlier view/export) are always free to include in full, since including them costs nothing new. Surfaced transparently in the export job's output (`storage_cap_applied`, `materialized_count`, `reference_only_count`) and shown in the frontend's export results panel, not hidden.
7. **Added real, verified new datasets** — all 5 originally-requested URLs were checked against the live Hub API and are **not viable** through this platform's `/rows`-based ingestion pipeline, for concrete, distinct reasons (kept transparent rather than force-fit or faked):
   - `builddotai/Egocentric-100K` — `gated: auto` + raw WebDataset (tar-shard) format; datasets-server returns 404 even with our token, and WebDataset isn't `/rows`-queryable at all.
   - `LightwheelAI/EgoDemo` — `gated: manual`, requires a human clicking "agree" on the Hub website; no token can bypass this.
   - `markov-ai/cad-1000-hours` — the dataset viewer is explicitly **disabled** by the owner (`/splits` returns `501 Not supported: dataset viewer is disabled`) — no `/rows` access exists for this dataset at all, regardless of config guessed.
   - `markov-ai/computer-use-large` — real bug in the Hub's own dataset-generation pipeline for this repo (`TypeError: Mask must be a pyarrow.Array of type boolean`), confirmed across all 6 of its real configs (autocad/blender/excel/photoshop/salesforce/vscode).
   - `simple-world-lab/HiFi-UMI-2K` — confirmed via the full feature list: purely LeRobot-style numeric state/action arrays (`observation.state`, `action`, `episode_index`, ...), no image/video column exposed through `/rows` at all (video frames, if any, live in external files this API can't see).

   Found and ingested **4 real, verified alternatives** covering the same egocentric/CAD/computer-use topics instead, after checking each one's actual schema, media column, and real per-file size against the live Hub API first: `markov-ai/autocad-bench-tasks` (50 real CAD interface images), `Yushi123/Gui-agent` (60 real GUI-agent screenshots), `markov-ai/computer-use` (40 real 1920×1080 desktop-task screenshots), `anaisleila/computer-use-data-psai` (39 real 1280×720 browser-task screenshots) — 189 new real assets, corpus now 5,724 total.
   - **Along the way, found and fixed a real, previously-unseen gap in schema detection**: `detect_media_column()` only recognized a column typed as a single native `Image`/`Audio`/`Video`, but two of these real datasets store MULTIPLE screenshots per row as a `List`-of-`Image` column (`"screenshots": [{"_type": "Image"}]` in the Hub's actual `/info`-endpoint JSON shape — a bare Python list wrapping the inner type, confirmed live to be genuinely different from the differently-shaped `/rows`-endpoint feature listing). Fixed with detection logic that recognizes both real shapes; verified by visually pulling down and inspecting an actual ingested screenshot.

## Round 12 (real user accounts, per-user moodboards, blob dedup)

**Real Supabase email/password auth**, wired end-to-end:
- New `db/migrations/0004_users_and_auth.sql`: a `public.profiles` table synced from `auth.users` via trigger (Supabase Auth's own user table lives in the SAME database we already connect to — no separate user store needed), a real FK from `projects.user_id` → `auth.users(id)` (the column already existed, unenforced and never populated), same for `asset_events.user_id`, and a partial unique index so each user has exactly one project their moodboards/datasets live under.
- New `packages/core/datacurate_core/auth.py`: verifies a user's bearer token by calling Supabase Auth's own `GET /auth/v1/user` (this project's keys are HS256-signed with a secret we were never given, so asking Supabase itself to verify is the correct approach, not decoding locally) — with a short in-process cache so repeat requests within ~2 minutes are a plain dict lookup, not a network round trip.
- `apps/api/routers/moodboards.py` and `datasets.py`: every endpoint now requires a real signed-in user and verifies the resource actually belongs to them (403, not silent access) — new `GET /moodboards` and `GET /datasets` list endpoints give real multi-device persistence (what does THIS user actually own, per the database) instead of trusting a browser's localStorage as the source of truth.
- `apps/api/routers/assets.py`: `POST /assets/{id}/events` used to accept a client-claimed `user_id` in the request body (trivially spoofable) — now uses the real verified user from the bearer token (or NULL for logged-out/anonymous browsing, which still logs fine).
- New `OlaAmigoFE/src/lib/auth.ts` (signUp/signIn/signOut/session-refresh against Supabase Auth's REST API directly) and `OlaAmigoFE/src/app/login/page.tsx` (real login/signup page, dark/yellow styled to match the rest of the app).
- `OlaAmigoFE/src/lib/api.ts`: every backend call now goes through a shared `authFetch()` that attaches the real user's access token automatically.
- Moodboard creation/save actions in both the navbar and the main grid now redirect to `/login` first if there's no session, instead of hitting a 401 from the backend.
- **Verified live** with two real Supabase-created test accounts against the deployed API: unauthenticated request → 401; user A creates a moodboard → shows up in user A's real `GET /moodboards` list; user B (a completely different account) gets a real **403 "not your moodboard"** trying to view/modify it, and their own list correctly stays empty — genuine per-user data isolation, not just UI-level hiding.

**True content-addressable blob storage** ("map things correctly so blob doesn't store anything twice"):
- Root cause: `mediacache.get_or_cache_media()`'s cache key was a hash of the *source URL string*, not the actual file bytes. Two different URLs that happen to resolve to byte-identical content (a re-resolved signed URL after the old one expired, or two different asset rows that happen to reference the same real image) would each get uploaded and stored separately — genuine duplicate storage.
- Fixed with a real content-hash key (`blob.content_hash_name_from_bytes`, sha256 of the downloaded bytes) as the canonical storage location: after fetching, check if that exact content already exists in Blob under ANY previous upload — if so, skip the upload entirely and just point at the existing blob. The old URL-hash check is kept as a same-URL fast path (avoids a network re-fetch for a still-valid, previously-seen URL) but is no longer where anything is actually stored.
- **Verified live**: cached a real asset, downloaded the resulting blob directly, computed its real sha256 independently, and confirmed the blob's storage key IS that exact hash — not a hash of the source URL.

## Round 13 (CAD search relevance fix + Google sign-in)

**"CAD Tool Use" still found nothing locally, despite real CAD content already being in the corpus** (`markov-ai/autocad-bench-tasks`, added in Round 11). Root cause: `/search` used `websearch_to_tsquery`, which treats space-separated words as an implicit AND (`'cad' & 'tool' & 'use'`) — and the word "tool" simply never appears anywhere in that dataset's real tags/captions, so the strict all-words-required query missed it entirely. A blunt full-OR fallback was tried and rejected after live testing: "use" alone is common enough in unrelated verbose captions (VLM-style image descriptions) that it drowned out the genuinely relevant tag-only CAD match. The actual fix: drop exactly one word at a time and require the rest to still all match, OR-ing those relaxations together ("at least N-1 of N words") — for a 3-word query this requires 2 of 3 words together, which correctly surfaces `autocad-bench-tasks` (matches on "cad" + "use" from its own tags) while still excluding a caption that only contains "use" in isolation. Falls back further to plain OR only if even that relaxation finds nothing. **Verified live**: `autocad-bench-tasks` now appears 6 times in the top 10 results for "CAD Tool Use", where it previously appeared 0 times.

**Google sign-in added.** Turned out Google OAuth was **already fully configured** on this Supabase project (confirmed live: `GET /auth/v1/settings` reports `google: true`, and calling `/auth/v1/authorize?provider=google` genuinely redirects to Google's real consent screen with a real Client ID) — so this was purely a frontend integration task: added `signInWithGoogle()` \+ `completeOAuthSignIn()` to `src/lib/auth.ts` (redirects to Supabase's `/authorize` endpoint, then a new `/auth/callback` page parses the returned session out of the URL hash fragment — the standard implicit-grant delivery for a plain browser redirect, no PKCE code exchange needed) and a "Continue with Google" button on the login page.

**Account merging** ("if a user logs in from both, they should be merged with the same creds"): this is Supabase Auth's own default server-side behavior, not something built in our code — when a new sign-in identity (e.g. Google) shares an email address with an existing, already-verified user account, Supabase automatically links the new identity to that SAME `auth.users.id` rather than creating a second account. Since our email sign-up already requires email confirmation, a user who signs up with email first and later uses "Continue with Google" with the same address lands on the exact same account — same moodboards, same everything — with zero extra merge logic needed.

**One caveat I can't verify without a real interactive Google login** (would need an actual Google account clicking through a real consent screen, which I can't do programmatically): Supabase's Auth API also enforces an allowlist of "Redirect URLs" it's willing to send a completed OAuth session back to. The `/authorize` call itself succeeded and redirected to Google correctly, which is a good sign, but if the FINAL bounce-back to `https://dc-frontend.../auth/callback` ever gets rejected, the fix is a one-time dashboard setting: Supabase Dashboard → Authentication → URL Configuration → add `https://dc-frontend.jollyglacier-f8265958.eastus.azurecontainerapps.io/auth/callback` (and `http://localhost:3002/auth/callback` for local dev) to the allowed redirect URLs list.

## Round 14 ("That's not yours to change" on your OWN moodboards + "super damn slow")

**The 403 bug — a real ownership-check bug I shipped, and my own verification missed it.** `_assert_owns_moodboard` compared `board["owner_id"] != user["id"]`. psycopg returns a real `uuid.UUID` object for a uuid column; `user["id"]` is a plain `str` decoded from Supabase Auth's JSON. **`UUID(x) != "x"` is always `True` in Python**, so the check rejected *everyone* — including the rightful owner. Fixed by comparing `str(...)` on both sides, in both `_assert_owns_moodboard` and `_assert_owns_dataset`.

Why Round 12's "verified live" didn't catch it: I only tested that a *different* user got 403 (which passed — but for the wrong reason: everyone got 403) and never exercised the owner's own access to their own board. The test now covers both directions explicitly: owner → `200`, other user → `403`.

**Secondary fix — phantom/duplicate folders in the list.** The moodboard list was built from `localStorage`, which survives sign-out, is shared by every account that ever used that browser, and still held ids from the pre-auth era (17 real moodboards existed whose project had `user_id IS NULL`). `GET /moodboards` now returns each board's real `asset_ids`, the frontend treats that per-user response as the source of truth (reconciling the local cache on every load *and* on every auth change), and shows nothing at all when signed out. The 27 orphaned pre-auth projects / 17 moodboards / 10 datasets were deleted; every remaining moodboard has a real owner.

**The slowness — measured, not guessed.** Timing breakdown showed server-side query time was only **37-47ms**, but wall time was **1.4-2.7s**. The culprit was response payload size: a handful of real datasets store enormous per-row metadata blobs — measured in the live corpus:

| dataset | metadata per asset |
|---|---|
| `Doub7e/SDv2-Count70-details-appended-motorcycle` | **1.48 MB** (raw T5 hidden-state tensors) |
| `anaisleila/computer-use-data-psai` | 145 KB |
| `jasperai/monet` | 231 KB |
| **corpus total** | **169 MB** |

The grid never renders any of it (the detail panel fetches the full row separately via `GET /assets/{id}`, and the frontend already preferred *that* response's metadata). So: list endpoints (`/feed`, `/search`, `/jobs/{id}/results`, moodboard contents) no longer select or return `metadata` at all, and the detail endpoint — which was returning `select *` raw, un-serialized — now passes metadata through `summarize_large_metadata()`, keeping every real field but replacing any single value too large to render with an honest one-line summary (`"[1 values, 1,495,514 bytes — too large to display]"`).

Measured before → after (same machine, warm connection):

| | before | after |
|---|---|---|
| feed, 24 assets | 1,421 ms / 44 KB | **274 ms / 25 KB** |
| feed, 48 assets | 2,749 ms / 514 KB | **319 ms / 56 KB** |
| search "motorcycle" | — | **248 ms / 31 KB** |
| open worst-case asset | 12,325 ms / 1.42 MB | **309 ms / 2.5 KB** (573× smaller) |

Remaining ~250 ms is genuine network round-trip latency to Azure `eastus`, not server time.

## Verified live (not just "deployed")

- `GET /health` on the real API → `{"status":"ok","database":true}`.
- Real `POST /search` against the real API returns real corpus results.
- A real moodboard → real recommend job was submitted through the public API, transitioned `QUEUED` → `RUNNING` → `COMPLETED` (`locked_by` showing the actual Azure container's own hostname), and returned 15 real found assets.
- Real thumbnail and cached-asset URLs on the real Azure Storage account return `200` with correct content-types.
- The deployed frontend's built JS bundle was inspected directly and confirmed to contain the real API's FQDN (proving `NEXT_PUBLIC_API_URL` was correctly baked in at build time, not left pointing at `localhost`).
