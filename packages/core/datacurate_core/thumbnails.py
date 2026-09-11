"""High-quality thumbnail + fingerprint generation for images, video, audio.

Design goals from the blueprint:
  - Pinterest-quality previews: 512px WebP, good compression, fast to load.
  - No GPU, no ML models — Pillow + ffmpeg (CPU) only.
  - Every image/video keyframe gets a perceptual hash (dedup) + dominant color
    (cheap visual-diversity signal used since we have no embeddings).
"""
from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import requests
from PIL import Image

from .config import settings

log = logging.getLogger(__name__)

THUMBNAIL_SIZE = 512
THUMBNAIL_QUALITY = 85
_FFMPEG = shutil.which("ffmpeg")


def fetch_bytes(url: str, max_bytes: int = 50_000_000) -> bytes:
    resp = requests.get(url, timeout=settings.http_timeout_seconds, stream=True)
    resp.raise_for_status()
    chunks = []
    total = 0
    for chunk in resp.iter_content(chunk_size=65536):
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"asset exceeds {max_bytes} bytes, aborting fetch")
        chunks.append(chunk)
    return b"".join(chunks)


# ── Images ──────────────────────────────────────────────────────────────

def load_image(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")
    return img


def make_image_thumbnail(img: Image.Image, size: int = THUMBNAIL_SIZE, quality: int = THUMBNAIL_QUALITY) -> bytes:
    thumb = img.copy()
    thumb.thumbnail((size, size), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    thumb.save(buf, format="WEBP", quality=quality, method=4)
    return buf.getvalue()


def compute_phash(img: Image.Image) -> int:
    """Difference hash (dHash) — perceptual fingerprint using only PIL+numpy.
    No scipy dependency (heavy, and DCT-based pHash offers little extra value
    for our near-duplicate use case). 8x8 gradient -> 64-bit int, same
    hamming-distance dedup semantics as a 'real' pHash.
    """
    small = img.copy().convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    arr = np.asarray(small, dtype=np.int16)
    diff = arr[:, 1:] > arr[:, :-1]  # 8x8 bool grid
    bits = diff.flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return _to_signed_64(value)


def _to_signed_64(unsigned_value: int) -> int:
    """Postgres bigint is signed 64-bit; our hash is naturally unsigned 64-bit.
    Two's-complement wraparound preserves hamming-distance semantics exactly
    (XOR is unaffected by the sign reinterpretation) while fitting the column.
    """
    if unsigned_value >= 2 ** 63:
        return unsigned_value - 2 ** 64
    return unsigned_value


def dominant_color(img: Image.Image) -> str:
    small = img.copy().convert("RGB").resize((32, 32))
    arr = np.array(small).reshape(-1, 3)
    mean = arr.mean(axis=0).astype(int)
    return "#{:02x}{:02x}{:02x}".format(*mean)


def image_pipeline(raw: bytes) -> dict:
    """Full image processing pipeline -> thumbnail bytes + derived fields."""
    img = load_image(raw)
    w, h = img.size
    return {
        "thumbnail_bytes": make_image_thumbnail(img),
        "width": w,
        "height": h,
        "aspect_ratio": round(w / h, 4) if h else None,
        "phash": compute_phash(img),
        "color_dominant": dominant_color(img),
    }


# ── Video ───────────────────────────────────────────────────────────────

def video_pipeline(raw: bytes, suffix: str = ".mp4") -> dict:
    """Extract a keyframe (thumbnail) + short animated preview via ffmpeg.
    Falls back to a neutral placeholder if ffmpeg isn't available so ingestion
    never hard-fails on a missing binary — it just gets no visual preview.
    """
    if not _FFMPEG:
        log.warning("ffmpeg not found — skipping video thumbnail generation")
        return {"thumbnail_bytes": None, "preview_bytes": None, "width": None, "height": None,
                "duration": None, "fps": None, "phash": None, "aspect_ratio": None, "color_dominant": None}

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / f"src{suffix}"
        src.write_bytes(raw)
        thumb_path = Path(td) / "thumb.webp"
        preview_path = Path(td) / "preview.webp"

        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(src)],
            capture_output=True, text=True,
        )
        import json as _json
        duration = fps = width = height = None
        try:
            info = _json.loads(probe.stdout)
            vstream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
            if vstream:
                width, height = vstream.get("width"), vstream.get("height")
                if vstream.get("avg_frame_rate") and "/" in vstream["avg_frame_rate"]:
                    n, d = vstream["avg_frame_rate"].split("/")
                    fps = round(int(n) / int(d), 2) if int(d) else None
            duration = float(info.get("format", {}).get("duration", 0)) or None
        except Exception:  # noqa: BLE001
            pass

        seek = (duration or 1) * 0.25
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(seek), "-i", str(src), "-vframes", "1",
             "-vf", f"scale={THUMBNAIL_SIZE}:-1", str(thumb_path)],
            capture_output=True,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(seek), "-i", str(src), "-t", "2",
             "-vf", f"fps=4,scale={THUMBNAIL_SIZE}:-1", "-loop", "0", str(preview_path)],
            capture_output=True,
        )

        thumb_bytes = thumb_path.read_bytes() if thumb_path.exists() else None
        preview_bytes = preview_path.read_bytes() if preview_path.exists() else None

        phash = color = aspect = None
        if thumb_bytes:
            img = load_image(thumb_bytes)
            phash = compute_phash(img)
            color = dominant_color(img)
            aspect = round(img.width / img.height, 4) if img.height else None

        return {
            "thumbnail_bytes": thumb_bytes, "preview_bytes": preview_bytes,
            "width": width, "height": height, "duration": duration, "fps": fps,
            "phash": phash, "aspect_ratio": aspect, "color_dominant": color,
        }


