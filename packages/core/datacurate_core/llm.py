"""Orbitrage — the only LLM provider this project talks to.

OpenAI-compatible gateway, open-weight models. Two hard-won facts shape this
whole module, both confirmed live against the real API:

1. `gpt-oss-120b` does NOT honour `response_format={"type":"json_schema"}` —
   it happily emits prose before the JSON (observed: `Below is a{"modality":…`).
   Forcing a function call instead (`tools=[…]` + `tool_choice`) returns clean,
   parseable arguments every time. So every structured call goes through
   `call_tool()`; nothing in this codebase should use `response_format`.

2. Orbitrage's Tools Gateway runs tools server-side when a bare tool-name
   string is passed in `tools` — but `gpt-oss-120b` garbles the tool name
   (observed: `exa_search_orbitratejson`), so the call comes back as an
   unexecutable tool_call instead of an answer. `glm-4.7` drives it correctly,
   which is why web search uses a different model than everything else.

Failures here are never fatal: callers catch `ToolCallRefused` and fall back to
the keyword-only behaviour the platform had before any LLM was involved.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from typing import Any

import requests
from openai import OpenAI

from . import hf_cache
from .config import settings

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


class LLMUnavailable(RuntimeError):
    """No API key configured, or the gateway is unreachable after retries."""


class ToolCallRefused(RuntimeError):
    """The model answered, but not with the tool call we forced.

    Always recoverable — every caller has a deterministic fallback.
    """


_client: OpenAI | None = None


def is_configured() -> bool:
    return bool(settings.orbitrage_api_key)


def get_client() -> OpenAI:
    global _client
    if _client is None:
        if not settings.orbitrage_api_key:
            raise LLMUnavailable("ORBITRAGE_API_KEY is not set")
        _client = OpenAI(
            base_url=settings.orbitrage_base_url,
            api_key=settings.orbitrage_api_key,
            timeout=settings.llm_timeout_seconds,
            max_retries=0,  # we do our own backoff so failures stay observable
        )
    return _client


def loads_lenient(raw: str) -> dict:
    """Parse JSON that a model may have wrapped in prose or markdown fences.

    Forced function calls rarely need this, but vision models on this gateway
    do fence their output, and gpt-oss has been seen prefixing text.
    """
    if not raw:
        raise ValueError("empty response")
    text = _FENCE_RE.sub("", raw.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"no JSON object found in: {text[:200]!r}")
    return json.loads(text[start : end + 1])


def _complete(**kwargs) -> Any:
    """One chat completion with backoff. Raises LLMUnavailable when exhausted."""
    last: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return get_client().chat.completions.create(**kwargs)
        except LLMUnavailable:
            raise
        except Exception as e:  # noqa: BLE001 — SDK raises a wide family here
            last = e
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(min(2**attempt, 8))
                log.warning("orbitrage call failed (attempt %d): %s", attempt + 1, e)
    raise LLMUnavailable(f"orbitrage unreachable: {last}")


def _forced_tool_call(model: str, messages: list[dict], tool: dict, temperature: float) -> dict:
    resp = _complete(
        model=model,
        messages=messages,
        tools=[{"type": "function", "function": tool}],
        tool_choice={"type": "function", "function": {"name": tool["name"]}},
        temperature=temperature,
    )
    message = resp.choices[0].message
    calls = message.tool_calls or []
    if not calls:
        # The model answered in prose despite tool_choice. Salvage it if it
        # happens to contain the object; otherwise let the caller degrade.
        try:
            return loads_lenient(message.content or "")
        except ValueError as e:
            raise ToolCallRefused(f"{model} returned no tool call for {tool['name']}: {e}") from e
    # Deliberately ignore the returned name: we forced exactly one tool, and
    # some models on this gateway mangle the name they echo back.
    return loads_lenient(calls[0].function.arguments)


def call_tool(
    tool: dict,
    messages: list[dict],
    *,
    model: str | None = None,
    cache_ttl: int | None = None,
    temperature: float = 0.0,
) -> dict:
    """Force `tool` to be called and return its parsed arguments.

    `tool` is the bare OpenAI function schema (`{"name", "description",
    "parameters"}`) — this wraps it in the `{"type": "function", …}` envelope.
    """
    model = model or settings.orbitrage_model_plan
    ttl = settings.llm_cache_ttl_seconds if cache_ttl is None else cache_ttl

    def fetch() -> dict:
        return _forced_tool_call(model, messages, tool, temperature)

    if ttl <= 0:
        return fetch()
    key = hf_cache.make_key(
        "llm.tool", model, tool["name"], json.dumps(messages, sort_keys=True)
    )
    return hf_cache.cached_call(key, ttl, fetch)


def call_managed_search(prompt: str, *, model: str | None = None, tool: str | None = None) -> str:
    """Web search executed server-side by Orbitrage; returns the model's prose.

    Must run on `settings.orbitrage_model_search` — see the module docstring for
    why the planning model cannot do this. The caller is responsible for
    extracting structure from the prose and for validating anything it names:
    this path is the only one in the system that can invent facts.
    """
    model = model or settings.orbitrage_model_search
    tool = tool or settings.orbitrage_search_tool

    def fetch() -> str:
        resp = _complete(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            tools=[tool],
        )
        return resp.choices[0].message.content or ""

    key = hf_cache.make_key("llm.search", model, tool, prompt)
    return hf_cache.cached_call(key, settings.llm_cache_ttl_seconds, fetch)


_MAX_IMAGE_BYTES = 6_000_000
_MAGIC_MIME = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG", "image/png"),
    (b"GIF8", "image/gif"),
)


def _as_data_url(url: str) -> str | None:
    """Fetch an image and inline it as a base64 data URL.

    Required, not an optimization: the vision model on this gateway rejects
    plain HTTP image links outright with "Only inline image data URLs and S3
    URLs are supported", so our own blob URLs have to be fetched and embedded.
    """
    if url.startswith("data:"):
        return url
    try:
        resp = requests.get(url, timeout=settings.http_timeout_seconds, stream=True)
        resp.raise_for_status()
        raw = resp.raw.read(_MAX_IMAGE_BYTES + 1, decode_content=True)
    except Exception as e:  # noqa: BLE001 — a dead thumbnail just means no vote
        log.warning("could not inline image %s: %s", url[:120], e)
        return None
    if not raw or len(raw) > _MAX_IMAGE_BYTES:
        return None
    mime = "image/webp"  # our thumbnails; sniffed below when they aren't
    for magic, candidate in _MAGIC_MIME:
        if raw.startswith(magic):
            mime = candidate
            break
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def call_tool_with_images(
    tool: dict,
    text: str,
    image_urls: list[str],
    *,
    model: str | None = None,
    cache_ttl: int | None = None,
) -> dict:
    """Same forced-function-call contract, with images attached.

    Callers must pass durable thumbnail URLs from our own blob storage, never a
    Hugging Face `content_uri` — those are signed and expire in ~48-72h, and a
    measured 76% of stored ones were already dead.
    """
    model = model or settings.orbitrage_model_vision
    ttl = settings.llm_cache_ttl_seconds if cache_ttl is None else cache_ttl

    def fetch() -> dict:
        content: list[dict] = [{"type": "text", "text": text}]
        for url in image_urls:
            data_url = _as_data_url(url)
            if data_url:
                content.append({"type": "image_url", "image_url": {"url": data_url}})
        if len(content) == 1:
            raise ToolCallRefused("none of the images could be fetched")
        return _forced_tool_call(model, [{"role": "user", "content": content}], tool, 0.0)

    if ttl <= 0:
        return fetch()
    # Keyed on the URLs, never the inlined bytes — the base64 payload is
    # megabytes and would make the cache key useless.
    key = hf_cache.make_key("llm.vision", model, tool["name"], text, *image_urls)
    return hf_cache.cached_call(key, ttl, fetch)
