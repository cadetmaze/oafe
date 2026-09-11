"""Environment-driven configuration. Same code targets local docker-compose
infra (Postgres + Azurite) or real Supabase + Azure Blob — only env vars change.
"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    azure_storage_connection_string: str
    hf_token: str | None
    public_blob_host: str | None = None  # local-dev only, see blob._rewrite_public_host
    supabase_url: str | None = None       # e.g. https://<ref>.supabase.co — used to verify user JWTs
    supabase_anon_key: str | None = None
    blob_container_thumbnails: str = "thumbnails"
    blob_container_previews: str = "previews"
    blob_container_waveforms: str = "audio-waveforms"
    blob_container_cache: str = "cached-assets"
    blob_container_exports: str = "exports"

    # Supabase's session pooler caps the whole project at 15 clients, shared
    # by the API, every worker and the web app.
    db_pool_max: int = int(os.getenv("DB_POOL_MAX", "2"))

    # Ingestion tuning
    tier1_max_rows_per_dataset: int = int(os.getenv("TIER1_MAX_ROWS", "500"))
    rows_page_size: int = 100
    http_timeout_seconds: float = 15.0

    # Recommendation tuning
    candidate_multiplier: int = 25          # candidates = count * multiplier (capped)
    max_candidates: int = 20_000
    phash_hamming_threshold: int = 6
    max_share_per_source: float = 0.25       # diversity cap: no source > 25% of results
    batch_emit_size: int = 50

    # LLM — Orbitrage is the only provider (see llm.py). Two models, because
    # they fail at different things: gpt-oss-120b ignores json_schema (needs
    # forced function calls) and garbles Orbitrage's managed tool names, so
    # glm-4.7 is what drives server-side web search.
    orbitrage_api_key: str | None = None
    orbitrage_base_url: str = "https://api.orbitrage.ai/v1"
    orbitrage_model_plan: str = "gpt-oss-120b"
    orbitrage_model_search: str = "glm-4.7"
    orbitrage_model_vision: str = "qwen3-vl-235b-a22b-instruct"
    orbitrage_search_tool: str = "tavily_orbitrage"
    llm_cache_ttl_seconds: int = 86_400
    llm_timeout_seconds: float = 180.0

    # Agent tuning
    agent_probe_rows: int = 40               # crawl this much, then MEASURE the hit rate
    agent_judge_batch: int = 20              # rows per judging call
    agent_screen_batch: int = 10             # dataset profiles per screening call
    agent_judge_overshoot: float = 1.3       # stop judging at target * this
    agent_materialize_cap: int = 800         # rows fully indexed; the rest are references
    agent_vision_cap_max: int = 150
    agent_vision_cap_ratio: float = 0.15


def load_settings() -> Settings:
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/datacurate"
        ),
        azure_storage_connection_string=os.environ.get(
            "AZURE_STORAGE_CONNECTION_STRING",
            "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
            "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/"
            "K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;",
        ),
        hf_token=os.environ.get("HF_TOKEN") or None,
        public_blob_host=os.environ.get("PUBLIC_BLOB_HOST") or None,
        supabase_url=os.environ.get("SUPABASE_URL") or None,
        supabase_anon_key=os.environ.get("SUPABASE_ANON_KEY") or None,
        orbitrage_api_key=os.environ.get("ORBITRAGE_API_KEY") or None,
        orbitrage_base_url=os.environ.get("ORBITRAGE_BASE_URL")
        or "https://api.orbitrage.ai/v1",
        orbitrage_model_plan=os.environ.get("ORBITRAGE_MODEL_PLAN") or "gpt-oss-120b",
        orbitrage_model_search=os.environ.get("ORBITRAGE_MODEL_SEARCH") or "glm-4.7",
        orbitrage_model_vision=os.environ.get("ORBITRAGE_MODEL_VISION")
        or "qwen3-vl-235b-a22b-instruct",
        orbitrage_search_tool=os.environ.get("ORBITRAGE_SEARCH_TOOL") or "tavily_orbitrage",
    )


settings = load_settings()
