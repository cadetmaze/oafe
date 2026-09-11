"""Export worker — dataset version -> Parquet + manifest + README dataset card.

Modes:
  references   — URIs only, no bytes fetched. Fast, tiny.
  materialized — fetches/caches original bytes into Blob, packages them.
  push_hf      — materialized, then uploaded to the user's own HF namespace.
"""
from __future__ import annotations

import io
import json
import logging
import time
import zipfile
from collections import Counter
from datetime import datetime, timezone

import pyarrow.parquet as pq

from datacurate_core import agent_state, queue, unify
from datacurate_core.blob import sas_url, upload_bytes
from datacurate_core.config import settings
from datacurate_core.db import fetch_all
from datacurate_core.mediacache import get_or_cache_media, guess_ext

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("azure").setLevel(logging.WARNING)
log = logging.getLogger("export_worker")

# CONFIRMED LIVE RISK (user-reported: "our exporter downloads in bg, filling
# our storage"): materialized/push_hf mode used to unconditionally fetch and
# durably cache real bytes for EVERY record in a version with no ceiling.
# The recommend endpoint alone allows requesting up to 1,000 assets per
# moodboard (see moodboards.request_recommendations) — with no cap here, a
# single export request could try to fetch+store up to 1,000 real media
# files (real videos can be tens of MB each) into our Blob storage in one
# job, either from a careless click or a deliberate attempt to run up
# storage. These two caps bound that per JOB, not per file (the existing
# fetch_bytes(max_bytes=100_000_000) in mediacache/thumbnails already bounds
# any ONE file): once EITHER is hit, remaining records fall back to
# reference-only (their real, original content_uri — not re-hosted, but
# still a complete, valid, traceable row in the exported dataset, nothing
# silently dropped) instead of triggering a new fetch. Assets that were
# ALREADY cached from an earlier view/export are always free to include in
# full — these caps only gate NEW fetches.
MAX_NEW_MATERIALIZATIONS_PER_JOB = 300
MAX_NEW_MATERIALIZED_BYTES_PER_JOB = 2 * 1024 * 1024 * 1024  # 2 GiB


