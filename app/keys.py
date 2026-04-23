"""Persistent API key store.

Stores API keys for keyed prompt providers at rest in a single JSON
file, mounted from a Docker volume so values survive container
restarts and image rebuilds.

## Scope and caveats

This is an experimental lab tool. The store is plaintext JSON with
file permissions 0600 — that keeps it away from other users inside
the container, but anyone with Docker socket access on the host
(i.e., anyone in the `docker` group) can read the volume. Do not
put keys here that would cost real money if leaked.

## File layout

::

    {
      "version": 1,
      "keys": {
        "google":     "AIzaSy...",
        "groq":       "gsk_...",
        "mistral":    "...",
        ...
      }
    }

Provider ids are stable slugs matching the ``PROMPT_TARGETS`` entries
in ``prompt.py``. The dict only contains providers the user has
supplied keys for; absence means "no key set". The ``version`` field
lets future schema migrations happen without data loss.

## Concurrency model

Writes are atomic via write-to-temp + os.replace so a crashed process
cannot leave a half-written file. Reads and writes are serialized
through an ``asyncio.Lock`` because both the HTTP layer and the
prompt runner can touch the store.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Default location inside the container. Docker compose mounts a named
# volume at this path. Overrideable via the AI_SPRAY_KEYS_PATH env var
# for tests / local dev.
DEFAULT_KEYS_PATH = Path(os.environ.get(
    "AI_SPRAY_KEYS_PATH", "/data/keys.json"
))

_SCHEMA_VERSION = 1
_FILE_MODE = 0o600
_DIR_MODE = 0o700


class KeyStore:
    """Asynchronous, file-backed key store."""

    def __init__(self, path: Path = DEFAULT_KEYS_PATH):
        self._path = path
        self._lock = asyncio.Lock()
        self._keys: dict[str, str] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # Loading / saving
    # ------------------------------------------------------------------

    async def load(self) -> None:
        """Read the keys file from disk. Safe to call repeatedly; a
        successful load clears any in-memory state first."""
        async with self._lock:
            self._keys = await asyncio.to_thread(self._load_sync)
            self._loaded = True

    def _load_sync(self) -> dict[str, str]:
        if not self._path.exists():
            log.info("no keys file at %s (fresh install)", self._path)
            return {}

        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as e:
            log.error("cannot read keys file %s: %s", self._path, e)
            return {}

        if not raw.strip():
            return {}

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            log.error(
                "keys file %s is corrupt JSON (%s); leaving empty — "
                "fix or delete the file to recover",
                self._path, e,
            )
            return {}

        if not isinstance(data, dict):
            log.error("keys file root is not an object; ignoring")
            return {}

        keys = data.get("keys", {})
        if not isinstance(keys, dict):
            return {}

        # Filter to string -> string only; silently drop other types.
        clean: dict[str, str] = {}
        for prov, val in keys.items():
            if isinstance(prov, str) and isinstance(val, str) and val.strip():
                clean[prov] = val.strip()
        return clean

    async def _save_locked(self) -> None:
        """Persist the in-memory dict. Caller must hold ``self._lock``."""
        await asyncio.to_thread(self._save_sync)

    def _save_sync(self) -> None:
        payload = json.dumps(
            {"version": _SCHEMA_VERSION, "keys": self._keys},
            indent=2, sort_keys=True,
        ) + "\n"

        parent = self._path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(parent, _DIR_MODE)
            except OSError:
                pass  # may fail on mounted volumes on some hosts
        except OSError as e:
            log.error("cannot create keys directory %s: %s", parent, e)
            raise

        # Atomic write: temp file in the same directory, flush + fsync,
        # then os.replace to swap it into place.
        fd, tmp_path = tempfile.mkstemp(
            prefix=".keys-", suffix=".tmp", dir=parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp_path, _FILE_MODE)
            os.replace(tmp_path, self._path)
        except OSError:
            # Best effort cleanup if the replace failed.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # Read / write API
    # ------------------------------------------------------------------

    async def has(self, provider: str) -> bool:
        if not self._loaded:
            await self.load()
        return provider in self._keys

    async def get(self, provider: str) -> str | None:
        if not self._loaded:
            await self.load()
        return self._keys.get(provider)

    async def set(self, provider: str, key: str) -> None:
        key = (key or "").strip()
        if not key:
            raise ValueError("key must be a non-empty string")
        async with self._lock:
            if not self._loaded:
                self._keys = await asyncio.to_thread(self._load_sync)
                self._loaded = True
            self._keys[provider] = key
            await self._save_locked()
        log.info("saved key for provider %s", provider)

    async def delete(self, provider: str) -> bool:
        async with self._lock:
            if not self._loaded:
                self._keys = await asyncio.to_thread(self._load_sync)
                self._loaded = True
            if provider not in self._keys:
                return False
            del self._keys[provider]
            await self._save_locked()
        log.info("deleted key for provider %s", provider)
        return True

    async def summary(self) -> dict[str, Any]:
        """Non-sensitive summary: which providers have keys, with a
        masked preview of each (first 4 + last 4 chars).

        Never returns full key values.
        """
        if not self._loaded:
            await self.load()
        out: dict[str, dict[str, Any]] = {}
        for prov, val in self._keys.items():
            out[prov] = {
                "present": True,
                "preview": _mask(val),
                "length": len(val),
            }
        return {"providers": out, "path": str(self._path)}


def _mask(v: str) -> str:
    if len(v) <= 10:
        return "•" * len(v)
    return f"{v[:4]}…{v[-4:]}"
