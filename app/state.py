"""Shared mutable run-state.

All the bits the scheduler and the web server both touch live here:

* ``RuntimeConfig`` — the pacing/burst knobs, mutated live via the UI.
* ``HealthState``   — aggregate counters.
* ``RunState``      — the umbrella object held by both the scheduler
  task and the aiohttp app.

The SSE broadcast is a simple ring buffer + fan-out: every recorded
event gets appended to ``event_ring`` and pushed into each subscriber's
``asyncio.Queue``. Slow or dead subscribers are pruned lazily.
"""
from __future__ import annotations

import asyncio
import collections
import time
from dataclasses import dataclass, field
from typing import Any, Deque, Set

from .config import Config


# ---------------------------------------------------------------------------
# Runtime config (mutable version of the pacing knobs)
# ---------------------------------------------------------------------------

@dataclass
class RuntimeConfig:
    min_interval: float
    max_interval: float
    burst_probability: float
    burst_min_size: int
    burst_max_size: int
    burst_gap_min: float
    burst_gap_max: float

    @classmethod
    def from_config(cls, cfg: Config) -> "RuntimeConfig":
        return cls(
            min_interval=cfg.min_interval,
            max_interval=cfg.max_interval,
            burst_probability=cfg.burst_probability,
            burst_min_size=cfg.burst_min_size,
            burst_max_size=cfg.burst_max_size,
            burst_gap_min=cfg.burst_gap_min,
            burst_gap_max=cfg.burst_gap_max,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "min_interval": self.min_interval,
            "max_interval": self.max_interval,
            "burst_probability": self.burst_probability,
            "burst_min_size": self.burst_min_size,
            "burst_max_size": self.burst_max_size,
            "burst_gap_min": self.burst_gap_min,
            "burst_gap_max": self.burst_gap_max,
        }

    def update_from(self, data: dict[str, Any]) -> list[str]:
        """Apply partial update; return list of changed fields.

        Raises ``ValueError`` on invalid values. Cross-field invariants are
        checked after individual field validation.
        """
        changed: list[str] = []

        def set_float(key: str, lo: float, hi: float) -> None:
            if key in data and data[key] is not None:
                v = float(data[key])
                if not (lo <= v <= hi):
                    raise ValueError(f"{key} must be in [{lo}, {hi}]")
                if v != getattr(self, key):
                    setattr(self, key, v)
                    changed.append(key)

        def set_int(key: str, lo: int, hi: int) -> None:
            if key in data and data[key] is not None:
                v = int(data[key])
                if not (lo <= v <= hi):
                    raise ValueError(f"{key} must be in [{lo}, {hi}]")
                if v != getattr(self, key):
                    setattr(self, key, v)
                    changed.append(key)

        set_float("min_interval", 0.1, 3600.0)
        set_float("max_interval", 0.1, 3600.0)
        set_float("burst_probability", 0.0, 1.0)
        set_int("burst_min_size", 1, 50)
        set_int("burst_max_size", 1, 50)
        set_float("burst_gap_min", 0.0, 60.0)
        set_float("burst_gap_max", 0.0, 60.0)

        if self.min_interval > self.max_interval:
            raise ValueError("min_interval cannot exceed max_interval")
        if self.burst_min_size > self.burst_max_size:
            raise ValueError("burst_min_size cannot exceed burst_max_size")
        if self.burst_gap_min > self.burst_gap_max:
            raise ValueError("burst_gap_min cannot exceed burst_gap_max")

        return changed


# ---------------------------------------------------------------------------
# Health / counters
# ---------------------------------------------------------------------------

@dataclass
class HealthState:
    started_at: float = field(default_factory=time.time)
    last_tick_at: float | None = None
    total_requests: int = 0
    total_ok: int = 0
    total_errors: int = 0
    per_category: dict[str, int] = field(default_factory=dict)
    per_target: dict[str, int] = field(default_factory=dict)
    per_target_last_status: dict[str, int | None] = field(default_factory=dict)
    per_target_last_ok: dict[str, bool] = field(default_factory=dict)

    def record(self, category: str, name: str, ok: bool, status: int | None) -> None:
        self.last_tick_at = time.time()
        self.total_requests += 1
        if ok:
            self.total_ok += 1
        else:
            self.total_errors += 1
        self.per_category[category] = self.per_category.get(category, 0) + 1
        self.per_target[name] = self.per_target.get(name, 0) + 1
        self.per_target_last_status[name] = status
        self.per_target_last_ok[name] = ok

    def snapshot(self) -> dict[str, Any]:
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
            "per_category": dict(self.per_category),
            "per_target": dict(
                sorted(self.per_target.items(), key=lambda kv: -kv[1])
            ),
        }


# ---------------------------------------------------------------------------
# Umbrella run state
# ---------------------------------------------------------------------------

class RunState:
    """Shared, mutable runtime state held by scheduler and web server."""

    EVENT_RING_SIZE = 500
    SSE_QUEUE_SIZE = 200
    FIRE_QUEUE_SIZE = 100

    def __init__(self, cfg: Config, providers: list) -> None:
        self.static_cfg = cfg
        self.providers = providers
        self.providers_by_name: dict[str, Any] = {p.name: p for p in providers}

        self.runtime_cfg = RuntimeConfig.from_config(cfg)
        self.health = HealthState()

        # Everything enabled out of the gate.
        self.enabled_targets: Set[str] = {p.name for p in providers}
        self.enabled_categories: Set[str] = set(cfg.categories)

        # Pause is represented as an asyncio.Event so the scheduler can
        # cheaply block on it. Event "set" means running; "clear" means
        # paused.
        self.running = asyncio.Event()
        self.running.set()

        # Manual fires: the UI enqueues a provider name, a worker task
        # pulls from this queue and runs it out-of-band of the scheduler.
        self.manual_fire_queue: asyncio.Queue[str] = asyncio.Queue(
            maxsize=self.FIRE_QUEUE_SIZE
        )

        # Event ring + SSE fan-out.
        self.event_ring: Deque[dict[str, Any]] = collections.deque(
            maxlen=self.EVENT_RING_SIZE
        )
        self.sse_subscribers: Set[asyncio.Queue] = set()

    # ----- pause / resume -----

    @property
    def paused(self) -> bool:
        return not self.running.is_set()

    def pause(self) -> None:
        self.running.clear()

    def resume(self) -> None:
        self.running.set()

    # ----- enabled-set helpers -----

    def is_target_enabled(self, name: str) -> bool:
        provider = self.providers_by_name.get(name)
        if provider is None:
            return False
        return (
            name in self.enabled_targets
            and provider.category in self.enabled_categories
        )

    def enabled_providers(self) -> list:
        return [p for p in self.providers if self.is_target_enabled(p.name)]

    def set_target_enabled(self, name: str, enabled: bool) -> None:
        if enabled:
            self.enabled_targets.add(name)
        else:
            self.enabled_targets.discard(name)

    def set_category_enabled(self, category: str, enabled: bool) -> None:
        if enabled:
            self.enabled_categories.add(category)
        else:
            self.enabled_categories.discard(category)

    # ----- event broadcast -----

    def record_event(self, event: dict[str, Any]) -> None:
        self.event_ring.append(event)
        dead: list[asyncio.Queue] = []
        for q in self.sse_subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.sse_subscribers.discard(q)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.SSE_QUEUE_SIZE)
        self.sse_subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.sse_subscribers.discard(q)
