"""HF schema inference — turns a dataset's `/info` `features` dict into:
  1. a canonical FieldType schema (for the dataset-builder UI)
  2. a detected "media column" (which column holds the image/video/audio)
  3. detected caption/text columns (our retrieval signal)

Real-world HF datasets are messy (verified against live datasets-server):
  - Some have a native typed column: {"_type": "Image"} / "Audio" / "Video"
    -> datasets-server /rows resolves these to a hosted `src` URL directly.
  - Some store media as a plain string column (a URL or a relative path)
    -> column name heuristics + value sniffing are needed.
  - Caption text can live in `caption`, `text`, `description`, `alt_text`,
    `transcription`, `raw_transcription` (audio), or be entirely absent
    (pure classification sets like CIFAR-10 — label becomes the caption).
"""
from __future__ import annotations

import json
import re
from typing import Any

from .models import FieldType, HF_DTYPE_TO_FIELD, HF_TYPE_TO_FIELD

MEDIA_NAME_HINTS = ("image", "img", "photo", "picture", "video", "clip", "audio", "wav", "sound", "path", "file", "url")
CAPTION_NAME_HINTS = (
    "caption", "text", "description", "desc", "alt_text", "alt", "title",
    "transcription", "raw_transcription", "summary", "prompt", "sentence",
    # Confirmed live (flowchart-dataset search returning nothing despite real
    # rows being indexed): plenty of real, richly-descriptive HF datasets
    # (QA/instruction-tuned, diagram-annotation, etc.) use column names that
    # don't contain any of the words above at all ("topic", "qa", "question",
    # "answer") even though the column IS exactly the descriptive text we want.
    "topic", "qa", "question", "answer", "instruction", "response",
)
LABEL_NAME_HINTS = ("label", "class", "category", "tag", "breed", "species", "genre")

# Nested (Sequence/List-of-dict) column shapes worth pulling text out of when
# NO scalar caption column exists at all. Chat/instruction-format datasets
# (confirmed live: a "conversations": [{"from":..., "value":...}] column,
# holding real descriptive text about the image) are common enough on the
# Hub to be worth this special case.
NESTED_TEXT_KEYS = ("value", "text", "content", "answer", "caption")


def _leaf_type(feature: dict[str, Any]) -> FieldType:
    """Map one HF feature node to a canonical FieldType."""
    if not isinstance(feature, dict):
        return FieldType.JSON
    t = feature.get("_type")
    if t in HF_TYPE_TO_FIELD:
        return HF_TYPE_TO_FIELD[t]
    if t == "Value":
        return HF_DTYPE_TO_FIELD.get(feature.get("dtype", ""), FieldType.TEXT)
    if t in ("Sequence", "List"):
        return FieldType.JSON
    # nested feature dict without an explicit _type marker (e.g. VQA-style blobs)
    return FieldType.JSON


def infer_schema(features: dict[str, Any]) -> dict[str, str]:
    """features: the dict from datasets-server /info response.
    Returns {column_name: FieldType.value}
    """
    return {name: _leaf_type(feat).value for name, feat in features.items()}


