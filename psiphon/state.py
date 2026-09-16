"""Atomic storage for non-secret Psiphon operational history."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .models import PUBLIC_STATES, bounded_float, safe_region

SCHEMA_VERSION = 1
SAFE_KEYS = frozenset(
    {
        "last_successful_location",
        "last_successful_setup_ms",
        "last_rotation_at",
        "rotation_count",
        "last_proxy_retest_at",
        "last_successful_health_check_at",
        "last_state",
    }
)


def sanitize(data: object) -> dict[str, Any]:
    raw = data if isinstance(data, dict) else {}
    state = str(raw.get("last_state") or "").upper()
    return {
        "schema_version": SCHEMA_VERSION,
        "last_successful_location": safe_region(raw.get("last_successful_location")),
        "last_successful_setup_ms": round(bounded_float(raw.get("last_successful_setup_ms")), 2),
        "last_rotation_at": _safe_timestamp(raw.get("last_rotation_at")),
        "rotation_count": _bounded_int(raw.get("rotation_count"), 0, 1_000_000),
        "last_proxy_retest_at": _safe_timestamp(raw.get("last_proxy_retest_at")),
        "last_successful_health_check_at": _safe_timestamp(
            raw.get("last_successful_health_check_at"),
        ),
        "last_state": state if state in PUBLIC_STATES else "",
    }


def _safe_timestamp(value: object) -> str:
    text = str(value or "").strip()
    # An ISO string is only display metadata. Keep it bounded even if an
    # operator's older state file contains unexpected content.
    return text[:64] if "T" in text and len(text) <= 64 else ""


def _bounded_int(value: object, default: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(parsed, high))


class OperationalStateStore:
    """A dedicated file prevents any optional Core state from changing Lumen's
    existing link/session persistence schema."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "session-state.json"

    async def load(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._load_sync)

    def _load_sync(self) -> dict[str, Any]:
        try:
            return sanitize(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return sanitize({})

    async def save(self, data: dict[str, Any]) -> None:
        await asyncio.to_thread(self._save_sync, sanitize(data))

    def _save_sync(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        fd, tmp_name = tempfile.mkstemp(
            prefix=".session-state-", suffix=".json", dir=str(self.path.parent),
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass