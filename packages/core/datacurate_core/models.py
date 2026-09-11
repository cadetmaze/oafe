"""Canonical shapes used across api/workers/frontend contract.

These mirror the blueprint's DataAsset / RetrievalPlan / SelectionStrategy
abstractions exactly — every subsystem (search, moodboard, export) speaks
these types.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class Modality(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"


class FieldType(str, Enum):
    """Canonical dataset field types — every export format maps from these."""
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    TEXT = "text"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    CATEGORICAL = "categorical"
    TIMESTAMP = "timestamp"
    JSON = "json"


class SelectionStrategy(str, Enum):
    """'Generate N' is a strategy, not a feature — new strategies are configs."""
    SIMILAR = "SIMILAR"          # strict: most keywords must match
    DIVERSE = "DIVERSE"          # core keywords required, rest optional, source-capped
    BROAD = "BROAD"              # any keyword matches — widest net
    HIGH_QUALITY = "HIGH_QUALITY"  # DIVERSE + higher resolution/quality floor


class AssetSource(BaseModel):
    provider: str = "huggingface"
    dataset: str
    config: str = "default"
    split: str = "train"
    revision: Optional[str] = None
    row_id: int


class AssetContent(BaseModel):
    uri: Optional[str] = None
    thumbnail_uri: Optional[str] = None
    preview_uri: Optional[str] = None
    mime_type: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[float] = None
    fps: Optional[float] = None
    sample_rate: Optional[int] = None


class DataAsset(BaseModel):
    """The universal curatable object — image, video, or audio."""
    asset_id: str
    modality: Modality
    source: AssetSource
    content: AssetContent
    caption: Optional[str] = None
    labels: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    license: Optional[str] = None
    quality_score: Optional[float] = None
    phash: Optional[int] = None


class DedupConfig(BaseModel):
    phash_max_hamming: int = 6
    same_source_unique: bool = True


class DiversityConfig(BaseModel):
    max_share_per_source: float = 0.25
    mode: str = "source_cap_interleave"  # no-embeddings diversity: cap + interleave


class RetrievalPlan(BaseModel):
    """The internal compiler target. Search, moodboard expansion, and (later)
    a natural-language builder all compile down to this one structure."""
    keywords: list[str] = Field(default_factory=list)
    raw_query: Optional[str] = None
    positive_asset_ids: list[str] = Field(default_factory=list)
    negative_asset_ids: list[str] = Field(default_factory=list)
    modality: Optional[list[Modality]] = None
    license_allow: Optional[list[str]] = None
    min_width: Optional[int] = None
    min_height: Optional[int] = None
    quality_min: Optional[float] = None
    dedup: DedupConfig = Field(default_factory=DedupConfig)
    diversity: DiversityConfig = Field(default_factory=DiversityConfig)
    strategy: SelectionStrategy = SelectionStrategy.DIVERSE
    limit: int = 100


HF_TYPE_TO_FIELD: dict[str, FieldType] = {
    "Image": FieldType.IMAGE,
    "Audio": FieldType.AUDIO,
    "Video": FieldType.VIDEO,
    "ClassLabel": FieldType.CATEGORICAL,
}

HF_DTYPE_TO_FIELD: dict[str, FieldType] = {
    "string": FieldType.TEXT,
    "int8": FieldType.INT, "int16": FieldType.INT, "int32": FieldType.INT, "int64": FieldType.INT,
    "float16": FieldType.FLOAT, "float32": FieldType.FLOAT, "float64": FieldType.FLOAT,
    "bool": FieldType.BOOL,
}
