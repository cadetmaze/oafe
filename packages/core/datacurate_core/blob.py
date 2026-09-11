"""Azure Blob Storage wrapper. Works identically against Azurite (local dev)
or real Azure Storage — only the connection string changes. This is the only
place that talks to blob storage; nothing else in the codebase should.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    ContentSettings,
    PublicAccess,
    generate_blob_sas,
)

from .config import settings

# Thumbnails/previews/waveforms are meant to be served directly by a CDN in
# front of Blob (never proxied, never signed) — so those containers are
# public-read. Cached originals and exports stay private and are only ever
# handed out via time-limited SAS URLs (see sas_url()).
# "cached-assets" holds full-resolution copies of already-public HF media
# (fetch-once-cache-forever originals) — same trust level as thumbnails
# (public HF content, just re-hosted by us for speed/reliability), so it's
# fine to serve directly to a browser without a SAS token, same as those.
PUBLIC_CONTAINERS = {"thumbnails", "previews", "audio-waveforms", "cached-assets"}


@lru_cache(maxsize=1)
def _service_client() -> BlobServiceClient:
    return BlobServiceClient.from_connection_string(settings.azure_storage_connection_string)


_acl_checked: set[str] = set()


def _ensure_container(name: str):
    client = _service_client()
    container = client.get_container_client(name)
    access = PublicAccess.Blob if name in PUBLIC_CONTAINERS else None
    try:
        container.create_container(public_access=access)
        _acl_checked.add(name)
    except Exception:  # noqa: BLE001 — already exists
        # A container created before a name was added to PUBLIC_CONTAINERS
        # (confirmed live: "cached-assets" pre-dated this) keeps its OLD acl
        # forever otherwise — create_container() only sets acl at creation
        # time. Patch it once per process so old local-dev state self-heals.
        if name not in _acl_checked:
            _acl_checked.add(name)
            try:
                current = container.get_container_access_policy().get("public_access")
                if current != access:
                    container.set_container_access_policy(signed_identifiers={}, public_access=access)
            except Exception:  # noqa: BLE001
                pass
    return container


# Every blob we write lives at a deterministic, content-derived path (a
# content hash for cached originals, a stable dataset/config/split/row path
# for thumbnails) — the bytes at a given URL never change. So they're safe
# to cache in the browser essentially forever.
#
# CONFIRMED LIVE (user-reported "media loads after a few sec, feels stuck"):
# blobs were being written with NO Cache-Control header at all, so the
# browser re-downloaded every single thumbnail on every page load, every
# re-render and every back-navigation — measured ~1.7s per thumbnail from
# a cold connection, x24 in a grid. With immutable caching the browser
# serves repeats straight from disk (0ms, no request at all).
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"


def upload_bytes(
    container: str,
    blob_name: str,
    data: bytes,
    content_type: str = "application/octet-stream",
    cache_control: str | None = IMMUTABLE_CACHE_CONTROL,
) -> str:
    """Upload bytes, return the blob's URL (in prod this sits behind a CDN)."""
    c = _ensure_container(container)
    c.upload_blob(
        blob_name,
        data,
        overwrite=True,
        content_settings=ContentSettings(content_type=content_type, cache_control=cache_control),
    )
    return _rewrite_public_host(c.get_blob_client(blob_name).url)


def blob_exists(container: str, blob_name: str) -> bool:
    c = _ensure_container(container)
    return c.get_blob_client(blob_name).exists()


def download_bytes(container: str, blob_name: str) -> bytes:
    c = _ensure_container(container)
    return c.get_blob_client(blob_name).download_blob().readall()


def get_url(container: str, blob_name: str) -> str:
    c = _ensure_container(container)
    url = c.get_blob_client(blob_name).url
    return _rewrite_public_host(url)


def _rewrite_public_host(url: str) -> str:
    """Local-dev-only concern: containerized workers write blob using the
    Docker-internal hostname (e.g. 'azurite'), which the browser/host can't
    resolve. If PUBLIC_BLOB_HOST is set, rewrite stored URLs to it before
    handing them to a client. In production this is a no-op — Azure Blob/CDN
    has one public hostname everyone (workers, API, browser) already shares.
    """
    public_host = settings.public_blob_host
    if not public_host:
        return url
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(url)
    new_netloc = public_host if ":" in public_host else f"{public_host}:{parts.port or 10000}"
    return urlunsplit((parts.scheme, new_netloc, parts.path, parts.query, parts.fragment))


def internal_url(url: str) -> str:
    """The inverse of `_rewrite_public_host`, for server-side reads.

    Stored blob URLs are deliberately browser-reachable (PUBLIC_BLOB_HOST),
    but inside a worker container that host resolves to the container itself,
    so anything server-side that needs to READ its own blob — e.g. inlining a
    thumbnail for a vision model — must map it back to the internal endpoint
    first. A no-op in production, where one hostname serves everyone.
    """
    public_host = settings.public_blob_host
    if not public_host or not url:
        return url
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    if parts.hostname != public_host.split(":")[0]:
        return url
    internal = urlsplit(_service_client().url)
    return urlunsplit((internal.scheme, internal.netloc, parts.path, parts.query, parts.fragment))


def sas_url(container: str, blob_name: str, ttl_hours: int = 1) -> str:
    """Time-limited signed URL for exports — never proxy bytes through the API."""
    client = _service_client()
    try:
        sas = generate_blob_sas(
            account_name=client.account_name,
            container_name=container,
            blob_name=blob_name,
            account_key=client.credential.account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(hours=ttl_hours),
        )
        return f"{get_url(container, blob_name)}?{sas}"
    except Exception:
        # Some local emulator configs don't support SAS generation cleanly — fall back to plain URL.
        return get_url(container, blob_name)


def content_hash_name(url_or_key: str, ext: str = "") -> str:
    """Deterministic cache key from a STRING (usually a source URL) so a
    repeat request for the exact same URL is a fast existence-check, not a
    re-fetch. NOTE: this alone does not dedup two DIFFERENT URLs that
    happen to point at byte-identical content (e.g. a re-resolved/expired
    signed URL for the same underlying file, or two different asset rows
    that happen to reference the same real image) — see
    content_hash_name_from_bytes for that.
    """
    h = hashlib.sha256(url_or_key.encode("utf-8")).hexdigest()
    return f"{h}{ext}"


def content_hash_name_from_bytes(data: bytes, ext: str = "") -> str:
    """True content-addressable key, from the actual downloaded bytes — two
    different source URLs (or two different asset rows) that happen to
    resolve to byte-identical content always land on the SAME blob path.
    This is the real "never store the same bytes twice" guarantee; see
    mediacache.get_or_cache_media for how it's used as the canonical
    storage key (content_hash_name above is only ever a same-URL fast-path
    pre-check, never the final storage location).
    """
    h = hashlib.sha256(data).hexdigest()
    return f"{h}{ext}"