def detect_media_column(features: dict[str, Any]) -> tuple[str | None, str | None]:
    """Find which column holds the actual media.

    Returns (column_name, media_column_type) where media_column_type is one of:
      'native_image' | 'native_audio' | 'native_video'   (datasets-server resolves via /rows)
      'url_string' | 'path_string'                        (we must construct/resolve the URL ourselves)
      None, None if nothing found.
    """
    # 1. Prefer a native typed column — most reliable, datasets-server gives us a hosted `src`.
    for name, feat in features.items():
        t = feat.get("_type") if isinstance(feat, dict) else None
        if t == "Image":
            return name, "native_image"
        if t == "Audio":
            return name, "native_audio"
        if t == "Video":
            return name, "native_video"

    # 1b. A List/Sequence *of* a native media type — confirmed live on real
    # GUI-agent/computer-use datasets (markov-ai/computer-use,
    # anaisleila/computer-use-data-psai): a "screenshots" column holding
    # MULTIPLE images per row/task. Row values come back as a real Python
    # list of {src, width, height} dicts — _resolve_source's existing "take
    # the first if it's a list" defensive handling (originally added for
    # multi-variant Audio/Video columns) already does the right thing once
    # this is recognized as native_image/native_audio/native_video, so no
    # ingest-time logic change needed beyond detection.
    #
    # CONFIRMED LIVE (the actual reason this needed a second fix): the HF
    # `datasets` library's own Features JSON serialization represents a
    # Sequence/List-of-X as a BARE PYTHON LIST wrapping the inner type
    # (`[{"_type": "Image"}]`), NOT as `{"_type": "List", "feature": {...}}`
    # — that dict-shaped form only shows up in the *different* /rows-endpoint
    # feature listing, not the /info endpoint's dataset_info.features this
    # function actually receives. Handle both shapes so this is robust
    # either way a caller assembles `features`.
    for name, feat in features.items():
        inner = None
        if isinstance(feat, list) and len(feat) == 1:
            inner = feat[0]
        elif isinstance(feat, dict) and feat.get("_type") in ("List", "Sequence"):
            inner = feat.get("feature")
        inner_type = inner.get("_type") if isinstance(inner, dict) else None
        if inner_type == "Image":
            return name, "native_image"
        if inner_type == "Audio":
            return name, "native_audio"
        if inner_type == "Video":
            return name, "native_video"

    # 2. Fall back to a string column whose name hints at media.
    # ("screenshot"/"screen" added after confirming live that some GUI-agent
    # datasets' media column is genuinely named that way, e.g. a fallback
    # plain-string screenshot path with no native List(Image) typing.)
    string_cols = [
        name for name, feat in features.items()
        if isinstance(feat, dict) and feat.get("_type") == "Value" and feat.get("dtype") == "string"
    ]
    for name in string_cols:
        lname = name.lower()
        if any(h in lname for h in ("url", "image", "img", "photo", "video", "clip", "screenshot", "screen")):
            return name, "url_string"
    for name in string_cols:
        lname = name.lower()
        if any(h in lname for h in ("path", "file", "filename")):
            return name, "path_string"

    return None, None


def detect_caption_columns(features: dict[str, Any]) -> list[str]:
    """All string columns that look like they hold descriptive text."""
    out = []
    for name, feat in features.items():
        if not (isinstance(feat, dict) and feat.get("_type") == "Value" and feat.get("dtype") == "string"):
            continue
        lname = name.lower()
        if any(h in lname for h in CAPTION_NAME_HINTS):
            out.append(name)
    return out


def detect_label_columns(features: dict[str, Any]) -> list[str]:
    """Columns that look like categorical labels/tags (used when no caption exists).

    BUG FIXED (confirmed live): the old version matched on column NAME only
    ("label" substring), so a column literally called `label_segmentation_bitmap`
    — an *Image*-typed column holding a segmentation mask — was misidentified
    as a label. Downstream code then did `str(image_dict)` on it, leaking a
    raw signed HF URL into the caption/labels shown to users. Fix: name hints
    are now only a signal on top of an ACTUAL scalar-compatible type (ClassLabel,
    or a Value with a simple string/int/bool dtype) — Image/Audio/Video/
    Sequence/List columns can never be treated as labels, no matter their name.
    """
    out = []
    for name, feat in features.items():
        if not isinstance(feat, dict):
            continue
        t = feat.get("_type")
        is_classlabel = t == "ClassLabel"
        is_scalar_value = t == "Value" and feat.get("dtype") in (
            "string", "int8", "int16", "int32", "int64", "bool",
        )
        if not (is_classlabel or is_scalar_value):
            continue  # never Image/Audio/Video/Sequence/List, regardless of name
        lname = name.lower()
        if is_classlabel or any(h in lname for h in LABEL_NAME_HINTS):
            out.append(name)
    return out


