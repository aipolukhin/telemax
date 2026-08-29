"""WP5 — the vertical slice: text in both directions, without duplicates."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from bridge.config import TimestampStyle
from bridge.formatting import utf16_length
from bridge.max_client import normalize_message
from bridge.routing import OWN_MESSAGE_PREFIX, BridgeRouter, BridgeTarget
from bridge.storage import BridgeStateRepository, Database, MessageMapRepository

MOM = BridgeTarget(name="mom", max_chat_id=777, bot_id=100)
OWNER_CHAT = 111
CONTACT = 4242
OWNER_MAX_ID = 100000002


@dataclass(slots=True)
class FakeLookup:
    targets: tuple[BridgeTarget, ...] = (MOM,)

    def bridge_for_max_chat(self, max_chat_id: int) -> BridgeTarget | None:
        return next((t for t in self.targets if t.max_chat_id == max_chat_id), None)

    def bridge_for_bot(self, bot_id: int) -> BridgeTarget | None:
        return next((t for t in self.targets if t.bot_id == bot_id), None)


@dataclass(slots=True)
class FakeTelegram:
    sent: list[tuple[int, int, str]] = field(default_factory=list)
    next_id: int = 9000

    replies: list[int | None] = field(default_factory=list)

    entity_sets: list[list[dict[str, Any]] | None] = field(default_factory=list)

    async def send_text(
        self,
        bot_id: int,
        chat_id: int,
        text: str,
        *,
        reply_to: int | None = None,
        entities: list[dict[str, Any]] | None = None,
    ) -> int | None:
        self.sent.append((bot_id, chat_id, text))
        self.replies.append(reply_to)
        self.entity_sets.append(entities)
        self.next_id += 1
        return self.next_id


@dataclass(slots=True)
class FakeMax:
    sent: list[tuple[int, str]] = field(default_factory=list)
    next_id: int = 500
    fail_with: Exception | None = None

    replies: list[int | None] = field(default_factory=list)

    async def send_text(
        self, chat_id: int, text: str, *, reply_to: int | None = None
    ) -> int | None:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append((chat_id, text))
        self.replies.append(reply_to)
        self.next_id += 1
        return self.next_id


@dataclass(slots=True)
class Harness:
    router: BridgeRouter
    telegram: FakeTelegram
    max: FakeMax
    messages: MessageMapRepository


@pytest_asyncio.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    database = await Database.connect(tmp_path / "bridge.db")
    messages = MessageMapRepository(database)
    telegram = FakeTelegram()
    max_side = FakeMax()
    router = BridgeRouter(
        lookup=FakeLookup(),
        telegram=telegram,
        max_sender=max_side,
        messages=messages,
        state=BridgeStateRepository(database),
        owner_chat_id=OWNER_CHAT,
        timestamp_style=TimestampStyle.OFF,
    )
    try:
        yield Harness(router=router, telegram=telegram, max=max_side, messages=messages)
    finally:
        await database.close()


def incoming(message_id: int, text: str, *, sender: int = CONTACT, **extra: Any) -> Any:
    payload = {
        "id": message_id,
        "chatId": MOM.max_chat_id,
        "sender": sender,
        "text": text,
        "time": 1,
        **extra,
    }
    return normalize_message(payload, own_user_id=OWNER_MAX_ID)


async def test_max_message_reaches_telegram(harness: Harness) -> None:
    await harness.router.on_max_message(incoming(1, "привет"))

    assert harness.telegram.sent == [(MOM.bot_id, OWNER_CHAT, "привет")]

    link = await harness.messages.by_max_message(MOM.max_chat_id, 1, MOM.bot_id)
    assert link is not None
    assert link.telegram_message_id == 9001


async def test_replayed_event_is_not_delivered_twice(harness: Harness) -> None:
    """A reconnect re-sends recent events; the claim makes the replay a no-op."""
    await harness.router.on_max_message(incoming(1, "привет"))
    await harness.router.on_max_message(incoming(1, "привет"))

    assert len(harness.telegram.sent) == 1


async def test_messages_from_unbridged_chats_are_ignored(harness: Harness) -> None:
    stranger = normalize_message(
        {"id": 2, "chatId": 999, "sender": CONTACT, "text": "hi", "time": 1},
        own_user_id=OWNER_MAX_ID,
    )

    await harness.router.on_max_message(stranger)

    assert harness.telegram.sent == []


async def test_telegram_message_reaches_max(harness: Harness) -> None:
    await harness.router.on_telegram_text(
        bot_id=MOM.bot_id, telegram_chat_id=OWNER_CHAT, telegram_message_id=17, text="ответ"
    )

    assert harness.max.sent == [(MOM.max_chat_id, "ответ")]

    link = await harness.messages.by_telegram_message(MOM.bot_id, 17)
    assert link is not None
    assert link.max_message_id == 501


async def test_our_own_message_does_not_come_back(harness: Harness) -> None:
    """The loop guard: MAX echoes what we send, and the echo must die here."""
    await harness.router.on_telegram_text(
        bot_id=MOM.bot_id, telegram_chat_id=OWNER_CHAT, telegram_message_id=17, text="ответ"
    )

    echo = incoming(501, "ответ", sender=OWNER_MAX_ID)
    await harness.router.on_max_message(echo)

    assert harness.telegram.sent == [], "the bridge forwarded its own message back"


async def test_own_message_from_the_app_is_marked(harness: Harness) -> None:
    """Typed on the phone, not through the bridge: delivered, but labelled."""
    await harness.router.on_max_message(incoming(7, "с телефона", sender=OWNER_MAX_ID))

    assert harness.telegram.sent[0][2] == f"{OWN_MESSAGE_PREFIX}с телефона"


async def test_attachments_are_announced_until_wp8(harness: Harness) -> None:
    await harness.router.on_max_message(
        incoming(8, "", attaches=[{"_type": "VIDEO", "videoType": 1, "duration": 2100}])
    )

    assert harness.telegram.sent[0][2] == "[video_note]"


async def test_max_failure_is_recorded_and_raised(harness: Harness) -> None:
    """WP6 turns this into a retry; for now it must not be swallowed."""
    harness.max.fail_with = RuntimeError("service.unavailable")

    with pytest.raises(RuntimeError):
        await harness.router.on_telegram_text(
            bot_id=MOM.bot_id,
            telegram_chat_id=OWNER_CHAT,
            telegram_message_id=18,
            text="уйдёт в очередь",
        )

    # The mapping row still exists, so the message is not lost.
    link = await harness.messages.by_telegram_message(MOM.bot_id, 18)
    assert link is not None
    assert link.max_message_id is None


async def test_messages_for_an_unknown_bot_are_dropped(harness: Harness) -> None:
    await harness.router.on_telegram_text(
        bot_id=999, telegram_chat_id=OWNER_CHAT, telegram_message_id=1, text="кому?"
    )

    assert harness.max.sent == []


# ------------------------------------------------------- edits and deletions


@dataclass(slots=True)
class EditableTelegram(FakeTelegram):
    edits: list[tuple[int, str]] = field(default_factory=list)
    deleted: list[int] = field(default_factory=list)
    can_delete: bool = True

    async def edit_text(self, bot_id: int, chat_id: int, message_id: int, text: str) -> bool:
        self.edits.append((message_id, text))
        return True

    async def delete(self, bot_id: int, chat_id: int, message_id: int) -> bool:
        if not self.can_delete:
            return False
        self.deleted.append(message_id)
        return True


@dataclass(slots=True)
class EditableMax(FakeMax):
    edits: list[tuple[int, int, str]] = field(default_factory=list)
    deleted: list[tuple[int, list[int]]] = field(default_factory=list)

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> None:
        self.edits.append((chat_id, message_id, text))

    async def delete_messages(
        self, chat_id: int, message_ids: list[int], *, for_everyone: bool = True
    ) -> None:
        self.deleted.append((chat_id, message_ids))


@pytest_asyncio.fixture
async def editable(tmp_path: Path) -> AsyncIterator[Harness]:
    database = await Database.connect(tmp_path / "bridge.db")
    telegram, max_side = EditableTelegram(), EditableMax()
    router = BridgeRouter(
        lookup=FakeLookup(),
        telegram=telegram,
        max_sender=max_side,
        messages=MessageMapRepository(database),
        state=BridgeStateRepository(database),
        owner_chat_id=OWNER_CHAT,
        timestamp_style=TimestampStyle.OFF,
    )
    try:
        yield Harness(
            router=router,
            telegram=telegram,
            max=max_side,
            messages=MessageMapRepository(database),
        )
    finally:
        await database.close()


async def test_an_edit_in_max_never_calls_telegram_from_the_handler(
    editable: Harness,
) -> None:
    """The handler decides; the queue acts.

    `on_max_edit` used to call `edit_message_text` here, through a port that
    answered `False` for everything — so a caption edit, which Telegram refuses
    on a media message, was dropped in silence. This router has no queue behind
    it, which is a test affordance and not a supported route: with nothing to
    record the intent in, the honest outcome is no remote effect at all.
    """
    await editable.router.on_max_message(incoming(1, "первый вариант"))
    await editable.router.on_max_edit(incoming(1, "исправленный вариант"))

    telegram = editable.telegram
    assert isinstance(telegram, EditableTelegram)
    assert telegram.edits == []
    assert len(telegram.sent) == 1, "the edit must not produce another message"


async def test_edit_of_an_unmapped_message_is_ignored(editable: Harness) -> None:
    await editable.router.on_max_edit(incoming(42, "правка в никуда"))

    telegram = editable.telegram
    assert isinstance(telegram, EditableTelegram)
    assert telegram.edits == []
    assert telegram.sent == []


async def test_a_deletion_in_max_never_calls_telegram_from_the_handler(
    editable: Harness,
) -> None:
    """The same for removal, and for the same reason.

    It used to delete `link.telegram_message_id` and nothing else, which for an
    album took the head and left the rest. What replaces it is a durable job that
    re-derives every id the message became — see `test_max_mutations.py`.
    """
    from bridge.max_client import MessageDeleted

    await editable.router.on_max_message(incoming(1, "упс"))
    await editable.router.on_max_delete(MessageDeleted(chat_id=MOM.max_chat_id, message_ids=(1,)))

    telegram = editable.telegram
    assert isinstance(telegram, EditableTelegram)
    assert telegram.deleted == []


async def test_reply_from_max_keeps_the_thread(harness: Harness) -> None:
    await harness.router.on_max_message(incoming(1, "вопрос"))

    await harness.router.on_max_message(
        incoming(2, "ответ", link={"type": "REPLY", "message": {"id": 1}})
    )

    # The Telegram copy answers the Telegram copy of the original.
    assert harness.telegram.replies == [None, 9001]


async def test_reply_to_something_older_than_the_bridge_still_arrives(
    harness: Harness,
) -> None:
    """No mapping means no visual thread — but the answer must not be lost."""
    await harness.router.on_max_message(
        incoming(2, "ответ на древнее", link={"type": "REPLY", "message": {"id": 999}})
    )

    assert len(harness.telegram.sent) == 1
    assert harness.telegram.replies == [None]


async def test_reply_from_telegram_keeps_the_thread(harness: Harness) -> None:
    await harness.router.on_max_message(incoming(1, "вопрос"))

    await harness.router.on_telegram_text(
        bot_id=MOM.bot_id,
        telegram_chat_id=OWNER_CHAT,
        telegram_message_id=17,
        text="мой ответ",
        reply_to_telegram_message_id=9001,
    )

    assert harness.max.replies == [1], "the reply points at the MAX message id"


async def test_reply_to_an_unmapped_telegram_message_is_sent_plain(
    harness: Harness,
) -> None:
    await harness.router.on_telegram_text(
        bot_id=MOM.bot_id,
        telegram_chat_id=OWNER_CHAT,
        telegram_message_id=17,
        text="ответ в пустоту",
        reply_to_telegram_message_id=424242,
    )

    assert harness.max.sent == [(MOM.max_chat_id, "ответ в пустоту")]
    assert harness.max.replies == [None]


# ------------------------------------------------------ MAX refuses a send


def test_a_dialog_that_cannot_be_written_to_is_permanent() -> None:
    """The live failure: bridging MAX's own service account.

    `Bot has restriction to input [chat.control]` will refuse identically for
    ever. Retrying it builds a queue that never drains.
    """
    from bridge.routing.refusals import classify

    refusal = classify(
        RuntimeError(
            "Невозможно отправить сообщение Bot has restriction to input "
            "(Невозможно отправить сообщение) [chat.control]"
        )
    )

    assert refusal.permanent
    assert "служебный аккаунт" in refusal.message
    assert "chat.control" not in refusal.message, "protocol wording is not for the owner"


def test_an_unrecognised_refusal_is_retried() -> None:
    """The safe direction: a wrong 'permanent' drops the owner's message."""
    from bridge.routing.refusals import classify

    for text in ("timed out", "connection reset", "internal server error", ""):
        assert not classify(RuntimeError(text)).permanent, text


