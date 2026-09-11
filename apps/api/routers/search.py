"""Search & feed — the Pinterest-speed core. Pure Postgres full-text search,
no embeddings, no ML inference in this process. Target: <100ms p95.
"""
from __future__ import annotations

import itertools
import re
import time
from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from datacurate_core.db import fetch_all, fetch_one
from datacurate_core.queue import enqueue
from datacurate_core.search import expand_query_synonyms, sanitize_keyword, tokenize

from serialize import row_to_asset

router = APIRouter()

THIN_RESULTS_THRESHOLD = 8  # below this, also look for brand-new HF datasets matching the query

# CONFIRMED LIVE (user-reported: "if someone gives the name of some dataset
# in HF in the search box, it's not able to fetch that"). Pasting a real
# repo id ("markov-ai/computer-use") or a full dataset URL is an
# unambiguous "go get exactly THIS" instruction — but it used to be treated
# as plain free text: full-text search on captions/tags never matches a
# repo id, and the Hub's own `search=` endpoint doesn't reliably match a
# fully-qualified id either, so the user got nothing. Detect that shape and
# ingest that exact dataset directly instead of guessing.
HF_REPO_RE = re.compile(
    r"^(?:https?://huggingface\.co/datasets/)?([A-Za-z0-9][\w.\-]*/[\w.\-]+)/?$"
)


def extract_hf_repo_id(query: str) -> str | None:
    m = HF_REPO_RE.match(query.strip())
    return m.group(1) if m else None

# NOTE: `metadata` is deliberately NOT selected here. See
# serialize.summarize_large_metadata for the full measured rationale — short
# version: real datasets in this corpus carry up to 1.48 MB of metadata PER
# ASSET (embedding arrays), the grid never renders any of it, and including
# it made a 48-asset page 513 KB / 2.7s instead of ~30 KB / ~0.3s. The detail
# view fetches the full row (metadata included) separately via
# GET /assets/{id}, which is the only place it's actually shown.
ASSET_COLUMNS = """
    id, modality, source_dataset, source_config, source_split, source_row,
    content_uri, thumbnail_uri, preview_uri, width, height, duration, fps,
    caption, labels, tags, license, phash, color_dominant, aspect_ratio, quality_score, dataset_kind
"""


class SearchFilters(BaseModel):
    modality: Optional[list[str]] = None
    license: Optional[list[str]] = None
    min_width: Optional[int] = None


def _row_to_asset(row: dict, score: float | None = None) -> dict:
    return row_to_asset(
        row,
        extra={"score": round(score, 4)} if score is not None else None,
        include_metadata=False,
    )


