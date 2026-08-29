"""The bot's own id for a message the owner wrote — observed, never guessed.

Since owner intake moved to the puppet session the mapping row for an
owner-authored message carries an owner-side id and nothing the bot recognises.
A bot needs its own id to put a reaction on a message, so a contact reacting in
MAX to something the *owner* wrote was not mirrored back at all, while a
reaction on the contact's own message was.

What is proved here is that the id is recorded, that the Bot API is still not an
ingress, and — the part that matters — that the binding refuses to guess. The
row carries no content identity, so the only honest evidence is how many rows
are waiting: one is unambiguous, two is ambiguous, and ambiguous means nothing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from bridge.routing.owner_binding import OwnerBotSideBinding
from bridge.storage import Database, MessageMapRepository

ACCOUNT, BOT, OTHER_BOT = 100000001, 9000000001, 9000000002


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


async def _owner_row(
    messages: MessageMapRepository, owner_message_id: int, *, bot_id: int = BOT
) -> int:
    return await messages.record_from_telegram(
        bridge_name="mom",
        max_chat_id=555,
        telegram_bot_id=bot_id,
        telegram_chat_id=ACCOUNT,
        telegram_message_id=None,
        telegram_owner_message_id=owner_message_id,
        telegram_owner_account_id=ACCOUNT,
    )


# ------------------------------------------------------------- the happy path


async def test_one_waiting_message_takes_the_id(database: Database) -> None:
    messages = MessageMapRepository(database)
    link_id = await _owner_row(messages, 1002400)
    binding = OwnerBotSideBinding(messages=messages)

    await binding.observe(bot_id=BOT, telegram_message_id=707)

    link = await messages.by_id(link_id)
    assert link is not None and link.telegram_message_id == 707
    assert binding.counts.bound == 1


async def test_the_reaction_path_can_now_resolve_it(database: Database) -> None:
    """The whole point. `ReactionSync._draw` returns when the row has no
    bot-side id, which is why a contact's reaction on the owner's own message
    was drawn nowhere."""
    messages = MessageMapRepository(database)
    link_id = await _owner_row(messages, 1002401)
    await messages.attach_max_message(link_id, 9001)
    await OwnerBotSideBinding(messages=messages).observe(bot_id=BOT, telegram_message_id=708)

    link = await messages.by_max_message(555, 9001, BOT)
    assert link is not None
    assert link.telegram_message_id == 708  # what the renderer needs
    assert link.max_message_id == 9001


async def test_observing_twice_binds_once(database: Database) -> None:
    """A replayed Bot API update. The second observation finds nothing waiting."""
    messages = MessageMapRepository(database)
    link_id = await _owner_row(messages, 1002402)
    binding = OwnerBotSideBinding(messages=messages)

    await binding.observe(bot_id=BOT, telegram_message_id=709)
    await binding.observe(bot_id=BOT, telegram_message_id=709)

    link = await messages.by_id(link_id)
    assert link is not None and link.telegram_message_id == 709
    assert binding.counts.bound == 1 and binding.counts.unresolved == 1


# ------------------------------------------------------ what it refuses to do


async def test_two_messages_in_flight_are_ambiguous_and_neither_is_bound(
    database: Database,
) -> None:
    """The case a "newest unattached row" rule gets wrong. Two identical texts,
    two waiting rows, and no evidence on either that says which is which —
    binding the wrong one puts a contact's reaction on the wrong message, which
    is worse than putting it nowhere."""
    messages = MessageMapRepository(database)
    first = await _owner_row(messages, 1002403)
    second = await _owner_row(messages, 1002404)
    binding = OwnerBotSideBinding(messages=messages)

    await binding.observe(bot_id=BOT, telegram_message_id=710)

    assert binding.counts.ambiguous == 1 and binding.counts.bound == 0
    for link_id in (first, second):
        link = await messages.by_id(link_id)
        assert link is not None and link.telegram_message_id is None


async def test_nothing_waiting_is_counted_not_guessed(database: Database) -> None:
    binding = OwnerBotSideBinding(messages=MessageMapRepository(database))
    await binding.observe(bot_id=BOT, telegram_message_id=711)
    assert binding.counts.unresolved == 1 and binding.counts.bound == 0


async def test_another_bots_message_is_not_a_candidate(database: Database) -> None:
    messages = MessageMapRepository(database)
    other = await _owner_row(messages, 1002405, bot_id=OTHER_BOT)
    binding = OwnerBotSideBinding(messages=messages)

    await binding.observe(bot_id=BOT, telegram_message_id=712)

    assert binding.counts.unresolved == 1
    link = await messages.by_id(other)
    assert link is not None and link.telegram_message_id is None


async def test_a_row_that_took_another_id_first_is_a_conflict(
    database: Database,
) -> None:
    """The read that chose the row and the write are two statements. The other
    binding may well be the right one, so it is not overwritten."""
    messages = MessageMapRepository(database)
    link_id = await _owner_row(messages, 1002406)
    binding = OwnerBotSideBinding(messages=messages)

    assert await messages.attach_bot_message_if_unset(link_id, 800) is True
    assert await messages.attach_bot_message_if_unset(link_id, 801) is False

    link = await messages.by_id(link_id)
    assert link is not None and link.telegram_message_id == 800
    assert binding.counts.conflict == 0  # this test drove the repository directly


async def test_a_max_delivery_waiting_for_its_own_id_is_not_a_candidate(
    database: Database,
) -> None:
    """A MAX→Telegram row has no bot-side id until its send settles. Reading it
    as an owner message would attach the owner's id to the contact's message."""
    messages = MessageMapRepository(database)
    claim = await messages.claim_from_max(
        bridge_name="mom", max_chat_id=555, max_message_id=9002,
        telegram_bot_id=BOT, telegram_chat_id=ACCOUNT,
    )
    assert claim is not None
    binding = OwnerBotSideBinding(messages=messages)

    await binding.observe(bot_id=BOT, telegram_message_id=713)

    assert binding.counts.unresolved == 1
    link = await messages.by_id(claim)
    assert link is not None and link.telegram_message_id is None


async def test_a_row_without_an_owner_id_is_not_a_candidate(
    database: Database,
) -> None:
    """Only what the owner's session wrote down is an owner message."""
    messages = MessageMapRepository(database)
    await messages.record_from_telegram(
        bridge_name="mom", max_chat_id=555, telegram_bot_id=BOT,
        telegram_chat_id=ACCOUNT, telegram_message_id=None,
    )
    binding = OwnerBotSideBinding(messages=messages)

    await binding.observe(bot_id=BOT, telegram_message_id=714)

    assert binding.counts.unresolved == 1


