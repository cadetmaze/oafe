"""Turn an agent-trajectory row into step-indexed, UI-renderable data.

Modelled directly on the reference viewer in `dataset_large/` for
`markov-ai/computer-use`. The important insight from that implementation is
that the arrays on a row are ALIGNED BY INDEX:

    actions[i], screenshots[i], responses[i], accessibility_trees[i],
    exe_statuses[i], exe_outputs[i], exe_errors[i], rewards[i], dones[i]

so step `i` is a complete, self-contained record: "the exact screen state the
agent saw, the action it took, and what happened". That alignment is the whole
product — it's what lets a click on step N show the precise frame plus a
reticle at the action's coordinates, rather than a vague scrub through a video.

Coordinates: VERIFIED against the live corpus (60 sampled computer-use rows) —
every screenshot is 1920x1080 and every coordinate is in raw pixels, max
observed (1900, 1056). We also accept normalized 0..1 coordinates because
`computer-use-large`-style rows express clicks as `click(x=0.6068, y=0.5630)`.
Both are converted to PERCENTAGES here, server-side, so the UI never has to
know which convention a dataset used.
"""

from __future__ import annotations

import json
import re

# Aligned per-step arrays, in the order we prefer to show them.
STEP_ARRAY_KEYS = (
    "actions", "responses", "accessibility_trees",
    "exe_statuses", "exe_outputs", "exe_errors", "rewards", "dones",
)

DEFAULT_FRAME_W = 1920
DEFAULT_FRAME_H = 1080

# One task can log thousands of events; cap what we ship to the browser.
MAX_EVENT_STEPS = 600

# pyautogui.click(941, 90) / moveTo(...) / rightClick(...) - raw pixels
_PIXEL_XY = re.compile(
    r"(?:click|moveTo|rightClick|doubleClick|tripleClick|dragTo|mouseDown|mouseUp)"
    r"\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)",
    re.I,
)
# click(x=0.6068, y=0.5630, button="left") - keyword form, often normalized
_KW_XY = re.compile(r"x\s*=\s*(-?[\d.]+)\s*,\s*y\s*=\s*(-?[\d.]+)", re.I)
# Agent.type(coordinates=[941, 90], ...) - the `responses` convention
_COORD_LIST = re.compile(r"coordinates\s*=\s*\[\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)", re.I)

_KIND_PATTERNS = (
    ("DOUBLE_CLICK", re.compile(r"doubleClick|double_click", re.I)),
    ("RIGHT_CLICK", re.compile(r"rightClick|right_click|button\s*=\s*[\"']right", re.I)),
    ("DRAG", re.compile(r"dragTo|drag\(", re.I)),
    ("SCROLL", re.compile(r"scroll", re.I)),
    ("CLICK", re.compile(r"\bclick|mouseDown|mouseUp", re.I)),
    ("TYPE", re.compile(r"\.write\(|typewrite|\.type\(", re.I)),
    ("HOTKEY", re.compile(r"hotkey", re.I)),
    ("KEYBOARD", re.compile(r"\.press\(|keyDown|keyUp|press\(keys", re.I)),
    ("MOVE", re.compile(r"moveTo|MOUSE\s*MOVE", re.I)),
    ("WAIT", re.compile(r"\bWAIT\b|time\.sleep", re.I)),
    ("DONE", re.compile(r"\bDONE\b|\bFAIL\b", re.I)),
)


def classify_action(action: str) -> str:
    """Label a step so the UI can colour-code it (CLICK / KEYBOARD / ...)."""
    if not action or not isinstance(action, str):
        return "STEP"
    for kind, pattern in _KIND_PATTERNS:
        if pattern.search(action):
            return kind
    return "STEP"


