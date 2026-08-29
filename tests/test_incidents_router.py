"""The four owner actions for stuck jobs — and what they must never print."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest_asyncio

from bridge.service.incidents import (
    NOTHING_AMBIGUOUS,
    NOTHING_FAILED,
    build_incidents_router,
    describe,
)
from bridge.storage import Database, Direction, OutboxRepository

BRIDGE = "dad"


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append(text)


async def handler(router: Any, name: str) -> Any:
    for observer in router.message.handlers:
        if observer.callback.__name__ == name:
            return observer.callback
    raise AssertionError(f"no handler {name}")


async def make_job(outbox: OutboxRepository, *, kind: str, text: str, key: str) -> int:
    return await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=kind,
        payload={"text": text, "bot_id": 999},
        source_key=key,
    )


async def test_nothing_to_report_says_so(database: Database) -> None:
    router = build_incidents_router(OutboxRepository(database), bridges=[BRIDGE])
    message = FakeMessage("/failed")
    await (await handler(router, "_failed"))(message)
    assert message.answers == [NOTHING_FAILED]

    message = FakeMessage("/ambiguous")
    await (await handler(router, "_ambiguous"))(message)
    assert message.answers == [NOTHING_AMBIGUOUS]


async def test_failed_jobs_are_listed_without_their_content(database: Database) -> None:
    outbox = OutboxRepository(database)
    job = await make_job(outbox, kind="max_to_tg_media", text="личная переписка", key="max:1:1")
    await outbox.mark_failed(job, error="permanent: the file is gone")

    router = build_incidents_router(outbox, bridges=[BRIDGE])
    message = FakeMessage("/failed")
    await (await handler(router, "_failed"))(message)

    answer = message.answers[0]
    assert f"#{job}" in answer
    assert BRIDGE in answer
    assert "вложение MAX → Telegram" in answer
    assert "личная переписка" not in answer, "never the message itself"
    assert "999" not in answer


async def test_retry_puts_a_failed_job_back(database: Database) -> None:
    outbox = OutboxRepository(database)
    job = await make_job(outbox, kind="max_to_tg_text", text="привет", key="max:1:2")
    await outbox.mark_failed(job, error="permanent")

    router = build_incidents_router(outbox, bridges=[BRIDGE])
    message = FakeMessage(f"/retry {job}")
    await (await handler(router, "_retry"))(message)

    assert str(job) in message.answers[0]
    assert len(await outbox.claim_due(BRIDGE)) == 1


async def test_resolved_closes_an_ambiguous_job(database: Database) -> None:
    outbox = OutboxRepository(database)
    job = await make_job(outbox, kind="max_to_tg_media", text="фото", key="max:1:3")
    await outbox.mark_ambiguous(job, error="died mid-send")

    router = build_incidents_router(outbox, bridges=[BRIDGE])
    message = FakeMessage(f"/resolved {job}")
    await (await handler(router, "_resolved"))(message)

    assert await outbox.needing_attention(BRIDGE) == []


async def test_resolved_refuses_a_job_that_is_not_ambiguous(database: Database) -> None:
    outbox = OutboxRepository(database)
    job = await make_job(outbox, kind="max_to_tg_text", text="привет", key="max:1:4")
    await outbox.mark_failed(job, error="permanent")

    router = build_incidents_router(outbox, bridges=[BRIDGE])
    message = FakeMessage(f"/resolved {job}")
    await (await handler(router, "_resolved"))(message)

    assert "не в неясном состоянии" in message.answers[0]


async def test_a_command_without_a_number_explains_itself(database: Database) -> None:
    router = build_incidents_router(OutboxRepository(database), bridges=[BRIDGE])
    message = FakeMessage("/retry")
    await (await handler(router, "_retry"))(message)
    assert "/retry 42" in message.answers[0]


async def test_describe_never_leaks_the_payload(database: Database) -> None:
    outbox = OutboxRepository(database)
    job = await make_job(outbox, kind="max_to_tg_text", text="секрет", key="max:1:5")
    await outbox.mark_failed(job, error="permanent: nope")

    item = (await outbox.needing_attention(BRIDGE))[0]
    line = describe(item)
    assert "секрет" not in line
    assert str(job) in line


async def test_the_hints_survive_html_parse_mode(database: Database) -> None:
    """The guardian sends HTML, so angle brackets are markup, not punctuation.

    `/retry <номер>` looked like a placeholder and reached Telegram as an
    unsupported start tag: every answer this router produced was rejected, and
    the owner saw silence. Live verification found it; nothing else would have.
    """
    outbox = OutboxRepository(database)
    job = await make_job(outbox, kind="max_to_tg_text", text="привет", key="max:1:9")
    await outbox.mark_failed(job, error="permanent")

    router = build_incidents_router(outbox, bridges=[BRIDGE])
    commands = (("/failed", "_failed"), ("/ambiguous", "_ambiguous"), ("/retry", "_retry"))
    for command, name in commands:
        message = FakeMessage(command)
        await (await handler(router, name))(message)
        for answer in message.answers:
            assert "<" not in answer and ">" not in answer, f"{command} sends raw markup"


# -------------------------------------------------------------------- archive


async def test_archiving_keeps_the_evidence(database: Database) -> None:
    """Set aside, not deleted: the error, the attempts and the times all stay."""
    outbox = OutboxRepository(database)
    job = await make_job(outbox, kind="tg_to_max_media", text="фото", key="max:1:20")
    await outbox.mark_failed(job, error="permanent: the file is gone")
    before = (await outbox.needing_attention(BRIDGE))[0]

    assert await outbox.archive(job, reason="legacy unrecoverable") is True

    row = await database.query_one("SELECT * FROM outbox WHERE id = ?", (job,))
    assert row is not None
    assert row["state"] == "archived"
    assert "the file is gone" in row["last_error"], "the original reason survives"
    assert "legacy unrecoverable" in row["last_error"], "and why it was archived"
    assert row["attempts"] == before.attempts
    assert row["created_at"] == before.created_at


async def test_archived_leaves_the_incident_count(database: Database) -> None:
    outbox = OutboxRepository(database)
    job = await make_job(outbox, kind="max_to_tg_media", text="x", key="max:1:21")
    await outbox.mark_failed(job, error="permanent")

    assert len(await outbox.needing_attention(BRIDGE)) == 1
    await outbox.archive(job, reason="unrecoverable")

    assert await outbox.needing_attention(BRIDGE) == [], "no longer needs a person"
    counts = await outbox.counts(BRIDGE)
    assert counts.get("failed") is None, "and no longer counts as failed"
    assert counts.get("archived") == 1, "but still visible in the statistics"


async def test_archived_jobs_are_listed_separately(database: Database) -> None:
    outbox = OutboxRepository(database)
    job = await make_job(outbox, kind="max_to_tg_media", text="x", key="max:1:22")
    await outbox.mark_failed(job, error="permanent")
    await outbox.archive(job, reason="unrecoverable")

    assert [item.id for item in await outbox.archived(BRIDGE)] == [job]


async def test_retry_refuses_an_archived_job(database: Database) -> None:
    """Archiving is a decision. Undoing it by accident would be a surprise."""
    outbox = OutboxRepository(database)
    job = await make_job(outbox, kind="max_to_tg_media", text="x", key="max:1:23")
    await outbox.mark_failed(job, error="permanent")
    await outbox.archive(job, reason="unrecoverable")

    assert await outbox.retry_now(job) is False
    assert await outbox.resolve(job) is False


async def test_only_failed_or_ambiguous_can_be_archived(database: Database) -> None:
    outbox = OutboxRepository(database)
    job = await make_job(outbox, kind="max_to_tg_text", text="x", key="max:1:24")

    assert await outbox.archive(job, reason="nope") is False, "a pending job is still owed"
    await outbox.mark_done(job, remote_message_id=1)
    assert await outbox.archive(job, reason="nope") is False, "a delivered one is done"


async def test_the_archive_command_needs_a_reason(database: Database) -> None:
    """A row that says only "archived" explains nothing a year later."""
    outbox = OutboxRepository(database)
    job = await make_job(outbox, kind="max_to_tg_media", text="x", key="max:1:25")
    await outbox.mark_failed(job, error="permanent")

    router = build_incidents_router(outbox, bridges=[BRIDGE])
    message = FakeMessage(f"/archive {job}")
    await (await handler(router, "_archive"))(message)

    assert "причину" in message.answers[0]
    assert (await outbox.needing_attention(BRIDGE))[0].state.value == "failed"


async def test_the_archive_command_archives_with_its_reason(database: Database) -> None:
    outbox = OutboxRepository(database)
    job = await make_job(outbox, kind="max_to_tg_media", text="x", key="max:1:26")
    await outbox.mark_failed(job, error="permanent")

    router = build_incidents_router(outbox, bridges=[BRIDGE])
    message = FakeMessage(f"/archive {job} legacy unrecoverable before durable media sources")
    await (await handler(router, "_archive"))(message)

    assert str(job) in message.answers[0]
    row = await database.query_one("SELECT last_error FROM outbox WHERE id = ?", (job,))
    assert row is not None
    assert "legacy unrecoverable before durable media sources" in row["last_error"]
