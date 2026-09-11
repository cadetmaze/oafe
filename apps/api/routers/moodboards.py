"""Moodboards — the curation-as-query primitive. Fast, boring, synchronous.

Real per-owner ownership: every moodboard belongs to exactly one project (see
user_project.get_or_create_project). The owner is either a signed-in Supabase
user or — for the newer frontend, which has no sign-in screen — a device-scoped
guest id. Either way the endpoints verify the board is actually theirs, so a
stranger with a guessed/leaked moodboard id gets a 403, not someone else's
curated data.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from datacurate_core.auth import actor_user_id, get_actor
from datacurate_core.db import cursor, execute, fetch_all, fetch_one
from datacurate_core.models import SelectionStrategy
from datacurate_core.queue import enqueue

from serialize import row_to_asset
from user_project import get_or_create_project

router = APIRouter()


def _assert_owns_moodboard(moodboard_id: str, actor: dict) -> dict:
    board = fetch_one(
        """select m.*, p.user_id as owner_id, p.guest_id as owner_guest_id
           from moodboards m
           join projects p on p.id = m.project_id
           where m.id = %s""",
        (moodboard_id,),
    )
    if not board:
        raise HTTPException(404, "moodboard not found")
    # CONFIRMED LIVE (user-reported: "That's not yours to change" on their
    # OWN moodboards): psycopg returns a real `uuid.UUID` object for a uuid
    # column, while `user["id"]` is a plain `str` decoded from Supabase
    # Auth's JSON response. `UUID(x) != "x"` is ALWAYS True in Python, so
    # this check rejected everyone — including the rightful owner. Compare
    # as strings on both sides. (My original verification of this only
    # tested that a DIFFERENT user got 403, which passed for the wrong
    # reason; the owner's own access was never exercised until now.)
    owner = board["owner_guest_id"] if actor.get("kind") == "guest" else board["owner_id"]
    if not owner or str(owner) != str(actor["id"]):
        raise HTTPException(403, "not your moodboard")
    return board


@router.get("/moodboards")
def list_moodboards(actor: dict = Depends(get_actor)):
    """Real multi-device persistence: what moodboards does THIS user
    actually own, per the database — not whatever ids happen to be sitting
    in one browser's localStorage.

    Returns each board's real `asset_ids` too. CONFIRMED LIVE (user-reported
    "That's not yours to change"): the frontend used to build its moodboard
    list purely from localStorage, which survives sign-out and is shared
    across every account that ever used that browser — so it happily listed
    boards belonging to a DIFFERENT user, plus legacy pre-auth boards whose
    project has user_id IS NULL (27 of those exist in the real database from
    before auth was added). Clicking any of them sent a real id the backend
    correctly refused with 403. Handing back the authoritative per-user list
    (ids included, so the "is this asset already saved?" heart state stays
    correct without N+1 fetches) is what makes that whole bug class
    impossible — the client never has to guess what it owns.
    """
    rows = fetch_all(
        """select m.id, m.name, m.extracted_keywords, m.created_at,
                  coalesce(
                    (select array_agg(ma.asset_id::text order by ma.position nulls last, ma.added_at)
                     from moodboard_assets ma where ma.moodboard_id = m.id),
                    '{}'
                  ) as asset_ids
           from moodboards m
           where m.project_id = %s order by m.created_at desc""",
        (get_or_create_project(actor),),
    )
    for r in rows:
        r["id"] = str(r["id"])
        r["asset_count"] = len(r["asset_ids"])
    return {"moodboards": rows}


@router.post("/moodboards")
def create_moodboard(payload: dict, actor: dict = Depends(get_actor)):
    project_id = get_or_create_project(actor)
    name = payload.get("name", "Untitled board")
    row = fetch_one(
        "insert into moodboards (project_id, name) values (%s, %s) returning id",
        (project_id, name),
    )
    return {"id": str(row["id"]), "project_id": project_id, "name": name}


@router.delete("/moodboards/{moodboard_id}")
def delete_moodboard(moodboard_id: str, actor: dict = Depends(get_actor)):
    """Delete a whole board. `moodboard_assets` rows cascade (see the FK in
    0001_init.sql); the underlying assets themselves are shared corpus data
    and are never touched — only this user's curation of them goes away."""
    _assert_owns_moodboard(moodboard_id, actor)
    execute("delete from moodboards where id = %s", (moodboard_id,))
    return {"ok": True, "deleted": moodboard_id}


