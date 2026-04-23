"""Entry point: run the scheduler loop and the Starlette HTTP app side by side."""
from __future__ import annotations

import asyncio
import logging
import random
import signal
import sys

import httpx
import structlog
import uvicorn

from .config import Config
from .registry import build_registry
from .state import AppState
from .web import create_app, run_and_publish


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _configure_logging(level: str) -> None:
    lvl = getattr(logging, level, logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=lvl)
    # Chatty httpx/httpcore internals just clutter the journal.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # Silence uvicorn's per-request access log; our 'traffic' events are
    # the interesting thing.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Scheduler loop
# ---------------------------------------------------------------------------

async def _scheduler_loop(
    state: AppState,
    client: httpx.AsyncClient,
    stop: asyncio.Event,
) -> None:
    rng = random.Random()
    log.info(
        "scheduler_started",
        target_count=len(state.all_providers()),
        categories=sorted(state.config.categories),
    )

    async def _sleep_or_stop(seconds: float) -> None:
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    while not stop.is_set():
        # Block while paused via the UI.
        await state.wait_for_resume()
        if stop.is_set():
            break

        cfg = state.config
        eligible = state.eligible_providers()

        if not eligible:
            # No enabled targets in the currently-enabled categories;
            # idle briefly and re-check.
            await _sleep_or_stop(5.0)
            continue

        if rng.random() < cfg.burst_probability:
            size = rng.randint(cfg.burst_min_size, cfg.burst_max_size)
            log.info("burst_start", size=size)
            for _ in range(size):
                if stop.is_set() or not state.is_running():
                    break
                picks = state.eligible_providers()
                if not picks:
                    break
                await run_and_publish(
                    rng.choice(picks), client, state, source="burst"
                )
                gap = rng.uniform(cfg.burst_gap_min, cfg.burst_gap_max)
                await _sleep_or_stop(gap)
            log.info("burst_end")
        else:
            await run_and_publish(
                rng.choice(eligible), client, state, source="scheduler"
            )

        # Inter-iteration pacing.
        delay = rng.uniform(cfg.min_interval, cfg.max_interval)
        await _sleep_or_stop(delay)

    log.info("scheduler_stopped")


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------

async def _main_async() -> int:
    cfg = Config.from_env()
    _configure_logging(cfg.log_level)

    log.info(
        "booting",
        health_port=cfg.health_port,
        enable_real_responses=cfg.enable_real_responses,
        categories=sorted(cfg.categories),
        tls_verify=cfg.tls_verify,
    )

    # Build the full target catalogue; per-category filtering happens
    # at scheduling time so the UI can toggle categories live.
    providers = build_registry()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    limits = httpx.Limits(max_connections=32, max_keepalive_connections=8)
    timeout = httpx.Timeout(cfg.http_timeout, connect=min(10.0, cfg.http_timeout))

    # TLS verification defaults to False because hAIrspray is designed to
    # run behind SASE fabrics / NGFWs that decrypt-and-re-sign HTTPS with
    # their own CA. Verifying against Mozilla's trust store would reject
    # every inspected flow. Set TLS_VERIFY=true if running outside any
    # MitM inspection. When verify=False, httpx emits no warning of its
    # own, but we silence the noisy urllib3 one in case any transitive
    # dep reaches for it.
    if not cfg.tls_verify:
        import warnings
        try:
            from urllib3.exceptions import InsecureRequestWarning
            warnings.simplefilter("ignore", InsecureRequestWarning)
        except Exception:
            pass
        log.warning(
            "tls_verify_disabled",
            reason="TLS_VERIFY=false; certificate validation skipped. "
                   "Expected when running behind a TLS-inspecting SASE/NGFW.",
        )

    async with httpx.AsyncClient(
        http2=True,
        limits=limits,
        timeout=timeout,
        follow_redirects=False,
        verify=cfg.tls_verify,
    ) as client:

        state = AppState(initial_config=cfg, providers=providers)
        app = create_app(state, client)

        uvi_config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=cfg.health_port,
            log_level="warning",
            access_log=False,
            lifespan="off",
            loop="asyncio",
            timeout_graceful_shutdown=3,
        )
        server = uvicorn.Server(uvi_config)

        async def _watch_stop() -> None:
            await stop.wait()
            server.should_exit = True

        tasks = [
            asyncio.create_task(_scheduler_loop(state, client, stop),
                                name="scheduler"),
            asyncio.create_task(server.serve(), name="http"),
            asyncio.create_task(_watch_stop(), name="shutdown-watcher"),
        ]

        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )
        stop.set()
        server.should_exit = True

        for t in pending:
            t.cancel()
        for t in (*done, *pending):
            try:
                await t
            except (asyncio.CancelledError, Exception) as e:
                if not isinstance(e, asyncio.CancelledError):
                    log.warning("task_exited", task=t.get_name(), error=str(e))

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
