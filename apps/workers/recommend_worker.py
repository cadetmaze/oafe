"""Recommend worker — the flagship pipeline. Consumes 'recommend' jobs.

MOODBOARD -> extract keywords -> build tsquery -> text search (25xK candidates)
          -> hard filters -> dedup (pHash) -> rerank -> diversify (source cap)
          -> STARVED? -> pull more rows from HF for the datasets that already
             matched (Tier-2 demand-driven densify, for real — not just logged)
          -> emit in batches of 50 (progressive results)

No embeddings anywhere in this file — text search IS the relevance signal.
"""
from __future__ import annotations

import json
import logging
import time
from collections import Counter

from datacurate_core import queue
from datacurate_core.config import settings
from datacurate_core.db import cursor, execute, fetch_all, fetch_one
from datacurate_core.dedup import dedupe_candidates
from datacurate_core.models import SelectionStrategy
from datacurate_core.rank import diversify_by_source, score_candidate
from datacurate_core.search import build_tsquery, count_matched_keywords, extract_keywords, rank_keywords_by_corpus_rarity

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("azure").setLevel(logging.WARNING)
log = logging.getLogger("recommend_worker")

# Tier-2 densify bounds — keep worst-case latency reasonable for a job that's
# already async/progress-tracked, but not unbounded.
#
# IMPORTANT (confirmed live): ingest_worker.crawl_dataset(max_rows=N) applies
# N *per split*, not per dataset total. A 2-split dataset (train+test) with
# current_count=99 and target=current_count+60=159 therefore fetched up to
# 159 PER split = up to 318 total, ~2x the intended cap and ~5.5 minutes for
# one job. Only densify ONE dataset inline (the strongest contributor) to
# keep interactive latency bounded; the second-best still gets a background
# ingest_dataset job so it improves for the NEXT request without blocking this one.
DENSIFY_MAX_DATASETS = 1
DENSIFY_EXTRA_ROWS = 30
DENSIFY_DEADLINE_SECONDS = 45.0  # hard wall-clock cap per inline-densified dataset (confirmed live: needed)

# Tier-3: catalog-wide discovery. Tier-2 only re-crawls datasets that ALREADY
# matched at least weakly — useless when the topic (e.g. "motorcycle") simply
# isn't covered by any locally-known dataset at all, which is exactly the
# case confirmed live: a bike-themed board only densified the one dataset
# that happened to have a couple of incidental motorcycle photos, and never
# looked for genuinely new motorcycle/motocross datasets on the Hub. This
# mirrors search.py's discover_for_query() — same mechanism, triggered from
# the recommend/expand path instead of the search box.
DISCOVER_NEW_DATASETS_MAX = 3  # a couple of picks are often metadata-only duds (confirmed live), so try a few more
DISCOVER_ROWS_PER_DATASET = 40
DISCOVER_DEADLINE_SECONDS = 35.0