@router.patch("/moodboards/{moodboard_id}")
def rename_moodboard(moodboard_id: str, payload: dict, actor: dict = Depends(get_actor)):
    _assert_owns_moodboard(moodboard_id, actor)
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    execute("update moodboards set name = %s where id = %s", (name, moodboard_id))
    return {"ok": True, "id": moodboard_id, "name": name}


@router.get("/moodboards/{moodboard_id}")
def get_moodboard(moodboard_id: str, actor: dict = Depends(get_actor)):
    board = _assert_owns_moodboard(moodboard_id, actor)
    assets = fetch_all(
        """select a.id, a.modality, a.thumbnail_uri, a.preview_uri, a.content_uri, a.caption,
                  a.labels, a.tags, a.license, a.source_dataset, a.source_config, a.source_split,
                  a.source_row, a.width, a.height, a.duration, a.fps, ma.action, ma.position
           from moodboard_assets ma join assets a on a.id = ma.asset_id
           where ma.moodboard_id = %s order by ma.position nulls last, ma.added_at""",
        (moodboard_id,),
    )
    board["id"] = str(board["id"])
    board["project_id"] = str(board["project_id"])
    board.pop("owner_id", None)
    board["assets"] = [
        row_to_asset(
            a,
            extra={"action": a["action"], "position": a["position"]},
            include_metadata=False,  # list endpoint — see serialize.summarize_large_metadata
        )
        for a in assets
    ]
    return board


@router.post("/moodboards/{moodboard_id}/assets")
def add_assets(moodboard_id: str, payload: dict, actor: dict = Depends(get_actor)):
    _assert_owns_moodboard(moodboard_id, actor)
    asset_ids = payload.get("asset_ids", [])
    action = payload.get("action", "add")  # 'add' (curated pick) or 'like' (lighter signal)
    with cursor() as cur:
        for i, asset_id in enumerate(asset_ids):
            cur.execute(
                """insert into moodboard_assets (moodboard_id, asset_id, action, position)
                   values (%s, %s, %s, %s)
                   on conflict (moodboard_id, asset_id) do update set action = excluded.action""",
                (moodboard_id, asset_id, action, i),
            )
            cur.execute(
                "insert into asset_events (asset_id, event, context, user_id) values (%s, %s, %s, %s)",
                (asset_id, "save" if action == "add" else "like", "{}", actor_user_id(actor)),
            )
    return {"ok": True, "added": len(asset_ids)}


@router.delete("/moodboards/{moodboard_id}/assets/{asset_id}")
def remove_asset(moodboard_id: str, asset_id: str, actor: dict = Depends(get_actor)):
    _assert_owns_moodboard(moodboard_id, actor)
    execute("delete from moodboard_assets where moodboard_id = %s and asset_id = %s", (moodboard_id, asset_id))
    execute(
        "insert into asset_events (asset_id, event, user_id) values (%s, 'remove', %s)",
        (asset_id, actor_user_id(actor)),
    )
    return {"ok": True}


@router.post("/moodboards/{moodboard_id}/recommendations")
def request_recommendations(moodboard_id: str, payload: dict, actor: dict = Depends(get_actor)):
    """Async — validate + enqueue + return job_id immediately."""
    _assert_owns_moodboard(moodboard_id, actor)

    # CONFIRMED LIVE RISK (user-reported): 10,000 was high enough that a
    # single request could, downstream, cause the export worker to try to
    # fetch+cache real bytes for 10k assets (some real videos are tens of MB
    # each) into OUR Blob storage in one job — either from a careless click
    # or a deliberate attempt to run up our storage bill. 1,000 already
    # matches the frontend's own largest offered choice (see COUNTS in
    # expand-export-dialog.tsx); the export worker's own per-job
    # materialize cap (see export_worker.MAX_MATERIALIZE_COUNT) is the real
    # backstop regardless of what's requested here.
    count = min(int(payload.get("count", 100)), 1_000)
    strategy = payload.get("strategy", SelectionStrategy.DIVERSE.value)
    if strategy not in SelectionStrategy.__members__:
        raise HTTPException(400, f"unknown strategy {strategy}")

    job_id = enqueue("recommend", {
        "moodboard_id": moodboard_id,
        "count": count,
        "strategy": strategy,
        "filters": payload.get("filters", {}),
    })
    return {"job_id": job_id}
