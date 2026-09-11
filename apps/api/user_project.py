"""Shared "find or create this user's project" helper — used by both
moodboards.py and datasets.py so a logged-in user has exactly one project
their moodboards/datasets live under (see the partial unique index
`projects_one_per_user_idx` in 0004_users_and_auth.sql). One atomic upsert on
both the new and already-existing paths; only inserts once per user, ever.
"""
from __future__ import annotations

from datacurate_core.db import fetch_one


def get_or_create_user_project(user: dict) -> str:
    row = fetch_one(
        """
        insert into projects (user_id, name) values (%s, %s)
        on conflict (user_id) where user_id is not null do update
          set user_id = excluded.user_id
        returning id
        """,
        (user["id"], f"{user.get('email') or 'My'} projects"),
    )
    return str(row["id"])


def get_or_create_project(actor: dict) -> str:
    """Same one-project-per-owner rule for a signed-out guest.

    A guest project has `user_id` NULL and `guest_id` set — `projects.user_id`
    is a FK to Supabase's `auth.users`, so a guest id could never live there.
    """
    if actor.get("kind") != "guest":
        return get_or_create_user_project(actor)
    row = fetch_one(
        """
        insert into projects (guest_id, name) values (%s, %s)
        on conflict (guest_id) where guest_id is not null do update
          set guest_id = excluded.guest_id
        returning id
        """,
        (actor["id"], "Guest projects"),
    )
    return str(row["id"])
