from __future__ import annotations

import asyncio
import json
from pathlib import Path

from bridge.service.heartbeat import RuntimeHeartbeat, check_heartbeat


def test_missing_damaged_and_stale_heartbeats_are_unhealthy(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    assert not check_heartbeat(path, now=100).healthy

    path.write_text("not-json", encoding="utf-8")
    assert not check_heartbeat(path, now=100).healthy

    path.write_text(json.dumps({"updated_at": 1}), encoding="utf-8")
    status = check_heartbeat(path, now=100, max_age_seconds=60)
    assert not status.healthy
    assert status.age_seconds == 99


def test_a_fresh_heartbeat_is_healthy(tmp_path: Path) -> None:
    path = tmp_path / "state" / "heartbeat.json"
    heartbeat = RuntimeHeartbeat(path, clock=lambda: 95)
    heartbeat.touch()

    status = check_heartbeat(path, now=100, max_age_seconds=60)

    assert status.healthy
    assert status.age_seconds == 5
    assert (path.stat().st_mode & 0o777) == 0o600


async def test_the_pulse_advances_until_the_supervisor_stops(tmp_path: Path) -> None:
    ticks = iter(float(value) for value in range(1, 100))
    heartbeat = RuntimeHeartbeat(
        tmp_path / "heartbeat.json",
        interval_seconds=0.01,
        clock=lambda: next(ticks),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(heartbeat.run(stop))

    updated = 0.0
    for _ in range(20):
        await asyncio.sleep(0.01)
        updated = json.loads(heartbeat.path.read_text(encoding="utf-8"))["updated_at"]
        if updated >= 2:
            break
    stop.set()
    await task

    assert updated >= 2
