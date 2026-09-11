"""Text-first retrieval: keyword extraction from a moodboard + tsquery building.

This is the whole trick that replaces embeddings: HF assets already carry
captions/labels/tags. A moodboard's "intent" is just the statistically
dominant words across its selected assets' text — no ML required.
"""
from __future__ import annotations

import re
from collections import Counter

from .db import fetch_all
from .models import SelectionStrategy

STOP_WORDS = {
    "the", "and", "for", "this", "that", "with", "from", "are", "was", "were",
    "has", "have", "had", "not", "but", "you", "your", "his", "her", "its",
    "into", "onto", "over", "under", "near", "very", "some", "any", "all",
    "than", "then", "them", "they", "she", "him", "who", "what", "when",
    "where", "which", "will", "would", "could", "should", "there", "here",
    "being", "been", "each", "such", "these", "those", "one", "two", "photo",
    "image", "picture", "shot", "view",
    # Confirmed live (real user testing) as high-frequency, low-signal filler
    # words specific to long, verbose AI-generated (VLM-style) captions —
    # e.g. LAION's GPT4Vision captions. These pass a naive board-frequency
    # count but are near-universal across the WHOLE corpus, so they cause
    # false-positive matches (an unrelated "sneakers on a white background"
    # photo matching an animal-themed board purely via the word "background").
    # A fixed list is a stopgap; rank_keywords_by_corpus_rarity() below is the
    # general fix (works for ANY dataset's caption style, not just ones we've
    # happened to see), but keeping the worst offenders here too is cheap insurance.
    "background", "other", "others", "suggests", "suggesting", "scene", "setting",
    "overall", "appears", "appear", "positioned", "presence", "likely", "possibly",
    "various", "visible", "natural", "environment", "surrounded", "including",
    "depicting", "resembling", "standing", "located", "appearing", "nearby",
}

TOKEN_RE = re.compile(r"[a-zA-Z]{3,}")
# A real word/short-phrase label never needs more than this many characters;
# anything longer flowing through here is almost certainly not natural-
# language label text (JSON, code, a stray full sentence, etc).
MAX_LABEL_LEN = 40


def tokenize(text: str) -> list[str]:
    return [w.lower() for w in TOKEN_RE.findall(text or "")]


def sanitize_keyword(word: str) -> str:
    """Defense in depth for build_tsquery(): whatever the source of a
    "keyword" (board caption words, label values, HF metadata...), this must
    ALWAYS be safe to drop straight into a raw `to_tsquery()` boolean
    expression. CONFIRMED LIVE this matters even after fixing the ingest-side
    root cause (see field_types.parse_prediction_list docstring): a raw JSON
    blob (`'[{"name": "turkeys", "prob": 0.3}, ...]'`) that slipped into a
    label value made it all the way to `to_tsquery()` as one "keyword" and
    raised a hard Postgres syntax error, taking the whole recommend job down
    with it. Strip to a single alpha token; anything that doesn't reduce to
    one clean word is worthless as a search term anyway.
    """
    tokens = TOKEN_RE.findall(word or "")
    return tokens[0].lower() if tokens else ""


def extract_keywords(assets: list[dict], top_k: int = 10) -> list[str]:
    """assets: list of dicts with caption/labels/tags fields (e.g. moodboard members).

    Weighting: caption words count once per occurrence; label/tag values count
    extra (they're curated signal, less noisy than free text).
    """
    counts: Counter[str] = Counter()

    for asset in assets:
        caption = asset.get("caption") or ""
        counts.update(tokenize(caption))

        for label in (asset.get("labels") or []):
            label_str = str(label)
            counts.update(tokenize(label_str))
            # Extra weight for the label AS A WHOLE (curated signal, less noisy
            # than free text) — but only when it's actually a short, clean,
            # single-concept label. A long/punctuation-heavy string (JSON,
            # a full sentence, code...) is not a real "label", it's something
            # that slipped past ingest-side detection; tokenizing it above is
            # still safe, but using the WHOLE raw string as one search token
            # is exactly how a JSON blob once broke to_tsquery() outright.
            if len(label_str) <= MAX_LABEL_LEN and re.fullmatch(r"[a-zA-Z0-9 _-]+", label_str):
                counts[label_str.lower()] += 2

        for tag in (asset.get("tags") or []):
            tag_str = str(tag)
            if len(tag_str) <= MAX_LABEL_LEN:
                counts[tag_str.lower()] += 2

    for stop in STOP_WORDS:
        counts.pop(stop, None)

    return [w for w, _ in counts.most_common(top_k)]


