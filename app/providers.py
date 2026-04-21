"""Request providers.

Each provider represents one way of generating outbound traffic:

* ``WebProbe``        — browser-style GET to a public SaaS URL. Cato
  identifies these via SNI + Host, so even a simple GET is enough.
* ``ApiProbe``        — realistically shaped POST to a vendor API endpoint
  without credentials. The response is almost always 401/403, but the URL
  path, SDK-style User-Agent, and JSON body are the exact shape Cato's app
  signatures expect to see.
* ``PollinationsText`` — keyless real LLM completions via pollinations.ai.
* ``PollinationsImage``— keyless image generation via pollinations.ai.
* ``DuckDuckGoChat``  — keyless proxy to GPT-4o-mini / Claude Haiku / Llama
  / Mistral via duckduckgo.com/duckchat. Best effort; DDG rotates API
  shape occasionally, so failures are logged and swallowed.
"""
from __future__ import annotations

import abc
import random
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
import structlog

from . import prompts

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Realistic user-agent pools
# ---------------------------------------------------------------------------

BROWSER_UAS: list[str] = [
    # Recent-ish Chrome on Windows
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    # Recent-ish Chrome on macOS
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    # Firefox on Linux
    ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"),
    # Safari on macOS
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
     "(KHTML, like Gecko) Version/17.5 Safari/605.1.15"),
    # Edge on Windows
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"),
]

BROWSER_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,image/apng,*/*;q=0.8"
)
BROWSER_ACCEPT_LANG = "en-US,en;q=0.9"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ProviderResult:
    name: str
    category: str
    method: str
    url: str
    status_code: int | None
    ok: bool
    error: str | None = None
    response_snippet: str | None = None


# ---------------------------------------------------------------------------
# Base provider
# ---------------------------------------------------------------------------

class Provider(abc.ABC):
    name: str
    category: str

    @abc.abstractmethod
    async def execute(self, client: httpx.AsyncClient) -> ProviderResult:
        ...


# ---------------------------------------------------------------------------
# Web probe — simple browser-style GET
# ---------------------------------------------------------------------------

class WebProbe(Provider):
    def __init__(self, name: str, url: str, category: str = "chatbot_ui"):
        self.name = name
        self.url = url
        self.category = category

    async def execute(self, client: httpx.AsyncClient) -> ProviderResult:
        headers = {
            "User-Agent": random.choice(BROWSER_UAS),
            "Accept": BROWSER_ACCEPT,
            "Accept-Language": BROWSER_ACCEPT_LANG,
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
        }
        try:
            r = await client.get(self.url, headers=headers, follow_redirects=True)
            return ProviderResult(
                name=self.name,
                category=self.category,
                method="GET",
                url=str(r.url),
                status_code=r.status_code,
                ok=r.status_code < 500,
            )
        except httpx.HTTPError as e:
            return ProviderResult(
                name=self.name,
                category=self.category,
                method="GET",
                url=self.url,
                status_code=None,
                ok=False,
                error=f"{type(e).__name__}: {e}",
            )


# ---------------------------------------------------------------------------
# API probe — realistic unauthenticated POST (or GET) to a vendor endpoint
# ---------------------------------------------------------------------------

@dataclass
class ApiProbe(Provider):
    name: str
    url: str
    user_agent: str
    category: str = "llm_api"
    method: str = "POST"
    body_builder: Callable[[str], dict[str, Any]] | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    # We still send a fake Authorization so the request looks 'real' to
    # app-ID engines that key off the header's presence.
    send_fake_auth: bool = True

    async def execute(self, client: httpx.AsyncClient) -> ProviderResult:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        if self.send_fake_auth and "Authorization" not in headers:
            headers["Authorization"] = "Bearer sk-lab-sim-no-real-credential"

        body = None
        if self.body_builder is not None:
            prompt = random.choice(prompts.TEXT_PROMPTS)
            body = self.body_builder(prompt)

        try:
            if self.method == "POST":
                r = await client.post(self.url, headers=headers, json=body)
            else:
                r = await client.request(self.method, self.url, headers=headers)
            # For unauth probes, anything that isn't a transport error counts
            # as success — the whole point is that Cato saw the flow.
            return ProviderResult(
                name=self.name,
                category=self.category,
                method=self.method,
                url=str(r.url),
                status_code=r.status_code,
                ok=True,
            )
        except httpx.HTTPError as e:
            return ProviderResult(
                name=self.name,
                category=self.category,
                method=self.method,
                url=self.url,
                status_code=None,
                ok=False,
                error=f"{type(e).__name__}: {e}",
            )


# ---------------------------------------------------------------------------
# Pollinations — keyless real responses (text + image)
# ---------------------------------------------------------------------------

class PollinationsText(Provider):
    category = "real_response"
    MODELS = ["openai", "mistral", "llama", "claude", "gemini", "qwen"]

    def __init__(self) -> None:
        self.name = "Pollinations-Text"

    async def execute(self, client: httpx.AsyncClient) -> ProviderResult:
        prompt = random.choice(prompts.TEXT_PROMPTS)
        model = random.choice(self.MODELS)
        url = f"https://text.pollinations.ai/{urllib.parse.quote(prompt)}"
        params = {"model": model}
        headers = {
            "User-Agent": random.choice(BROWSER_UAS),
            "Accept": "text/plain, */*",
        }
        try:
            r = await client.get(url, headers=headers, params=params)
            snippet = r.text[:180] if r.status_code == 200 else None
            return ProviderResult(
                name=f"{self.name} ({model})",
                category=self.category,
                method="GET",
                url=str(r.url),
                status_code=r.status_code,
                ok=r.status_code == 200,
                response_snippet=snippet,
            )
        except httpx.HTTPError as e:
            return ProviderResult(
                name=self.name,
                category=self.category,
                method="GET",
                url=url,
                status_code=None,
                ok=False,
                error=f"{type(e).__name__}: {e}",
            )