def process_job(job: dict) -> dict:
    payload = job["input"]
    moodboard_id = payload["moodboard_id"]
    count = min(int(payload.get("count", 100)), 10_000)
    strategy = SelectionStrategy(payload.get("strategy", "DIVERSE"))
    filters = payload.get("filters", {})

    board_assets = fetch_all(
        """select a.id, a.caption, a.labels, a.tags, a.source_dataset
           from moodboard_assets ma join assets a on a.id = ma.asset_id
           where ma.moodboard_id = %s""",
        (moodboard_id,),
    )
    if not board_assets:
        return {"error": "moodboard is empty", "found": 0}

    exclude_ids = {str(a["id"]) for a in board_assets}
    raw_keywords = extract_keywords(board_assets, top_k=15)
    # Re-rank by corpus rarity BEFORE building the query: this is what filters
    # out generic filler words ("background", "other", "scene") that pass a
    # naive board-frequency count but are near-universal across the whole
    # corpus and cause false-positive matches on unrelated assets (confirmed
    # live — see search.py's rank_keywords_by_corpus_rarity docstring).
    keywords = rank_keywords_by_corpus_rarity(raw_keywords, top_k=10)
    tsquery = build_tsquery(keywords, strategy)
    log.info("job %s: raw_keywords=%s -> rarity-ranked=%s tsquery=%r", job["id"], raw_keywords, keywords, tsquery)

    execute("update moodboards set extracted_keywords = %s where id = %s", (keywords, moodboard_id))

    candidates, deduped, final = _search_filter_dedup_diversify(
        keywords, tsquery, filters, exclude_ids, count, strategy, job_id=job["id"]
    )
    already_emitted = {r["id"] for r in final}
    batch_offset = _emit_results(job["id"], final, keywords, batch_offset=0)

    densify_report = None
    discovery_report = None
    if len(final) < count and len(final) < count * 0.8:
        # Genuinely starved (not just "diversity cap trimmed a plentiful pool").
        # Real Tier-2 hook: go fetch MORE rows from HF right now for the
        # datasets that already proved relevant, then re-search and top up.
        queue.update_progress(job["id"], min(0.5, (len(final) / max(count, 1))), status="PARTIAL")
        densify_report = _densify_and_retry(
            job_id=job["id"], candidates=candidates, keywords=keywords, tsquery=tsquery,
            filters=filters, exclude_ids=exclude_ids | already_emitted, remaining=count - len(final),
        )
        if densify_report["new_candidates"]:
            new_final = diversify_by_source(
                densify_report["new_candidates"], limit=count - len(final),
                max_share_per_source=settings.max_share_per_source,
            )
            batch_offset = _emit_results(job["id"], new_final, keywords, batch_offset=batch_offset, rank_start=len(final))
            final = final + new_final
            already_emitted |= {r["id"] for r in new_final}

        # Tier-2 re-crawling known datasets still wasn't enough — the topic
        # may genuinely not be covered by anything we've indexed yet. Search
        # the WHOLE Hugging Face catalog for it (same mechanism `/search`
        # uses), not just the handful of datasets that already weakly matched.
        if len(final) < count and len(final) < count * 0.8:
            queue.update_progress(job["id"], min(0.7, (len(final) / max(count, 1))), status="PARTIAL")
            discovery_report = _discover_new_datasets_and_retry(
                job_id=job["id"], keywords=keywords, tsquery=tsquery, filters=filters,
                exclude_ids=exclude_ids | already_emitted, remaining=count - len(final),
            )
            if discovery_report["new_candidates"]:
                newer_final = diversify_by_source(
                    discovery_report["new_candidates"], limit=count - len(final),
                    max_share_per_source=settings.max_share_per_source,
                )
                batch_offset = _emit_results(job["id"], newer_final, keywords, batch_offset=batch_offset, rank_start=len(final))
                final = final + newer_final

    return {
        "found": len(final),
        "requested": count,
        "keywords": keywords,
        "candidates_considered": len(candidates),
        "densify": densify_report,
        "discovery": discovery_report,
    }


