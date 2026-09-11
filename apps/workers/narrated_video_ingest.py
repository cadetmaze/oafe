"""Ingest `markov-ai/computer-use-large`-shaped narrated-video datasets.

Why this exists separately from ingest_worker's /rows path: this dataset has
NO working dataset-server viewer (confirmed: HF returns a pyarrow boolean-mask
error for it). Its real content lives in the repo file tree:

    data/<domain>/<video_id>.mp4
    descriptions/<domain>/<video_id>_descriptions.json

and the JSON is a *narration*, not an action trajectory:

    {video_id, title, category, channel, total_segments, window_seconds,
     timing:{...}, segments:[{start, end, text, word_count, description}, ...]}

So there are no click coordinates here and we must not invent any — the
reference viewer is explicit about that. What this dataset DOES give us is
timestamped segments, which make the video seekable by meaning: click a
segment, jump to that second. That is the correct analogue of the trajectory
step list, and it's what the UI renders for these rows.

Domains include `autocad`/`blender`, which is why each asset carries its
domain as a real label — that's what routes this content into the CAD
collection without mislabelling the excel/photoshop/salesforce videos.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

import requests

from datacurate_core.config import settings
from datacurate_core.db import cursor

log = logging.getLogger("narrated_video_ingest")

HF_API = "https://huggingface.co/api/datasets"
HF_RESOLVE = "https://huggingface.co/datasets"

# Domain -> the topic labels that should surface it. `autocad`/`blender` are
# genuine CAD/3D authoring tools, so they belong under the CAD collection.
DOMAIN_LABELS = {
    "autocad": ["autocad", "cad", "cad tool use", "3d design"],
    "blender": ["blender", "cad", "3d design", "modeling"],
    "excel": ["excel", "spreadsheet", "productivity"],
    "photoshop": ["photoshop", "image editing", "design"],
    "salesforce": ["salesforce", "crm", "enterprise software"],
    "vscode": ["vscode", "code editor", "programming"],
}

# Videos above this are indexed and streamable but never pulled into our blob
# cache — a 400 MB AutoCAD screencast is real and worth showing, but caching
# hundreds of them would be a storage bill with no user benefit.
CACHEABLE_MAX_BYTES = 80 * 1024 * 1024


def _headers() -> dict:
    token = getattr(settings, "hf_token", None)
    return {"Authorization": f"Bearer {token}"} if token else {}


def _stable_row_id(video_id: str) -> int:
    """Deterministic surrogate row id.

    Must NOT use Python's builtin hash(): it is salted per-process by
    PYTHONHASHSEED, so re-running ingest produced a DIFFERENT source_row for
    the same video and the ON CONFLICT upsert inserted duplicates instead of
    updating (confirmed: 108 videos became 216 after one re-run).
    """
    return int(hashlib.sha1(video_id.encode()).hexdigest()[:12], 16) % 2_000_000_000


def _base_domain(domain: str) -> str:
    """`autocad_2` is a continuation shard of `autocad`, not a new tool."""
    return re.sub(r"_\d+$", "", domain)


def list_domains(repo: str) -> list[str]:
    r = requests.get(f"{HF_API}/{repo}/tree/main/data", headers=_headers(), timeout=60)
    r.raise_for_status()
    return [e["path"].split("/")[-1] for e in r.json() if e.get("type") == "directory"]


def list_videos(repo: str, domain: str, limit: int) -> list[dict]:
    out, cursor_param = [], ""
    while len(out) < limit:
        url = f"{HF_API}/{repo}/tree/main/data/{domain}?limit=100{cursor_param}"
        r = requests.get(url, headers=_headers(), timeout=60)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out.extend(e for e in batch if e.get("type") == "file" and e["path"].endswith(".mp4"))
        if len(batch) < 100:
            break
        cursor_param = f"&cursor={batch[-1].get('path')}"
        break  # one page per domain is plenty for curation; keeps ingest bounded
    return out[:limit]


def fetch_description(repo: str, domain: str, video_id: str) -> dict | None:
    url = f"{HF_RESOLVE}/{repo}/resolve/main/descriptions/{domain}/{video_id}_descriptions.json"
    try:
        r = requests.get(url, headers=_headers(), timeout=60)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:  # noqa: BLE001
        return None


def _useful(description: str | None) -> bool:
    """`NO_TASK` is the dataset's sentinel for "no task activity in this window"."""
    return bool(description) and description.strip().upper() != "NO_TASK"


def build_rich_text(desc: dict) -> str:
    """Everything genuinely searchable about this video, flattened.

    Segment `description` lines are the LLM's summary of what's happening on
    screen — the highest-signal text this dataset has — so they lead. The raw
    `text` is speech transcript and much noisier, so it only fills leftover
    budget.
    """
    parts = [desc.get("title") or "", desc.get("channel") or "", desc.get("category") or ""]
    segs = desc.get("segments") or []
    parts += [s.get("description", "") for s in segs if s.get("description")]
    parts += [s.get("text", "") for s in segs if s.get("text")]
    return " \n".join(p for p in parts if p)[:4000]


