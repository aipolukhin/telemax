"""AU-3 G4 — an emoji reply is a message, so it takes the queue.

When the owner reacts with something MAX has no reaction for, the bridge says it
the way a person would: a reply consisting of the emoji. That is a message
somebody receives, and it was sent straight at MAX with nothing behind it. A
timeout raised, the update was replayed, and the contact got the emoji twice.

Setting and clearing an actual MAX reaction stay exactly as they were. They
replace state rather than adding to it, so a repeat is free and a job would be
overhead.

The reaction now arrives from the owner's puppet session rather than over Bot
API, so the identity of the *event* changed with it: the update's `pts` and the
owner-side message id, in place of a Bot API update id and a bot-side one. What
the key has to do did not change, and is what these tests hold.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest_asyncio

from bridge.config import ReactionsConfig
from bridge.reactions.sync import ReactionSync
from bridge.routing.delivery import KIND_TG_TO_MAX_TEXT, DeliveryPipe
from bridge.routing.echo import owner_emoji_note_source_key
from bridge.routing.router import BridgeRouter, BridgeTarget
from bridge.routing.settlement import settle_max_delivery_mapping
from bridge.storage import (
    BridgeStateRepository,
    Database,
    MessageMapRepository,
    OutboxRepository,
    ReactionStateRepository,
)

BOT = 9000000001
MAX_CHAT = 555
OWNER = 100000001
TARGET_TG = 4242
OWNER_MESSAGE = 1002319
TARGET_MAX = 9001
#: An emoji MAX's own set does not carry, so the bridge has to send it as text.
UNMAPPED = "\N{PINCHING HAND}"


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


class MaxSpy:
    def __init__(self, *, fault: BaseException | None = None) -> None:
        self.texts: list[tuple[int, str, int | None]] = []
        self.added: list[tuple[int, int, str]] = []
        self.removed: list[tuple[int, int]] = []
        self._fault = fault

    async def send_text(self, chat_id: int, text: str, *, reply_to: int | None = None) -> int:
        self.texts.append((chat_id, text, reply_to))
        if self._fault is not None:
            raise self._fault
        return 7777

    async def send_media(self, *a: Any, **k: Any) -> int:
        return 1

    async def send_contact(self, *a: Any, **k: Any) -> int:
        return 1

    async def edit_text(self, *a: Any, **k: Any) -> None: ...

    async def delete_messages(self, *a: Any, **k: Any) -> None: ...

    async def add_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        self.added.append((chat_id, message_id, emoji))

    async def remove_reaction(self, chat_id: int, message_id: int) -> None:
        self.removed.append((chat_id, message_id))

    async def reactions_for(self, *a: Any, **k: Any) -> dict[int, Any]:
        return {}


class Lookup:
    def bridge_for_bot(self, bot_id: int) -> BridgeTarget | None:
        return BridgeTarget(name="mom", max_chat_id=MAX_CHAT, bot_id=BOT) if bot_id == BOT else None

    def bridge_for_max_chat(self, max_chat_id: int) -> BridgeTarget | None:
        return (
            BridgeTarget(name="mom", max_chat_id=MAX_CHAT, bot_id=BOT)
            if max_chat_id == MAX_CHAT
            else None
        )


class Live:
    def __init__(self, database: Database, *, fault: BaseException | None = None) -> None:
        self.database = database
        self.messages = MessageMapRepository(database)
        self.outbox = OutboxRepository(database)
        self.max = MaxSpy(fault=fault)
        self.router = BridgeRouter(
            lookup=Lookup(),
            telegram=MaxSpy(),
            max_sender=self.max,
            messages=self.messages,
            state=BridgeStateRepository(database),
            owner_chat_id=OWNER,
            pipe=DeliveryPipe(outbox=self.outbox, send=self._send),
        )
        self.sync = ReactionSync(
            renderer=MaxSpy(),
            max_sender=self.max,
            messages=self.messages,
            snapshots=ReactionStateRepository(database),
            config=ReactionsConfig(),
            notes=self.router,
        )

    async def _send(self, kind: str, direction: Any, payload: dict[str, Any], sending: Any) -> int:
        """The text branch of `send_job`, including its settlement."""
        assert kind == KIND_TG_TO_MAX_TEXT
        await sending()
        sent = await self.max.send_text(
            payload["max_chat_id"], payload["text"], reply_to=payload.get("reply_to")
        )
        return await settle_max_delivery_mapping(self.messages, payload, sent)

    async def a_known_message(self) -> None:
        link_id = await self.messages.record_from_telegram(
            bridge_name="mom",
            max_chat_id=MAX_CHAT,
            telegram_bot_id=BOT,
            telegram_chat_id=OWNER,
            telegram_message_id=TARGET_TG,
        )
        await self.messages.attach_max_message(link_id, TARGET_MAX)

    async def react(self, emoji: str | None, *, event_id: int) -> None:
        await self.sync.apply_owner_reaction(
            telegram_bot_id=BOT,
            max_chat_id=MAX_CHAT,
            max_message_id=TARGET_MAX,
            owner_account_id=OWNER,
            owner_message_id=OWNER_MESSAGE,
            emoji=emoji,
            custom_id=None,
            pts=event_id,
        )

    async def jobs(self) -> list[Any]:
        return await self.database.query(
            "SELECT source_key, state FROM outbox WHERE kind = ?", (KIND_TG_TO_MAX_TEXT,)
        )


# ------------------------------------------------------------------- the note


async def test_an_unmapped_emoji_becomes_a_durable_job(database: Database) -> None:
    live = Live(database)
    await live.a_known_message()

    await live.react(UNMAPPED, event_id=500)

    jobs = await live.jobs()
    assert len(jobs) == 1
    assert jobs[0]["source_key"] == owner_emoji_note_source_key(
        OWNER, BOT, OWNER_MESSAGE, 500, UNMAPPED
    )
    assert live.max.texts == [(MAX_CHAT, UNMAPPED, TARGET_MAX)]


async def test_the_same_update_replayed_makes_one_note(database: Database) -> None:
    """A catch-up re-delivers a version already settled. Keyed by that version,
    the replay finds the job that already exists."""
    live = Live(database)
    await live.a_known_message()

    await live.react(UNMAPPED, event_id=500)
    await live.react(UNMAPPED, event_id=500)
    await live.react(UNMAPPED, event_id=500)

    assert len(await live.jobs()) == 1
    assert len(live.max.texts) == 1


async def test_setting_clearing_and_setting_again_are_three_notes(
    database: Database,
) -> None:
    """The same emoji twice with a removal in between is not a replay — the
    source key has to tell them apart, which is why it is not built from the
    emoji."""
    live = Live(database)
    await live.a_known_message()

    await live.react(UNMAPPED, event_id=500)
    await live.react(None, event_id=501)  # cleared: a real MAX reaction call
    await live.react(UNMAPPED, event_id=502)

    assert len(await live.jobs()) == 2
    assert len(live.max.texts) == 2


async def test_a_timeout_leaves_one_ambiguous_job_and_no_second_note(
    database: Database,
) -> None:
    """The failure this commit exists for. The send may have landed, so the job
    waits for the owner — and the replay behind it sends nothing."""
    from bridge.storage import OutboxState

    live = Live(database, fault=TimeoutError("no answer"))
    await live.a_known_message()

    # The pipe settles the job and does not re-raise an unconfirmed send: there
    # is nothing for the handler to do about it, and raising would make the inbox
    # replay an update whose message may already have arrived.
    await live.react(UNMAPPED, event_id=500)

    jobs = await live.jobs()
    assert len(jobs) == 1
    assert jobs[0]["state"] == OutboxState.AMBIGUOUS.value

    # The inbox replays the update behind the raised handler.
    await live.react(UNMAPPED, event_id=500)
    assert len(live.max.texts) == 1  # not two
    assert len(await live.jobs()) == 1


async def test_the_note_leaves_a_claim_so_its_echo_is_recognised(
    database: Database,
) -> None:
    """MAX echoes back everything the bridge sends. Without a claim row the
    bridge's own emoji came back looking like a message from the contact — which
    the direct send had no way to avoid, because it wrote no row at all."""
    live = Live(database)
    await live.a_known_message()

    await live.react(UNMAPPED, event_id=500)

    assert await live.messages.is_echo_of_our_own(MAX_CHAT, 7777)


# ------------------------------------------------- the idempotent half, unchanged


async def test_a_mapped_emoji_takes_the_queue_too(database: Database) -> None:
    """It used to be direct, because setting a reaction is idempotent. That says
    a repeat is free; it says nothing about a process that dies before the call,
    and an MTProto update has no durable inbox to be replayed from."""
    live = Live(database)
    await live.a_known_message()

    await live.react("\N{THUMBS UP SIGN}", event_id=500)

    assert live.max.added == [], "no reaction goes straight at MAX any more"
    assert live.max.texts == []
    rows = await live.database.query(
        "SELECT source_key FROM outbox WHERE kind = 'tg_to_max_reaction'"
    )
    assert len(rows) == 1


async def test_clearing_a_reaction_is_a_job_as_well(database: Database) -> None:
    live = Live(database)
    await live.a_known_message()

    await live.react(None, event_id=500)

    assert live.max.removed == []
    rows = await live.database.query(
        "SELECT payload_json FROM outbox WHERE kind = 'tg_to_max_reaction'"
    )
    assert len(rows) == 1


async def test_without_a_queue_the_note_is_not_sent_at_all(database: Database) -> None:
    """A note has to be durable or not exist. Sending it straight at MAX is what
    put the emoji in the chat twice, and there is no configuration in which the
    owner path runs without a queue behind it."""
    live = Live(database)
    live.sync = ReactionSync(
        renderer=MaxSpy(),
        max_sender=live.max,
        messages=live.messages,
        snapshots=ReactionStateRepository(database),
        config=ReactionsConfig(),
        notes=None,
    )
    await live.a_known_message()

    await live.react(UNMAPPED, event_id=500)

    assert live.max.texts == []
    assert await live.jobs() == []