class PollinationsImage(Provider):
    category = "real_response"
    MODELS = ["flux", "flux-realism", "turbo"]

    def __init__(self) -> None:
        self.name = "Pollinations-Image"

    async def execute(self, client: httpx.AsyncClient) -> ProviderResult:
        prompt = random.choice(prompts.IMAGE_PROMPTS)
        model = random.choice(self.MODELS)
        url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}"
        # Ask for a small image and suppress the logo to keep bandwidth sane.
        params = {"model": model, "width": "512", "height": "512", "nologo": "true"}
        headers = {
            "User-Agent": random.choice(BROWSER_UAS),
            "Accept": "image/*",
        }
        try:
            r = await client.get(url, headers=headers, params=params)
            snippet = None
            if r.status_code == 200:
                snippet = f"image/{r.headers.get('content-type', 'unknown')} {len(r.content)} bytes"
            return ProviderResult(
                name=f"{self.name} ({model})",
                category=self.category,
                method="GET",
                url=str(r.url),
                status_code=r.status_code,
                ok=r.status_code == 200,
                response_snippet=snippet,
            )
        except httpx.HTTPError as e:
            return ProviderResult(
                name=self.name,
                category=self.category,
                method="GET",
                url=url,
                status_code=None,
                ok=False,
                error=f"{type(e).__name__}: {e}",
            )


# ---------------------------------------------------------------------------
# DuckDuckGo AI Chat — keyless proxy to GPT-4o-mini / Claude / Llama / Mistral
# ---------------------------------------------------------------------------

class DuckDuckGoChat(Provider):
    category = "real_response"
    MODELS = [
        "gpt-4o-mini",
        "claude-3-haiku-20240307",
        "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "mistralai/Mistral-Small-24B-Instruct-2501",
    ]
    STATUS_URL = "https://duckduckgo.com/duckchat/v1/status"
    CHAT_URL = "https://duckduckgo.com/duckchat/v1/chat"

    def __init__(self) -> None:
        self.name = "DuckDuckGo-AIChat"

    async def execute(self, client: httpx.AsyncClient) -> ProviderResult:
        model = random.choice(self.MODELS)
        ua = random.choice(BROWSER_UAS)

        # Step 1: fetch VQD challenge token. DDG has tweaked this header
        # name a few times ("x-vqd-4", "x-vqd-hash-1"); we just grab
        # whatever looks like one.
        status_headers = {
            "User-Agent": ua,
            "Accept": "*/*",
            "x-vqd-accept": "1",
            "Cache-Control": "no-store",
        }
        try:
            s = await client.get(self.STATUS_URL, headers=status_headers)
        except httpx.HTTPError as e:
            return ProviderResult(
                name=self.name,
                category=self.category,
                method="GET",
                url=self.STATUS_URL,
                status_code=None,
                ok=False,
                error=f"status handshake failed: {type(e).__name__}: {e}",
            )

        vqd = s.headers.get("x-vqd-4") or s.headers.get("x-vqd-hash-1")
        if not vqd:
            return ProviderResult(
                name=self.name,
                category=self.category,
                method="GET",
                url=self.STATUS_URL,
                status_code=s.status_code,
                ok=False,
                error="no vqd token in status response",
            )

        # Step 2: chat POST.
        chat_headers = {
            "User-Agent": ua,
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "x-vqd-4": vqd,
            "Origin": "https://duckduckgo.com",
            "Referer": "https://duckduckgo.com/",
        }
        body = {
            "model": model,
            "messages": [
                {"role": "user", "content": random.choice(prompts.TEXT_PROMPTS)}
            ],
        }
        try:
            r = await client.post(self.CHAT_URL, headers=chat_headers, json=body)
            snippet = r.text[:180] if r.status_code == 200 else None
            return ProviderResult(
                name=f"{self.name} ({model})",
                category=self.category,
                method="POST",
                url=self.CHAT_URL,
                status_code=r.status_code,
                ok=r.status_code == 200,
                response_snippet=snippet,
            )
        except httpx.HTTPError as e:
            return ProviderResult(
                name=self.name,
                category=self.category,
                method="POST",
                url=self.CHAT_URL,
                status_code=None,
                ok=False,
                error=f"chat post failed: {type(e).__name__}: {e}",
            )
