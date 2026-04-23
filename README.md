# hAIrspray

**Generate traffic at public AI applications from a single box so your
SASE / NGFW / SWG shows you whether it can actually identify, classify,
and control them.**

Vendors claim AI-app visibility. hAIrspray lets you find out. Point it
at your corporate egress, enable the probes relevant to your test, and
watch your security fabric's application-analytics dashboard light up —
or fail to.

> _Created by Damon Cassell · Cato Networks · No Warranty · You Accept
> All Risk._

---

## What this is for

Modern SASE platforms and next-generation firewalls market AI-app
inspection as a first-class feature: policy rules that target
"OpenAI ChatGPT" or "Generative AI" as a category, DLP over GenAI
prompt bodies, block lists for specific model providers, and so on.

The only way to know whether a given vendor's app-ID actually sees
what it claims to see is to generate realistic traffic at the real
services and look at what the fabric reports. hAIrspray is that
traffic generator. It runs as a container behind your SASE fabric (or
through your NGFW egress) and fires outbound requests at **161 curated
AI endpoints** so you can:

- Verify your vendor's app signatures fire for each AI service you
  care about (not just OpenAI).
- Measure classification latency — how fast does the first hit become
  a matched app event?
- Test policy rules: block categories, rate-limit models, inspect
  prompt bodies with DLP, force TLS inspection on AI gateways.
- Compare vendors side-by-side on the same probe catalog.
- Produce reproducible sample traffic for customer demos or MSP lab
  setups.

## What actually goes out the wire

- **161 probes across 5 categories**: LLM APIs (OpenAI, Anthropic,
  Gemini, Groq, Mistral, Cohere, and ~16 more), chatbot UIs (ChatGPT,
  Claude.ai, Gemini Web, Copilot, Character.AI, and ~27 more), media
  generation (Midjourney, Leonardo, Runway, Suno, ElevenLabs, and ~44
  more), aggregators (HuggingFace, Replicate, OpenRouter, Cursor,
  Lovable, and ~50 more), and a small "real response" set.
- **Realistic request shapes.** LLM-API probes use SDK-style
  User-Agents (`OpenAI/Python 1.51.0`, `anthropic-python/0.39.0`) and
  the right endpoints, bodies, and auth headers. Chatbot-UI probes
  use browser User-Agents and the URLs a browser would fetch. No key
  required — 401/403 is fine, the flow is the point.
- **Real AI responses** for the "Prompt & Fire" flow. Twelve keyless
  model/provider pairs (Pollinations text + image, DuckDuckGo AI Chat)
  return real completions. Fourteen additional providers unlock real
  responses if you paste free-tier API keys (Gemini, Groq, Mistral,
  Cohere, OpenRouter, HuggingFace, Together, Cerebras, SambaNova,
  Hyperbolic, DeepSeek, xAI, AI21, Fireworks).
- **Configurable pacing**: 30–180s random gaps between probes by
  default, plus optional burst mode (3–7 back-to-back requests at
  1–4s intervals) for session-like patterns.
- **TLS / HTTP/2** on every endpoint that supports it (`httpx[http2]`).

## Quick start

On a Linux Docker host sitting behind the SASE fabric or NGFW you want
to test:

```bash
cd /opt
sudo git clone https://github.com/dzcassell/hAIrspray.git
sudo chown -R "$USER": hAIrspray
cd hAIrspray

cp .env.example .env
# Edit .env if you want to narrow categories, tune pacing, or bind
# the UI to loopback only.

docker compose up -d --build
docker compose logs -f
```

Then open `http://<host>:8090/` — the web UI is the primary interface.
You can also run it as a systemd service with the unit file in
`systemd/hairspray.service`.

## The UI

Four tabs:

- **Prompt & Fire** — send a real prompt to every keyless (and keyed,
  if you saved keys) AI provider at once. Responses stream back
  inline. This is the fastest way to show a SASE demo audience that
  AI DLP either is or isn't inspecting the reply body.
- **App Probes** — the full 161-entry probe catalog. Filter by name,
  URL, or category; enable/disable individual probes; fire a single
  probe, fire an entire category, or fire every enabled probe once
  (concurrency-capped) via **⚡ Fire All**.
- **Config** — scheduler knobs (min/max interval, burst probability,
  burst size, burst gap, category toggles) and the persistent API
  key store for the 14 keyed providers.
- **Monitor** — live stats (totals, per-category bars, OK vs error
  counts) and an SSE-streamed event log with search, category
  filter, status filter, and NDJSON export.

## Typical SASE/NGFW test workflow

1. **Deploy behind the fabric** you want to test. For SASE sockets,
   that's usually a LAN host whose default route is the socket. For
   NGFWs, any host on the inside interface.
2. **Disable everything** in the App Probes tab, then enable only the
   category you're testing (e.g. `llm_api` if you're validating LLM
   API signatures).
3. **Click ⚡ Fire All.** hAIrspray will hit every enabled probe
   once with bounded concurrency (default 10).
4. **Open your vendor's app-analytics dashboard.** For Cato, that's
   _Monitoring → Events → Application Analytics_ filtered by the
   source IP of your hAIrspray host. You should see one matched
   event per probe.
5. **Compare against the probe list.** Any miss is a gap in your
   vendor's signatures or your license/policy tier.
6. **Optional**: save a free-tier API key for Groq or Google Gemini
   in Config, then use Prompt & Fire to submit a prompt. The
   response body is what DLP will see — good for testing GenAI DLP
   rules.

## Architecture

Python 3.12, asyncio, `httpx[http2]` for egress, Starlette + uvicorn
for the web UI/API, structlog for JSON logs. Single-process, single-
container. State lives in memory except for saved API keys, which are
persisted to a Docker-managed named volume at `/data/keys.json`
(mode 0600, schema-versioned JSON). See `app/keys.py`.

Event flow: the scheduler picks a probe, runs it through an
`httpx.AsyncClient`, and publishes a `ProviderResult` into shared
state. The state publishes to an internal ring buffer plus any
connected SSE subscribers, driving the Live Log and stats in the UI.
Fire All and Prompt & Fire use the same publish path, so every
request — scheduled or manual — appears in the same Monitor stream
and in your SASE's logs at the same layer.

## Security caveats

- **No authentication on the web UI.** Bind it to loopback
  (`HEALTH_BIND=127.0.0.1` in `.env`) or keep the host behind your
  SASE fabric. _Do not expose to the public internet._
- **API keys are stored in plaintext** inside a Docker volume. File
  mode is 0600 but anyone with Docker socket access on the host
  (i.e. membership in the `docker` group) can read them. This is a
  lab tool; do not store keys here that would cost real money if
  leaked.
- **This tool generates actual requests at real services.** If you
  leave it running indefinitely at high concurrency it is fully
  capable of tripping rate limiters and getting your source IP
  temporarily banned by some providers. Default pacing is
  deliberately slow.

## Documentation

- `app/registry.py` — the 161-probe catalog
- `app/prompt.py` — the 12 keyless + 14 keyed prompt-capable providers
- `app/config.py` — all `.env` knobs and their defaults
- `app/web.py` — REST + SSE API surface
- `.env.example` — every tunable with inline notes

## License

MIT — see [`LICENSE`](LICENSE). You accept all risk of using this
tool. Nothing about it is warrantied.
