"""Datasets — versioned, provenance-complete curated outputs. Creating a
version is pure metadata (no bytes move); export is where bytes get touched,
and it's async.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from datacurate_core.auth import get_current_user
from datacurate_core.db import cursor, fetch_all, fetch_one
from datacurate_core.field_types import infer_schema_from_asset_rows
from datacurate_core.queue import enqueue

from user_project import get_or_create_user_project

router = APIRouter()


def _assert_owns_dataset(dataset_id: str, user: dict) -> None:
    row = fetch_one(
        """select p.user_id as owner_id from datasets d join projects p on p.id = d.project_id
           where d.id = %s""",
        (dataset_id,),
    )
    if not row:
        raise HTTPException(404, "dataset not found")
    # Same uuid-vs-str trap as _assert_owns_moodboard — psycopg hands back a
    # `uuid.UUID`, the token's user id is a `str`, and `UUID(x) != "x"` is
    # always True, which would 403 the rightful owner. Compare as strings.
    if str(row["owner_id"]) != str(user["id"]):
        raise HTTPException(403, "not your dataset")


@router.get("/datasets")
def list_datasets(user: dict = Depends(get_current_user)):
    rows = fetch_all(
        """select d.id, d.name, d.description, d.created_at,
                  (select max(version) from dataset_versions dv where dv.dataset_id = d.id) as latest_version
           from datasets d join projects p on p.id = d.project_id
           where p.user_id = %s order by d.created_at desc""",
        (user["id"],),
    )
    for r in rows:
        r["id"] = str(r["id"])
    return {"datasets": rows}


@router.post("/datasets")
def create_dataset(payload: dict, user: dict = Depends(get_current_user)):
    project_id = get_or_create_user_project(user)
    row = fetch_one(
        "insert into datasets (project_id, name, description) values (%s, %s, %s) returning id",
        (project_id, payload.get("name", "Untitled dataset"), payload.get("description")),
    )
    return {"id": str(row["id"]), "project_id": project_id}


@router.post("/datasets/{dataset_id}/versions")
def create_version(dataset_id: str, payload: dict, user: dict = Depends(get_current_user)):
    """
    { "asset_ids": [...] }  OR  { "job_id": "..." }  (pulls from recommendation_results)
    """
    _assert_owns_dataset(dataset_id, user)
    asset_ids = payload.get("asset_ids")
    selection_meta_by_asset: dict[str, dict] = {}

    if not asset_ids and payload.get("job_id"):
        rows = fetch_all(
            "select asset_id, rank, score from recommendation_results where job_id = %s order by rank",
            (payload["job_id"],),
        )
        asset_ids = [str(r["asset_id"]) for r in rows]
        selection_meta_by_asset = {
            str(r["asset_id"]): {"picked_by": payload["job_id"], "rank": r["rank"], "score": r["score"]}
            for r in rows
        }

    if not asset_ids:
        raise HTTPException(400, "asset_ids or job_id required")

    next_version = fetch_one(
        "select coalesce(max(version), 0) + 1 as v from dataset_versions where dataset_id = %s", (dataset_id,)
    )["v"]

    rows = fetch_all(
        f"""select id, modality, caption, labels, license, width, height, duration, metadata
            from assets where id = any(%s)""",
        (asset_ids,),
    )
    schema = infer_schema_from_asset_rows(rows)
    stats = _compute_stats(rows)

    version_row = fetch_one(
        """insert into dataset_versions (dataset_id, version, schema, stats, status)
           values (%s, %s, %s, %s, 'ready') returning id""",
        (dataset_id, next_version, json.dumps(schema), json.dumps(stats)),
    )
    version_id = str(version_row["id"])

    with cursor() as cur:
        for i, asset_id in enumerate(asset_ids):
            cur.execute(
                """insert into dataset_records (version_id, asset_id, position, selection_meta)
                   values (%s, %s, %s, %s) on conflict do nothing""",
                (version_id, asset_id, i, json.dumps(selection_meta_by_asset.get(asset_id, {}))),
            )

    return {"version_id": version_id, "version": next_version, "schema": schema, "stats": stats, "count": len(asset_ids)}


def _compute_stats(rows: list[dict]) -> dict:
    licenses: dict[str, int] = {}
    modalities: dict[str, int] = {}
    for r in rows:
        lic = r.get("license") or "unknown"
        licenses[lic] = licenses.get(lic, 0) + 1
        mod = r.get("modality") or "unknown"
        modalities[mod] = modalities.get(mod, 0) + 1
    return {"total": len(rows), "by_license": licenses, "by_modality": modalities}


@router.get("/datasets/{dataset_id}/versions/{version}")
def get_version(dataset_id: str, version: int, user: dict = Depends(get_current_user)):
    _assert_owns_dataset(dataset_id, user)
    v = fetch_one(
        "select * from dataset_versions where dataset_id = %s and version = %s", (dataset_id, version)
    )
    if not v:
        raise HTTPException(404, "version not found")
    v["id"] = str(v["id"])
    v["dataset_id"] = str(dataset_id)
    records = fetch_all(
        """select dr.position, dr.selection_meta, a.id, a.modality, a.thumbnail_uri, a.caption,
                  a.license, a.source_dataset
           from dataset_records dr join assets a on a.id = dr.asset_id
           where dr.version_id = %s order by dr.position""",
        (v["id"],),
    )
    for r in records:
        r["id"] = str(r["id"])
    v["records"] = records
    return v


@router.post("/datasets/{dataset_id}/versions/{version}/exports")
def export_version(dataset_id: str, version: int, payload: dict, user: dict = Depends(get_current_user)):
    _assert_owns_dataset(dataset_id, user)
    v = fetch_one(
        "select id from dataset_versions where dataset_id = %s and version = %s", (dataset_id, version)
    )
    if not v:
        raise HTTPException(404, "version not found")
    mode = payload.get("mode", "references")  # references | materialized | push_hf
    job_id = enqueue("export", {
        "version_id": str(v["id"]), "dataset_id": dataset_id, "version": version,
        "mode": mode, "hf_repo": payload.get("hf_repo"), "hf_token": payload.get("hf_token"),
        "zip": bool(payload.get("zip")),
    })
    return {"job_id": job_id}
