"""Starlette HTTP layer.

Serves:
* ``GET /``                          — single-page UI (HTML with inline CSS/JS)
* ``GET /healthz``                   — Docker HEALTHCHECK target (plain text)
* ``GET /metrics``                   — JSON snapshot, kept for scrapers
* ``GET /api/status``                — full dashboard snapshot
* ``GET /api/config``                — current live-tunable config
* ``PATCH|POST /api/config``         — apply a partial config update
* ``GET /api/targets``               — list all 58 providers with state
* ``POST /api/targets/{n}/toggle``   — flip enabled state
* ``POST /api/targets/{n}/fire``     — fire a one-shot request (async)
* ``POST /api/scheduler/pause``      — pause the scheduler loop
* ``POST /api/scheduler/resume``     — resume it
* ``GET /api/events``                — recent events (JSON array)
* ``GET /api/events/stream``         — live SSE stream of events
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

from .state import AppState

log = structlog.get_logger()

UI_DIR = Path(__file__).parent / "ui"


# ---------------------------------------------------------------------------
# Core: run a provider and publish the result through AppState.
# Used by both the scheduler loop and the manual "fire" endpoint.
# ---------------------------------------------------------------------------

async def run_and_publish(
    provider,
    client: httpx.AsyncClient,
    state: AppState,
    source: str = "scheduler",
) -> None:
    try:
        result = await provider.execute(client)
    except Exception as e:  # noqa: BLE001 — scheduler must never die
        log.exception("provider_crashed", target=provider.name, error=str(e))
        state.publish_result({
            "target": provider.name,
            "category": provider.category,
            "method": "?",
            "url": AppState._provider_url(provider),
            "status": None,
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "snippet": None,
            "source": source,
        })
        return

    state.publish_result({
        "target": result.name,
        "category": result.category,
        "method": result.method,
        "url": result.url,
        "status": result.status_code,
        "ok": result.ok,
        "error": result.error,
        "snippet": result.response_snippet,
        "source": source,
    })
    log.info(
        "traffic",
        target=result.name,
        category=result.category,
        method=result.method,
        url=result.url,
        status=result.status_code,
        ok=result.ok,
        error=result.error,
        source=source,
    )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def _config_to_dict(cfg) -> dict:
    return {
        "min_interval": cfg.min_interval,
        "max_interval": cfg.max_interval,
        "burst_probability": cfg.burst_probability,
        "burst_min_size": cfg.burst_min_size,
        "burst_max_size": cfg.burst_max_size,
        "burst_gap_min": cfg.burst_gap_min,
        "burst_gap_max": cfg.burst_gap_max,
        "categories": sorted(cfg.categories),
        "enable_real_responses": cfg.enable_real_responses,
        "http_timeout": cfg.http_timeout,
        "log_level": cfg.log_level,
    }


def create_app(state: AppState, client: httpx.AsyncClient) -> Starlette:

    # ------------------ UI ------------------

    async def index(request: Request) -> Response:
        try:
            html = (UI_DIR / "index.html").read_text(encoding="utf-8")
        except FileNotFoundError:
            return PlainTextResponse("UI assets missing", status_code=500)
        return HTMLResponse(html)

    # ------------------ Legacy / compat ------------------

    async def healthz(request: Request) -> Response:
        return PlainTextResponse("ok")

    async def metrics(request: Request) -> Response:
        return JSONResponse(state.stats_snapshot())

    # ------------------ Status (everything at once) ------------------

    async def status(request: Request) -> Response:
        return JSONResponse({
            **state.stats_snapshot(),
            "config": _config_to_dict(state.config),
        })

    # ------------------ Config ------------------

    async def config_handler(request: Request) -> Response:
        if request.method == "GET":
            return JSONResponse(_config_to_dict(state.config))

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "expected object"}, status_code=400)

        applied = await state.update_config(body)
        # Normalize for JSON (set → sorted list).
        return JSONResponse({
            "applied": {
                k: sorted(v) if isinstance(v, set) else v
                for k, v in applied.items()
            },
            "config": _config_to_dict(state.config),
        })

    # ------------------ Targets ------------------

    async def list_targets(request: Request) -> Response:
        return JSONResponse(state.targets_snapshot())

    async def toggle_target(request: Request) -> Response:
        name = request.path_params["name"]
        body: dict[str, Any] = {}
        try:
            body = await request.json()
        except json.JSONDecodeError:
            pass

        if "enabled" in body:
            new_state = bool(body["enabled"])
        else:
            new_state = not state.is_enabled(name)

        if not state.set_enabled(name, new_state):
            return JSONResponse({"error": "unknown target"}, status_code=404)
        return JSONResponse({"name": name, "enabled": state.is_enabled(name)})

    async def fire_target(request: Request) -> Response:
        name = request.path_params["name"]
        provider = state.get_provider(name)
        if provider is None:
            return JSONResponse({"error": "unknown target"}, status_code=404)
        # Fire-and-forget. Result will appear in the SSE stream + stats.
        asyncio.create_task(
            run_and_publish(provider, client, state, source="manual")
        )
        return JSONResponse({"name": name, "fired": True}, status_code=202)

    # ------------------ Scheduler control ------------------

    async def pause_scheduler(request: Request) -> Response:
        state.pause()
        log.info("scheduler_paused_via_api")
        return JSONResponse({"running": state.is_running()})

    async def resume_scheduler(request: Request) -> Response:
        state.resume()
        log.info("scheduler_resumed_via_api")
        return JSONResponse({"running": state.is_running()})

    # ------------------ Events ------------------

    async def recent_events(request: Request) -> Response:
        try:
            limit = int(request.query_params.get("limit", "200"))
        except ValueError:
            limit = 200
        return JSONResponse(state.recent_events(limit=limit))

    async def event_stream(request: Request) -> Response:
        q = state.subscribe()

        async def gen():
            try:
                # Catch the client up with recent history.
                for ev in state.recent_events(limit=50):
                    yield f"data: {json.dumps(ev)}\n\n".encode("utf-8")
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15.0)
                        yield f"data: {json.dumps(ev)}\n\n".encode("utf-8")
                    except asyncio.TimeoutError:
                        # Keep-alive comment frame so proxies don't idle-close.
                        yield b": ping\n\n"
            finally:
                state.unsubscribe(q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    routes = [
        Route("/", index),
        Route("/healthz", healthz),
        Route("/metrics", metrics),

        Route("/api/status", status),
        Route("/api/config", config_handler, methods=["GET", "PATCH", "POST"]),
        Route("/api/targets", list_targets),
        Route("/api/targets/{name:path}/toggle", toggle_target, methods=["POST"]),
        Route("/api/targets/{name:path}/fire", fire_target, methods=["POST"]),
        Route("/api/scheduler/pause", pause_scheduler, methods=["POST"]),
        Route("/api/scheduler/resume", resume_scheduler, methods=["POST"]),
        Route("/api/events", recent_events),
        Route("/api/events/stream", event_stream),
    ]

    return Starlette(debug=False, routes=routes)