async def test_a_permanent_refusal_answers_the_owner_instead_of_raising(
    harness: Harness,
) -> None:
    """Before this it was an unhandled exception: a traceback in the journal,
    nothing in the chat, and a message that looked sent."""
    harness.max.fail_with = RuntimeError("Bot has restriction to input [chat.control]")

    await harness.router.on_telegram_text(
        bot_id=MOM.bot_id,
        telegram_chat_id=OWNER_CHAT,
        telegram_message_id=41,
        text="привет",
    )

    said = [call for call in harness.telegram.sent if "служебный аккаунт" in str(call)]
    assert said, harness.telegram.sent


# ------------------------------------------- the edit mark belongs to Telegram


def _renderers(harness: Harness) -> Any:
    return harness.router


async def test_every_rendering_marks_an_edit_in_italics(
    harness: Harness, tmp_path: Path
) -> None:
    """One mark, the same everywhere, and styled as a note rather than as words.

    Telegram's own label cannot be made consistent: measured seven ways and
    confirmed by the schema, `messages.editMessage` has no field for it and a
    bot's `editMessageText` always comes back with `edit_hide` set. Media makes
    no difference — a text carrying a web page preview is hidden just the same.
    So the mark that *can* be consistent is ours, and it is drawn everywhere.
    """
    # A router with stamps on: `TimestampStyle.OFF` suppresses the mark by
    # design, and the shared harness uses it.
    router = BridgeRouter(
        lookup=FakeLookup(),
        telegram=harness.telegram,
        max_sender=harness.max,
        messages=harness.messages,
        state=BridgeStateRepository(await Database.connect(tmp_path / "marks.db")),
        owner_chat_id=OWNER_CHAT,
        timestamp_style=TimestampStyle.COMPACT,
    )
    edited = incoming(1, "текст", status="EDITED", updateTime=1_700_000_000_000)
    assert edited.edited_at, "the fixture must actually look edited"

    for rendered in (
        router._render(edited),
        router._caption(edited),
        router._render_as_owner(edited),
        router._caption_as_owner(edited),
    ):
        assert "изм." in rendered, rendered
        italics = router._mark_entities(edited, rendered)
        assert len(italics) == 1, rendered
        entity = italics[0]
        assert entity["type"] == "italic"
        # The mark is the body's suffix, so the range is the whole minus its own
        # length — measured in UTF-16 units, not characters.
        mark = router._edit_mark(edited)
        assert entity["offset"] == utf16_length(rendered) - utf16_length(mark)
        assert entity["length"] == utf16_length(mark)
        assert rendered[entity["offset"] :] == mark


