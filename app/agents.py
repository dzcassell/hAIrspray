"""Agents — coder-prompt random-sprinkle traffic against AI assistant CLIs.

Powers the Agents tab in the UI. Two sources of traffic:

* **Anthropic** — calls the `/v1/messages` endpoint directly with a
  `claude-cli` User-Agent. If the `claude` binary is on the container
  PATH at startup, we *prefer* the subprocess invocation over a raw
  HTTP call (real CLI, real wire shape, including its specific
  multipart-message-batching behavior). Otherwise we fall back to
  raw API. Either way the wire signature is what an enterprise SASE
  needs to classify as Claude Code.

* **Cursor** — uses Cursor's User API Key (issued via the Cursor
  Integrations Dashboard for the headless CLI) against
  `api2.cursor.sh`. The Cursor binary is *not* installed in the
  container — it auto-updates aggressively on every launch which
  generates noisy classifiable traffic of its own and breaks
  predictable container behavior. We hit the API directly with the
  CLI's documented User-Agent.

The "fire" loop runs on the server, not the browser, so it survives
page reloads and tab closes. State lives in AgentLoopState; the
single asyncio.Task is started on /api/agents/start and cancelled
on /api/agents/stop. Random gap between fires is configurable in
the UI (defaults: 60-120s). On each fire we pick a random enabled
prompt × a random enabled provider, fire it, record the result in
a ring buffer, and sleep until the next fire.
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import random
import shutil
import subprocess  # nosec B404 — used only for the well-known `claude` binary
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Predefined coder prompt matrix
# ---------------------------------------------------------------------------
# Ten prompts spanning five genres. Each prompt has a slug for the UI to
# reference, a genre (for filtering / display grouping), and the prompt
# text itself. Prompts are chosen to:
#   * be small enough that the wire signature is the prompt body, not
#     a 4KB context dump
#   * not require file-system access or shell execution (so neither the
#     real Claude CLI nor a Cursor agent tries to mutate anything)
#   * exercise different traffic shapes — short Q&A, code-gen with
#     longer responses, code-review with longer prompts, etc.

PROMPTS: list[dict[str, str]] = [
    # ---- Pure Q&A (short prompt, short response) ----
    {
        "slug":  "qa-btree",
        "genre": "qa",
        "label": "Q&A: B-tree vs B+ tree",
        "text":  "In two short paragraphs, explain how a B-tree differs "
                 "from a B+ tree, and when you'd choose one over the other.",
    },
    {
        "slug":  "qa-cap",
        "genre": "qa",
        "label": "Q&A: CAP theorem",
        "text":  "Briefly: what does the CAP theorem actually say, and "
                 "what's the most common misunderstanding of it?",
    },

    # ---- Code generation (short prompt, longer response) ----
    {
        "slug":  "gen-palindrome",
        "genre": "codegen",
        "label": "Code: palindrome detector in Python",
        "text":  "Write a Python function `is_palindrome(s: str) -> bool` "
                 "that ignores case and non-alphanumeric characters. "
                 "Include three short doctests.",
    },
    {
        "slug":  "gen-binsearch-rust",
        "genre": "codegen",
        "label": "Code: binary search in Rust",
        "text":  "Write an idiomatic Rust function for binary search over "
                 "a `&[i32]`. Use the standard signature returning a "
                 "`Result<usize, usize>` matching slice::binary_search. "
                 "No external crates.",
    },

    # ---- Code review (long prompt with embedded code, terse response) ----
    {
        "slug":  "review-py-bug",
        "genre": "review",
        "label": "Review: subtle Python bug",
        "text":  ("Review this snippet for bugs. Be specific.\n\n"
                  "```python\n"
                  "def merge_dicts(a, b):\n"
                  "    out = a\n"
                  "    for k, v in b.items():\n"
                  "        out[k] = v\n"
                  "    return out\n"
                  "\n"
                  "user_defaults = {'theme': 'dark', 'lang': 'en'}\n"
                  "alice = merge_dicts(user_defaults, {'lang': 'fr'})\n"
                  "bob   = merge_dicts(user_defaults, {'lang': 'es'})\n"
                  "print(user_defaults)\n"
                  "```\n"),
    },
    {
        "slug":  "review-sql-injection",
        "genre": "review",
        "label": "Review: SQL injection risk",
        "text":  ("Is this code safe? Why or why not?\n\n"
                  "```python\n"
                  "import sqlite3\n"
                  "def find_user(name):\n"
                  "    con = sqlite3.connect('app.db')\n"
                  "    cur = con.cursor()\n"
                  "    cur.execute(f\"SELECT * FROM users WHERE name = '{name}'\")\n"
                  "    return cur.fetchall()\n"
                  "```\n"),
    },

    # ---- Refactoring (long prompt, long response) ----
    {
        "slug":  "refactor-fizzbuzz",
        "genre": "refactor",
        "label": "Refactor: clarify a tangled function",
        "text":  ("Refactor for clarity. Keep the same behavior.\n\n"
                  "```python\n"
                  "def f(n):\n"
                  "    r = []\n"
                  "    for i in range(1, n+1):\n"
                  "        x = ''\n"
                  "        if i % 3 == 0: x += 'Fizz'\n"
                  "        if i % 5 == 0: x += 'Buzz'\n"
                  "        r.append(x or str(i))\n"
                  "    return r\n"
                  "```\n"),
    },

    # ---- Debugging (long prompt with stack trace, focused response) ----
    {
        "slug":  "debug-asyncio",
        "genre": "debug",
        "label": "Debug: asyncio RuntimeError",
        "text":  ("What's likely going wrong here, and how do I fix it?\n\n"
                  "```\n"
                  "RuntimeError: This event loop is already running\n"
                  "  File \"app.py\", line 42, in handle\n"
                  "    result = asyncio.run(do_thing())\n"
                  "  File \".../asyncio/runners.py\", line 33, in run\n"
                  "    raise RuntimeError(\n"
                  "```\n\n"
                  "Context: this is inside a request handler in a "
                  "FastAPI app."),
    },

    # ---- Architecture (moderate prompt, very long response) ----
    {
        "slug":  "arch-rate-limiter",
        "genre": "architecture",
        "label": "Architecture: distributed rate limiter",
        "text":  "Sketch the design of a rate limiter that works across "
                 "a horizontally-scaled web tier (~50 nodes). Cover: "
                 "what backing store, what algorithm, how you handle "
                 "the 'thundering herd' near reset boundaries, and "
                 "what the failure mode is if the backing store is "
                 "briefly unavailable.",
    },
    {
        "slug":  "arch-feature-flags",
        "genre": "architecture",
        "label": "Architecture: feature-flag system",
        "text":  "What are the must-have properties of a feature-flag "
                 "system for a 200-engineer org? Cover propagation "
                 "latency, percentage rollouts, kill-switch semantics, "
                 "and how you avoid the flag store becoming a single "
                 "point of failure.",
    },
]

PROMPT_BY_SLUG: dict[str, dict[str, str]] = {p["slug"]: p for p in PROMPTS}
PROMPT_GENRES: tuple[str, ...] = (
    "qa", "codegen", "review", "refactor", "debug", "architecture",
)

# Providers we can sprinkle. Each entry knows how to fire one prompt at
# the matching service. Ordered: anthropic first (because the real CLI
# may be present), cursor second.
PROVIDERS: tuple[str, ...] = ("anthropic", "cursor")


# ---------------------------------------------------------------------------
# Loop state
# ---------------------------------------------------------------------------

@dataclass
class AgentFireResult:
    """One completed agent fire — what gets shown in the Agents tab."""
    provider: str             # "anthropic" | "cursor"
    prompt_slug: str
    prompt_label: str
    started_at: float         # epoch seconds
    elapsed_ms: int
    ok: bool
    response_text: str        # the model's response, or error message if !ok
    response_chars: int       # length of response_text
    error: str | None = None  # short error class name on failure


@dataclass
class AgentLoopState:
    """Server-side state for the random-sprinkle loop.

    Lives in AppState so it survives page reloads. One asyncio.Task at
    most; cancelled on stop. fire_history is a deque so it auto-trims
    to the last N results.
    """
    running: bool = False
    task: asyncio.Task | None = None
    started_at: float | None = None  # epoch seconds when current run began
    total_fired: int = 0
    enabled_prompts: set[str] = field(
        default_factory=lambda: set(p["slug"] for p in PROMPTS)
    )
    enabled_providers: set[str] = field(
        default_factory=lambda: set(PROVIDERS)
    )
    min_gap_sec: int = 60
    max_gap_sec: int = 120
    fire_history: deque[AgentFireResult] = field(
        default_factory=lambda: deque(maxlen=50)
    )

    def status(self) -> dict[str, Any]:
        """JSON-safe snapshot for /api/agents/status."""
        return {
            "running":           self.running,
            "started_at":        self.started_at,
            "total_fired":       self.total_fired,
            "enabled_prompts":   sorted(self.enabled_prompts),
            "enabled_providers": sorted(self.enabled_providers),
            "min_gap_sec":       self.min_gap_sec,
            "max_gap_sec":       self.max_gap_sec,
            "history": [
                dataclasses.asdict(r) for r in self.fire_history
            ],
        }


# ---------------------------------------------------------------------------
# Real-CLI detection
# ---------------------------------------------------------------------------

def claude_cli_available() -> bool:
    """Did the operator install the `claude` binary in the container?

    We don't try to install it ourselves — the operator either bakes
    it into a derived image or doesn't. Detection is just `shutil.which`.
    """
    return shutil.which("claude") is not None


# ---------------------------------------------------------------------------
# Anthropic dispatcher (real CLI when available, API fallback otherwise)
# ---------------------------------------------------------------------------

# Anthropic API base. If a future user has ANTHROPIC_BASE_URL set in
# the environment we honor it (e.g. for routing through their own
# proxy or a Bedrock-style gateway), but the default is the Anthropic
# public endpoint.
ANTHROPIC_API_BASE = os.environ.get(
    "ANTHROPIC_BASE_URL", "https://api.anthropic.com",
).rstrip("/")
ANTHROPIC_DEFAULT_MODEL = os.environ.get(
    "AGENTS_ANTHROPIC_MODEL", "claude-sonnet-4-5",
)

# User-Agent matching the official `claude` CLI (Anthropic Claude Code).
# Real claude-code/v1.x. Updated April 2026; SASE classifiers key off
# this UA prefix.
ANTHROPIC_CLI_USER_AGENT = "claude-cli/1.0 (sasetest hAIrspray)"


async def _fire_anthropic_via_cli(
    prompt: dict[str, str],
    api_key: str,
    timeout: float,
) -> tuple[bool, str, str | None]:
    """Run `claude -p '<prompt>'` and capture stdout. Returns
    (ok, response_text, error_class_name)."""
    # Subprocess in a thread so we don't block the event loop.
    def _run() -> tuple[int, str, str]:
        env = dict(os.environ)
        env["ANTHROPIC_API_KEY"] = api_key
        proc = subprocess.run(  # nosec B603 — well-known binary, fixed args
            ["claude", "-p", prompt["text"], "--model",
             ANTHROPIC_DEFAULT_MODEL],
            capture_output=True, text=True,
            timeout=timeout, env=env, check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr

    try:
        rc, stdout, stderr = await asyncio.to_thread(_run)
    except subprocess.TimeoutExpired:
        return False, "", "TimeoutExpired"
    except FileNotFoundError:
        # Race with config — `claude` was on PATH at startup but isn't
        # now. Fall back to API.
        return False, "", "FileNotFoundError"
    if rc != 0:
        return False, stderr.strip()[:1000] or "non-zero exit", \
               f"ExitCode{rc}"
    return True, stdout.strip(), None


async def _fire_anthropic_via_api(
    client: httpx.AsyncClient,
    prompt: dict[str, str],
    api_key: str,
    timeout: float,
) -> tuple[bool, str, str | None]:
    """POST /v1/messages with x-api-key auth and a CLI-style UA."""
    headers = {
        "x-api-key":          api_key,
        "anthropic-version":  "2023-06-01",
        "Content-Type":       "application/json",
        "User-Agent":         ANTHROPIC_CLI_USER_AGENT,
    }
    body = {
        "model":      ANTHROPIC_DEFAULT_MODEL,
        "max_tokens": 1024,
        "messages":   [{"role": "user", "content": prompt["text"]}],
    }
    url = f"{ANTHROPIC_API_BASE}/v1/messages"
    try:
        r = await client.post(
            url, headers=headers, json=body, timeout=timeout,
        )
    except httpx.HTTPError as e:
        return False, "", type(e).__name__
    if r.status_code >= 400:
        return False, r.text[:1000], f"HTTP{r.status_code}"
    try:
        data = r.json()
        # Anthropic /v1/messages returns content blocks. Concatenate the
        # text blocks. Tool-use blocks are ignored — these prompts don't
        # ask for tools.
        parts = [
            blk.get("text", "")
            for blk in data.get("content", [])
            if blk.get("type") == "text"
        ]
        return True, "".join(parts).strip(), None
    except (ValueError, KeyError) as e:
        return False, "", type(e).__name__


async def fire_anthropic(
    client: httpx.AsyncClient,
    prompt: dict[str, str],
    api_key: str,
) -> AgentFireResult:
    """Fire one prompt at Anthropic. Prefers the real CLI if available."""
    started = time.time()
    timeout = 60.0  # generous — long-form responses can take a while

    if claude_cli_available():
        ok, text, err = await _fire_anthropic_via_cli(
            prompt, api_key, timeout,
        )
    else:
        ok, text, err = await _fire_anthropic_via_api(
            client, prompt, api_key, timeout,
        )

    elapsed_ms = int((time.time() - started) * 1000)
    return AgentFireResult(
        provider="anthropic",
        prompt_slug=prompt["slug"],
        prompt_label=prompt["label"],
        started_at=started,
        elapsed_ms=elapsed_ms,
        ok=ok,
        response_text=text,
        response_chars=len(text),
        error=err,
    )


# ---------------------------------------------------------------------------
# Cursor dispatcher (API only — binary deliberately not installed)
# ---------------------------------------------------------------------------

# Cursor's headless API surface. Cursor does NOT expose a public
# chat-completions endpoint that accepts user API keys for inference —
# their BYOK only works inside the IDE, where the user's
# OpenAI/Anthropic key is routed through Cursor's backend.
#
# What they DO expose at api.cursor.com/v0/* is the **Background
# Agents API**, which takes a User API Key and is what `cursor-agent`
# uses for its remote/headless mode. That's what hAIrspray fires
# against. From a SASE classification standpoint this is the wire
# shape that actually identifies Cursor traffic — which is exactly
# what we want to test.
#
# Two endpoints get exercised per fire:
#   GET  /v0/me            — lightweight key-validation hit
#   POST /v0/agents        — launches a Background Agent with the
#                            coder prompt as its initial instruction
# Both go out on api.cursor.com, both with Bearer auth, both with a
# cursor-agent UA. The agent itself doesn't actually run repo work
# (we don't supply a source.repository, so creation 4xx's), but
# the *POST hits the wire* — which is what matters for classification.
CURSOR_API_BASE = os.environ.get(
    "CURSOR_BASE_URL", "https://api.cursor.com",
).rstrip("/")
CURSOR_CLI_USER_AGENT = "cursor-agent/0.4 (sasetest hAIrspray)"


async def fire_cursor(
    client: httpx.AsyncClient,
    prompt: dict[str, str],
    api_key: str,
) -> AgentFireResult:
    """Fire one prompt at Cursor's Background Agents API.

    No chat-completions endpoint exists on the public Cursor API
    surface for user keys — their docs only expose /v0/me and
    /v0/agents. We POST a Background Agent creation request with
    the coder prompt as the agent's initial instruction. The
    request will likely 4xx (no repo supplied, free-tier limits)
    but the wire shape is what SASE classifies.
    """
    started = time.time()
    timeout = 30.0
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
        "User-Agent":    CURSOR_CLI_USER_AGENT,
    }
    body = {
        # Background Agents API shape per Cursor docs (April 2026).
        # The 'prompt' field carries the human's instruction. Other
        # fields (source.repository, model) are normally required for
        # a real run; omitting them produces a 400 — which is fine,
        # the POST went out the wire either way and that's what the
        # classifier sees.
        "prompt": {
            "text": prompt["text"],
        },
        "model": "auto",
    }
    url = f"{CURSOR_API_BASE}/v0/agents"

    try:
        r = await client.post(
            url, headers=headers, json=body, timeout=timeout,
        )
    except httpx.HTTPError as e:
        elapsed_ms = int((time.time() - started) * 1000)
        return AgentFireResult(
            provider="cursor", prompt_slug=prompt["slug"],
            prompt_label=prompt["label"], started_at=started,
            elapsed_ms=elapsed_ms, ok=False, response_text="",
            response_chars=0, error=type(e).__name__,
        )

    elapsed_ms = int((time.time() - started) * 1000)

    # 200/201 with a JSON body containing an agent ID is success.
    # Anything in 4xx is "fabric saw a real Cursor request" — log it
    # as ok=False with the response body as the result so the operator
    # can see what came back, but it's still useful traffic.
    response_text = r.text[:1500] if r.text else ""
    if r.status_code < 400:
        return AgentFireResult(
            provider="cursor", prompt_slug=prompt["slug"],
            prompt_label=prompt["label"], started_at=started,
            elapsed_ms=elapsed_ms, ok=True,
            response_text=response_text, response_chars=len(response_text),
            error=None,
        )
    return AgentFireResult(
        provider="cursor", prompt_slug=prompt["slug"],
        prompt_label=prompt["label"], started_at=started,
        elapsed_ms=elapsed_ms, ok=False,
        response_text=response_text, response_chars=len(response_text),
        error=f"HTTP{r.status_code}",
    )


# ---------------------------------------------------------------------------
# The random-sprinkle loop itself
# ---------------------------------------------------------------------------

async def run_loop(
    state: AgentLoopState,
    client: httpx.AsyncClient,
    key_provider: Any,
) -> None:
    """Long-running task: fire random (provider, prompt) pairs at random
    intervals until cancelled. Blocks on httpx I/O; cancellation is the
    only way out.

    `key_provider` is the KeyStore. We look up keys per-fire so that if
    the operator pastes/changes a key mid-loop, the next fire uses the
    new value without restart.
    """
    state.running = True
    state.started_at = time.time()
    state.total_fired = 0

    log.info("agent_loop_started",
             min_gap=state.min_gap_sec, max_gap=state.max_gap_sec,
             enabled_prompts=len(state.enabled_prompts),
             enabled_providers=sorted(state.enabled_providers))

    try:
        while True:
            # Snapshot enabled sets — the user can toggle these from the
            # UI mid-loop and we want the next fire to reflect the change.
            providers = [
                p for p in PROVIDERS
                if p in state.enabled_providers
            ]
            prompts = [
                PROMPT_BY_SLUG[s] for s in state.enabled_prompts
                if s in PROMPT_BY_SLUG
            ]

            if not providers or not prompts:
                # Nothing to fire — sleep briefly and check again. Don't
                # spin tightly.
                await asyncio.sleep(5.0)
                continue

            provider = random.choice(providers)
            prompt = random.choice(prompts)

            # Look up the key. If missing, record an error result and
            # continue — the loop should not silently stop because one
            # provider's key was removed.
            try:
                if provider == "anthropic":
                    key = await key_provider.get("anthropic")
                else:
                    key = await key_provider.get("cursor")
            except Exception as e:  # noqa: BLE001
                log.warning("agent_key_lookup_failed",
                            provider=provider, error=str(e))
                key = None

            if not key:
                state.fire_history.append(AgentFireResult(
                    provider=provider, prompt_slug=prompt["slug"],
                    prompt_label=prompt["label"], started_at=time.time(),
                    elapsed_ms=0, ok=False, response_text="",
                    response_chars=0,
                    error=f"no API key saved for {provider}",
                ))
                state.total_fired += 1
            else:
                if provider == "anthropic":
                    result = await fire_anthropic(client, prompt, key)
                else:
                    result = await fire_cursor(client, prompt, key)
                state.fire_history.append(result)
                state.total_fired += 1
                log.info("agent_fire",
                         provider=provider, prompt_slug=prompt["slug"],
                         ok=result.ok, elapsed_ms=result.elapsed_ms,
                         response_chars=result.response_chars,
                         error=result.error)

            # Sleep until the next fire. Random gap in [min, max] —
            # matches the existing scheduler's pacing semantics.
            gap = random.uniform(state.min_gap_sec, state.max_gap_sec)
            await asyncio.sleep(gap)

    except asyncio.CancelledError:
        log.info("agent_loop_cancelled", total_fired=state.total_fired)
        raise
    finally:
        state.running = False
        state.task = None