# ── Audio ───────────────────────────────────────────────────────────────

def audio_pipeline(raw: bytes, suffix: str = ".wav", width: int = 800, height: int = 100) -> dict:
    """Waveform visualization + duration/sample_rate via ffmpeg."""
    if not _FFMPEG:
        log.warning("ffmpeg not found — skipping audio waveform generation")
        return {"thumbnail_bytes": None, "duration": None, "sample_rate": None}

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / f"src{suffix}"
        src.write_bytes(raw)

        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(src)],
            capture_output=True, text=True,
        )
        import json as _json
        duration = sample_rate = None
        try:
            info = _json.loads(probe.stdout)
            astream = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
            if astream:
                sample_rate = int(astream.get("sample_rate", 0)) or None
            duration = float(info.get("format", {}).get("duration", 0)) or None
        except Exception:  # noqa: BLE001
            pass

        pcm = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-ac", "1", "-filter:a", f"aresample={width * 4}",
             "-f", "s16le", "-"],
            capture_output=True,
        )
        samples = np.frombuffer(pcm.stdout, dtype=np.int16)
        thumbnail_bytes = None
        if samples.size:
            samples = samples.astype(np.float32)
            samples /= (np.max(np.abs(samples)) or 1)
            thumbnail_bytes = _draw_waveform(samples, width, height)

        return {"thumbnail_bytes": thumbnail_bytes, "duration": duration, "sample_rate": sample_rate}


def _draw_waveform(samples: np.ndarray, width: int, height: int) -> bytes:
    from PIL import ImageDraw
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    mid = height // 2
    bucket = max(1, len(samples) // width)
    for x in range(width):
        chunk = samples[x * bucket:(x + 1) * bucket]
        if chunk.size == 0:
            continue
        amp = float(np.max(np.abs(chunk)))
        y = int(amp * mid * 0.9)
        draw.line([(x, mid - y), (x, mid + y)], fill=(59, 130, 246))
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=90)
    return buf.getvalue()
