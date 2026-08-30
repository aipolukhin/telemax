"""A local liveness signal for service managers and container healthchecks.

External MAX or Telegram availability is not process health. The outer Telemax
runtime deliberately stays alive while either integration is degraded, so this
heartbeat answers the smaller and more useful question: is the supervisor event
loop still making progress?
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bridge.config.writer import atomic_write_text

HEARTBEAT_FILE_NAME = "runtime-heartbeat.json"
HEARTBEAT_INTERVAL_SECONDS = 15.0
HEARTBEAT_MAX_AGE_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class HeartbeatStatus:
    healthy: bool
    detail: str
    age_seconds: float | None = None


class RuntimeHeartbeat:
    """Persist a tiny supervisor pulse without probing either messenger."""

    def __init__(
        self,
        path: Path,
        *,
        interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self._interval = interval_seconds
        self._clock = clock

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> RuntimeHeartbeat:
        return cls(data_dir / "state" / HEARTBEAT_FILE_NAME)

    def touch(self) -> None:
        payload = {
            "schema": 1,
            "pid": os.getpid(),
            "updated_at": self._clock(),
        }
        atomic_write_text(
            self.path,
            json.dumps(payload, separators=(",", ":")) + "\n",
        )

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self.touch()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue


def check_heartbeat(
    path: Path,
    *,
    max_age_seconds: float = HEARTBEAT_MAX_AGE_SECONDS,
    now: float | None = None,
) -> HeartbeatStatus:
    """Read and validate one pulse. Never raises on damaged local state."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        updated_at = float(raw["updated_at"])
    except FileNotFoundError:
        return HeartbeatStatus(False, "heartbeat is missing")
    except (OSError, ValueError, TypeError, KeyError):
        return HeartbeatStatus(False, "heartbeat is unreadable")

    age = max(0.0, (time.time() if now is None else now) - updated_at)
    if age > max_age_seconds:
        return HeartbeatStatus(False, f"heartbeat is stale ({age:.0f}s)", age)
    return HeartbeatStatus(True, f"supervisor heartbeat is fresh ({age:.0f}s)", age)


__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "HEARTBEAT_MAX_AGE_SECONDS",
    "HeartbeatStatus",
    "RuntimeHeartbeat",
    "check_heartbeat",
]
