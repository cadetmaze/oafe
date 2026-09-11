"""Postgres-backed job queue — local stand-in for Supabase's `pgmq` extension.

Same semantics pgmq gives you: at-least-once delivery, visibility timeout,
safe concurrent workers via `FOR UPDATE SKIP LOCKED`. Swapping to real pgmq
(or Azure Service Bus) later only touches this file — workers call
`enqueue`/`dequeue`/`complete`/`fail`/`partial` and don't know the difference.
"""
from __future__ import annotations

import json
import socket
import uuid
from typing import Any, Optional

from .db import cursor


def enqueue_in_transaction(
    cur: Any,
    job_type: str,
    payload: dict[str, Any],
    created_by: str | None = None,
) -> str:
    """Insert a job using the caller's transaction.

    This is for workflows that must commit a job and its domain state
    together. Ordinary callers should use :func:`enqueue`.
    """
    cur.execute(
        """insert into jobs (type, status, input, created_by)
           values (%s, 'QUEUED', %s, %s) returning id""",
        (job_type, json.dumps(payload), created_by),
    )
    return str(cur.fetchone()["id"])


def enqueue(job_type: str, payload: dict[str, Any], created_by: str | None = None) -> str:
    with cursor() as cur:
        return enqueue_in_transaction(cur, job_type, payload, created_by)


def dequeue(job_type: str, lease_seconds: int = 300) -> Optional[dict]:
    """Claim one QUEUED or expired in-progress job of this type atomically."""
    worker_id = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
    with cursor() as cur:
        cur.execute(
            """
            update jobs set status = 'RUNNING',
                            locked_by = %(worker)s,
                            locked_until = now() + interval '%(lease)s seconds',
                            attempts = attempts + 1,
                            updated_at = now()
            where id = (
                select id from jobs
                where type = %(type)s
                  and (
                    status = 'QUEUED'
                    or (status in ('RUNNING', 'PARTIAL') and locked_until < now())
                  )
                order by created_at
                limit 1
                for update skip locked
            )
            returning *
            """,
            {"worker": worker_id, "lease": lease_seconds, "type": job_type},
        )
        return cur.fetchone()


def update_progress(job_id: str, progress: float, status: str = "RUNNING") -> None:
    with cursor() as cur:
        cur.execute(
            "update jobs set progress=%s, status=%s, updated_at=now() where id=%s",
            (progress, status, job_id),
        )


def complete(job_id: str, output: dict[str, Any] | None = None) -> None:
    with cursor() as cur:
        cur.execute(
            "update jobs set status='COMPLETED', progress=1.0, output=%s, updated_at=now() where id=%s",
            (json.dumps(output or {}), job_id),
        )


def fail(job_id: str, error: str) -> None:
    with cursor() as cur:
        cur.execute(
            "update jobs set status='FAILED', error=%s, updated_at=now() where id=%s",
            (error, job_id),
        )


def get_job(job_id: str) -> Optional[dict]:
    with cursor() as cur:
        cur.execute("select * from jobs where id=%s", (job_id,))
        return cur.fetchone()
