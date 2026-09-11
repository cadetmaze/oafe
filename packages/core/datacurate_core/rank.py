"""Reranking + diversification — the "don't return 1000 near-identical
horse photos" logic, done without embeddings.

Since we have no vector similarity, diversity is enforced via:
  - source cap: no single HF dataset may contribute more than X% of results
  - round-robin interleaving across sources (spreads variety evenly)
  - dedup (pHash) already removes visual near-duplicates upstream
"""
from __future__ import annotations

from collections import defaultdict


def score_candidate(row: dict, keywords: list[str]) -> float:
    """Blend the FTS rank with light quality/metadata signals.
    (No relevance-from-embeddings term exists in V1 — text rank *is* relevance.)
    """
    text_rank = float(row.get("text_rank") or 0.0)
    quality = float(row.get("quality_score") or 0.5)
    resolution_bonus = 0.0
    if row.get("width") and row["width"] >= 1024:
        resolution_bonus = 0.05
    return 0.75 * text_rank + 0.20 * quality + resolution_bonus


def diversify_by_source(
    candidates: list[dict],
    limit: int,
    max_share_per_source: float = 0.25,
) -> list[dict]:
    """Round-robin selection across source_dataset groups, capping any single
    source at `max_share_per_source` of the final result set.

    IMPORTANT: this must be provably terminating. An earlier version looped
    `while ... any(by_source[s] for s in sources)` and used "is there another
    non-empty source" as the skip condition — which hangs forever (confirmed
    live, reproducible) whenever >=2 sources are simultaneously at-cap while
    still holding leftover items: every source gets skipped, on every pass,
    forever, with the while-condition never becoming false. Fixed with an
    explicit stall counter that forces termination of the capped round-robin,
    followed by an uncapped top-up pass so we still return up to `limit`
    results (mildly over-representing a source beats silently returning too
    few, given V1 has no embeddings to fall back on for "more relevant" fill).
    """
    by_source: dict[str, list[dict]] = defaultdict(list)
    for c in candidates:
        by_source[c.get("source_dataset", "unknown")].append(c)

    for src in by_source:
        by_source[src].sort(key=lambda r: -r.get("_score", 0))

    max_per_source = max(1, int(limit * max_share_per_source))
    sources = list(by_source.keys())
    result: list[dict] = []
    taken: dict[str, int] = defaultdict(int)
    idx = 0
    stall = 0  # consecutive skips with zero progress; a full round-robin lap of skips means we're stuck

    while len(result) < limit and stall < len(sources):
        src = sources[idx % len(sources)]
        idx += 1
        if not by_source[src] or taken[src] >= max_per_source:
            stall += 1
            continue
        result.append(by_source[src].pop(0))
        taken[src] += 1
        stall = 0

    if len(result) < limit:
        # Cap-respecting selection ran dry before hitting `limit` — top up from
        # whatever's left, ignoring the cap. Bounded by list length: cannot loop forever.
        leftovers = sorted(
            (c for items in by_source.values() for c in items),
            key=lambda r: -r.get("_score", 0),
        )
        result.extend(leftovers[: limit - len(result)])

    return result[:limit]
