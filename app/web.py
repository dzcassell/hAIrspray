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

    # ------------------ Fire all ------------------
    #
    # Runs one request per selected target using a bounded concurrency
    # pool (default 10). Ten parallel flights finishes a full 161-target
    # spray in ~30 s while staying well under any reasonable Cloudflare
    # WAF threshold that might briefly affect co-tenant containers on
    # the same outbound IP. Only one fire-all job runs at a time; a
    # second call while one is in progress returns 409.
    #
    # Selection rules (any combination may be supplied in the POST body):
    #   * "scope": "enabled" (default) | "all"
    #     - "enabled" honors the per-target toggles and the channel
    #        checkboxes (same set the scheduler picks from).
    #     - "all" ignores all toggles and fires every known target.
    #   * "category": "llm_api" etc. — restrict to one channel.
    #   * "names": explicit list of provider names; overrides everything
    #     else.
    #   * "concurrency": max in-flight requests (default 10, hard cap 32).
    #   * "gap_min_sec" / "gap_max_sec": minimum launch spacing between
    #     new requests starting (default 0.1 → 0.3). Keeps the initial
    #     burst from arriving as a single TCP fan-out blip.

    async def _fire_all_runner(
        targets: list,
        concurrency: int,
        gap_min: float,
        gap_max: float,
        source: str,
    ) -> None:
        import random
        rng = random.Random()
        total = len(targets)
        sem = asyncio.Semaphore(concurrency)
        launched = 0
        completed = 0

        state.begin_fire_all(
            total=total, source=source, concurrency=concurrency,
        )
        log.info(
            "fire_all_started",
            total=total, concurrency=concurrency, source=source,
        )

        async def _one(provider) -> None:
            nonlocal completed
            async with sem:
                if state.fire_all_cancel_requested():
                    return
                state.update_fire_all_current(provider.name)
                await run_and_publish(provider, client, state, source=source)
                completed += 1
                state.update_fire_all_progress(done=completed)

        tasks: list[asyncio.Task] = []
        try:
            for provider in targets:
                if state.fire_all_cancel_requested():
                    log.info(
                        "fire_all_cancelled_before_launch",
                        launched=launched, total=total,
                    )
                    break
                tasks.append(asyncio.create_task(_one(provider)))
                launched += 1
                # Small random stagger so the initial fan-out isn't a
                # single tight burst at t=0.
                if launched < total:
                    gap = rng.uniform(gap_min, gap_max)
                    try:
                        await asyncio.wait_for(
                            state.fire_all_cancel_event().wait(), timeout=gap
                        )
                    except asyncio.TimeoutError:
                        pass

            # Wait for all in-flight work to settle, even if we broke
            # out of the launch loop due to cancel.
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            state.end_fire_all()
            log.info(
                "fire_all_finished",
                completed=completed, launched=launched, total=total,
            )

    async def fire_all(request: Request) -> Response:
        if state.fire_all_running():
            return JSONResponse(
                {"error": "fire-all already in progress",
                 "progress": state.fire_all_snapshot()},
                status_code=409,
            )

        body: dict[str, Any] = {}
        try:
            body = await request.json()
        except json.JSONDecodeError:
            pass

        scope = str(body.get("scope", "enabled")).lower()
        category = body.get("category")
        names = body.get("names")
        concurrency = int(body.get("concurrency", 10))
        concurrency = max(1, min(32, concurrency))
        gap_min = float(body.get("gap_min_sec", 0.1))
        gap_max = float(body.get("gap_max_sec", 0.3))
        if gap_min < 0: gap_min = 0.0
        if gap_max < gap_min: gap_max = gap_min

        # Build the target list.
        if names and isinstance(names, list):
            providers = [
                p for n in names
                if (p := state.get_provider(str(n))) is not None
            ]
        elif scope == "all":
            providers = list(state.all_providers())
        else:
            providers = state.eligible_providers()

        if category:
            providers = [p for p in providers if p.category == str(category)]

        if not providers:
            return JSONResponse(
                {"error": "no targets matched selection"},
                status_code=400,
            )

        # Shuffle so repeated fire-all runs produce different orderings
        # and don't always hammer the same vendor first.
        import random as _r
        _r.shuffle(providers)

        asyncio.create_task(
            _fire_all_runner(
                providers, concurrency, gap_min, gap_max, source="fire-all",
            )
        )
        # With concurrency=C and per-request time ≈T_req, total ≈
        # (T_req * N) / C + launch_gap * N; we don't know T_req so we
        # return a rough wall-clock floor of launch_gap * N.
        return JSONResponse(
            {
                "started": True,
                "total": len(providers),
                "concurrency": concurrency,
                "launch_gap_sec": [gap_min, gap_max],
            },
            status_code=202,
        )

    async def fire_all_status(request: Request) -> Response:
        return JSONResponse(state.fire_all_snapshot())

    async def fire_all_cancel(request: Request) -> Response:
        if not state.fire_all_running():
            return JSONResponse({"running": False})
        state.request_fire_all_cancel()
        return JSONResponse({"cancelling": True})

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
        Route("/api/fire-all", fire_all, methods=["POST"]),
        Route("/api/fire-all/status", fire_all_status),
        Route("/api/fire-all/cancel", fire_all_cancel, methods=["POST"]),
        Route("/api/scheduler/pause", pause_scheduler, methods=["POST"]),
        Route("/api/scheduler/resume", resume_scheduler, methods=["POST"]),
        Route("/api/events", recent_events),
        Route("/api/events/stream", event_stream),
    ]

    return Starlette(debug=False, routes=routes)
