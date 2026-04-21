"""Entry point for the ai-spray daemon.

Runs three concurrent tasks:

1. ``_scheduler``     — the pacing loop that fires traffic at random
   targets on the interval range in ``state.runtime_cfg`` (unless
   paused).
2. ``_manual_fire_worker`` — pulls names out of the fire queue and
   executes them out-of-band of the scheduler. Runs even when the
   scheduler is paused.
3. ``run_web_server`` — the aiohttp UI + REST + SSE server.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
import random
import signal
import sys

import httpx
import structlog

from .config import Config
from .providers import Provider, ProviderResult
from .registry import build_registry
from .state import RunState
from .web import run_web_server


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _configure_logging(level: str) -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level, logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level, logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


async def _run_one(
    provider: Provider,
    client: httpx.AsyncClient,
    state: RunState,
    trigger: str = "scheduler",
) -> ProviderResult:
    result = await provider.execute(client)
    state.health.record(result.category, result.name, result.ok, result.status_code)

    event = {
        "type": "traffic",
        "timestamp": _now_iso(),
        "trigger": trigger,
        "target": result.name,
        "category": result.category,
        "method": result.method,
        "url": result.url,
        "status": result.status_code,
        "ok": result.ok,
        "error": result.error,
        "snippet": result.response_snippet,
    }

    # Stdout JSON log for docker logs / journal / opensearch ingestion.
    log.info(
        "traffic",
        trigger=trigger,
        target=result.name,
        category=result.category,
        method=result.method,
        url=result.url,
        status=result.status_code,
        ok=result.ok,
        error=result.error,
        snippet=result.response_snippet,
    )
    # UI / SSE stream + ring buffer.
    state.record_event(event)
    return result


# ---------------------------------------------------------------------------
# Scheduler (automatic pacing)
# ---------------------------------------------------------------------------

async def _scheduler(
    state: RunState,
    client: httpx.AsyncClient,
    stop: asyncio.Event,
) -> None:
    rng = random.Random()
    log.info(
        "scheduler_started",
        target_count=len(state.providers),
        categories=sorted(state.enabled_categories),
    )

    while not stop.is_set():
        # Wait while paused. The running Event is set when running; we
        # wake cheaply when the UI resumes us.
        if state.paused:
            try:
                await asyncio.wait_for(state.running.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                continue

        providers = state.enabled_providers()
        if not providers:
            # Nothing enabled; idle-poll for config changes.
            try:
                await asyncio.wait_for(stop.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
            continue

        cfg = state.runtime_cfg

        if rng.random() < cfg.burst_probability:
            burst_size = rng.randint(cfg.burst_min_size, cfg.burst_max_size)
            state.record_event({
                "type": "scheduler",
                "timestamp": _now_iso(),
                "event": "burst_start",
                "size": burst_size,
            })
            for _ in range(burst_size):
                if stop.is_set() or state.paused:
                    break
                providers_now = state.enabled_providers()
                if not providers_now:
                    break
                provider = rng.choice(providers_now)
                await _run_one(provider, client, state, trigger="scheduler:burst")
                gap = rng.uniform(cfg.burst_gap_min, cfg.burst_gap_max)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=gap)
                except asyncio.TimeoutError:
                    pass
            state.record_event({
                "type": "scheduler",
                "timestamp": _now_iso(),
                "event": "burst_end",
            })
        else:
            provider = rng.choice(providers)
            await _run_one(provider, client, state, trigger="scheduler")

        # Inter-iteration sleep.
        delay = rng.uniform(cfg.min_interval, cfg.max_interval)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    log.info("scheduler_stopped")


# ---------------------------------------------------------------------------
# Manual-fire worker (out-of-band of the scheduler)
# ---------------------------------------------------------------------------

async def _manual_fire_worker(
    state: RunState,
    client: httpx.AsyncClient,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            name = await asyncio.wait_for(
                state.manual_fire_queue.get(), timeout=1.0
            )
        except asyncio.TimeoutError:
            continue
        provider = state.providers_by_name.get(name)
        if provider is None:
            continue
        try:
            await _run_one(provider, client, state, trigger="manual")
        except Exception as e:  # noqa: BLE001
            log.warning("manual_fire_error", target=name, error=str(e))


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------

async def _main_async() -> int:
    cfg = Config.from_env()
    _configure_logging(cfg.log_level)

    providers = build_registry(cfg.categories)
    if not providers:
        log.error("no_providers_configured", categories=sorted(cfg.categories))
        return 2

    state = RunState(cfg, providers)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    limits = httpx.Limits(
        max_connections=max(cfg.max_concurrent * 4, 16),
        max_keepalive_connections=max(cfg.max_concurrent * 2, 8),
    )
    timeout = httpx.Timeout(cfg.http_timeout, connect=min(10.0, cfg.http_timeout))

    web_host = os.getenv("WEB_HOST", "0.0.0.0")
    web_port = int(os.getenv("WEB_PORT", str(cfg.health_port)))

    log.info(
        "booting",
        target_count=len(providers),
        enable_real_responses=cfg.enable_real_responses,
        web_host=web_host,
        web_port=web_port,
    )

    async with httpx.AsyncClient(
        http2=True,
        limits=limits,
        timeout=timeout,
        follow_redirects=False,
        verify=True,
    ) as client:
        tasks = [
            asyncio.create_task(run_web_server(state, web_host, web_port),
                                name="web"),
            asyncio.create_task(_scheduler(state, client, stop),
                                name="scheduler"),
            asyncio.create_task(_manual_fire_worker(state, client, stop),
                                name="fire_worker"),
            # Watchdog: trip stop when SIGTERM arrives via the event.
            asyncio.create_task(stop.wait(), name="stop_watch"),
        ]

        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )

        # Something finished (likely stop_watch). Cancel the rest.
        stop.set()
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        for t in done:
            exc = t.exception() if not t.cancelled() else None
            if exc:
                log.error("task_crashed", task=t.get_name(), error=str(exc))

    log.info("shutdown_complete")
    return 0


def main() -> None:
    try:
        code = asyncio.run(_main_async())
    except KeyboardInterrupt:
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