def _search_filter_dedup_diversify(keywords, tsquery, filters, exclude_ids, count, strategy, job_id):
    candidate_limit = min(count * settings.candidate_multiplier, settings.max_candidates)

    where = ["1=1", "not (id = any(%(exclude)s))"]
    params: dict = {"exclude": list(exclude_ids), "limit": candidate_limit, "tsquery": tsquery}

    if filters.get("modality"):
        where.append("modality = any(%(modality)s)")
        params["modality"] = filters["modality"]
    if filters.get("license"):
        where.append("license = any(%(license)s)")
        params["license"] = filters["license"]
    min_width = filters.get("min_width")
    if strategy == SelectionStrategy.HIGH_QUALITY and not min_width:
        min_width = 512
    if min_width:
        where.append("width >= %(min_width)s")
        params["min_width"] = min_width

    if tsquery:
        where.append("search_vector @@ to_tsquery('english', %(tsquery)s)")
        sql = f"""
            select id, modality, source_dataset, source_config, source_split, source_row,
                   thumbnail_uri, preview_uri, caption, labels, tags, license, phash,
                   width, height, quality_score,
                   ts_rank_cd(search_vector, to_tsquery('english', %(tsquery)s)) as text_rank
            from assets where {" and ".join(where)}
            order by text_rank desc limit %(limit)s
        """
    else:
        sql = f"""
            select id, modality, source_dataset, source_config, source_split, source_row,
                   thumbnail_uri, preview_uri, caption, labels, tags, license, phash,
                   width, height, quality_score, 0.0 as text_rank
            from assets where {" and ".join(where)}
            order by created_at desc limit %(limit)s
        """

    candidates = fetch_all(sql, params)
    log.info("job %s: %d candidates before filtering", job_id, len(candidates))

    for c in candidates:
        c["id"] = str(c["id"])
        matched = count_matched_keywords([c.get("caption") or "", " ".join(c.get("labels") or [])], keywords)
        # Multi-keyword matches ALWAYS outrank single-keyword matches — a
        # candidate sharing one generic noun with the board (confirmed live:
        # "wooden"/"trees" pulling in unrelated train/kitchen photos) is a much
        # weaker relevance signal than one sharing several themed words.
        c["_matched_keywords"] = matched
        c["_score"] = matched * 10.0 + score_candidate(c, keywords)

    candidates.sort(key=lambda c: -c["_score"])
    deduped = dedupe_candidates(candidates, threshold=settings.phash_hamming_threshold)
    log.info("job %s: %d after dedup", job_id, len(deduped))

    final = diversify_by_source(deduped, limit=count, max_share_per_source=settings.max_share_per_source)
    log.info("job %s: %d after diversify (target %d)", job_id, len(final), count)
    return candidates, deduped, final


def _densify_and_retry(job_id, candidates, keywords, tsquery, filters, exclude_ids, remaining):
    """Real Tier-2 densify: pull more rows from Hugging Face RIGHT NOW for the
    datasets that already matched this query, then re-search the enriched index.
    """
    import sys
    sys.path.insert(0, ".")
    from ingest_worker import crawl_dataset  # sibling module, same workdir

    contributing = Counter(c["source_dataset"] for c in candidates)
    ranked_datasets = [d for d, _ in contributing.most_common(DENSIFY_MAX_DATASETS + 2)]

    if not ranked_datasets:
        log.warning("job %s: starved (0 candidates) and nothing to densify — no dataset even weakly matched", job_id)
        return {"datasets_densified": [], "rows_added": 0, "new_candidates": []}

    inline_datasets = ranked_datasets[:DENSIFY_MAX_DATASETS]
    background_only = ranked_datasets[DENSIFY_MAX_DATASETS:]
    log.warning("job %s: starved (need %d more) — densifying %s from live HF data now, %s in background",
                job_id, remaining, inline_datasets, background_only)

    rows_added = 0
    for repo_id in inline_datasets:
        current_count = fetch_one(
            "select count(*) as c from assets where source_dataset=%s", (repo_id,)
        )["c"]
        target = current_count + DENSIFY_EXTRA_ROWS
        try:
            t0 = time.time()
            stats = crawl_dataset(repo_id, max_rows=target, deadline_seconds=DENSIFY_DEADLINE_SECONDS)
            log.info("job %s: densified %s -> %s in %.1fs", job_id, repo_id, stats, time.time() - t0)
            rows_added += stats.get("assets_indexed", 0)
        except Exception as e:  # noqa: BLE001
            log.warning("job %s: densify failed for %s: %s", job_id, repo_id, e)

        # Also enqueue a standalone ingest job for further/deeper indexing —
        # decoupled from this request, benefits future searches too.
        queue.enqueue("ingest_dataset", {"repo_id": repo_id, "max_rows": target + 100})

    for repo_id in background_only:
        current_count = fetch_one(
            "select count(*) as c from assets where source_dataset=%s", (repo_id,)
        )["c"]
        queue.enqueue("ingest_dataset", {"repo_id": repo_id, "max_rows": current_count + DENSIFY_EXTRA_ROWS})

    top_datasets = inline_datasets

    if rows_added == 0:
        return {"datasets_densified": top_datasets, "rows_added": 0, "new_candidates": []}

    _, new_deduped, _ = _search_filter_dedup_diversify(
        keywords, tsquery, filters, exclude_ids, remaining, SelectionStrategy.DIVERSE, job_id
    )
    return {"datasets_densified": top_datasets, "rows_added": rows_added, "new_candidates": new_deduped}