def ingest(repo: str = "markov-ai/computer-use-large",
           per_domain: int = 12,
           domains: list[str] | None = None) -> dict:
    stats = {"domains": 0, "videos": 0, "ingested": 0, "no_description": 0}
    all_domains = domains or list_domains(repo)

    with cursor() as cur:
        cur.execute(
            """insert into hf_datasets (repo_id, author, modalities, tags, curation_tier, dataset_kind,
                                        viewer_supported, media_column, media_column_type)
               values (%s, %s, %s, %s, 2, 'video', false, null, null)
               on conflict (repo_id) do update set curation_tier = 2""",
            (repo, repo.split("/")[0], ["video"], ["computer-use", "screencast", "narrated"]),
        )

    for domain in all_domains:
        base = _base_domain(domain)
        labels = DOMAIN_LABELS.get(base, [base])
        videos = list_videos(repo, domain, per_domain)
        stats["domains"] += 1

        for v in videos:
            stats["videos"] += 1
            video_id = v["path"].split("/")[-1].removesuffix(".mp4")
            size = int(v.get("size") or 0)
            desc = fetch_description(repo, domain, video_id)
            if not desc:
                stats["no_description"] += 1
                continue

            segments = desc.get("segments") or []
            video_url = f"{HF_RESOLVE}/{repo}/resolve/main/{v['path']}"
            title = desc.get("title") or video_id
            duration = max((s.get("end") or 0) for s in segments) if segments else None

            metadata = {
                "video_id": video_id,
                "title": title,
                "channel": desc.get("channel"),
                "category": desc.get("category") or base,
                "domain": base,
                "total_segments": desc.get("total_segments") or len(segments),
                "window_seconds": desc.get("window_seconds"),
                "duration_seconds": duration,
                "size_bytes": size,
                # Structural (`_`-prefixed) keys survive metadata summarisation.
                "_segments": [
                    {"start": s.get("start"), "end": s.get("end"),
                     "text": s.get("text"),
                     "description": s.get("description") if _useful(s.get("description")) else None,
                     "idle": not _useful(s.get("description"))}
                    for s in segments
                ],
                "_segment_count": len(segments),
                "_recording_uri": video_url,
                "_cacheable": size > 0 and size <= CACHEABLE_MAX_BYTES,
            }

            with cursor() as cur:
                cur.execute(
                    """insert into assets
                         (source_dataset, source_config, source_split, source_row,
                          modality, content_uri, mime_type, caption, description,
                          labels, tags, license, metadata, quality_score)
                       values (%s, 'default', 'train', %s, 'video', %s, 'video/mp4', %s, %s,
                               %s, %s, %s, %s, %s)
                       on conflict (source_dataset, source_config, source_split, source_row)
                       do update set caption = excluded.caption,
                                     description = excluded.description,
                                     labels = excluded.labels,
                                     tags = excluded.tags,
                                     metadata = excluded.metadata""",
                    (repo, _stable_row_id(video_id), video_url, title[:500],
                     build_rich_text(desc), labels,
                     ["computer-use", "screencast", base], "unknown",
                     json.dumps(metadata), 0.8),
                )
            stats["ingested"] += 1
        log.info("%s/%s: %s videos ingested", repo, domain, len(videos))

    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(json.dumps(ingest(), indent=2))


def generate_thumbnails(repo: str = "markov-ai/computer-use-large", limit: int = 200) -> dict:
    """Extract a poster frame per video so the grid isn't a wall of blanks.

    ffmpeg is given the REMOTE url with `-ss` before `-i`, so it issues HTTP
    range requests and pulls only the bytes around the seek point instead of
    downloading the file — which matters because some AutoCAD screencasts here
    are 400 MB. Seeks to 3s rather than 0s: the first frames of these videos
    are usually a black fade-in or an intro title card.
    """
    import subprocess, tempfile
    from pathlib import Path
    from datacurate_core.blob import upload_bytes, content_hash_name_from_bytes
    from datacurate_core.config import settings as cfg
    from datacurate_core.db import cursor as dbcur
    from datacurate_core.thumbnails import image_pipeline

    stats = {"considered": 0, "made": 0, "failed": 0}
    with dbcur() as cur:
        cur.execute("""select id, content_uri from assets
                       where source_dataset = %s and thumbnail_uri is null
                       limit %s""", (repo, limit))
        rows = cur.fetchall()

    for row in rows:
        aid, url = row["id"], row["content_uri"]
        stats["considered"] += 1
        try:
            with tempfile.TemporaryDirectory() as td:
                out = Path(td) / "f.jpg"
                proc = subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error",
                     "-ss", "3", "-i", url, "-frames:v", "1", "-q:v", "3", str(out)],
                    capture_output=True, timeout=180,
                )
                if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
                    # Retry at t=0: very short clips have no 3s mark.
                    proc = subprocess.run(
                        ["ffmpeg", "-y", "-loglevel", "error",
                         "-i", url, "-frames:v", "1", "-q:v", "3", str(out)],
                        capture_output=True, timeout=180,
                    )
                if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
                    stats["failed"] += 1
                    continue
                raw = out.read_bytes()

            out_fields = image_pipeline(raw)
            thumb = out_fields["thumbnail_bytes"]
            if not thumb:
                stats["failed"] += 1
                continue
            tname = content_hash_name_from_bytes(thumb, ext=".webp")
            turl = upload_bytes(cfg.blob_container_thumbnails, tname, thumb, "image/webp")
            with dbcur() as cur:
                cur.execute("""update assets set thumbnail_uri=%s,
                                      width=coalesce(width,%s), height=coalesce(height,%s),
                                      aspect_ratio=coalesce(aspect_ratio,%s),
                                      phash=coalesce(phash,%s),
                                      color_dominant=coalesce(color_dominant,%s)
                               where id=%s""",
                            (turl, out_fields["width"], out_fields["height"],
                             out_fields["aspect_ratio"], out_fields["phash"],
                             out_fields["color_dominant"], aid))
            stats["made"] += 1
        except Exception as e:  # noqa: BLE001
            stats["failed"] += 1
            log.warning("thumbnail failed for %s: %s", aid, e)
    log.info("generate_thumbnails(%s) -> %s", repo, stats)
    return stats
