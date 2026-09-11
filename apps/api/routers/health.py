from fastapi import APIRouter, Response, status

from datacurate_core.config import settings
from datacurate_core.db import fetch_one

router = APIRouter()

REQUIRED_TABLES = (
    "dataset_requests",
    "dataset_request_files",
    "jobs",
    "agent_runs",
    "agent_events",
)
REQUIRED_REQUEST_COLUMNS = ("example_count", "seed", "origin_context")


@router.get("/health")
def health(response: Response):
    database_ok = False
    missing_tables: list[str] = []
    missing_columns: list[str] = []

    try:
        row = fetch_one(
            """
            select
              to_regclass('public.dataset_requests') is not null as dataset_requests,
              to_regclass('public.dataset_request_files') is not null as dataset_request_files,
              to_regclass('public.jobs') is not null as jobs,
              to_regclass('public.agent_runs') is not null as agent_runs,
              to_regclass('public.agent_events') is not null as agent_events,
              exists (
                select 1 from information_schema.columns
                where table_schema = 'public'
                  and table_name = 'dataset_requests'
                  and column_name = 'example_count'
              ) as dataset_requests_example_count,
              exists (
                select 1 from information_schema.columns
                where table_schema = 'public'
                  and table_name = 'dataset_requests'
                  and column_name = 'seed'
              ) as dataset_requests_seed,
              exists (
                select 1 from information_schema.columns
                where table_schema = 'public'
                  and table_name = 'dataset_requests'
                  and column_name = 'origin_context'
              ) as dataset_requests_origin_context
            """
        ) or {}
        database_ok = True
        missing_tables = [
            table
            for table in REQUIRED_TABLES
            if not row.get(table)
        ]
        missing_columns = [
            f"dataset_requests.{column}"
            for column in REQUIRED_REQUEST_COLUMNS
            if not row.get(f"dataset_requests_{column}")
        ]
    except Exception:  # noqa: BLE001
        database_ok = False

    schema_ok = database_ok and not missing_tables and not missing_columns
    agent_configured = bool(settings.orbitrage_api_key)
    if not database_ok or not schema_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if database_ok and schema_ok and agent_configured else "degraded",
        "database": database_ok,
        "schema": schema_ok,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "agent_configured": agent_configured,
    }