def _discover_new_datasets_and_retry(job_id, keywords, tsquery, filters, exclude_ids, remaining):
    """Tier-3: search ALL of Hugging Face (not just our local corpus) for
    datasets about this topic, sample a couple of NEW ones live, and re-search
    the freshly-enriched index. Confirmed-live gap this fixes: a motorcycle-
    themed board only ever densified the one dataset with a couple of
    incidental bike photos and never discovered genuinely bike-focused
    datasets on the Hub at all.
    """
    import sys
    sys.path.insert(0, ".")
    from ingest_worker import discover_for_query  # sibling module, same workdir

    query = " ".join(keywords[:3])
    if not query:
        return {"query": "", "datasets_found": [], "rows_added": 0, "new_candidates": []}

    log.warning("job %s: still starved after Tier-2 densify (need %d more) — "
                "searching the whole HF catalog for %r", job_id, remaining, query)
    try:
        result = discover_for_query(
            query, max_new_datasets=DISCOVER_NEW_DATASETS_MAX,
            rows_per_dataset=DISCOVER_ROWS_PER_DATASET, deadline_seconds=DISCOVER_DEADLINE_SECONDS,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("job %s: catalog discovery failed for %r: %s", job_id, query, e)
        return {"query": query, "datasets_found": [], "rows_added": 0, "new_candidates": []}

    log.info("job %s: catalog discovery for %r -> %s", job_id, query, result)
    if not result.get("rows_added"):
        return {**result, "new_candidates": []}

    _, new_deduped, _ = _search_filter_dedup_diversify(
        keywords, tsquery, filters, exclude_ids, remaining, SelectionStrategy.DIVERSE, job_id
    )
    return {**result, "new_candidates": new_deduped}


def _emit_results(job_id: str, results: list[dict], keywords: list[str], batch_offset: int = 0, rank_start: int = 0) -> int:
    batch_size = settings.batch_emit_size
    for batch_start in range(0, len(results), batch_size):
        batch = results[batch_start:batch_start + batch_size]
        batch_no = batch_offset + batch_start // batch_size
        with cursor() as cur:
            for i, r in enumerate(batch):
                rank = rank_start + batch_start + i
                cur.execute(
                    """insert into recommendation_results (job_id, asset_id, rank, score, batch, reason)
                       values (%s, %s, %s, %s, %s, %s)
                       on conflict (job_id, asset_id) do nothing""",
                    (job_id, r["id"], rank, r["_score"], batch_no,
                     json.dumps({"matched_keywords": r.get("_matched_keywords"), "source": r.get("source_dataset")})),
                )
        progress = 1.0 if not results else min(1.0, (batch_start + len(batch)) / max(len(results), 1))
        queue.update_progress(job_id, progress, status="RUNNING")
        log.info("job %s: emitted batch %d (%d results)", job_id, batch_no, len(batch))
    return batch_offset + (len(results) + batch_size - 1) // batch_size


def run_poll():
    log.info("recommend_worker polling for 'recommend' jobs...")
    while True:
        job = queue.dequeue("recommend")
        if not job:
            time.sleep(1)
            continue
        job["id"] = str(job["id"])
        log.info("picked up job %s", job["id"])
        try:
            output = process_job(job)
            queue.complete(job["id"], output)
        except Exception as e:  # noqa: BLE001
            log.exception("job %s failed", job["id"])
            queue.fail(job["id"], str(e))


if __name__ == "__main__":
    run_poll()
