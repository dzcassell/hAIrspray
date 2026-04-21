# ai-spray

Spray outbound traffic at a wide set of public AI services from a
single Docker container so that your **Cato Networks** (or any SASE /
NGFW / SWG) lab lights up the right application signatures.

It does **not** require API keys. The "real response" category routes
through keyless AI gateways (Pollinations, DuckDuckGo AI Chat) so you
actually get LLM completions back. Name-brand vendors (OpenAI,
Anthropic, Gemini, Mistral, Cohere, Groq, xAI, Together, DeepSeek,
Perplexity) are exercised with realistically shaped but unauthenticated
API calls — Cato identifies the application regardless of the 401.

## What it generates

| Category         | Provider behaviour                                      |
|------------------|---------------------------------------------------------|
| `llm_api`        | SDK-shaped POSTs to 10 major LLM APIs                   |
| `chatbot_ui`     | Browser-style GETs to 14 consumer chatbot front-ends    |
| `media_gen`      | Browser-style GETs to 15 image/video/audio gen sites    |
| `aggregator`     | Web + API hits against HF, Replicate, OpenRouter, etc.  |
| `real_response`  | Keyless Pollinations (text + image) and DDG AI Chat     |

See [`app/registry.py`](app/registry.py) for the full target list
(58 providers total).

## Traffic shape

- **Pacing:** uniform-random gap between requests (default 30–180 s).
- **Bursts:** with a configurable probability each iteration, fires
  3–7 back-to-back requests with 1–4 s gaps to simulate an active
  session.
- **Headers:** realistic browser User-Agents for UI targets; SDK-style
  User-Agents (`OpenAI/Python 1.51.0`, `anthropic-python/0.39.0`, etc.)
  for API targets; Content-Type, Accept, and other lifecycle headers
  set appropriately.
- **TLS:** HTTP/2 via `httpx[http2]` where supported, which is the
  standard for nearly every listed endpoint.

## Quick start

On your Debian Docker host:

```bash
cd /opt
sudo git clone https://github.com/dzcassell/ai-spray.git
sudo chown -R "$USER": ai-spray
cd ai-spray

cp .env.example .env
# Edit .env if you want to narrow categories or tune pacing.

docker compose build
docker compose up -d
docker compose logs -f
```

Health + metrics:

```bash
curl -s http://127.0.0.1:8080/healthz
curl -s http://127.0.0.1:8080/metrics | jq
```

## Running under systemd

```bash
sudo cp systemd/ai-spray.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-spray
sudo journalctl -u ai-spray -f
```

## Configuration

All knobs via env vars (see `.env.example`):

| Variable                  | Default    | Notes                                             |
|---------------------------|------------|---------------------------------------------------|
| `MIN_INTERVAL_SEC`        | `30`       | Lower bound of gap between iterations             |
| `MAX_INTERVAL_SEC`        | `180`      | Upper bound of gap between iterations             |
| `BURST_PROBABILITY`       | `0.15`     | Chance (per iteration) of firing a burst instead  |
| `BURST_MIN_SIZE`          | `3`        | Min requests per burst                            |
| `BURST_MAX_SIZE`          | `7`        | Max requests per burst                            |
| `BURST_GAP_MIN_SEC`       | `1`        | Min gap inside a burst                            |
| `BURST_GAP_MAX_SEC`       | `4`        | Max gap inside a burst                            |
| `CATEGORIES`              | all five   | Comma-separated subset of the categories above    |
| `ENABLE_REAL_RESPONSES`   | `true`     | Toggle Pollinations + DDG chat                    |
| `HTTP_TIMEOUT_SEC`        | `30`       | Per-request timeout                               |
| `MAX_CONCURRENT`          | `1`        | Kept at 1 so traffic looks human-paced            |
| `LOG_LEVEL`               | `INFO`     | `DEBUG` / `INFO` / `WARNING` / `ERROR`            |
| `HEALTH_PORT`             | `8080`     | Port for `/healthz` and `/metrics`                |

## Observability

Logs are JSON lines on stdout (captured by Docker / the journal):

```json
{"event": "traffic", "target": "OpenAI", "category": "llm_api",
 "method": "POST", "url": "https://api.openai.com/v1/chat/completions",
 "status": 401, "ok": true, "timestamp": "2026-04-21T14:32:11Z", "level": "info"}
```

`ok: true` on a 401 is intentional — it means the flow was generated
successfully, which is the only thing we care about here. The status
field tells you whether the server actually answered.

`GET /metrics` returns a JSON snapshot:

```json
{
  "uptime_seconds": 3812.4,
  "total_requests": 142,
  "total_ok": 140,
  "total_errors": 2,
  "per_category": {
    "llm_api": 38, "chatbot_ui": 41, "media_gen": 25,
    "aggregator": 22, "real_response": 16
  },
  "per_target": { "ChatGPT": 7, "OpenAI": 5, "Pollinations-Text (openai)": 4, "...": "..." }
}
```

## Verifying in Cato

Outbound traffic from your Debian host should already be routed
through your Cato socket / tunnel. Once the container is up:

1. In the Cato Management Application go to **Monitoring → Events →
   Application Analytics** and filter by the source IP of your host.
2. You should start seeing application matches like `OpenAI`,
   `ChatGPT`, `Anthropic`, `Hugging Face`, `Replicate`,
   `Midjourney`, `ElevenLabs`, etc.
3. Cross-reference against the `per_target` counters from `/metrics`
   to confirm coverage.

If you want DNS flows to traverse Cato as well, uncomment the `dns:`
block in `docker-compose.yml` and point it at your Cato-facing
resolver.

## Extending

Adding a new target is two lines. Example — adding `api.fireworks.ai`
to the aggregator category:

```python
# in app/registry.py, inside _aggregator_api_probes()
ApiProbe(
    name="Fireworks-Inference",
    url="https://api.fireworks.ai/inference/v1/chat/completions",
    user_agent="fireworks-ai/0.15.0",
    body_builder=lambda p: {
        "model": "accounts/fireworks/models/llama-v3p3-70b-instruct",
        "messages": [{"role": "user", "content": p}],
    },
    category="aggregator",
),
```

Rebuild and redeploy:

```bash
docker compose build && docker compose up -d
```

## Notes and caveats

- DuckDuckGo AI Chat rotates their anti-abuse headers every few
  months. If the `DuckDuckGo-AIChat` target starts consistently
  returning `no vqd token in status response`, that's the cause.
  Pollinations will keep working, and the failure doesn't affect the
  rest of the simulator.
- All simulator traffic is **outbound to public services** using
  benign prompts from `app/prompts.py`. No scraping, no credential
  stuffing, no rate-limit abuse — the pacing is deliberately slow.
- If you want to drive even more app-ID coverage, widen the host list
  in `registry.py` from aggregator sites like "There's An AI For
  That" — the file is organized so that adding a `(Name, URL)` tuple
  to the relevant category list is all it takes.

## License

MIT. See [`LICENSE`](LICENSE).
