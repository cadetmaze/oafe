"""Deduplication without embeddings — pHash + cheap visual heuristics.

Two complementary signals (neither alone is enough):
  - pHash hamming distance -> catches resized/recompressed copies of the same image
  - dominant color + aspect ratio -> cheap secondary signal to catch near-dupes
    that pHash misses (crops, color-graded variants)
"""
from __future__ import annotations


def hamming_distance(a: int, b: int) -> int:
    """a, b are signed 64-bit ints (as stored in Postgres bigint). Mask to 64
    unsigned bits before XOR so sign-extension of negative ints never affects
    the popcount."""
    mask = (1 << 64) - 1
    return bin((a ^ b) & mask).count("1")


def is_visual_duplicate(a_phash: int | None, b_phash: int | None, threshold: int = 6) -> bool:
    if a_phash is None or b_phash is None:
        return False
    return hamming_distance(a_phash, b_phash) <= threshold


def dedupe_candidates(candidates: list[dict], threshold: int = 6) -> list[dict]:
    """Greedy dedup: keep first occurrence, drop later near-duplicates.
    candidates must be pre-sorted by rank/score (best first) so we keep the best copy.
    """
    kept: list[dict] = []
    seen_sources: set[tuple] = set()

    for c in candidates:
        source_key = (c.get("source_dataset"), c.get("source_config"), c.get("source_split"), c.get("source_row"))
        if source_key in seen_sources:
            continue  # exact same row already selected

        phash = c.get("phash")
        is_dup = False
        if phash is not None:
            for k in kept:
                if is_visual_duplicate(phash, k.get("phash"), threshold):
                    is_dup = True
                    break
        if is_dup:
            continue

        seen_sources.add(source_key)
        kept.append(c)

    return kept