def detect_nested_text_columns(features: dict[str, Any]) -> list[str]:
    """Sequence/List columns whose inner shape is a dict with a recognizable
    text-bearing key ("conversations": [{"from":..., "value":...}], etc).
    Only used as a FALLBACK when no scalar caption column was found at all
    (see detect_caption_columns) — a real caption column is always preferred.
    """
    out = []
    for name, feat in features.items():
        if not isinstance(feat, dict):
            continue
        if feat.get("_type") not in ("Sequence", "List"):
            # Some /info responses represent a list-of-dicts as a bare list
            # containing one dict describing the element shape, no "_type" key.
            if not isinstance(feat, list) or not feat or not isinstance(feat[0], dict):
                continue
            inner = feat[0]
        else:
            inner = feat.get("feature")
        if not isinstance(inner, dict):
            continue
        # inner is itself a {field_name: feature} mapping for struct-typed elements
        if any(k in inner for k in NESTED_TEXT_KEYS):
            out.append(name)
    return out


def extract_nested_text(value: Any, max_items: int = 6) -> str | None:
    """Pull readable text out of a conversations-style list-of-dicts row value.
    Defensive: only ever joins genuine scalar strings, never str()'s a dict/list.
    """
    if not isinstance(value, list):
        return None
    parts = []
    for item in value[:max_items]:
        if not isinstance(item, dict):
            continue
        for key in NESTED_TEXT_KEYS:
            v = item.get(key)
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
                break
    text = " ".join(parts).strip()
    return text or None


_PREDICTION_LIST_RE = re.compile(r"^\s*[\[{]")


def parse_prediction_list(value: str, prob_threshold: float = 0.15, max_labels: int = 5) -> list[str] | None:
    """Some HF datasets store multi-label classifier output as a JSON-ENCODED
    STRING in an otherwise-plain Value(dtype=string) column (no native
    Sequence/struct type used at all) — confirmed live:
    `classifier_yolo`/`classifier_clip-vit-base-patch32` columns on
    `jasperai/monet` hold literal text like
    '[{"name": "turkeys", "prob": 0.30...}, {"name": "peacock", ...}]'.

    Column-name/type checks alone can't catch this (it genuinely IS a
    string) — without this, that raw JSON blob was getting treated as a
    single opaque "label", which (a) is useless as a label and (b) once it
    became an extracted board keyword, broke `to_tsquery()` with a hard
    syntax error (the JSON punctuation isn't valid tsquery syntax).

    Returns clean label strings (e.g. ["turkeys", "peacock"]) if this parses
    as a list of {"name": ..., "prob": ...}-shaped dicts, else None — callers
    should then treat the original string as an opaque non-label rather than
    falling back to storing it raw.
    """
    if not isinstance(value, str) or not _PREDICTION_LIST_RE.match(value):
        return None
    try:
        data = json.loads(value)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list) or not data:
        return None
    out = []
    for item in data:
        if not isinstance(item, dict):
            return None  # not the shape we expect — bail rather than guess
        name = item.get("name") or item.get("label")
        if not isinstance(name, str):
            return None
        prob = item.get("prob") or item.get("score") or item.get("probability")
        try:
            prob = float(prob) if prob is not None else None
        except (TypeError, ValueError):
            prob = None
        if prob is None or prob >= prob_threshold:
            out.append(name)
    return out[:max_labels] if out else None


def looks_like_json_blob(value: str) -> bool:
    """Cheap pre-check: does this string look like it's actually serialized
    JSON rather than natural-language text? Used to stop the same class of
    bug (raw JSON leaking into a user-visible caption/label) from recurring
    in datasets we haven't specifically seen yet — confirmed live TWICE now:
    once for a `classifier_yolo` LABEL column (see parse_prediction_list),
    and again for a `qa` CAPTION column on a completely different dataset
    (`gankun/...`) once "qa" was added to CAPTION_NAME_HINTS — its value was
    a JSON-encoded list of {question, answer, explanation} dicts, and without
    this check it would've been dumped verbatim as the asset's "caption".
    """
    return isinstance(value, str) and bool(_PREDICTION_LIST_RE.match(value))


