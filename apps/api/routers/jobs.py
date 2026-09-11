"""Job status + progressive results. No websockets required for V1 — the
frontend polls this at ~1s intervals, which is indistinguishable from push at
this latency. (Swapping to Supabase Realtime later removes the poll entirely
without changing the job/result schema.)
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from datacurate_core.db import fetch_all
from datacurate_core.queue import get_job

from serialize import row_to_asset

router = APIRouter()


@router.get("/jobs/{job_id}")
def job_status(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    job["id"] = str(job["id"])
    if job.get("created_by"):
        job["created_by"] = str(job["created_by"])
    return job


@router.get("/jobs/{job_id}/results")
def job_results(job_id: str, since_batch: int = 0):
    rows = fetch_all(
        """select r.rank, r.score as rec_score, r.batch, r.reason,
                  a.id, a.modality, a.thumbnail_uri, a.preview_uri, a.content_uri,
                  a.caption, a.labels, a.tags, a.source_dataset, a.source_config,
                  a.source_split, a.source_row, a.license, a.width, a.height, a.duration, a.fps
           from recommendation_results r join assets a on a.id = r.asset_id
           where r.job_id = %s and r.batch >= %s
           order by r.rank""",
        (job_id, since_batch),
    )
    results = [
        row_to_asset(
            r,
            extra={"rank": r["rank"], "score": r["rec_score"], "batch": r["batch"], "reason": r["reason"]},
            include_metadata=False,  # list endpoint — see serialize.summarize_large_metadata
        )
        for r in rows
    ]
    return {"results": results, "count": len(results)}