def process_job(job: dict) -> dict:
    payload = job["input"]
    version_id = payload["version_id"]
    mode = payload.get("mode", "references")
    want_zip = bool(payload.get("zip"))

    # hf_datasets.license is joined in because assets.license is never
    # populated at ingest — without this every export claims "unknown licence"
    # for rows whose dataset does declare one on the Hub.
    records = fetch_all(
        """select dr.position, dr.selection_meta, a.*, d.license as dataset_license
           from dataset_records dr
           join assets a on a.id = dr.asset_id
           left join hf_datasets d on d.repo_id = a.source_dataset
           where dr.version_id = %s order by dr.position""",
        (version_id,),
    )
    if not records:
        return {"error": "no records in this version"}

    # Built by the agent when a version spans several source vocabularies;
    # absent (and harmless) for hand-curated moodboard exports.
    label_map = payload.get("label_map") or {}

    log.info("job %s: exporting %d records, mode=%s, zip=%s", job["id"], len(records), mode, want_zip)

    rows = []
    media_bytes_for_zip: dict[str, bytes] = {}  # populated only if want_zip + materialized
    new_materializations = 0
    new_materialized_bytes = 0
    capped_count = 0
    for i, r in enumerate(records):
        media_uri = r["content_uri"]
        media_state = "reference"
        if mode in ("materialized", "push_hf"):
            already_cached = bool(r.get("cached_content_uri"))
            cap_hit = (
                new_materializations >= MAX_NEW_MATERIALIZATIONS_PER_JOB
                or new_materialized_bytes >= MAX_NEW_MATERIALIZED_BYTES_PER_JOB
            )
            if already_cached or not cap_hit:
                media_uri, raw = _materialize(r, return_bytes=want_zip)
                media_state = "materialized"
                if not already_cached:
                    new_materializations += 1
                    new_materialized_bytes += len(raw) if raw is not None else 0
                if want_zip and raw is not None:
                    ext = _guess_ext(r["mime_type"], r["modality"])
                    media_bytes_for_zip[f"media/{r['id']}{ext}"] = raw
            else:
                # Cap hit — this record falls back to a plain reference (its
                # real original content_uri) instead of triggering a new
                # fetch. Still a complete, valid, traceable row; just not
                # re-hosted in our own storage.
                capped_count += 1
        # NO INFORMATION LOSS (user-reported): a row of an agent-trajectory
        # dataset is a whole episode. Exporting only the single representative
        # image would throw away the other frames, the screen recording, and
        # every per-step field (actions / responses / rewards / accessibility
        # trees) that makes the episode worth training on. `unify.unified_row`
        # ships all of it, and keeps MIXED exports (many datasets with totally
        # different shapes in one version) consistent by carrying each row's
        # own real fields as JSON rather than forcing one rigid column set.
        r["_media_state"] = media_state
        rows.append(
            unify.unified_row(
                r,
                media_uri=media_uri,
                media_state=media_state,
                dataset_license=r.get("dataset_license"),
                label_map=label_map,
            )
        )
        if (i + 1) % 100 == 0:
            queue.update_progress(job["id"], (i + 1) / len(records), status="PARTIAL")
            log.info("job %s: materialized %d/%d", job["id"], i + 1, len(records))

    data_bytes = _write_parquet(rows)
    manifest_bytes = _write_manifest(records)
    readme_bytes = _write_readme(records, mode)

    prefix = f"{payload['dataset_id']}/v{payload['version']}"
    data_url = upload_bytes(settings.blob_container_exports, f"{prefix}/data.parquet", data_bytes,
                             "application/vnd.apache.parquet")
    manifest_url = upload_bytes(settings.blob_container_exports, f"{prefix}/manifest.parquet", manifest_bytes,
                                 "application/vnd.apache.parquet")
    readme_url = upload_bytes(settings.blob_container_exports, f"{prefix}/README.md", readme_bytes, "text/markdown")

    output = {
        "row_count": len(rows),
        "data_url": sas_url(settings.blob_container_exports, f"{prefix}/data.parquet"),
        "manifest_url": sas_url(settings.blob_container_exports, f"{prefix}/manifest.parquet"),
        "readme_url": sas_url(settings.blob_container_exports, f"{prefix}/README.md"),
        "mode": mode,
    }
    if mode in ("materialized", "push_hf"):
        output["materialized_count"] = new_materializations
        output["materialized_bytes"] = new_materialized_bytes
        if capped_count:
            # Transparent, not a silent surprise: tell the caller exactly
            # how many of their requested rows were capped down to
            # reference-only so this export didn't over-fill our storage.
            output["storage_cap_applied"] = True
            output["reference_only_count"] = capped_count
            log.warning("job %s: hit materialize cap — %d/%d records fell back to reference-only "
                        "(new_materializations=%d, new_bytes=%d)",
                        job["id"], capped_count, len(records), new_materializations, new_materialized_bytes)

    if want_zip:
        zip_bytes = _write_zip(data_bytes, manifest_bytes, readme_bytes, media_bytes_for_zip, label_map)
        upload_bytes(settings.blob_container_exports, f"{prefix}/bundle.zip", zip_bytes, "application/zip")
        output["zip_url"] = sas_url(settings.blob_container_exports, f"{prefix}/bundle.zip")
        output["zip_size_bytes"] = len(zip_bytes)

    if mode == "push_hf" and payload.get("hf_repo") and payload.get("hf_token"):
        output["hf_push"] = _push_to_hf(payload["hf_repo"], payload["hf_token"], data_bytes, readme_bytes)

    return output


