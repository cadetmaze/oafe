"""Real user identity, verified against Supabase Auth — the SAME Supabase
project the app's database already lives in (see config.supabase_url).

Why call out to Supabase's Auth API instead of verifying the JWT locally:
this project's anon/service_role keys are signed HS256 (a shared secret we
were never given — only Supabase's own Auth service holds it), so the only
way to verify a user-issued access token without that secret is to ask
Supabase itself. `GET /auth/v1/user` does exactly that: valid token -> the
real user record, invalid/expired -> 401. This is the standard approach for
a self-hosted API in front of Supabase Auth when you don't have (and
shouldn't need) the raw JWT secret.

Speed: verifying on every single request would add a real network round
trip to every API call. Since a validated token can't become MORE valid
later, we cache a short-lived positive result in-process, keyed by the
token itself — a repeat request within the window is a plain dict lookup,
no network call. Cache is intentionally short (well under Supabase's
~1h token lifetime) so a revoked/expired token is never trusted for long.
"""
from __future__ import annotations

import time
import uuid

import requests
from fastapi import Header, HTTPException

from .config import settings

_CACHE_TTL_SECONDS = 120
_cache: dict[str, tuple[dict, float]] = {}


def _verify_token(token: str) -> dict | None:
    now = time.monotonic()
    cached = _cache.get(token)
    if cached and cached[1] > now:
        return cached[0]

    if not settings.supabase_url or not settings.supabase_anon_key:
        # No Supabase configured (e.g. plain local dev without auth wired up
        # yet) — fail closed, not open: nothing pretends to be authenticated.
        return None

    try:
        resp = requests.get(
            f"{settings.supabase_url}/auth/v1/user",
            headers={"Authorization": f"Bearer {token}", "apikey": settings.supabase_anon_key},
            timeout=settings.http_timeout_seconds,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        _cache.pop(token, None)
        return None

    data = resp.json()
    user = {"id": data["id"], "email": data.get("email")}
    _cache[token] = (user, now + _CACHE_TTL_SECONDS)
    return user


def _extract_token(authorization: str | None) -> str | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization[7:].strip() or None


def get_current_user(authorization: str | None = Header(None)) -> dict:
    """FastAPI dependency: require a real, currently-valid logged-in user.
    Raises 401 if there's no token or Supabase says it isn't valid.
    """
    token = _extract_token(authorization)
    user = _verify_token(token) if token else None
    if not user:
        raise HTTPException(401, "sign in required")
    return user


def get_optional_user(authorization: str | None = Header(None)) -> dict | None:
    """Same check, but returns None instead of raising — for endpoints (like
    search/feed) that behave the same for everyone but still want to know
    who's asking, e.g. for logging real per-user interaction events.
    """
    token = _extract_token(authorization)
    return _verify_token(token) if token else None


def _clean_guest_id(raw: str | None) -> str | None:
    """Accept only a well-formed UUID as a guest id.

    The value is client-supplied and becomes a durable ownership key, so it
    must not be free text — otherwise anyone could claim another guest's
    boards by guessing a short string, and the column would collect junk.
    """
    if not raw:
        return None
    try:
        return str(uuid.UUID(raw.strip()))
    except (ValueError, AttributeError):
        return None


def get_actor(
    authorization: str | None = Header(None),
    x_guest_id: str | None = Header(None),
) -> dict:
    """A real signed-in user if there is one, otherwise a device-scoped guest.

    The newer frontend has no sign-in screen, but moodboards still have to
    persist server-side rather than in localStorage (which is shared across
    accounts on one browser and doesn't survive a device change). A guest gets
    its own project, so ownership checks work identically for both kinds.

    `kind` matters downstream: `asset_events.user_id` is a FK to Supabase's
    `auth.users`, so a guest id must never be written there.
    """
    token = _extract_token(authorization)
    user = _verify_token(token) if token else None
    if user:
        return {"id": user["id"], "email": user.get("email"), "kind": "user"}
    guest = _clean_guest_id(x_guest_id)
    if guest:
        return {"id": guest, "email": None, "kind": "guest"}
    raise HTTPException(401, "sign in required")


def actor_user_id(actor: dict) -> str | None:
    """The value safe to store in a column that references auth.users."""
    return actor["id"] if actor.get("kind") == "user" else None