@router.post("/search")
def search(payload: dict):
    """
    { "query": "horses running on a beach", "filters": {...}, "limit": 30, "offset": 0 }
    """
    t0 = time.perf_counter()
    query_text = (payload.get("query") or "").strip()
    filters = payload.get("filters") or {}
    limit = min(int(payload.get("limit", 30)), 200)
    offset = int(payload.get("offset", 0))

    # Results are rendered as image tiles, so a row with no media copy of its
    # own has nothing to show. The dataset agent creates such rows deliberately
    # (see AGENT.md §6) — they belong in a built dataset, not in a grid.
    where = ["thumbnail_uri is not null"]
    params: dict = {"limit": limit, "offset": offset}

    # expanded_query broadens known-synonym short queries ("pov" -> also
    # matches "egocentric") for the actual search; discovery (below) still
    # uses the user's literal original query_text against the Hub API.
    expanded_query = expand_query_synonyms(query_text) if query_text else query_text
    if query_text:
        where.append("search_vector @@ websearch_to_tsquery('english', %(q)s)")
        params["q"] = expanded_query

    if filters.get("modality"):
        where.append("modality = ANY(%(modality)s)")
        params["modality"] = filters["modality"]
    if filters.get("license"):
        where.append("license = ANY(%(license)s)")
        params["license"] = filters["license"]
    if filters.get("min_width"):
        where.append("width >= %(min_width)s")
        params["min_width"] = filters["min_width"]
    if filters.get("dataset_kind"):
        where.append("dataset_kind = ANY(%(dataset_kind)s)")
        params["dataset_kind"] = filters["dataset_kind"]

    rank_expr = "ts_rank_cd(search_vector, websearch_to_tsquery('english', %(q)s))" if query_text else "extract(epoch from created_at)"

    # Interleave across source datasets instead of clumping (confirmed live:
    # a naive `order by created_at desc` put 8+ consecutive results from the
    # same dataset back to back, since a whole dataset gets ingested in one
    # batch with near-identical timestamps). row_number() partitioned by
    # source_dataset groups "best from A, best from B, best from C, 2nd-best
    # from A, ..." — genuinely mixed, still deterministic for stable paging.
    # CONFIRMED LIVE (user-reported, with a screenshot): the SAME image
    # appeared three times in one grid. 569 redundant rows across 335
    # duplicated perceptual hashes exist in the real corpus — the same
    # picture legitimately shows up in multiple HF datasets (and sometimes
    # repeatedly within one). Collapse byte/visually-identical assets to one
    # representative via phash. NULL-phash rows (audio has no phash) must
    # each stay their own partition, hence the coalesce-to-unique-id trick;
    # phash 0 is the "blank/solid image" sentinel and is also collapsed.
    dedup_partition = "coalesce(phash::text, 'u' || id::text)"
    sql = f"""
        with deduped as (
            select {ASSET_COLUMNS}, {rank_expr} as rank_val,
                   coalesce((select d.curation_tier from hf_datasets d
                             where d.repo_id = assets.source_dataset), 0) as tier,
                   row_number() over (partition by {dedup_partition}
                                      order by {rank_expr} desc, quality_score desc nulls last, id) as dup_rn
            from assets
            where {" and ".join(where)}
        ), ranked as (
            select *, row_number() over (partition by source_dataset order by rank_val desc) as rn
            from deduped where dup_rn = 1
        )
        select * from ranked
        order by rn asc, rank_val desc, tier desc, quality_score desc nulls last
        limit %(limit)s offset %(offset)s
    """
    rows = fetch_all(sql, params)

    # CONFIRMED LIVE (user-reported): "CAD Tool Use" found nothing locally
    # even though the corpus has real, well-tagged CAD content
    # (markov-ai/autocad-bench-tasks, tags: autocad/cad/computer-use/gui-
    # agent/...). Root cause: websearch_to_tsquery treats space-separated
    # words as an implicit AND ("cad" & "tool" & "use") — and "tool" simply
    # never appears anywhere in that dataset's real tags/captions, so the
    # strict all-words-must-match query misses it entirely despite being
    # obviously, strongly on-topic via the other two words.
    #
    # A blunt full-OR fallback ( "cad" | "tool" | "use" ) was tried and
    # rejected after live testing: "use" alone (a generic word, appears in
    # many unrelated VLM-style captions at the higher caption weight) then
    # outranks a genuine tag-only match on "cad", burying the actually-
    # relevant dataset under incidental single-word noise — the exact same
    # failure class fixed for "icons" a few rounds ago, just showing up
    # differently. The real middle ground, mirroring how a human reads
    # "needs most, not all, of these words": drop exactly ONE word at a time
    # and require the REST to still all match (AND), OR-ing those
    # relaxations together. For "cad"/"tool"/"use" this requires >=2 of the
    # 3 words together — satisfied by autocad-bench-tasks (cad & use, from
    # its "cad"/"computer-use" tags), but NOT by a caption that only
    # contains "use" alone. Falls further back to plain OR only if even that
    # relaxation finds nothing (better a loose match than none).
    if not rows and query_text and offset == 0:
        words = list(dict.fromkeys(w for w in (sanitize_keyword(w) for w in tokenize(expanded_query)) if w))
        if len(words) > 2:
            # CONFIRMED LIVE (user-reported, with a screenshot): clicking
            # "Code Repositories" returned Trump photos, an NFL logo and
            # dollar bills. Root cause was THIS relaxation degrading to a
            # pure OR for short queries: with 2 words, N-1 == 1, so the
            # "drop one word" rule reduced to "match ANY single word" — any
            # asset whose caption merely contained "code" or
            # "repositories" anywhere qualified. A relaxation must never
            # drop below TWO co-occurring words, which is what made it
            # meaningful in the first place; for a 2-word query that means
            # no relaxation at all (strict AND stands, and coming up empty
            # correctly hands off to live Hugging Face discovery instead of
            # padding the grid with junk).
            group_size = max(2, len(words) - 1)
            combos = list(itertools.combinations(words, group_size)) or [tuple(words)]
            relaxed_tsquery = " | ".join("(" + " & ".join(combo) + ")" for combo in combos)
            relaxed_rank_expr = "ts_rank_cd(search_vector, to_tsquery('english', %(relaxed_q)s))"
            relaxed_where = [w for w in where if "websearch_to_tsquery" not in w]
            relaxed_where.append("search_vector @@ to_tsquery('english', %(relaxed_q)s)")
            relaxed_sql = f"""
                with deduped as (
                    select {ASSET_COLUMNS}, {relaxed_rank_expr} as rank_val,
                   coalesce((select d.curation_tier from hf_datasets d
                             where d.repo_id = assets.source_dataset), 0) as tier,
                           row_number() over (partition by {dedup_partition}
                                              order by {relaxed_rank_expr} desc, quality_score desc nulls last, id) as dup_rn
                    from assets
                    where {" and ".join(relaxed_where)}
                ), ranked as (
                    select *, row_number() over (partition by source_dataset order by rank_val desc) as rn
                    from deduped where dup_rn = 1
                )
                select * from ranked
                order by rn asc, rank_val desc, tier desc
                limit %(limit)s offset %(offset)s
            """
            rows = fetch_all(relaxed_sql, {**params, "relaxed_q": relaxed_tsquery})

    for r in rows:
        r["score"] = r.pop("rank_val", None)
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    # Gate the (relatively expensive, network-bound) whole-catalog discovery
    # trigger behind the caller explicitly saying the query is "final" —
    # confirmed live: live search-as-you-type firing this on EVERY keystroke
    # meant typing "anim" (on the way to "animals") could trigger a Hub
    # search for "anim" and pull in unrelated "anime" datasets before the
    # user even finished typing. Default True so non-typing callers
    # (recommend's Tier-3, programmatic use) are unaffected — only the
    # frontend's live-as-you-type path explicitly passes False.
    allow_discovery = payload.get("allow_discovery", True)
    discovery_job_id = None

    # Explicit "fetch this exact dataset" path — takes priority over any
    # keyword heuristics below, because the user told us precisely what they
    # want. Enqueues a real ingest of that repo (deduped against an already
    # in-flight job for the same repo) and reports it back through the same
    # discovery_job_id channel the frontend already polls, so results stream
    # into the grid exactly like a normal discovery.
    repo_id = extract_hf_repo_id(query_text) if query_text else None
    if allow_discovery and repo_id and offset == 0:
        recent_ingest = fetch_one(
            """select id from jobs where type='ingest_dataset' and input->>'repo_id' = %s
               and status in ('QUEUED','RUNNING') and created_at > now() - interval '10 minutes'
               limit 1""",
            (repo_id,),
        )
        discovery_job_id = str(recent_ingest["id"]) if recent_ingest else enqueue(
            "ingest_dataset", {"repo_id": repo_id, "max_rows": 60, "deadline_seconds": 120}
        )
        return {
            "assets": [_row_to_asset(r, r.get("score")) for r in rows],
            "count": len(rows),
            "elapsed_ms": elapsed_ms,
            "next_offset": offset + limit,
            "discovery_job_id": discovery_job_id,
            "fetching_repo": repo_id,
        }
    # CONFIRMED LIVE (user-reported): a raw result COUNT alone is not enough
    # to decide "we have good local coverage of this topic". Searching
    # "icons" returned a full page of 10 rows and so never triggered
    # discovery — but every single one of those 10 was an UNRELATED photo
    # whose caption just happened to mention the word "icon(s)" in passing
    # (a phone photo captioned "...positioned upright as an icon...", a
    # website SCREENSHOT captioned "...a number of different icons...",
    # etc) — not one was an actual dedicated icon image; the corpus simply
    # had no real icon dataset in it at all.
    # ts_rank_cd score is a genuine, free (no extra query) quality signal
    # for this: a caption that merely CONTAINS the query word once scores
    # ~1.0; a result whose match is reinforced (repeated term, or present in
    # the higher-weighted labels/tags fields too — see assets_search_trigger
    # in 0001_init.sql: caption=A, labels=B, tags=C) scores meaningfully
    # higher. Empirically verified live: "icons" (all incidental caption
    # mentions) -> every row scored exactly 1.0; "motorcycle" (a topic we
    # actually have a dedicated, well-tagged dataset for) -> scores of
    # 2.0-4.0. So: even with a full page of hits, if NONE of them clear a
    # "this is a real, reinforced match, not an incidental mention" bar,
    # treat local coverage as thin anyway and go look at HF's whole catalog
    # for something genuinely better.
    best_rank = max((r.get("score") or 0 for r in rows), default=0) if query_text else 0
    STRONG_MATCH_THRESHOLD = 2.0
    results_are_weak = bool(query_text) and best_rank < STRONG_MATCH_THRESHOLD
    if allow_discovery and query_text and offset == 0 and (len(rows) < THIN_RESULTS_THRESHOLD or results_are_weak):
        # Thin (or zero) results for a real query -> our ~handful of seed
        # datasets probably just don't cover this topic. Look at HF's WHOLE
        # catalog for something that does, live, in the background — never
        # blocks this response. Deduped: skip if we already have a recent
        # in-flight discovery job for this exact query text.
        recent = fetch_one(
            """select id from jobs where type='discover_query' and input->>'query' = %s
               and status in ('QUEUED','RUNNING') and created_at > now() - interval '5 minutes'
               limit 1""",
            (query_text,),
        )
        if recent:
            discovery_job_id = str(recent["id"])
        else:
            discovery_job_id = enqueue("discover_query", {"query": query_text})

    return {
        "assets": [_row_to_asset(r, r.get("score")) for r in rows],
        "count": len(rows),
        "elapsed_ms": elapsed_ms,
        "next_offset": offset + limit,
        "discovery_job_id": discovery_job_id,
    }


