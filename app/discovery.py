"""Per-provider model-catalog discovery.

Each keyed provider has a ``/models`` endpoint that enumerates the
catalog accessible with a given API key. Rather than hard-coding
model IDs in ``KEYED_PROVIDERS`` (which go stale every few months as
vendors churn their rosters), we call the provider's listing endpoint
after a key is saved and cache the result in the key store.

Three distinct request shapes cover all 14 keyed providers:

* **openai-compatible** — ``GET {base_url}/models`` with
  ``Authorization: Bearer {key}``, response is OpenAI's list format
  (``{"object":"list","data":[{"id":"..."},...]}``). Used by Groq,
  Mistral, OpenRouter, Together, Cerebras, SambaNova, Hyperbolic,
  DeepSeek, xAI, AI21, Fireworks, and HuggingFace's router.
* **gemini** — ``GET .../v1beta/models?key=KEY``, response has
  ``models[]`` entries with ``name: "models/<id>"`` and a
  ``supportedGenerationMethods`` array we must filter by.
* **cohere** — ``GET api.cohere.com/v1/models?endpoint=chat`` with
  Bearer auth, response has ``models[]`` entries each with a ``name``
  and an ``endpoints`` array.

Every function in this module has the same shape: take an
``httpx.AsyncClient`` and credentials, return a sorted ``list[str]``
of chat-capable model IDs on success, or ``None`` on any failure
(network error, non-200 status, parse error, empty result). The
caller decides what to do with ``None`` — typically keep the
previously cached catalog or fall back to hard-coded defaults.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Chat-capable model filter.
#
# /models responses include audio, embedding, moderation, image, etc.
# models that don't belong in a chat-completion picker. We filter them
# out via substring match on the model ID. Conservative — we'd rather
# occasionally include a niche chat model than accidentally let a
# whisper-large-v3 into the UI.
# ---------------------------------------------------------------------------

_NON_CHAT_SUBSTRINGS = (
    # Audio
    "whisper", "tts", "audio", "speech", "transcribe",
    # Embeddings
    "embed", "embedding",
    # Image / video generation
    "dall-e", "dalle", "stable-diffusion", "imagen", "image-gen",
    "flux", "sd-xl", "sdxl",
    # Safety / rerank utilities
    "moderation", "guard", "rerank",
)

# A small allowlist of substrings that are OK despite partial overlap.
# "vision" by itself is common in multimodal chat models (e.g.
# llama-3.2-11b-vision-preview is a valid chat model that also takes
# images) — we don't want to filter those out.
_CHAT_OK_SUBSTRINGS = (
    "vision",
)


def is_chat_model(model_id: str) -> bool:
    """Return True if the given model ID looks like a chat/completion
    model (not audio, embeddings, moderation, image generation, etc.)."""
    m = model_id.lower()
    # Allowlist check first so "vision" in a chat model's name doesn't
    # get swept away by some future entry in _NON_CHAT_SUBSTRINGS.
    if any(ok in m for ok in _CHAT_OK_SUBSTRINGS):
        # Still drop if it's clearly an embedding/audio despite also
        # containing "vision". Unlikely but cheap to check.
        hard_drops = ("embed", "whisper", "tts", "rerank")
        if any(hd in m for hd in hard_drops):
            return False
        return True
    return not any(n in m for n in _NON_CHAT_SUBSTRINGS)


# ---------------------------------------------------------------------------
# Per-shape fetchers.
# ---------------------------------------------------------------------------

async def _fetch_openai_compatible(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    timeout: float,
) -> list[str] | None:
    """GET {base_url}/models returning OpenAI's list schema."""
    url = f"{base_url.rstrip('/')}/models"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept":        "application/json",
    }
    try:
        r = await client.get(url, headers=headers, timeout=timeout)
    except httpx.HTTPError as e:
        log.info("discovery_transport_err url=%s err=%s",
                 url, f"{type(e).__name__}: {e}")
        return None
    if r.status_code != 200:
        log.info("discovery_non200 url=%s status=%s",
                 url, r.status_code)
        return None
    try:
        data = r.json()
    except ValueError:
        log.info("discovery_parse_err url=%s body=%r", url, r.text[:200])
        return None
    entries = data.get("data") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        # Some providers wrap differently (e.g. some return a bare list).
        if isinstance(data, list):
            entries = data
        else:
            return None
    out: list[str] = []
    for m in entries:
        mid = m.get("id") if isinstance(m, dict) else None
        if not isinstance(mid, str):
            continue
        if not is_chat_model(mid):
            continue
        out.append(mid)
    return sorted(set(out))


