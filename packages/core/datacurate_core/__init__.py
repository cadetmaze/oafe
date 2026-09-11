"""datacurate_core — shared library for the Dataset Curation Platform.

Modules:
  config       — env-driven settings
  db           — Postgres connection pool + helpers
  models       — DataAsset / RetrievalPlan / enums (the canonical shapes)
  field_types  — HF schema -> canonical FieldType inference
  hf_client    — Hugging Face Hub + datasets-server integration
  blob         — Azure Blob Storage wrapper (works against Azurite or real Azure)
  queue        — Postgres-backed job queue (pgmq-compatible semantics)
  thumbnails   — image/video/audio thumbnail + fingerprint generation
  dedup        — perceptual-hash duplicate detection
  search       — keyword extraction + tsquery building (text-first retrieval)
"""

__version__ = "0.1.0"