@router.get("/feed")
def feed(
    cursor_offset: int = Query(0, alias="offset"),
    limit: int = Query(30, le=100),
    modality: Optional[str] = None,
    dataset_kind: Optional[str] = None,
    source_dataset: Optional[str] = None,
    min_quality: bool = True,
    label: str | None = None,
):
    """Default browsing grid — most recently indexed assets, interleaved
    across source datasets (no query)."""
    t0 = time.perf_counter()
    where = ["1=1"]
    params: dict = {"limit": limit, "offset": cursor_offset}
    # `modality` accepts a comma-separated list ("image,video,audio").
    # CONFIRMED LIVE (user-reported: "I only see images more", "I don't see
    # videos of that"): the frontend always sent exactly ONE modality (the
    # navbar toggle, defaulting to image), so video and audio were filtered
    # out of every grid and every topic unless you knew to flip the toggle.
    # The corpus really is image-heavy (6,014 image / 1,019 audio / 40
    # video), but hard-filtering to one modality made that far worse than it
    # is. Empty/absent now correctly means "everything".
    if modality:
        mods = [m.strip() for m in modality.split(",") if m.strip()]
        if mods:
            # CONFIRMED LIVE (user-reported: "Computer Use" + the video
            # filter showed nothing, despite 60/60 of those assets having a
            # real screen recording): an EPISODIC asset's own modality is
            # `image` (its representative media is a screenshot) while its
            # video lives in metadata._recording_uri. A plain
            # `modality = 'video'` filter therefore hid every episode
            # recording in the corpus. Asking for video must also surface
            # anything that genuinely HAS a video attached.
            if "video" in mods:
                where.append("(modality = ANY(%(modality)s) or metadata ? '_recording_uri')")
            else:
                where.append("modality = ANY(%(modality)s)")
            params["modality"] = mods
    if dataset_kind:
        where.append("dataset_kind = %(dataset_kind)s")
        params["dataset_kind"] = dataset_kind

    # Curated "collection" chips in the top nav point at REAL datasets by id
    # rather than guessing with free-text keywords (see TOPICS in navbar.tsx)
    # — that's what makes a chip's contents actually match its label.
    if source_dataset:
        repos = [d.strip() for d in source_dataset.split(",") if d.strip()]
        if repos:
            where.append("source_dataset = ANY(%(source_dataset)s)")
            params["source_dataset"] = repos

    # A collection can be narrower than a whole dataset. markov-ai/computer-use-large
    # holds autocad/blender AND excel/photoshop/salesforce/vscode screencasts in one
    # repo, so "CAD Tool Use" must filter by the asset's real domain labels, not just
    # by repo id, or it would claim Photoshop tutorials are CAD.
    if label:
        wanted = [l.strip().lower() for l in label.split(",") if l.strip()]
        if wanted:
            where.append("exists (select 1 from unnest(labels) l where lower(l) = any(%(label)s))")
            params["label"] = wanted

    # CONFIRMED LIVE (user-reported: "the media are just worst quality"):
    # the corpus contains classic ML benchmark sets stored at their native
    # tiny sizes — uoft-cs/cifar10 is 32x32 (807 assets) and ylecun/mnist is
    # 28x28 (70 assets). Blown up into a full-width Pinterest tile those are
    # unrecognisable mush, and they were a big share of the default grid.
    # Keep them fully searchable (someone explicitly looking for CIFAR/MNIST
    # should still find them) but keep them out of the default browse grid.
    if min_quality:
        where.append("(modality <> 'image' or width is null or width >= 200)")
        # A browse grid can only show rows it can render. The dataset agent
        # tops up the corpus with reference-only rows — real provenance and
        # text, no media copy — which belong in a built dataset but appear here
        # as blank tiles.
        where.append("thumbnail_uri is not null")
        # CONFIRMED LIVE (user-reported: "anyone searches for any xyz thing
        # and it comes to popular section"). Popular was ordered by
        # `created_at desc`, so whatever a user's search auto-discovered
        # became the newest rows and immediately took over the front page.
        # Only curated (tier 2) or demonstrably reputable (tier 1) datasets
        # may appear here; unvetted auto-discovered ones stay fully
        # searchable but never hijack the default grid. See migration 0005.
        where.append("""exists (select 1 from hf_datasets d
                            where d.repo_id = assets.source_dataset
                              and d.curation_tier >= 1)""")

    # Same phash de-duplication as /search — see the comment there; the
    # default browsing grid is exactly where the user actually noticed the
    # same image repeating three times.
    sql = f"""
        with deduped as (
            select {ASSET_COLUMNS}, created_at,
                   coalesce((select d.curation_tier from hf_datasets d
                             where d.repo_id = assets.source_dataset), 0) as tier,
                   row_number() over (partition by coalesce(phash::text, 'u' || id::text)
                                      order by quality_score desc nulls last, created_at desc, id) as dup_rn
            from assets
            where {" and ".join(where)}
        ), ranked as (
            select *, row_number() over (partition by source_dataset order by created_at desc) as rn
            from deduped where dup_rn = 1
        )
        select * from ranked
        -- Curated first, then per-dataset round-robin (rn) so no single
        -- dataset can monopolise the grid, then intrinsic quality.
        order by tier desc, rn asc, quality_score desc nulls last, created_at desc
        limit %(limit)s offset %(offset)s
    """
    rows = fetch_all(sql, params)
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    return {
        "assets": [_row_to_asset(r) for r in rows],
        "next_offset": cursor_offset + limit,
        "elapsed_ms": elapsed_ms,
    }