def extract_point(*texts: str) -> dict | None:
    """Find the screen coordinate an action targets, as PERCENTAGES.

    Tries the action text first, then any fallback text (e.g. the model's
    `responses` entry, which uses `coordinates=[x, y]`) so a step still gets a
    reticle when the action string itself is coordinate-free.
    """
    for text in texts:
        if not text or not isinstance(text, str):
            continue
        for pattern in (_PIXEL_XY, _KW_XY, _COORD_LIST):
            m = pattern.search(text)
            if not m:
                continue
            try:
                x, y = float(m.group(1)), float(m.group(2))
            except (TypeError, ValueError):
                continue
            if x < 0 or y < 0:
                continue
            # Normalized (0..1) vs raw pixels. A value <= 1 for BOTH axes is
            # only ever normalized in practice: a real click at pixel (1, 1)
            # would be a corner artifact, not a genuine UI target.
            if x <= 1.0 and y <= 1.0:
                px, py = x * 100.0, y * 100.0
                raw_x, raw_y = round(x * DEFAULT_FRAME_W), round(y * DEFAULT_FRAME_H)
            else:
                px, py = x / DEFAULT_FRAME_W * 100.0, y / DEFAULT_FRAME_H * 100.0
                raw_x, raw_y = round(x), round(y)
            if not (0.0 <= px <= 100.0 and 0.0 <= py <= 100.0):
                continue
            return {"x_pct": round(px, 3), "y_pct": round(py, 3), "x": raw_x, "y": raw_y}
    return None


def _at(values, i):
    if isinstance(values, list) and i < len(values):
        return values[i]
    return None


def build_trajectory(metadata: dict, frames: list[str] | None = None) -> dict | None:
    """Build the full step-indexed trajectory for one episode row.

    `frames` (freshly re-resolved screenshot URLs) is passed in rather than
    read from metadata, because the stored ones are expiring signed URLs.
    Returns None when the row genuinely has no action trajectory, so callers
    can cleanly fall back to a plain frame gallery.
    """
    actions = metadata.get("actions")
    if not isinstance(actions, list) or not actions:
        return None

    frames = frames or []
    steps = []
    for i, action in enumerate(actions):
        action_text = action if isinstance(action, str) else str(action)
        response = _at(metadata.get("responses"), i)
        steps.append({
            "index": i,
            "action": action_text,
            "kind": classify_action(action_text),
            # Screenshots align by index; fall back to the last available
            # frame so a truncated gallery still renders something real
            # rather than a blank panel.
            "screenshot": frames[i] if i < len(frames) else (frames[-1] if frames else None),
            "screenshot_exact": i < len(frames),
            "point": extract_point(action_text, response if isinstance(response, str) else ""),
            "response": response,
            "exe_status": _at(metadata.get("exe_statuses"), i),
            "exe_output": _at(metadata.get("exe_outputs"), i),
            "exe_error": _at(metadata.get("exe_errors"), i),
            "reward": _at(metadata.get("rewards"), i),
            "done": _at(metadata.get("dones"), i),
            "accessibility_tree": _at(metadata.get("accessibility_trees"), i),
        })

    return {
        "instruction": metadata.get("instruction"),
        "domain": metadata.get("domain"),
        "task_id": metadata.get("task_id"),
        "score": metadata.get("score"),
        "num_steps": metadata.get("num_steps") or len(steps),
        "frame_width": DEFAULT_FRAME_W,
        "frame_height": DEFAULT_FRAME_H,
        "steps": steps,
    }


# ── DuckTrack-style raw input event logs (anaisleila/computer-use-data-psai) ──
# This dataset publishes no pre-built action list. What it has is `events`: a
# ~175 KB JSON log of raw OS input, plus `metadata` holding the screen size and
# the OBS recording start timestamp. Three facts drive the handling below,
# all verified against real rows:
#   1. ~88% of events are raw mouse `move` samples (1,938 of 2,196 in one row).
#      Showing those as steps would bury the 26 real clicks in noise.
#   2. The log is NOT time-ordered as published — a click at t+0.65s appears
#      after a window_focus at t+2.60s — so it must be sorted.
#   3. `time_stamp` is a monotonic clock, not video time. Subtracting
#      metadata.obs_record_state_timings.OBS_WEBSOCKET_OUTPUT_STARTED converts
#      it to seconds into the recording, which is what makes each step
#      genuinely seekable in the video.

_EVENT_KIND = {
    "click": "CLICK",
    "scroll": "SCROLL",
    "release": "KEYBOARD",
    "press": "KEYBOARD",
    "modifier_change": "HOTKEY",
    "window_focus": "WINDOW",
    "dom_snapshot": "SNAPSHOT",
    "move": "MOVE",
}

# Raw pointer movement is sampled continuously and is not a user intent.
NOISE_ACTIONS = {"move"}


