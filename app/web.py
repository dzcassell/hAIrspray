"""aiohttp web server for ai-spray.

Serves:

* ``/``                       the single-file UI
* ``/healthz``                liveness probe
* ``/metrics``                JSON snapshot of counters
* ``/api/state``              runtime config + health + flags
* ``/api/targets``            per-target detail
* ``/api/targets/{n}/toggle`` enable/disable one target
* ``/api/categories/{c}/toggle``
* ``/api/control/pause``      pause scheduler
* ``/api/control/resume``     resume scheduler
* ``/api/fire``               fire a specific target (or random)
* ``/api/burst``              enqueue N random fires
* ``/api/config``             mutate pacing / burst config live
* ``/api/logs/recent``        replay the ring buffer
* ``/api/logs/stream``        Server-Sent Events stream of new events
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import random
import time
from typing import Any

import structlog
from aiohttp import web

from .state import RunState

log = structlog.get_logger()

UI_DIR = pathlib.Path(__file__).parent / "ui"


# ---------------------------------------------------------------------------
# Static / UI
# ---------------------------------------------------------------------------

async def h_index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(UI_DIR / "index.html")


async def h_healthz(request: web.Request) -> web.Response:
    return web.Response(text="ok", content_type="text/plain")


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------

def _snapshot(state: RunState) -> dict[str, Any]:
    snap = state.health.snapshot()
    snap["paused"] = state.paused
    return snap


async def h_metrics(request: web.Request) -> web.Response:
    state: RunState = request.app["state"]
    return web.json_response(_snapshot(state))


async def h_state(request: web.Request) -> web.Response:
    state: RunState = request.app["state"]
    all_categories = sorted({p.category for p in state.providers})
    return web.json_response({
        "paused": state.paused,
        "all_categories": all_categories,
        "enabled_categories": sorted(state.enabled_categories),
        "runtime_config": state.runtime_cfg.as_dict(),
        "health": _snapshot(state),
        "target_count": len(state.providers),
        "enabled_target_count": len(state.enabled_providers()),
    })


async def h_targets(request: web.Request) -> web.Response:
    state: RunState = request.app["state"]
    out = []
    for p in state.providers:
        out.append({
            "name": p.name,
            "category": p.category,
            "effective_enabled": state.is_target_enabled(p.name),
            "individually_enabled": p.name in state.enabled_targets,
            "category_enabled": p.category in state.enabled_categories,
            "hits": state.health.per_target.get(p.name, 0),
            "last_status": state.health.per_target_last_status.get(p.name),
            "last_ok": state.health.per_target_last_ok.get(p.name),
        })
    return web.json_response({"targets": out})


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------

async def h_pause(request: web.Request) -> web.Response:
    state: RunState = request.app["state"]
    state.pause()
    state.record_event({
        "type": "scheduler",
        "timestamp": _now_iso(),
        "event": "paused",
    })
    log.info("ui_pause")
    return web.json_response({"paused": True})


async def h_resume(request: web.Request) -> web.Response:
    state: RunState = request.app["state"]
    state.resume()
    state.record_event({
        "type": "scheduler",
        "timestamp": _now_iso(),
        "event": "resumed",
    })
    log.info("ui_resume")
    return web.json_response({"paused": False})


async def h_toggle_target(request: web.Request) -> web.Response:
    state: RunState = request.app["state"]
    name = request.match_info["name"]
    if name not in state.providers_by_name:
        return web.json_response({"error": "unknown target"}, status=404)
    data = await _json_body(request)
    enabled = bool(data.get("enabled"))
    state.set_target_enabled(name, enabled)
    return web.json_response({
        "name": name,
        "individually_enabled": name in state.enabled_targets,
        "effective_enabled": state.is_target_enabled(name),
    })


async def h_toggle_category(request: web.Request) -> web.Response:
    state: RunState = request.app["state"]
    category = request.match_info["category"]
    data = await _json_body(request)
    enabled = bool(data.get("enabled"))
    state.set_category_enabled(category, enabled)
    return web.json_response({
        "category": category,
        "enabled": category in state.enabled_categories,
    })


async def h_fire(request: web.Request) -> web.Response:
    state: RunState = request.app["state"]
    data = await _json_body(request)
    name = data.get("target")
    if name:
        if name not in state.providers_by_name:
            return web.json_response({"error": "unknown target"}, status=404)
    else:
        providers = state.enabled_providers()
        if not providers:
            return web.json_response(
                {"error": "no targets enabled"}, status=400
            )
        name = random.choice(providers).name
    try:
        state.manual_fire_queue.put_nowait(name)
    except asyncio.QueueFull:
        return web.json_response({"error": "fire queue full"}, status=503)
    return web.json_response({"queued": name})


async def h_burst(request: web.Request) -> web.Response:
    state: RunState = request.app["state"]
    data = await _json_body(request)
    size = int(data.get("size", 5))
    size = max(1, min(size, 20))
    providers = state.enabled_providers()
    if not providers:
        return web.json_response({"error": "no targets enabled"}, status=400)
    queued: list[str] = []
    for _ in range(size):
        name = random.choice(providers).name
        try:
            state.manual_fire_queue.put_nowait(name)
            queued.append(name)
        except asyncio.QueueFull:
            break
    return web.json_response({"queued": queued, "count": len(queued)})


async def h_config(request: web.Request) -> web.Response:
    state: RunState = request.app["state"]
    data = await _json_body(request)
    try:
        changed = state.runtime_cfg.update_from(data)
    except (ValueError, TypeError) as e:
        return web.json_response({"error": str(e)}, status=400)
    if changed:
        state.record_event({
            "type": "scheduler",
            "timestamp": _now_iso(),
            "event": "config_updated",
            "fields": changed,
        })
        log.info("ui_config_update", changed=changed)
    return web.json_response({
        "changed": changed,
        "runtime_config": state.runtime_cfg.as_dict(),
    })


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

async def h_logs_recent(request: web.Request) -> web.Response:
    state: RunState = request.app["state"]
    try:
        limit = int(request.query.get("limit", "200"))
    except ValueError:
        limit = 200
    limit = max(1, min(limit, state.EVENT_RING_SIZE))
    events = list(state.event_ring)[-limit:]
    return web.json_response({"events": events})


async def h_logs_stream(request: web.Request) -> web.StreamResponse:
    state: RunState = request.app["state"]
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
    await response.prepare(request)
    q = state.subscribe()
    try:
        # Replay the ring buffer first.
        for event in list(state.event_ring):
            await response.write(_sse_frame(event))

        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=15.0)
                await response.write(_sse_frame(event))
            except asyncio.TimeoutError:
                # Comment line keeps the connection alive through proxies.
                await response.write(b": keepalive\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("sse_error", error=str(e))
    finally:
        state.unsubscribe(q)
    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sse_frame(event: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event)}\n\n".encode("utf-8")


def _now_iso() -> str:
    import datetime as _dt
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


async def _json_body(request: web.Request) -> dict[str, Any]:
    if not request.can_read_body:
        return {}
    try:
        return await request.json()
    except (json.JSONDecodeError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def build_app(state: RunState) -> web.Application:
    app = web.Application()
    app["state"] = state

    app.router.add_get("/", h_index)
    app.router.add_get("/healthz", h_healthz)
    app.router.add_get("/metrics", h_metrics)

    app.router.add_get("/api/state", h_state)
    app.router.add_get("/api/targets", h_targets)
    app.router.add_post("/api/targets/{name}/toggle", h_toggle_target)
    app.router.add_post("/api/categories/{category}/toggle", h_toggle_category)
    app.router.add_post("/api/control/pause", h_pause)
    app.router.add_post("/api/control/resume", h_resume)
    app.router.add_post("/api/fire", h_fire)
    app.router.add_post("/api/burst", h_burst)
    app.router.add_post("/api/config", h_config)
    app.router.add_get("/api/logs/recent", h_logs_recent)
    app.router.add_get("/api/logs/stream", h_logs_stream)

    return app


async def run_web_server(state: RunState, host: str, port: int) -> None:
    app = build_app(state)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    log.info("web_server_listening", host=host, port=port)
    try:
        # Park until cancelled.
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