# --------------------------------------------------- only what is in flight


async def test_history_is_not_a_candidate(database: Database) -> None:
    """The live smoke found 79 waiting rows — every owner message written before
    this observer existed — so nothing was ever unambiguous and nothing ever
    bound. Their Bot API sighting happened long ago and was dropped: they are
    not stale candidates, they are not candidates."""
    from bridge.routing.owner_binding import IN_FLIGHT_MS

    messages = MessageMapRepository(database)
    for owner_message_id in range(1002500, 1002510):
        await _owner_row(messages, owner_message_id)
    await database.execute(
        "UPDATE message_map SET created_at = created_at - ?", (IN_FLIGHT_MS * 10,)
    )
    fresh = await _owner_row(messages, 1002511)

    binding = OwnerBotSideBinding(messages=messages)
    await binding.observe(bot_id=BOT, telegram_message_id=900)

    assert binding.counts.bound == 1 and binding.counts.ambiguous == 0
    link = await messages.by_id(fresh)
    assert link is not None and link.telegram_message_id == 900


async def test_two_messages_seconds_apart_each_take_their_own_id(
    database: Database,
) -> None:
    """The realistic repeat: the first binds before the second is written, so
    each observation sees exactly one candidate. FIFO inside one ordered stream,
    which is the only place FIFO is allowed to decide anything."""
    messages = MessageMapRepository(database)
    binding = OwnerBotSideBinding(messages=messages)

    first = await _owner_row(messages, 1002520)
    await binding.observe(bot_id=BOT, telegram_message_id=901)
    second = await _owner_row(messages, 1002521)
    await binding.observe(bot_id=BOT, telegram_message_id=902)

    assert binding.counts.bound == 2 and binding.counts.ambiguous == 0
    assert (await messages.by_id(first)).telegram_message_id == 901  # type: ignore[union-attr]
    assert (await messages.by_id(second)).telegram_message_id == 902  # type: ignore[union-attr]


async def test_an_old_row_does_not_steal_a_new_message(database: Database) -> None:
    from bridge.routing.owner_binding import IN_FLIGHT_MS

    messages = MessageMapRepository(database)
    stale = await _owner_row(messages, 1002530)
    await database.execute(
        "UPDATE message_map SET created_at = created_at - ? WHERE id = ?",
        (IN_FLIGHT_MS * 10, stale),
    )
    binding = OwnerBotSideBinding(messages=messages)

    await binding.observe(bot_id=BOT, telegram_message_id=903)

    assert binding.counts.unresolved == 1
    link = await messages.by_id(stale)
    assert link is not None and link.telegram_message_id is None


# ------------------------------------------- the chat a bot can actually post in


async def test_an_owner_row_stores_the_owner_chat_not_the_peer(
    database: Database,
) -> None:
    """`telegram_chat_id` is what a bot passes to `setMessageReaction`, and for a
    private dialog that is the *owner's* chat with the bot. The MTProto intake
    works in peers and hands over the bot as the peer — the same dialog seen
    from the other end, and the wrong number to give a bot.

    Storing it was why a contact's reaction on an owner-authored message was
    drawn nowhere even after the row had a bot-side id: the renderer was pointed
    at a chat the bot cannot post in.
    """
    from bridge.routing.delivery import DeliveryPipe
    from bridge.routing.router import BridgeRouter, BridgeTarget
    from bridge.storage import BridgeStateRepository, OutboxRepository

    class Lookup:
        def bridge_for_bot(self, bot_id: int) -> BridgeTarget | None:
            return BridgeTarget(name="mom", max_chat_id=555, bot_id=BOT)

        def bridge_for_max_chat(self, chat_id: int) -> BridgeTarget | None:
            return None

    class Max:
        async def send_text(self, chat_id: int, text: str, **kw: object) -> int:
            return 9001

    async def _send(
        kind: str, direction: object, payload: dict[str, object], sending: object
    ) -> int:
        return 9001

    messages = MessageMapRepository(database)
    router = BridgeRouter(
        lookup=Lookup(),  # type: ignore[arg-type]
        telegram=None,  # type: ignore[arg-type]
        max_sender=Max(),  # type: ignore[arg-type]
        messages=messages,
        state=BridgeStateRepository(database),
        owner_chat_id=ACCOUNT,
        pipe=DeliveryPipe(outbox=OutboxRepository(database), send=_send),  # type: ignore[arg-type]
    )

    await router.on_telegram_text(
        bot_id=BOT,
        telegram_chat_id=BOT,  # what the MTProto intake hands over: the peer
        telegram_message_id=1002600,
        text="привет",
        owner_account_id=ACCOUNT,
    )

    link = await messages.by_owner_account_message(ACCOUNT, 1002600)
    assert link is not None
    assert link.telegram_chat_id == ACCOUNT, "the row points at a chat the bot cannot post in"
    assert link.telegram_bot_id == BOT