async def _fetch_gemini(
    client: httpx.AsyncClient,
    api_key: str,
    timeout: float,
) -> list[str] | None:
    """GET generativelanguage.googleapis.com/v1beta/models?key=KEY

    Response shape:
        {"models": [
            {"name": "models/gemini-2.5-pro",
             "supportedGenerationMethods": ["generateContent",...]},
            ...
        ]}
    """
    url = "https://generativelanguage.googleapis.com/v1beta/models"
    try:
        r = await client.get(url, params={"key": api_key}, timeout=timeout)
    except httpx.HTTPError as e:
        log.info("discovery_transport_err url=%s err=%s",
                 url, f"{type(e).__name__}: {e}")
        return None
    if r.status_code != 200:
        log.info("discovery_non200 url=%s status=%s",
                 url, r.status_code)
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return None
    out: list[str] = []
    for m in models:
        if not isinstance(m, dict):
            continue
        name = m.get("name", "")
        methods = m.get("supportedGenerationMethods", [])
        if not isinstance(name, str) or not name.startswith("models/"):
            continue
        if not isinstance(methods, list) or "generateContent" not in methods:
            continue
        short = name[len("models/"):]
        if not is_chat_model(short):
            continue
        out.append(short)
    return sorted(set(out))


async def _fetch_cohere(
    client: httpx.AsyncClient,
    api_key: str,
    timeout: float,
) -> list[str] | None:
    """GET api.cohere.com/v1/models?endpoint=chat

    Pre-filters server-side via the ``endpoint=chat`` parameter so we
    only get chat-capable models back. Does not paginate — 100 models
    per page, and no provider ships near that many chat models.
    """
    url = "https://api.cohere.com/v1/models"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept":        "application/json",
    }
    try:
        r = await client.get(
            url, headers=headers,
            params={"endpoint": "chat", "page_size": 100},
            timeout=timeout,
        )
    except httpx.HTTPError as e:
        log.info("discovery_transport_err url=%s err=%s",
                 url, f"{type(e).__name__}: {e}")
        return None
    if r.status_code != 200:
        log.info("discovery_non200 url=%s status=%s",
                 url, r.status_code)
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return None
    out: list[str] = []
    for m in models:
        if not isinstance(m, dict):
            continue
        name = m.get("name")
        endpoints = m.get("endpoints", [])
        if not isinstance(name, str):
            continue
        if isinstance(endpoints, list) and "chat" not in endpoints:
            # Shouldn't happen since we filtered server-side, but belt-
            # and-braces in case the API changes.
            continue
        if not is_chat_model(name):
            continue
        out.append(name)
    return sorted(set(out))


# ---------------------------------------------------------------------------
# Dispatcher — the only symbol most callers need.
# ---------------------------------------------------------------------------

async def discover_models(
    client: httpx.AsyncClient,
    shape: str,
    base_url: str | None,
    api_key: str,
    timeout: float = 10.0,
) -> list[str] | None:
    """Call the right per-shape fetcher and return the filtered,
    sorted list of chat-capable model IDs, or None on any failure."""
    if shape == "openai-compatible" or shape == "hf-router":
        if not base_url:
            return None
        return await _fetch_openai_compatible(
            client, base_url, api_key, timeout,
        )
    if shape == "gemini":
        return await _fetch_gemini(client, api_key, timeout)
    if shape == "cohere":
        return await _fetch_cohere(client, api_key, timeout)
    log.info("discovery_unknown_shape shape=%s", shape)
    return None
