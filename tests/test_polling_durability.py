"""Polling: what is acknowledged, when, and what survives the process.

The property under test is a single ordering rule — an update is written to
SQLite before its offset is acknowledged to Telegram — plus everything that
follows from it: a crash re-delivers nothing that was already handled and loses
nothing that was not, and shutdown leaves the remainder on disk rather than in a
task that is about to be cancelled.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiogram.types import Chat, Message, Update, User

from bridge.storage import Database, TelegramInboxRepository
from bridge.telegram.runner import BotRunner

OWNER = 111
BOT_ID = 100


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


def make_update(update_id: int, text: str) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(tz=UTC),
            chat=Chat(id=OWNER, type="private"),
            from_user=User(id=OWNER, is_bot=False, first_name="Owner"),
            text=text,
        ),
    )


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeBot:
    """Serves canned update batches, and records the offsets it was asked for."""

    def __init__(self, batches: list[list[Update]] | None = None) -> None:
        self.id = BOT_ID
        self.session = FakeSession()
        self._batches = list(batches or [])
        self.offsets: list[int | None] = []
        self.exhausted = asyncio.Event()

    async def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: int = 0,  # noqa: ASYNC109 - mirrors the Bot API signature
        allowed_updates: Any = None,
    ) -> list[Update]:
        self.offsets.append(offset)
        if self._batches:
            return self._batches.pop(0)
        self.exhausted.set()
        # Nothing left: behave like a long poll that timed out.
        await asyncio.sleep(0.02)
        return []


class RecordingDispatcher:
    def __init__(self, fail_on: set[int] | None = None) -> None:
        self.seen: list[int] = []
        self._fail_on = fail_on or set()
        self.gate: asyncio.Event | None = None

    async def feed_update(self, bot: Any, update: Update) -> None:
        if self.gate is not None:
            await self.gate.wait()
        if update.update_id in self._fail_on:
            raise RuntimeError("handler blew up")
        self.seen.append(update.update_id)


def make_runner(
    bot: FakeBot, dispatcher: Any, inbox: TelegramInboxRepository | None
) -> BotRunner:
    return BotRunner(
        bridge_name="dad",
        bot=bot,  # type: ignore[arg-type]
        dispatcher=dispatcher,
        allowed_updates=["message"],
        inbox=inbox,
        bot_id=BOT_ID,
    )


async def _settle(runner: BotRunner, bot: FakeBot) -> None:
    await asyncio.wait_for(bot.exhausted.wait(), timeout=2)
    await asyncio.sleep(0.15)


# --------------------------------------------------------------- intake first


async def test_update_is_stored_before_the_offset_moves(database: Database) -> None:
    """The rule everything else rests on."""
    inbox = TelegramInboxRepository(database)
    bot = FakeBot([[make_update(10, "привет")]])
    runner = make_runner(bot, RecordingDispatcher(), inbox)

    runner.start()
    try:
        await _settle(runner, bot)
    finally:
        await runner.stop()

    # The first poll asked with no offset; a later one carries 11, and by then
    # the row already existed.
    assert bot.offsets[0] is None
    assert 11 in [offset for offset in bot.offsets if offset is not None]
    assert await inbox.offset(BOT_ID) == 11


async def test_offset_does_not_move_when_the_store_fails(database: Database) -> None:
    """A failed intake must leave Telegram willing to send the update again."""
    inbox = TelegramInboxRepository(database)
    bot = FakeBot([[make_update(10, "привет")]])

    async def explode(**kwargs: Any) -> int | None:
        raise RuntimeError("disk is angry")

    inbox.store = explode  # type: ignore[method-assign]
    runner = make_runner(bot, RecordingDispatcher(), inbox)

    runner.start()
    try:
        await asyncio.sleep(0.2)
    finally:
        await runner.stop()

    # Never acknowledged: every request still asks from the same place.
    assert all(offset is None for offset in bot.offsets)
    assert await inbox.offset(BOT_ID) is None


async def test_a_stored_update_is_processed(database: Database) -> None:
    inbox = TelegramInboxRepository(database)
    bot = FakeBot([[make_update(10, "привет")]])
    dispatcher = RecordingDispatcher()
    runner = make_runner(bot, dispatcher, inbox)

    runner.start()
    try:
        await _settle(runner, bot)
    finally:
        await runner.stop()

    assert dispatcher.seen == [10]
    assert await inbox.depth(BOT_ID) == 0


async def test_updates_keep_their_order(database: Database) -> None:
    """Two messages from one contact must not overtake each other."""
    inbox = TelegramInboxRepository(database)
    bot = FakeBot(
        [[make_update(1, "первое"), make_update(2, "второе")], [make_update(3, "третье")]]
    )
    dispatcher = RecordingDispatcher()
    runner = make_runner(bot, dispatcher, inbox)

    runner.start()
    try:
        await _settle(runner, bot)
    finally:
        await runner.stop()

    assert dispatcher.seen == [1, 2, 3]


# ------------------------------------------------------------- crash recovery


async def test_an_update_stored_but_unhandled_survives_a_restart(tmp_path: Path) -> None:
    """The crash window: acknowledged to Telegram, not yet delivered anywhere."""
    path = tmp_path / "bridge.db"

    first = await Database.connect(path)
    stalled = RecordingDispatcher()
    stalled.gate = asyncio.Event()  # nothing will ever finish processing
    bot = FakeBot([[make_update(10, "не потеряй меня")]])
    runner = make_runner(bot, stalled, TelegramInboxRepository(first))

    runner.start()
    await asyncio.wait_for(bot.exhausted.wait(), timeout=2)
    await asyncio.sleep(0.1)
    # Simulate SIGKILL: no graceful stop, just drop everything.
    runner._task.cancel()  # type: ignore[union-attr]
    runner._worker.cancel()  # type: ignore[union-attr]
    await asyncio.sleep(0)
    await first.close()

    assert stalled.seen == []

    # Next process: same database, a dispatcher that works.
    second = await Database.connect(path)
    recovered = RecordingDispatcher()
    bot2 = FakeBot([])
    runner2 = make_runner(bot2, recovered, TelegramInboxRepository(second))
    runner2.start()
    try:
        await asyncio.sleep(0.3)
    finally:
        await runner2.stop()
        await second.close()

    assert recovered.seen == [10], "the stored update must be delivered after a restart"


async def test_the_offset_is_resumed_after_a_restart(database: Database) -> None:
    inbox = TelegramInboxRepository(database)
    await inbox.remember_offset(BOT_ID, 500)

    bot = FakeBot([])
    runner = make_runner(bot, RecordingDispatcher(), inbox)
    runner.start()
    try:
        await asyncio.wait_for(bot.exhausted.wait(), timeout=2)
    finally:
        await runner.stop()

    assert bot.offsets[0] == 500


async def test_a_replayed_update_is_not_delivered_twice(database: Database) -> None:
    """Telegram re-sends whatever was never acknowledged. That must be free."""
    inbox = TelegramInboxRepository(database)
    same = make_update(10, "привет")
    bot = FakeBot([[same], [make_update(10, "привет")]])
    dispatcher = RecordingDispatcher()
    runner = make_runner(bot, dispatcher, inbox)

    runner.start()
    try:
        await _settle(runner, bot)
    finally:
        await runner.stop()

    assert dispatcher.seen == [10]


# ------------------------------------------------------------------- failures


async def test_a_failing_update_is_retried_then_parked(database: Database) -> None:
    """It must not spin for ever, and it must not vanish either."""
    inbox = TelegramInboxRepository(database)
    bot = FakeBot([[make_update(10, "плохое")]])
    runner = make_runner(bot, RecordingDispatcher(fail_on={10}), inbox)

    runner.start()
    try:
        await asyncio.sleep(0.5)
    finally:
        await runner.stop()

    row = await database.query_one(
        "SELECT state, attempts FROM telegram_inbox WHERE update_id = 10"
    )
    assert row is not None
    assert row["attempts"] >= 1
    # Either still being retried or parked as failed — never silently gone.
    assert row["state"] in {"received", "leased", "failed"}


async def test_one_bad_update_does_not_stop_the_next(database: Database) -> None:
    inbox = TelegramInboxRepository(database)
    bot = FakeBot([[make_update(1, "плохое"), make_update(2, "хорошее")]])
    dispatcher = RecordingDispatcher(fail_on={1})
    runner = make_runner(bot, dispatcher, inbox)

    runner.start()
    try:
        await asyncio.sleep(0.6)
    finally:
        await runner.stop()

    assert 2 in dispatcher.seen


# ------------------------------------------------------------------- shutdown


async def test_stop_closes_the_session(database: Database) -> None:
    bot = FakeBot([])
    runner = make_runner(bot, RecordingDispatcher(), TelegramInboxRepository(database))
    runner.start()
    await runner.stop()

    assert bot.session.closed is True
    assert runner.is_running is False


async def test_shutdown_leaves_unfinished_work_on_disk(database: Database) -> None:
    """A drain that runs out of time must not take the message with it."""
    inbox = TelegramInboxRepository(database)
    stalled = RecordingDispatcher()
    stalled.gate = asyncio.Event()
    bot = FakeBot([[make_update(10, "привет")]])
    runner = make_runner(bot, stalled, inbox)

    runner.start()
    await asyncio.wait_for(bot.exhausted.wait(), timeout=2)
    await asyncio.sleep(0.1)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("bridge.telegram.runner.DRAIN_TIMEOUT_SECONDS", 0.1)
        await runner.stop()

    # Still in the table, and claimable again once its lease lapses.
    row = await database.query_one("SELECT state FROM telegram_inbox WHERE update_id = 10")
    assert row is not None
    assert row["state"] in {"received", "leased"}


async def test_stopping_twice_is_safe(database: Database) -> None:
    bot = FakeBot([])
    runner = make_runner(bot, RecordingDispatcher(), TelegramInboxRepository(database))
    runner.start()
    await runner.stop()
    await runner.stop()


# ------------------------------------------------- straight-through (guardian)


async def test_without_an_inbox_updates_still_flow(database: Database) -> None:
    """The guardian has no message to lose; it must keep working regardless."""
    bot = FakeBot([[make_update(10, "/status")]])
    dispatcher = RecordingDispatcher()
    runner = make_runner(bot, dispatcher, None)

    runner.start()
    try:
        await _settle(runner, bot)
    finally:
        await runner.stop()

    assert dispatcher.seen == [10]


async def test_intake_does_not_start_before_the_handlers(database: Database) -> None:
    """A runner must not drain the inbox into a dispatcher with no routers.

    This is the silent loss that live verification produced. Startup registered
    every bridge — which began polling and, worse, began draining the durable
    inbox — and only wired the routers a hundred lines later, after several MAX
    round trips. The stored update was fed to a dispatcher where nothing
    matched, `feed_update` returned without error, and the row was marked
    handled. The message a restart existed to deliver disappeared with a clean
    log.

    So a runner created but not started must touch nothing at all.
    """
    inbox = TelegramInboxRepository(database)
    stored = make_update(42, "ждал перезапуска")
    await inbox.store(
        bot_id=BOT_ID,
        update_id=42,
        payload=stored.model_dump(mode="json", exclude_none=True),
    )

    dispatcher = RecordingDispatcher()
    runner = make_runner(FakeBot([]), dispatcher, inbox)

    # Constructed, deliberately not started — the window in which the real
    # startup sequence is still building handlers.
    await asyncio.sleep(0.3)

    assert dispatcher.seen == []
    assert await inbox.depth(BOT_ID) == 1, "the update must still be waiting"

    runner.start()
    try:
        await asyncio.sleep(0.4)
    finally:
        await runner.stop()

    assert dispatcher.seen == [42], "and delivered once the handlers exist"
    assert await inbox.depth(BOT_ID) == 0


async def test_an_update_with_a_link_preview_can_be_stored() -> None:
    """A `Default` marker used to make the update unstorable — and the bridge stuck.

    Telegram fills `link_preview_options` on any message carrying a URL, and
    aiogram defaults its `is_disabled` to a marker pydantic cannot render as
    JSON. The dump raised, the row was never written, the offset therefore never
    moved, and Telegram re-sent the same batch for as long as it existed. Seen
    live on 2026-08-04, when forwarding made link previews ordinary.
    """
    from aiogram.client.default import Default
    from aiogram.types import LinkPreviewOptions

    from bridge.telegram.runner import storable_update

    update = Update(
        update_id=7,
        message=Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=1, type="private"),
            text="смотри: https://example.com",
            link_preview_options=LinkPreviewOptions(
                url="https://example.com",
                is_disabled=Default("link_preview_is_disabled"),
            ),
        ),
    )

    payload = storable_update(update)

    # Stored as JSON, and still the same update when read back.
    restored = Update.model_validate(json.loads(json.dumps(payload)))
    assert restored.message is not None
    assert restored.message.text == "смотри: https://example.com"
    assert restored.message.link_preview_options is not None
    assert restored.message.link_preview_options.url == "https://example.com"
    # The marker meant "not set"; so does None.
    assert restored.message.link_preview_options.is_disabled is None


async def test_an_update_with_a_genuinely_unstorable_field_still_raises() -> None:
    """The narrow fallback must not become a blanket one.

    Turning every unknown type into null is how a field silently stops arriving.
    Only aiogram's "not set" marker is translated; anything else keeps the old
    behaviour, which is to raise and leave the offset where it is.
    """
    from bridge.telegram.runner import _unset

    with pytest.raises(TypeError):
        _unset(object())