def _obs_start(meta: dict) -> float | None:
    timings = (meta or {}).get("obs_record_state_timings") or {}
    started = timings.get("OBS_WEBSOCKET_OUTPUT_STARTED")
    if isinstance(started, list) and started:
        try:
            return float(started[0])
        except (TypeError, ValueError):
            return None
    return None


def _describe_event(e: dict) -> str:
    action = e.get("action")
    if action == "click":
        btn = e.get("button") or "left"
        state = "press" if e.get("pressed") else "release"
        return f'click(x={e.get("x")}, y={e.get("y")}, button="{btn}", {state})'
    if action == "scroll":
        return f'scroll(x={e.get("x")}, y={e.get("y")}, dy={e.get("dy", "")})'.replace(", dy=)", ")")
    if action in ("release", "press"):
        return f'key({e.get("name")!r})'
    if action == "modifier_change":
        return f'modifier({e.get("name") or e.get("modifiers") or ""})'
    if action == "window_focus":
        return f'focus({e.get("app_name") or ""}: {(e.get("window_title") or "")[:70]})'
    if action == "dom_snapshot":
        return "dom_snapshot()"
    return str(action)


def build_event_trajectory(metadata: dict, frames: list[str] | None = None) -> dict | None:
    """Build a step list from a raw input-event log, with video timestamps."""
    raw_events = metadata.get("events")
    if isinstance(raw_events, str):
        try:
            raw_events = json.loads(raw_events)
        except (ValueError, TypeError):
            return None
    if not isinstance(raw_events, list) or not raw_events:
        return None

    inner = metadata.get("metadata")
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except (ValueError, TypeError):
            inner = {}
    inner = inner or {}

    width = int(inner.get("screen_width") or DEFAULT_FRAME_W)
    height = int(inner.get("screen_height") or DEFAULT_FRAME_H)
    origin = _obs_start(inner)

    kept = [e for e in raw_events
            if isinstance(e, dict) and e.get("action") not in NOISE_ACTIONS]
    kept.sort(key=lambda e: e.get("time_stamp") or 0.0)
    if not kept:
        return None

    if origin is None:
        stamps = [e.get("time_stamp") for e in kept if isinstance(e.get("time_stamp"), (int, float))]
        origin = min(stamps) if stamps else 0.0

    span = max(((e.get("time_stamp") or origin) - origin) for e in kept) or 1.0
    frames = frames or []

    steps = []
    for i, e in enumerate(kept[:MAX_EVENT_STEPS]):
        ts = e.get("time_stamp")
        t = round(float(ts) - origin, 3) if isinstance(ts, (int, float)) else None
        x, y = e.get("x"), e.get("y")
        point = None
        if isinstance(x, (int, float)) and isinstance(y, (int, float)) and x >= 0 and y >= 0:
            point = {
                "x_pct": round(x / width * 100.0, 3),
                "y_pct": round(y / height * 100.0, 3),
                "x": int(x), "y": int(y),
            }
        # No per-event screenshots exist; map each step onto the nearest of the
        # sampled frames by its position in time so the stage still tracks.
        shot = None
        if frames and t is not None and span:
            idx = min(len(frames) - 1, max(0, int(t / span * len(frames))))
            shot = frames[idx]
        steps.append({
            "index": i,
            "action": _describe_event(e),
            "kind": _EVENT_KIND.get(e.get("action"), "STEP"),
            "screenshot": shot,
            "screenshot_exact": False,
            "point": point,
            "video_seconds": t,
            "response": e.get("window_title") or e.get("app_name"),
            "exe_status": None, "exe_output": None, "exe_error": None,
            "reward": None, "done": None,
            "accessibility_tree": e.get("accessibility_tree"),
        })

    return {
        "instruction": metadata.get("task_name"),
        "domain": metadata.get("application_website") or metadata.get("category"),
        "task_id": metadata.get("taskId") or metadata.get("unique_data_id"),
        "score": None,
        "num_steps": len(steps),
        "frame_width": width,
        "frame_height": height,
        "duration_seconds": round(span, 2),
        "seekable": True,
        "facets": {k: metadata.get(k) for k in
                   ("category", "subCategory", "application_website", "appType",
                    "difficulty", "os", "requires_login", "benchmark", "tags")
                   if metadata.get(k)},
        "steps": steps,
    }