def extract_text_from_json_string(value: str, max_len: int = 600) -> str | None:
    """When a caption/text candidate column's value turns out to be a
    JSON-encoded string (not proper natural-language text), try to pull out
    something genuinely readable from it instead of either (a) dumping the
    raw JSON as a fake "caption" or (b) silently discarding real content.
    Handles the common shapes seen in the wild: a list of QA dicts
    (question/answer/explanation), a list of caption/label dicts, or a
    single dict with any of those keys. Returns None if nothing readable
    could be extracted — callers should then treat this column as empty,
    never fall back to the raw string.
    """
    if not looks_like_json_blob(value):
        return None
    try:
        data = json.loads(value)
    except (ValueError, TypeError):
        return None

    texts: list[str] = []
    text_keys = ("question", "answer", "caption", "text", "name", "value", "content", "topic", "title", "description")

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            for key in text_keys:
                v = node.get(key)
                if isinstance(v, str) and v.strip():
                    texts.append(v.strip())
        elif isinstance(node, list):
            for item in node[:10]:
                collect(item)

    collect(data)
    joined = " ".join(texts).strip()
    return joined[:max_len] if joined else None


# Hub tags are a mix of genuinely descriptive content signal ("egocentric",
# "robotics", "video-classification") and pure boilerplate metadata
# ("license:...", "library:...", "format:...", "region:us", "arxiv:...").
# Only the former is worth surfacing as a searchable per-asset tag.
_BOILERPLATE_TAG_PREFIXES = (
    "license:", "library:", "format:", "region:", "size_categories:",
    "arxiv:", "language:", "doi:", "modality:",  # modality already tracked as its own column
)


def clean_hub_tags(raw_tags: list[str], max_tags: int = 8) -> list[str]:
    """Strip boilerplate Hub tags down to genuinely descriptive content
    signal, e.g. `task_categories:video-classification` -> `video-
    classification`, keep `egocentric`/`robotics` as-is, drop
    `license:apache-2.0`/`library:datasets`/`region:us` entirely.
    """
    out = []
    for tag in raw_tags:
        if not isinstance(tag, str):
            continue
        if any(tag.startswith(p) for p in _BOILERPLATE_TAG_PREFIXES):
            continue
        if ":" in tag:
            tag = tag.split(":", 1)[1]  # task_categories:robotics -> robotics
        tag = tag.strip().lower()
        if tag and len(tag) <= 40:
            out.append(tag)
    return out[:max_tags]


def is_safe_scalar(value: Any) -> bool:
    """Runtime guard used at ingest time: only str/int/float/bool are ever
    safe to stringify into caption/labels. Defense in depth alongside the
    schema-level fix above — protects against any column whose declared type
    looks scalar but whose actual sampled value isn't (seen in the wild with
    inconsistent/malformed HF dataset schemas).
    """
    return isinstance(value, (str, int, float, bool)) and not isinstance(value, (dict, list))


DETECTION_NAME_HINTS = ("bbox", "bounding_box", "segmentation", "objects", "annotations", "polygon", "mask", "keypoints")


