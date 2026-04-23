"""Prompt mode — send a real user prompt to specific keyless models
and return structured responses.

This is a different execution path from the scheduler + fire-all. Those
paths pick a random benign prompt from ``prompts.py`` and treat any
response as success (the point is flow generation, not content). The
prompt mode here takes a user-supplied prompt, targets *specific*
model/provider pairs, and returns the full response body so the UI can
render it.

Only keyless providers are exposed: Pollinations (text + image) and
DuckDuckGo AI Chat. No API keys are accepted from the UI; the scope is
intentionally narrow.
"""
from __future__ import annotations

import asyncio
import random
import time
import urllib.parse
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from .providers import BROWSER_UAS


# ---------------------------------------------------------------------------
# Model catalogue — the only keyless, prompt-capable targets.
# ---------------------------------------------------------------------------

# Each entry: (provider id, model id, kind)
# - provider id: "pollinations-text" | "pollinations-image" | "duckduckgo"
# - model id: the specific model within that provider
# - kind: "text" | "image"
PROMPT_TARGETS: list[dict[str, str]] = [
    # Pollinations text (6 upstream models via their gateway)
    {"provider": "pollinations-text", "model": "openai",  "kind": "text"},
    {"provider": "pollinations-text", "model": "mistral", "kind": "text"},
    {"provider": "pollinations-text", "model": "llama",   "kind": "text"},
    {"provider": "pollinations-text", "model": "claude",  "kind": "text"},
    {"provider": "pollinations-text", "model": "gemini",  "kind": "text"},
    {"provider": "pollinations-text", "model": "qwen",    "kind": "text"},
    # Pollinations image (3 Flux variants)
    {"provider": "pollinations-image", "model": "flux",         "kind": "image"},
    {"provider": "pollinations-image", "model": "flux-realism", "kind": "image"},
    {"provider": "pollinations-image", "model": "turbo",        "kind": "image"},
    # DuckDuckGo AI Chat (4 models via their duckchat gateway)
    {"provider": "duckduckgo", "model": "gpt-4o-mini",                                "kind": "text"},
    {"provider": "duckduckgo", "model": "claude-3-haiku-20240307",                    "kind": "text"},
    {"provider": "duckduckgo", "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",    "kind": "text"},
    {"provider": "duckduckgo", "model": "mistralai/Mistral-Small-24B-Instruct-2501",  "kind": "text"},
]


def target_id(provider: str, model: str) -> str:
    """Stable opaque id for (provider, model) pairs used by the UI."""
    return f"{provider}::{model}"


def targets_catalogue() -> list[dict[str, str]]:
    """Shape returned from GET /api/prompt/targets. Stable ids + display labels."""
    out: list[dict[str, str]] = []
    for t in PROMPT_TARGETS:
        out.append({
            "id": target_id(t["provider"], t["model"]),
            "provider": t["provider"],
            "model": t["model"],
            "kind": t["kind"],
            "label": _display_label(t["provider"], t["model"]),
        })
    return out


def _display_label(provider: str, model: str) -> str:
    if provider == "pollinations-text":
        return f"Pollinations-Text · {model}"
    if provider == "pollinations-image":
        return f"Pollinations-Image · {model}"
    if provider == "duckduckgo":
        # DDG model strings are long (e.g. "meta-llama/Llama-3.3-70B-Instruct-Turbo")
        short = model.split("/")[-1]
        return f"DuckDuckGo · {short}"
    return f"{provider} · {model}"


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------

@dataclass
class PromptResult:
    target_id: str
    provider: str
    model: str
    kind: str            # "text" | "image" | "error"
    label: str
    ok: bool
    status: int | None
    latency_ms: int
    url: str | None      # for images, this IS the image; for text, the source URL
    body: str | None     # text response or error message
    content_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Per-provider execution
# ---------------------------------------------------------------------------

async def _run_pollinations_text(
    client: httpx.AsyncClient, prompt: str, model: str,
) -> PromptResult:
    started = time.monotonic()
    url = f"https://text.pollinations.ai/{urllib.parse.quote(prompt)}"
    params = {"model": model}
    headers = {
        "User-Agent": random.choice(BROWSER_UAS),
        "Accept": "text/plain, */*",
    }
    tid = target_id("pollinations-text", model)
    label = _display_label("pollinations-text", model)
    try:
        r = await client.get(url, headers=headers, params=params)
        body = r.text if r.status_code == 200 else None
        return PromptResult(
            target_id=tid, provider="pollinations-text", model=model,
            kind="text", label=label,
            ok=r.status_code == 200, status=r.status_code,
            latency_ms=int((time.monotonic() - started) * 1000),
            url=str(r.url), body=_trim_text(body),
            content_type=r.headers.get("content-type"),
        )
    except httpx.HTTPError as e:
        return PromptResult(
            target_id=tid, provider="pollinations-text", model=model,
            kind="error", label=label,
            ok=False, status=None,
            latency_ms=int((time.monotonic() - started) * 1000),
            url=url, body=f"{type(e).__name__}: {e}",
        )