def rank_keywords_by_corpus_rarity(board_keywords: list[str], top_k: int = 10) -> list[str]:
    """The general fix for the 'background'/'other'/'scene' problem: a word
    that's frequent on THIS board but also frequent across the ENTIRE corpus
    carries little discriminative signal (it's just common vocabulary in
    whatever caption style this dataset uses) — confirmed live: extracting
    purely by board-frequency let generic AI-caption filler words leak in and
    match totally unrelated assets. This re-scores each candidate keyword by
    how RARE it is corpus-wide (classic IDF idea), using one small GIN-indexed
    count query per word (fast: single-word to_tsquery lookups, not a table scan).

    Words extracted from the board are already frequency-ranked (best first);
    this only re-orders/filters them by corpus rarity, it doesn't invent new words.
    """
    if not board_keywords:
        return []

    total = fetch_all("select count(*) as c from assets")[0]["c"] or 1
    scored = []
    for rank, raw_word in enumerate(board_keywords):
        # Same sanitize-before-to_tsquery discipline as build_tsquery() — this
        # function runs BEFORE build_tsquery() in the pipeline and has its own
        # direct to_tsquery() call, so it needs the same defense independently
        # (confirmed live: this exact call site is what actually raised the
        # "syntax error in tsquery" exception, since it runs first).
        word = sanitize_keyword(raw_word)
        if not word:
            continue
        df_row = fetch_all(
            "select count(*) as c from assets where search_vector @@ to_tsquery('english', %s)",
            (word,),
        )
        df = df_row[0]["c"] if df_row else 0
        # rarity in [0,1]: 1 = appears in nothing else (highly discriminative),
        # 0 = appears in every single asset (pure noise). board rank (earlier
        # = more frequent on the board) breaks ties among equally-rare words.
        rarity = 1.0 - min(df / total, 1.0)
        board_weight = 1.0 / (rank + 1)
        scored.append((word, rarity * 0.7 + board_weight * 0.3, df))

    scored.sort(key=lambda x: -x[1])
    # Drop words that are near-ubiquitous (appear in >30% of the whole corpus)
    # entirely — they're not "lower priority", they're actively misleading.
    filtered = [w for w, _, df in scored if df <= total * 0.30]
    return (filtered or [w for w, _, _ in scored])[:top_k]