def classify_dataset_kind(features: dict[str, Any], caption_cols: list[str], label_cols: list[str]) -> str:
    """Coarse classification surfaced as a browse filter ("Captioned" /
    "Labeled" / "Detection & Segmentation" / "Other") so users can pick a
    coherent kind of data to curate instead of getting object-detection
    annotation blobs mixed in with plain photo captions.
    """
    image_type_columns = 0
    for name, feat in features.items():
        lname = name.lower()
        if not isinstance(feat, dict):
            continue
        if feat.get("_type") == "Image":
            image_type_columns += 1
        if any(h in lname for h in DETECTION_NAME_HINTS):
            return "detection_segmentation"
        if feat.get("_type") in ("Sequence", "List"):
            inner = feat.get("feature") if isinstance(feat.get("feature"), dict) else {}
            if any(h in lname for h in DETECTION_NAME_HINTS) or inner.get("_type") in ("Image",):
                return "detection_segmentation"
    if image_type_columns >= 2 and not caption_cols:
        # A second Image-typed column (confirmed live: `label` holding a
        # segmentation mask, named nothing detection-ish at all) is virtually
        # always a mask/heatmap paired with the main photo, never a genuine
        # second caption-worthy photo. Name-based hints alone missed this.
        #
        # BUT (also confirmed live, SingleBicycle/4KLSDB: hr+lr image pair +
        # real caption/cogvlm_caption columns) plenty of *captioned* datasets
        # also carry a second image column (paired before/after, hi-res/
        # lo-res, sketch/photo...). A genuine caption column is a stronger,
        # more useful signal than "has 2 image columns" — don't let this
        # heuristic steal captioned datasets into the segmentation bucket.
        return "detection_segmentation"
    if caption_cols:
        return "captioned"
    if label_cols:
        return "labeled"
    return "other"


def classlabel_names(features: dict[str, Any], column: str) -> list[str] | None:
    feat = features.get(column)
    if isinstance(feat, dict) and feat.get("_type") == "ClassLabel":
        return feat.get("names")
    return None


