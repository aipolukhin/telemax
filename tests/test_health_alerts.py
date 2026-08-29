"""Health that outlives the process, and alerts that outlive Telegram."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest_asyncio

from bridge.service.health import (
    KEY_SHUTDOWN_MARK,
    AlertDispatcher,
    HealthService,
)
from bridge.storage import (
    AlertRepository,
    Database,
    Direction,
    HealthStateRepository,
    OutboxRepository,
    TelegramInboxRepository,
)

BRIDGE = "dad"


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


def make_service(
    database: Database, *, ready: bool = True, bridges: list[str] | None = None
) -> HealthService:
    return HealthService(
        health=HealthStateRepository(database),
        outbox=OutboxRepository(database),
        inbox=TelegramInboxRepository(database),
        alerts=AlertRepository(database),
        db_path=Path(database.path),
        bridges=lambda: [(name, True) for name in (bridges or [BRIDGE])],
        max_is_ready=lambda: ready,
    )


# ------------------------------------------------------------------- snapshot


async def test_the_snapshot_survives_a_restart(tmp_path: Path) -> None:
    """The question after a crash is what happened before it."""
    path = tmp_path / "bridge.db"
    first = await Database.connect(path)
    await make_service(first).note_start(schema_version=10, commit="abc1234")
    await first.close()

    second = await Database.connect(path)
    try:
        snapshot = await make_service(second).snapshot()
        assert snapshot.process_started_at is not None
        assert snapshot.schema_version == 10
        assert snapshot.commit == "abc1234"
    finally:
        await second.close()


async def test_an_unclean_start_is_noticed(tmp_path: Path) -> None:
    """The marker is set on start and cleared on a clean stop."""
    path = tmp_path / "bridge.db"
    first = await Database.connect(path)
    assert await make_service(first).note_start(schema_version=10) is False
    await first.close()  # killed: no clean shutdown

    second = await Database.connect(path)
    try:
        service = make_service(second)
        assert await service.note_start(schema_version=10) is True
        snapshot = await service.snapshot()
        assert snapshot.last_unclean_start_at is not None
    finally:
        await second.close()


async def test_a_clean_shutdown_is_recorded(database: Database) -> None:
    service = make_service(database)
    await service.note_start(schema_version=10)
    await service.note_clean_shutdown()

    assert await HealthStateRepository(database).get(KEY_SHUTDOWN_MARK) is False
    snapshot = await service.snapshot()
    assert snapshot.last_clean_shutdown_at is not None


async def test_offline_duration_is_computed(database: Database) -> None:
    service = make_service(database, ready=False)
    await service.note_start(schema_version=10)
    await service.note_max_disconnected("connection reset")

    snapshot = await service.snapshot()
    assert snapshot.max_offline_ms is not None
    assert snapshot.max_offline_ms >= 0
    assert snapshot.max_connected is False
    assert snapshot.max_last_error == "connection reset"


async def test_reconnecting_updates_the_timestamps(database: Database) -> None:
    service = make_service(database)
    await service.note_max_disconnected()
    await service.note_max_connected()

    snapshot = await service.snapshot()
    assert snapshot.max_offline_ms is None
    assert snapshot.max_connected_at is not None
    assert snapshot.max_reconnects == 1


async def test_queue_numbers_are_counted_not_remembered(database: Database) -> None:
    outbox = OutboxRepository(database)
    for n in range(3):
        await outbox.enqueue(
            bridge_name=BRIDGE,
            direction=Direction.MAX_TO_TG,
            kind="max_to_tg_text",
            payload={"n": n},
            source_key=f"max:1:{n}",
        )
    failed = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind="max_to_tg_text",
        payload={},
        source_key="max:1:99",
    )
    await outbox.mark_failed(failed, error="permanent")

    snapshot = await make_service(database).snapshot()
    assert snapshot.outbox_pending == 3
    assert snapshot.outbox_failed == 1
    assert snapshot.oldest_pending_ms is not None
    assert snapshot.bridges[0].failed == 1


async def test_the_snapshot_holds_no_secrets(database: Database) -> None:
    """It says how many and how old, never what was said."""
    outbox = OutboxRepository(database)
    await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind="max_to_tg_text",
        payload={"text": "совершенно секретный текст", "token": "123:AAsecret"},
        source_key="max:1:1",
    )

    snapshot = await make_service(database).snapshot()
    rendered = repr(snapshot)
    assert "секретный" not in rendered
    assert "AAsecret" not in rendered
    assert "+7" not in rendered


# --------------------------------------------------------------------- alerts


async def test_an_alert_is_stored_before_it_is_sent(database: Database) -> None:
    alerts = AlertRepository(database)
    assert await alerts.open_incident(incident_key="max-offline", text="⚠️ MAX недоступен")
    assert await alerts.pending_count() == 1


async def test_an_alert_survives_a_restart(tmp_path: Path) -> None:
    """The outage that caused the alert is often the one that would eat it."""
    path = tmp_path / "bridge.db"
    first = await Database.connect(path)
    await AlertRepository(first).open_incident(incident_key="max-offline", text="⚠️")
    await first.close()

    second = await Database.connect(path)
    try:
        assert len(await AlertRepository(second).claim_due()) == 1
    finally:
        await second.close()


async def test_one_incident_does_not_spam(database: Database) -> None:
    """A queue forty deep is one incident, not forty messages."""
    alerts = AlertRepository(database)
    first = await alerts.open_incident(incident_key="queue-depth", text="⚠️ 40")
    second = await alerts.open_incident(incident_key="queue-depth", text="⚠️ 41")
    third = await alerts.open_incident(incident_key="queue-depth", text="⚠️ 42")

    assert first is not None
    assert second is None and third is None
    assert await alerts.pending_count() == 1


async def test_recovery_is_announced_once(database: Database) -> None:
    alerts = AlertRepository(database)
    await alerts.open_incident(incident_key="max-offline", text="⚠️ упал")

    assert await alerts.resolve_incident(incident_key="max-offline", text="✅ поднялся")
    assert await alerts.resolve_incident(incident_key="max-offline", text="✅ поднялся") is None
    assert await alerts.pending_count() == 2  # the warning and the one recovery


async def test_recovery_without_an_incident_is_silence(database: Database) -> None:
    """Never tell the owner something is fixed if they were never told it broke."""
    alerts = AlertRepository(database)
    assert await alerts.resolve_incident(incident_key="never-happened", text="✅") is None
    assert await alerts.pending_count() == 0


async def test_an_incident_can_reopen_after_resolving(database: Database) -> None:
    alerts = AlertRepository(database)
    await alerts.open_incident(incident_key="max-offline", text="⚠️ 1")
    await alerts.resolve_incident(incident_key="max-offline", text="✅")

    assert await alerts.open_incident(incident_key="max-offline", text="⚠️ 2")
    assert await alerts.is_open("max-offline") is True


async def test_a_failing_guardian_gets_retried(database: Database) -> None:
    alerts = AlertRepository(database)
    await alerts.open_incident(incident_key="max-offline", text="⚠️")

    attempts = 0

    async def flaky(alert: dict[str, Any]) -> None:
        nonlocal attempts
        attempts += 1
        raise ConnectionError("Telegram is down too")

    dispatcher = AlertDispatcher(alerts=alerts, deliver=flaky, backoff_ms=0)
    assert await dispatcher.drain_once() == 0
    assert attempts == 1
    # Still queued, not dropped.
    assert await alerts.pending_count() == 1

    delivered: list[str] = []

    async def works(alert: dict[str, Any]) -> None:
        delivered.append(str(alert["text"]))

    assert await AlertDispatcher(alerts=alerts, deliver=works).drain_once() == 1
    assert delivered == ["⚠️"]
    assert await alerts.pending_count() == 0


async def test_an_alert_is_given_up_on_eventually(database: Database) -> None:
    alerts = AlertRepository(database)
    await alerts.open_incident(incident_key="max-offline", text="⚠️")

    async def never(alert: dict[str, Any]) -> None:
        raise ConnectionError("still down")

    dispatcher = AlertDispatcher(alerts=alerts, deliver=never, max_attempts=2, backoff_ms=0)
    await dispatcher.drain_once()
    await dispatcher.drain_once()

    assert await alerts.pending_count() == 0
    row = await database.query_one("SELECT state FROM alert_outbox WHERE id = 1")
    assert row is not None
    assert row["state"] == "failed"


# ------------------------------------------------------------------ evaluation


async def test_a_long_max_outage_raises_one_alert(database: Database) -> None:
    service = HealthService(
        health=HealthStateRepository(database),
        outbox=OutboxRepository(database),
        inbox=TelegramInboxRepository(database),
        alerts=AlertRepository(database),
        db_path=Path(database.path),
        bridges=lambda: [(BRIDGE, True)],
        max_is_ready=lambda: False,
        offline_alert_seconds=0,
    )
    await service.note_max_disconnected("gone")

    assert "max-offline" in await service.evaluate(await service.snapshot())
    # Checked again a moment later: the same incident, no second message.
    assert "max-offline" not in await service.evaluate(await service.snapshot())
    assert await AlertRepository(database).pending_count() == 1


async def test_recovery_closes_the_outage_incident(database: Database) -> None:
    alerts = AlertRepository(database)
    offline = HealthService(
        health=HealthStateRepository(database),
        outbox=OutboxRepository(database),
        inbox=TelegramInboxRepository(database),
        alerts=alerts,
        db_path=Path(database.path),
        bridges=lambda: [(BRIDGE, True)],
        max_is_ready=lambda: False,
        offline_alert_seconds=0,
    )
    await offline.note_max_disconnected()
    await offline.evaluate(await offline.snapshot())

    healthy = make_service(database, ready=True)
    await healthy.note_max_connected()
    assert "max-offline:resolved" in await healthy.evaluate(await healthy.snapshot())


async def test_failed_jobs_are_aggregated_into_one_alert(database: Database) -> None:
    outbox = OutboxRepository(database)
    for n in range(4):
        job = await outbox.enqueue(
            bridge_name=BRIDGE,
            direction=Direction.MAX_TO_TG,
            kind="max_to_tg_media",
            payload={"n": n},
            source_key=f"max:1:{n}",
        )
        await outbox.mark_failed(job, error="permanent")

    service = make_service(database)
    assert "delivery-failed" in await service.evaluate(await service.snapshot())

    alerts = await AlertRepository(database).claim_due()
    assert len(alerts) == 1, "four failures, one message"
    assert "4" in alerts[0]["text"]
    assert BRIDGE in alerts[0]["text"]


async def test_an_alert_carries_no_message_content(database: Database) -> None:
    outbox = OutboxRepository(database)
    job = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind="max_to_tg_text",
        payload={"text": "личная переписка", "bot_id": 12345},
        source_key="max:1:1",
    )
    await outbox.mark_failed(job, error="permanent")

    service = make_service(database)
    await service.evaluate(await service.snapshot())

    for alert in await AlertRepository(database).claim_due():
        assert "личная переписка" not in alert["text"]
        assert "12345" not in alert["text"]


async def test_ambiguous_jobs_get_their_own_alert(database: Database) -> None:
    outbox = OutboxRepository(database)
    job = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind="max_to_tg_media",
        payload={},
        source_key="max:1:1",
    )
    await outbox.mark_ambiguous(job, error="died mid-send")

    service = make_service(database)
    assert "delivery-ambiguous" in await service.evaluate(await service.snapshot())
    alerts = await AlertRepository(database).claim_due()
    assert "MAX" in alerts[0]["text"]


async def test_a_connected_max_does_not_alert_on_a_stale_timestamp(
    database: Database,
) -> None:
    """A working session must not keep alerting because of an old value."""
    alerts = AlertRepository(database)
    service = HealthService(
        health=HealthStateRepository(database),
        outbox=OutboxRepository(database),
        inbox=TelegramInboxRepository(database),
        alerts=alerts,
        db_path=Path(database.path),
        bridges=lambda: [(BRIDGE, True)],
        max_is_ready=lambda: True,          # plainly working
        offline_alert_seconds=0,
    )
    await HealthStateRepository(database).set("max_disconnected_at", 1)  # ancient

    touched = await service.evaluate(await service.snapshot())
    assert "max-offline" not in touched
    assert await alerts.pending_count() == 0


async def test_archived_jobs_close_the_incident(database: Database) -> None:
    """The point of archiving: an unrecoverable job must stop masking the next one.

    Two legacy failures held `delivery-failed` open indefinitely in production,
    so a genuine new failure had nothing left to announce.
    """
    outbox = OutboxRepository(database)
    job = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind="tg_to_max_media",
        payload={},
        source_key="max:1:1",
    )
    await outbox.mark_failed(job, error="permanent: the file is gone")

    service = make_service(database)
    assert "delivery-failed" in await service.evaluate(await service.snapshot())

    await outbox.archive(job, reason="legacy unrecoverable before durable media sources")

    touched = await service.evaluate(await service.snapshot())
    assert "delivery-failed:resolved" in touched
    assert (await service.snapshot()).outbox_failed == 0


async def test_archived_jobs_do_not_raise_a_new_incident(database: Database) -> None:
    outbox = OutboxRepository(database)
    job = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind="tg_to_max_media",
        payload={},
        source_key="max:1:2",
    )
    await outbox.mark_failed(job, error="permanent")
    await outbox.archive(job, reason="unrecoverable")

    service = make_service(database)
    assert await service.evaluate(await service.snapshot()) == []
    assert await AlertRepository(database).pending_count() == 0
