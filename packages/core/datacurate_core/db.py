"""Postgres connection pool. Targets local docker-compose Postgres today;
pointing DATABASE_URL at a Supabase project is the entire production migration.
"""
from __future__ import annotations

from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        # Sized against Supabase's session pooler, which allows only 15 clients
        # for the whole project — and that budget is shared by the API, four
        # workers and the web app. Ten per process oversubscribed it and real
        # queries started failing with EMAXCONNSESSION; these are short
        # transactional queries, so a small pool is plenty.
        # min_size=0 matters as much as the max: five workers each holding one
        # idle connection burns a third of the project's budget doing nothing.
        _pool = ConnectionPool(
            settings.database_url,
            min_size=0,
            max_size=settings.db_pool_max,
            max_idle=30.0,
            # Wait out contention rather than failing the whole job: the client
            # budget is shared, so a busy moment is normal and transient.
            timeout=90.0,
            open=True,
        )
    return _pool


@contextmanager
def cursor(commit: bool = True):
    """with cursor() as cur: cur.execute(...)"""
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            yield cur
        if commit:
            conn.commit()


def fetch_all(sql: str, params: tuple | dict = ()) -> list[dict]:
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(sql: str, params: tuple | dict = ()) -> dict | None:
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(sql: str, params: tuple | dict = ()) -> None:
    with cursor() as cur:
        cur.execute(sql, params)