def build_tsquery(keywords: list[str], strategy: SelectionStrategy) -> str:
    """Build a Postgres `to_tsquery`-compatible string from extracted keywords."""
    # Sanitize every keyword right before it touches raw tsquery syntax — the
    # single choke point defense (see sanitize_keyword docstring). Cheap and
    # makes to_tsquery() syntax errors structurally impossible from here on,
    # regardless of what any future dataset's weird schema manages to sneak
    # into a caption/label/tag value upstream.
    kws = [sanitize_keyword(k) for k in keywords]
    kws = [k for k in kws if k]
    if not kws:
        return ""

    # Note: strategy controls how *narrow the query is*, not directly how
    # varied the results look — visual/source variety is enforced downstream
    # by diversify_by_source() on the candidate set. A wide OR net here with
    # multi-keyword-match reranking works far better than AND-ing keywords
    # from a heterogeneous board (e.g. dogs + cats + horses rarely co-occur
    # in one caption — AND-ing them starves the result set to zero).

    if strategy == SelectionStrategy.SIMILAR:
        # Narrowest net: OR across only the top 3 (highest-signal) keywords.
        # NOTE: this used to require the #1 keyword via AND, on the theory
        # that a cohesive board's top word should anchor every match. In
        # practice (confirmed live) the #1 keyword is often a rare/specific
        # word (e.g. "toy") that only co-occurs with itself — AND-ing it
        # starves results to zero the same way the old DIVERSE bug did.
        # Same fix, same reasoning: narrow via keyword COUNT, never via AND.
        return " | ".join(kws[:3])

    if strategy == SelectionStrategy.HIGH_QUALITY:
        # Same broad net as DIVERSE; quality comes from the min_width filter
        # applied by the caller, not from narrowing the text query.
        return " | ".join(kws[:8])

    if strategy == SelectionStrategy.DIVERSE:
        # Wide net across ALL extracted themes — lets a heterogeneous board
        # (dogs + cats + horses) pull candidates for every theme, not just
        # whichever two words happened to rank highest.
        return " | ".join(kws[:8])

    # BROAD: any keyword can match — widest possible net.
    return " | ".join(kws[:10])


def count_matched_keywords(candidate_text_fields: list[str], keywords: list[str]) -> int:
    """How many DISTINCT extracted keywords actually appear in this candidate's
    text. This is the key relevance signal a pure OR-match ranking misses:
    ts_rank_cd rewards lexeme frequency/position but can't tell "this photo's
    single generic noun happens to overlap" from "this photo genuinely shares
    multiple themes with the board." Used to force multi-keyword matches
    (strong, contextual signal) to always outrank single-keyword matches
    (weak, coincidental-noun-overlap-prone — confirmed live: this is exactly
    how unrelated train/kitchen photos leaked into an animal-board's DIVERSE
    results, matching only via a single generic word like "wooden"/"trees").
    """
    tokens = set()
    for field in candidate_text_fields:
        tokens.update(tokenize(field or ""))
    return sum(1 for kw in keywords if kw in tokens)


# Small, deliberately narrow synonym expansion for domain terms real users
# type differently than how datasets/authors tag their own content. Confirmed
# live gap: real egocentric-robot-vision datasets get tagged "egocentric"/
# "egocentric-vision" by their authors (real Hub tags, see
# field_types.clean_hub_tags), but a user is just as likely to search "pov"
# or "first person" for the exact same content and get zero matches from
# pure keyword search, which has no notion of synonymy at all.
SYNONYM_GROUPS: list[set[str]] = [
    {"pov", "point of view", "first person", "first-person", "egocentric", "wearable camera", "head mounted camera"},
    {"robot", "robotic", "robotics", "robot arm", "manipulator", "humanoid"},
]


def expand_query_synonyms(query_text: str, max_extra_terms: int = 4) -> str:
    """If the (short) query text matches a known synonym group, OR in the
    other members so "pov" also finds content tagged/captioned "egocentric".
    Deliberately conservative: only touches short queries (<=4 words) so it
    never mangles a genuine natural-language search phrase, and only fires
    on a whole-word/whole-phrase match, never a substring.
    """
    q_lower = query_text.lower().strip()
    if not q_lower or len(q_lower.split()) > 4:
        return query_text
    padded = f" {q_lower} "
    extra: list[str] = []
    for group in SYNONYM_GROUPS:
        if any(padded == f" {term} " or f" {term} " in padded for term in group):
            for term in group:
                if term != q_lower and term not in extra:
                    extra.append(term)
    if not extra:
        return query_text
    extra = extra[:max_extra_terms]
    parts = [f'"{t}"' if " " in t else t for t in extra]
    return query_text + " OR " + " OR ".join(parts)


def build_search_query(free_text: str) -> str:
    """User-typed search box query -> websearch_to_tsquery is used directly by
    the API (Postgres parses natural phrasing itself); this helper is for
    programmatic (non-user) query construction only.
    """
    kws = tokenize(free_text)
    return " & ".join(kws) if kws else ""