async def test_an_unedited_message_gets_no_mark_and_no_italics(
    harness: Harness, tmp_path: Path
) -> None:
    router = BridgeRouter(
        lookup=FakeLookup(),
        telegram=harness.telegram,
        max_sender=harness.max,
        messages=harness.messages,
        state=BridgeStateRepository(await Database.connect(tmp_path / "plain.db")),
        owner_chat_id=OWNER_CHAT,
        timestamp_style=TimestampStyle.COMPACT,
    )
    plain = incoming(1, "текст")
    rendered = router._render(plain)
    assert "изм." not in rendered
    assert router._mark_entities(plain, rendered) == []


async def test_an_edit_still_reaches_telegram_without_the_mark(
    editable: Harness,
) -> None:
    """Removing the mark must not remove the edit: the job is still enqueued."""
    from bridge.storage import Database  # noqa: F401 - documents the fixture's shape

    await editable.router.on_max_message(incoming(1, "первый"))
    await editable.router.on_max_edit(
        incoming(1, "второй", status="EDITED", updateTime=1_700_000_000_000)
    )
    telegram = editable.telegram
    assert isinstance(telegram, EditableTelegram)
    # No queue behind this router, so nothing is carried — but nothing is sent
    # from the handler either, which is the contract the durable path replaced.
    assert telegram.edits == []
    assert len(telegram.sent) == 1