def _write_zip(
    data_bytes: bytes,
    manifest_bytes: bytes,
    readme_bytes: bytes,
    media_files: dict[str, bytes],
    label_map: dict[str, str] | None = None,
) -> bytes:
    """Bundle everything into one .zip for a single-click download — the
    references/materialized modes still expose individual file links too,
    but most users just want one file to download and unpack."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("data.parquet", data_bytes)
        zf.writestr("manifest.parquet", manifest_bytes)
        zf.writestr("README.md", readme_bytes)
        if label_map:
            # Shipped so the canonical labels are auditable and reversible —
            # `labels` stays verbatim, and this says exactly what was folded.
            zf.writestr("label_map.json", json.dumps(label_map, indent=2, sort_keys=True))
        for name, raw in media_files.items():
            zf.writestr(name, raw)
    return buf.getvalue()


def _materialize(asset: dict, return_bytes: bool = False) -> tuple[str, bytes | None]:
    """Fetch-once-cache-forever, via the SAME shared cache the API's asset
    detail view uses (datacurate_core.mediacache) — one canonical
    implementation, not two that could silently drift apart. If the asset
    detail view already cached this asset (e.g. someone previewed it before
    curating), export gets a free cache hit here; if export runs first,
    detail view gets the free hit later.
    """
    return get_or_cache_media(asset, return_bytes=return_bytes)


_guess_ext = guess_ext  # kept as a local alias; call sites below reference _guess_ext


def _write_parquet(rows: list[dict]) -> bytes:
    # Declared schema, not inferred: a build spanning several sources will have
    # columns that are all-None for this particular selection, and pyarrow
    # would type those as `null` — giving two exports of the same "shape"
    # incompatible schemas. See datacurate_core.unify.
    buf = io.BytesIO()
    pq.write_table(unify.to_parquet_table(rows), buf)
    return buf.getvalue()


def _write_manifest(records: list[dict]) -> bytes:
    """Provenance manifest — every row traceable to its exact HF source."""
    rows = [
        unify.manifest_row(
            r,
            media_state=r.get("_media_state", "reference"),
            dataset_license=r.get("dataset_license"),
        )
        for r in records
    ]
    buf = io.BytesIO()
    pq.write_table(unify.to_parquet_table(rows, unify.MANIFEST_SCHEMA), buf)
    return buf.getvalue()


def _write_readme(records: list[dict], mode: str) -> bytes:
    licenses = Counter(
        unify.resolve_license(r, r.get("dataset_license"))[0] or "unknown" for r in records
    )
    modalities = Counter(r["modality"] for r in records)
    sources = Counter(r["source_dataset"] for r in records)
    lines = [
        "# Curated Dataset Export",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Export mode: `{mode}`",
        f"Total assets: **{len(records)}**",
        "",
        "## Modalities",
        *[f"- {m}: {c}" for m, c in modalities.items()],
        "",
        "## License breakdown",
        *[f"- {lic}: {c}" for lic, c in licenses.most_common()],
        "",
        # Stated outright rather than left to be inferred from the table: a
        # row whose licence we could not determine is NOT a permissive row,
        # and anyone redistributing this export needs to know how many there
        # are before they rely on it.
        f"**Rows with unknown license: {licenses.get('unknown', 0)}** — check these "
        "against their source dataset before redistribution.",
        "",
        "## Source datasets",
        *[f"- `{src}`: {c} assets" for src, c in sources.most_common()],
        "",
        "## Provenance",
        "Every row in `manifest.parquet` traces back to its exact source dataset, ",
        "config, split, and row index on Hugging Face, plus the selection method ",
        "(search / recommendation job / manual pick) that chose it.",
        "",
        "## Collection method",
        "Curated via a Pinterest-style moodboard workflow: a user selected seed ",
        "assets, the platform extracted keywords from their captions/labels, and ",
        "expanded the selection via full-text search, deduplication (perceptual ",
        "hashing), and source-diversity balancing — no image embeddings were used.",
    ]
    return "\n".join(lines).encode("utf-8")


def _push_to_hf(repo_id: str, token: str, data_bytes: bytes, readme_bytes: bytes) -> dict:
    try:
        from huggingface_hub import HfApi
        api = HfApi(token=token)
        api.create_repo(repo_id, repo_type="dataset", exist_ok=True)
        api.upload_file(path_or_fileobj=io.BytesIO(data_bytes), path_in_repo="data.parquet",
                         repo_id=repo_id, repo_type="dataset")
        api.upload_file(path_or_fileobj=io.BytesIO(readme_bytes), path_in_repo="README.md",
                         repo_id=repo_id, repo_type="dataset")
        return {"repo_id": repo_id, "url": f"https://huggingface.co/datasets/{repo_id}"}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


EXPORT_JOB_TYPES = ("export", "agent_export")


def _finish_job_safely(
    job: dict,
    *,
    output: dict | None = None,
    error: str | None = None,
) -> None:
    try:
        if job.get("type") == "agent_export":
            agent_state.finish_export_job(job["id"], output=output, error=error)
        elif error:
            queue.fail(job["id"], error)
        else:
            queue.complete(job["id"], output)
    except Exception:  # noqa: BLE001
        # For agent exports the job and run settle in one transaction. A
        # transient database failure leaves the leased job retriable instead
        # of committing a terminal job while the run stays EXPORTING.
        log.exception("could not finish export job %s", job["id"])


def run_poll():
    # `agent_export` is a separate type on purpose. The agent's exports carry a
    # label map and expect the declared unified schema, so they must only be
    # served by a worker running this code — a worker on an older build would
    # silently produce the old inferred-schema output instead.
    log.info("export_worker polling for %s jobs...", " and ".join(EXPORT_JOB_TYPES))
    while True:
        # A transient database problem must not kill the worker — see the same
        # guard in agent_worker.run_poll.
        try:
            job = next((j for j in (queue.dequeue(t) for t in EXPORT_JOB_TYPES) if j), None)
        except Exception as e:  # noqa: BLE001
            log.warning("could not poll for export jobs (%s); retrying", e)
            time.sleep(5)
            continue
        if not job:
            time.sleep(1)
            continue
        job["id"] = str(job["id"])
        log.info("picked up job %s", job["id"])
        try:
            output = process_job(job)
            if output.get("error"):
                raise RuntimeError(str(output["error"]))
        except Exception as e:  # noqa: BLE001
            log.exception("job %s failed", job["id"])
            _finish_job_safely(job, error=str(e))
        else:
            _finish_job_safely(job, output=output)


if __name__ == "__main__":
    run_poll()
