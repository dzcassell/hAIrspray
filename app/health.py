"""Minimal async HTTP server exposing ``/healthz`` and ``/metrics``.

No external deps; implemented on top of ``asyncio`` streams so it stays
tiny and robust.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger()


@dataclass
class HealthState:
    started_at: float = field(default_factory=time.time)
    last_tick_at: float | None = None
    total_requests: int = 0
    total_ok: int = 0
    total_errors: int = 0
    per_category: dict[str, int] = field(default_factory=dict)
    per_target: dict[str, int] = field(default_factory=dict)

    def record(self, category: str, name: str, ok: bool) -> None:
        self.last_tick_at = time.time()
        self.total_requests += 1
        if ok:
            self.total_ok += 1
        else:
            self.total_errors += 1
        self.per_category[category] = self.per_category.get(category, 0) + 1
        self.per_target[name] = self.per_target.get(name, 0) + 1

    def snapshot(self) -> dict:
        now = time.time()
        return {
            "uptime_seconds": round(now - self.started_at, 1),
            "last_tick_seconds_ago": (
                round(now - self.last_tick_at, 1)
                if self.last_tick_at is not None else None
            ),
            "total_requests": self.total_requests,
            "total_ok": self.total_ok,
            "total_errors": self.total_errors,
            "per_category": self.per_category,
            "per_target": dict(
                sorted(self.per_target.items(), key=lambda kv: -kv[1])
            ),
        }


async def _handle(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    state: HealthState,
) -> None:
    try:
        request_line = await reader.readline()
        if not request_line:
            return
        try:
            method, path, _ = request_line.decode("ascii", errors="replace").split(" ", 2)
        except ValueError:
            writer.close()
            return

        # Drain headers
        while True:
            line = await reader.readline()
            if not line or line in (b"\r\n", b"\n"):
                break

        if method != "GET":
            body = b"method not allowed"
            writer.write(
                b"HTTP/1.1 405 Method Not Allowed\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            await writer.drain()
            return

        if path.startswith("/healthz"):
            body = b"ok"
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
        elif path.startswith("/metrics") or path.startswith("/stats"):
            body = json.dumps(state.snapshot(), indent=2).encode("utf-8")
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
        else:
            body = b"not found"
            writer.write(
                b"HTTP/1.1 404 Not Found\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
        await writer.drain()
    except Exception as e:  # noqa: BLE001 — health endpoint must never crash
        log.warning("health_handler_error", error=str(e))
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def run_health_server(port: int, state: HealthState) -> None:
    server = await asyncio.start_server(
        lambda r, w: _handle(r, w, state), host="0.0.0.0", port=port
    )
    log.info("health_server_listening", port=port)
    async with server:
        await server.serve_forever()
