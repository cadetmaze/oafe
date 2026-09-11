"""FastAPI gateway — thin by design. Search is synchronous and fast (Postgres
full-text search, no ML inference here). Everything expensive (recommendations,
exports, deep ingestion) is enqueued and handed to workers; handlers only
validate + enqueue + return a job_id.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import assets, datasets, health, jobs, moodboards, requests, search

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Dataset Curation Platform API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(search.router)
app.include_router(assets.router)
app.include_router(moodboards.router)
app.include_router(jobs.router)
app.include_router(datasets.router)
app.include_router(requests.router)
