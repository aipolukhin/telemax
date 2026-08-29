"""The gate closes the Bot API path — and says so.

Found on the live stand, not by reading. With owner MTProto intake enabled, the
Bot API path stands aside for every owner-originated message: the gate stays shut
across a disconnect on purpose, because a lost message is the accepted trade
against a duplicated one. What was not acceptable is that the trade was **silent**.
A session that had stopped receiving owner updates looked exactly like a session
carrying them — the messages simply stopped arriving in MAX, no error, no
incident, and the only trace was a `debug` line nobody has enabled.

So the hand-off is now counted, durably, and logged at a level that is on. The
routing decision is unchanged: this is a report, never a gate.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest_asyncio

from bridge.routing.adapters import note_owner_intake_suppressed
from bridge.service.health import KEY_OWNER_INTAKE_SUPPRESSED, HealthService
from bridge.storage import (
    AlertRepository,
    Database,
    HealthStateRepository,
    OutboxRepository,
    TelegramInboxRepository,
)

BOT = 9000000001


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.connect(tmp_path / "bridge.db")
    try:
        yield database
    finally:
        await database.close()


def _health(db: Database, tmp_path: Path) -> HealthService:
    return HealthService(
        health=HealthStateRepository(db),
        outbox=OutboxRepository(db),
        inbox=TelegramInboxRepository(db),
        alerts=AlertRepository(db),
        db_path=tmp_path / "bridge.db",
    )


async def test_the_hand_off_is_counted_durably(db: Database, tmp_path: Path) -> None:
    """Three suppressed messages are three, and they survive the process.

    The count is the signal: with a healthy session it tracks what the session
    carried, and with a session that has gone quiet it keeps climbing while
    nothing reaches MAX. That difference is the whole point of writing it down.
    """
    health = _health(db, tmp_path)
    state = HealthStateRepository(db)

    for _ in range(3):
        await health.note_owner_intake_suppressed()

    assert int(await state.get(KEY_OWNER_INTAKE_SUPPRESSED, 0)) == 3
    snapshot = await health.snapshot()
    assert snapshot.owner_intake_suppressed == 3


async def test_a_fresh_install_reports_none(db: Database, tmp_path: Path) -> None:
    snapshot = await _health(db, tmp_path).snapshot()
    assert snapshot.owner_intake_suppressed == 0


async def test_the_notice_says_nothing_about_the_message(
    caplog: Any,
) -> None:
    """Structure only: the bot id is ours, the message is not read at all.

    The same discipline every alert in this project keeps. A line that carried a
    caption or a peer would turn a health counter into a leak.
    """
    seen: list[str] = []

    async def note() -> None:
        seen.append("counted")

    with caplog.at_level(logging.INFO, logger="bridge.routing.adapters"):
        await note_owner_intake_suppressed(note, BOT)

    assert seen == ["counted"]
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert str(BOT) in message
    assert "MTProto" in message


async def test_a_broken_counter_never_blocks_the_routing_decision(caplog: Any) -> None:
    """Health is a report. A counter that cannot be written must not raise into
    a handler that has already decided what to do with the message."""

    async def note() -> None:
        raise RuntimeError("the database is busy")

    with caplog.at_level(logging.INFO, logger="bridge.routing.adapters"):
        await note_owner_intake_suppressed(note, BOT)  # must not raise


async def test_a_router_without_the_hook_behaves_as_before() -> None:
    """Optional on purpose: every existing caller keeps working untouched."""
    await note_owner_intake_suppressed(None, BOT)


# ------------------------------------------------- the three suppression sites


def test_every_owner_handler_reports_the_hand_off() -> None:
    """Read from the source, because a handler added later would be silent again
    and nothing else would notice.

    There is no gate to read any more: the Bot API path carries no owner event at
    all, so *every* handler that sees one has to say so. What is checked is the
    count — one report per owner-event handler in each router.
    """
    import ast

    root = Path(__file__).resolve().parent.parent / "bridge" / "routing"
    for name, expected in (("adapters.py", 3), ("upload_router.py", 1)):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        reports = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "note_owner_intake_suppressed"
        )
        assert reports == expected, (
            f"{name}: {reports} hand-off reports, expected {expected} — "
            "a new owner handler must report too"
        )
