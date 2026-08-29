"""WP14 — a background loop that dies must come back, and say that it fell.

Everything in this bridge is self-healing in the small: PyMax reconnects, each
poller retries. What none of them survives is the task itself ending, which is
silent — messages just stop. These tests pin the three behaviours that make that
visible and recoverable.
"""

from __future__ import annotations

import asyncio

from bridge.service.supervisor import Supervisor


async def test_a_crashed_task_is_restarted() -> None:
    attempts: list[int] = []
    done = asyncio.Event()

    def factory() -> object:
        async def loop() -> None:
            attempts.append(len(attempts))
            if len(attempts) < 3:
                raise RuntimeError("fell over")
            done.set()
            await asyncio.sleep(3600)

        return loop()

    supervisor = Supervisor(initial_backoff=0.01, max_backoff=0.02)
    supervisor.start("flaky", factory)  # type: ignore[arg-type]
    await asyncio.wait_for(done.wait(), timeout=2)

    try:
        assert len(attempts) == 3
        state = supervisor.snapshot()["flaky"]
        assert state.restarts == 2
        assert state.last_error is not None and "fell over" in state.last_error
        assert state.running is True
    finally:
        await supervisor.stop()


async def test_a_task_that_finishes_stays_finished() -> None:
    """A poller stopping on a revoked token decided that; do not fight it."""
    runs: list[int] = []

    def factory() -> object:
        async def loop() -> None:
            runs.append(1)

        return loop()

    supervisor = Supervisor(initial_backoff=0.01)
    supervisor.start("one-shot", factory)  # type: ignore[arg-type]
    await asyncio.sleep(0.1)

    try:
        assert runs == [1]
        state = supervisor.snapshot()["one-shot"]
        assert state.stopped_deliberately is True
        assert supervisor.unhealthy == [], "a clean exit is not a fault"
    finally:
        await supervisor.stop()


async def test_a_dead_task_is_reported_as_unhealthy() -> None:
    """`/status` saying 'polling is down' beats a stack trace nobody reads."""
    supervisor = Supervisor(initial_backoff=5, max_backoff=5)

    def factory() -> object:
        async def loop() -> None:
            raise RuntimeError("down")

        return loop()

    supervisor.start("broken", factory)  # type: ignore[arg-type]
    await asyncio.sleep(0.05)

    try:
        assert [state.name for state in supervisor.unhealthy] == ["broken"]
    finally:
        await supervisor.stop()


async def test_stop_cancels_everything() -> None:
    started = asyncio.Event()

    def factory() -> object:
        async def loop() -> None:
            started.set()
            await asyncio.sleep(3600)

        return loop()

    supervisor = Supervisor()
    supervisor.start("long", factory)  # type: ignore[arg-type]
    await asyncio.wait_for(started.wait(), timeout=1)

    await supervisor.stop()

    assert supervisor.snapshot()["long"].running is False


async def test_starting_twice_does_not_double_up() -> None:
    running = asyncio.Event()

    def factory() -> object:
        async def loop() -> None:
            running.set()
            await asyncio.sleep(3600)

        return loop()

    supervisor = Supervisor()
    supervisor.start("single", factory)  # type: ignore[arg-type]
    await asyncio.wait_for(running.wait(), timeout=1)
    supervisor.start("single", factory)  # type: ignore[arg-type]

    try:
        assert len(supervisor.snapshot()) == 1
    finally:
        await supervisor.stop()
