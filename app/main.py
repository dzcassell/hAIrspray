"""Entry point for the AI traffic simulator daemon."""
from __future__ import annotations

import asyncio
import logging
import random
import signal
import sys

import httpx
import structlog

from .config import Config
from .health import HealthState, run_health_server
from .providers import Provider, ProviderResult
from .registry import build_registry


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
# Execution
# ---------------------------------------------------------------------------

async def _run_one(
    provider: Provider,
    client: httpx.AsyncClient,
    state: HealthState,
) -> ProviderResult:
    result = await provider.execute(client)
    state.record(result.category, result.name, result.ok)
    log.info(
        "traffic",
        target=result.name,
        category=result.category,
        method=result.method,
        url=result.url,
        status=result.status_code,
        ok=result.ok,
        error=result.error,
        snippet=result.response_snippet,
    )
    return result


async def _scheduler(
    cfg: Config,
    providers: list[Provider],
    client: httpx.AsyncClient,
    state: HealthState,
    stop: asyncio.Event,
) -> None:
    rng = random.Random()
    log.info(
        "scheduler_started",
        target_count=len(providers),
        categories=sorted(cfg.categories),
        min_interval=cfg.min_interval,
        max_interval=cfg.max_interval,
        burst_probability=cfg.burst_probability,
    )

    while not stop.is_set():
        # Decide: single shot or burst?
        if rng.random() < cfg.burst_probability:
            burst_size = rng.randint(cfg.burst_min_size, cfg.burst_max_size)
            log.info("burst_start", size=burst_size)
            for _ in range(burst_size):
                if stop.is_set():
                    break
                provider = rng.choice(providers)
                await _run_one(provider, client, state)
                gap = rng.uniform(cfg.burst_gap_min, cfg.burst_gap_max)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=gap)
                except asyncio.TimeoutError:
                    pass
            log.info("burst_end")
        else:
            provider = rng.choice(providers)
            await _run_one(provider, client, state)

        # Inter-request sleep (respect shutdown)
        delay = rng.uniform(cfg.min_interval, cfg.max_interval)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    log.info("scheduler_stopped")


# ---------------------------------------------------------------------------
# Signal handling & boot
# ---------------------------------------------------------------------------

async def _main_async() -> int:
    cfg = Config.from_env()
    _configure_logging(cfg.log_level)

    providers = build_registry(cfg.categories)
    if not providers:
        log.error("no_providers_configured", categories=sorted(cfg.categories))
        return 2

    state = HealthState()
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows; we won't hit this in the container but keep defensive.
            pass

    limits = httpx.Limits(
        max_connections=max(cfg.max_concurrent * 4, 16),
        max_keepalive_connections=max(cfg.max_concurrent * 2, 8),
    )
    timeout = httpx.Timeout(cfg.http_timeout, connect=min(10.0, cfg.http_timeout))

    log.info(
        "booting",
        target_count=len(providers),
        enable_real_responses=cfg.enable_real_responses,
        health_port=cfg.health_port,
    )

    async with httpx.AsyncClient(
        http2=True,
        limits=limits,
        timeout=timeout,
        follow_redirects=False,  # providers opt in where appropriate
        verify=True,
    ) as client:
        tasks = [
            asyncio.create_task(run_health_server(cfg.health_port, state)),
            asyncio.create_task(_scheduler(cfg, providers, client, state, stop)),
        ]

        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        for t in done:
            exc = t.exception()
            if exc:
                log.error("task_crashed", error=str(exc))

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