async def _run_pollinations_image(
    client: httpx.AsyncClient, prompt: str, model: str,
) -> PromptResult:
    started = time.monotonic()
    base = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}"
    params = {"model": model, "width": "512", "height": "512", "nologo": "true"}
    headers = {
        "User-Agent": random.choice(BROWSER_UAS),
        "Accept": "image/*",
    }
    tid = target_id("pollinations-image", model)
    label = _display_label("pollinations-image", model)
    # We issue the GET so the image is actually generated and Cato sees
    # the flow. We do NOT read the body (images can be ~200KB) — we just
    # return the final URL. Use HEAD-like behavior: stream and close.
    try:
        async with client.stream(
            "GET", base, headers=headers, params=params,
        ) as r:
            # Consume a small chunk to ensure TLS + first body bytes
            # actually flow (for Cato app-ID). Then abort.
            async for _chunk in r.aiter_bytes(1024):
                break
            final_url = str(r.url)
            status = r.status_code
            ctype = r.headers.get("content-type")
        return PromptResult(
            target_id=tid, provider="pollinations-image", model=model,
            kind="image", label=label,
            ok=status == 200, status=status,
            latency_ms=int((time.monotonic() - started) * 1000),
            url=final_url, body=None,
            content_type=ctype,
        )
    except httpx.HTTPError as e:
        return PromptResult(
            target_id=tid, provider="pollinations-image", model=model,
            kind="error", label=label,
            ok=False, status=None,
            latency_ms=int((time.monotonic() - started) * 1000),
            url=base, body=f"{type(e).__name__}: {e}",
        )


async def _run_duckduckgo(
    client: httpx.AsyncClient, prompt: str, model: str,
) -> PromptResult:
    started = time.monotonic()
    STATUS_URL = "https://duckduckgo.com/duckchat/v1/status"
    CHAT_URL = "https://duckduckgo.com/duckchat/v1/chat"
    ua = random.choice(BROWSER_UAS)
    tid = target_id("duckduckgo", model)
    label = _display_label("duckduckgo", model)

    def err(msg: str) -> PromptResult:
        return PromptResult(
            target_id=tid, provider="duckduckgo", model=model,
            kind="error", label=label,
            ok=False, status=None,
            latency_ms=int((time.monotonic() - started) * 1000),
            url=STATUS_URL, body=msg,
        )

    try:
        s = await client.get(
            STATUS_URL,
            headers={
                "User-Agent": ua, "Accept": "*/*",
                "x-vqd-accept": "1", "Cache-Control": "no-store",
            },
        )
    except httpx.HTTPError as e:
        return err(f"status handshake failed: {type(e).__name__}: {e}")

    vqd = s.headers.get("x-vqd-4") or s.headers.get("x-vqd-hash-1")
    if not vqd:
        return PromptResult(
            target_id=tid, provider="duckduckgo", model=model,
            kind="error", label=label,
            ok=False, status=s.status_code,
            latency_ms=int((time.monotonic() - started) * 1000),
            url=STATUS_URL,
            body=("no vqd token in status response — DDG likely rotated "
                  "their anti-abuse header scheme"),
        )

    try:
        r = await client.post(
            CHAT_URL,
            headers={
                "User-Agent": ua,
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
                "x-vqd-4": vqd,
                "Origin": "https://duckduckgo.com",
                "Referer": "https://duckduckgo.com/",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
    except httpx.HTTPError as e:
        return err(f"chat post failed: {type(e).__name__}: {e}")

    body = _parse_ddg_stream(r.text) if r.status_code == 200 else None
    return PromptResult(
        target_id=tid, provider="duckduckgo", model=model,
        kind="text", label=label,
        ok=r.status_code == 200, status=r.status_code,
        latency_ms=int((time.monotonic() - started) * 1000),
        url=CHAT_URL, body=_trim_text(body),
        content_type=r.headers.get("content-type"),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MAX_TEXT_BYTES = 8_000


def _trim_text(s: str | None) -> str | None:
    if s is None:
        return None
    if len(s) <= _MAX_TEXT_BYTES:
        return s
    return s[:_MAX_TEXT_BYTES] + "\n\n[... truncated]"


def _parse_ddg_stream(raw: str) -> str:
    """DDG returns a text/event-stream where each `data:` line is a JSON
    object like ``{"message": "Hello"}`` or the sentinel ``[DONE]``. We
    concatenate all ``message`` fields in order to get the full reply.
    """
    import json as _json
    chunks: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]" or not payload:
            continue
        try:
            obj = _json.loads(payload)
        except _json.JSONDecodeError:
            continue
        msg = obj.get("message")
        if isinstance(msg, str):
            chunks.append(msg)
    return "".join(chunks)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_target_id(tid: str) -> dict[str, str] | None:
    """Return the PROMPT_TARGETS entry matching this id, or None."""
    for t in PROMPT_TARGETS:
        if target_id(t["provider"], t["model"]) == tid:
            return t
    return None


async def run_prompt_target(
    client: httpx.AsyncClient,
    prompt: str,
    provider: str,
    model: str,
) -> PromptResult:
    """Dispatch to the right provider runner."""
    if provider == "pollinations-text":
        return await _run_pollinations_text(client, prompt, model)
    if provider == "pollinations-image":
        return await _run_pollinations_image(client, prompt, model)
    if provider == "duckduckgo":
        return await _run_duckduckgo(client, prompt, model)
    # Unknown — shouldn't happen because the endpoint validates first.
    return PromptResult(
        target_id=target_id(provider, model),
        provider=provider, model=model,
        kind="error",
        label=_display_label(provider, model),
        ok=False, status=None, latency_ms=0,
        url=None, body=f"unknown provider: {provider}",
    )