def infer_schema_from_asset_rows(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Dataset-builder schema inference at export/version time: looks at the
    canonical asset columns plus each asset's free-form `metadata` jsonb
    (the original HF columns we couldn't map to a fixed field) and infers a
    FieldType per key by sampling values across the selection.
    """
    schema: dict[str, str] = {}
    modalities = {r.get("modality") for r in rows if r.get("modality")}
    if modalities:
        schema["media"] = next(iter(modalities)) if len(modalities) == 1 else "json"
    if any(r.get("caption") for r in rows):
        schema["caption"] = FieldType.TEXT.value
    if any(r.get("labels") for r in rows):
        schema["labels"] = FieldType.JSON.value
    if any(r.get("license") for r in rows):
        schema["license"] = FieldType.CATEGORICAL.value
    if any(r.get("width") for r in rows):
        schema["width"] = FieldType.INT.value
        schema["height"] = FieldType.INT.value
    if any(r.get("duration") for r in rows):
        schema["duration"] = FieldType.FLOAT.value

    extra_keys: dict[str, set[type]] = {}
    for r in rows:
        meta = r.get("metadata") or {}
        if isinstance(meta, str):
            import json
            try:
                meta = json.loads(meta)
            except Exception:  # noqa: BLE001
                meta = {}
        for k, v in meta.items():
            extra_keys.setdefault(k, set()).add(type(v))

    for k, types in extra_keys.items():
        if types <= {int}:
            schema[k] = FieldType.INT.value
        elif types <= {int, float}:
            schema[k] = FieldType.FLOAT.value
        elif types <= {bool}:
            schema[k] = FieldType.BOOL.value
        elif types <= {str}:
            schema[k] = FieldType.TEXT.value
        else:
            schema[k] = FieldType.JSON.value

    return schema


# ── Rich multi-media / multi-field rows ────────────────────────────────────
# CONFIRMED LIVE (user-reported, with a full row dump of
# markov-ai/computer-use): ONE row of an agent-trajectory dataset is a whole
# episode — 233 screenshots, an .mp4 screen recording, 12 actions, 12 LLM
# reasoning blocks, 12 accessibility trees, per-step rewards/statuses, plus
# the natural-language `instruction` that gives the whole thing meaning.
# Ingest was keeping the FIRST screenshot and discarding everything else,
# which is both a huge information loss and the reason search couldn't find
# these rows: the searchable text (the instruction!) lived in fields we
# never indexed.

RECORDING_NAME_HINTS = ("recording", "video_file", "video_path", "clip", "movie", "mp4")
# Fields that are pure machine noise for a human reading/searching.
_TEXT_SKIP_HINTS = ("accessibility_tree", "_id", "index", "hash", "embedding", "hidden_state")
MAX_FRAMES_PER_ASSET = 120
MAX_RICH_TEXT_CHARS = 4_000


def collect_media_items(media_val: Any) -> list[dict]:
    """Every media item in a row's media column, not just the first.

    A list-of-Image column (see detect_media_column) is the whole point
    here: those 233 screenshots are the actual content of the episode.
    """
    if media_val is None:
        return []
    items = media_val if isinstance(media_val, list) else [media_val]
    out = []
    for item in items:
        if isinstance(item, dict) and item.get("src"):
            out.append({"src": item["src"], "width": item.get("width"), "height": item.get("height")})
    return out[:MAX_FRAMES_PER_ASSET]


def find_recording_in_values(values: dict) -> tuple[str, str] | None:
    """Find a screen-recording reference by looking at the ROW'S ACTUAL
    VALUES, not the declared schema.

    Deliberately value-driven: the schema we get from /info is cached
    (hf_cache, 6h TTL) and has been observed to disagree with what /rows
    actually returns, so a schema-only lookup can silently find nothing even
    when the row plainly contains `recordings/chrome/<uuid>.mp4`. Matching on
    the real value is immune to that entire class of drift.
    """
    for key, value in values.items():
        if not isinstance(value, str) or len(value) > 500:
            continue
        low = value.lower()
        if low.endswith((".mp4", ".webm", ".mov", ".mkv", ".avi")):
            return key, value
    return None


def collect_frames_from_values(values: dict, media_col: str | None) -> list[dict]:
    """Every frame in the row. Prefers the detected media column, but falls
    back to scanning ALL values for a list of {src,...} media dicts — same
    anti-drift reasoning as find_recording_in_values above.
    """
    frames = collect_media_items(values.get(media_col)) if media_col else []
    if len(frames) > 1:
        return frames
    best: list[dict] = []
    for value in values.values():
        if isinstance(value, list) and len(value) > len(best):
            candidate = collect_media_items(value)
            if len(candidate) > 1:
                best = candidate
    return best if len(best) > len(frames) else frames


def find_recording_column(features: dict[str, Any]) -> str | None:
    """A string column naming a video file inside the repo (e.g.
    `recording_path` -> 'recordings/chrome/<uuid>.mp4'). These never show up
    as a typed Video column, so nothing else in the pipeline would find them.
    """
    for name, feat in features.items():
        if not (isinstance(feat, dict) and feat.get("_type") == "Value" and feat.get("dtype") == "string"):
            continue
        if any(h in name.lower() for h in RECORDING_NAME_HINTS):
            return name
    return None


def build_rich_text(values: dict, limit: int = MAX_RICH_TEXT_CHARS) -> str:
    """All genuinely human-meaningful text in a row, flattened for indexing.

    This is what makes an agent-trajectory row findable at all: without it
    the row's only indexed text was a filename-ish caption, so searching the
    words that actually describe it ("chrome", "privacy", "analytics", the
    instruction text itself) matched nothing.
    """
    parts: list[str] = []
    for key, value in values.items():
        lname = key.lower()
        if any(h in lname for h in _TEXT_SKIP_HINTS):
            continue
        if isinstance(value, str):
            if not looks_like_json_blob(value):
                parts.append(value)
        elif isinstance(value, list):
            for item in value[:20]:
                if isinstance(item, str) and len(item) < 600 and not looks_like_json_blob(item):
                    parts.append(item)
        if sum(len(p) for p in parts) > limit:
            break
    text = " ".join(parts)
    return text[:limit]
