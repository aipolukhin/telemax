"""Inc 3 — binding the owner's own id onto messages the bridge sent them.

Three things are under test and nothing else: that what was sent is written down
before it is sent, that an echo is matched against the *head* of the queue rather
than against whatever looks similar, and that a binding which cannot be proved
leaves the delivery exactly as delivered. Albums are Inc 4 and are only checked
here to the extent that they must not be bound by accident.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from bridge.routing.delivery import (
    KIND_MAX_TO_TG_TEXT,
    KIND_OWNER_ECHO_BIND,
    DeferDelivery,
    DeliveryPipe,
    UnconfirmedDeliveryError,
)
from bridge.routing.echo import media_echo_fingerprint, text_echo_fingerprint
from bridge.routing.owner_echo import GIVE_UP_MS, echo_source_key, resolve_echo
from bridge.routing.owner_mutation import delete_source_key, resolve_delete
from bridge.storage import (
    BridgeStateRepository,
    Database,
    Direction,
    MessageMapRepository,
    OutboxRepository,
    OutboxState,
)

ACCOUNT = 100000001
BRIDGE = "mom"
OTHER_BRIDGE = "dad"
BOT = 9000000001
OTHER_BOT = 9000000002
MAX_CHAT = 555
OWNER_CHAT = 4242


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.connect(tmp_path / "bridge.db")
    try:
        yield database
    finally:
        await database.close()


async def _sent(
    messages: MessageMapRepository,
    *,
    text: str | None = None,
    media: tuple[str, str] | None = None,
    bot_id: int = BOT,
    bridge: str = BRIDGE,
    max_message_id: int,
) -> int:
    """One MAX→TG message claimed the way the router claims it: fingerprint first."""
    if media is not None:
        kind, caption = media
        fingerprint = media_echo_fingerprint(kind, caption=caption)
    else:
        fingerprint = text_echo_fingerprint(text or "")
    link = await messages.claim_from_max(
        bridge_name=bridge,
        max_chat_id=MAX_CHAT,
        max_message_id=max_message_id,
        telegram_bot_id=bot_id,
        telegram_chat_id=OWNER_CHAT,
        echo_fingerprint=fingerprint,
    )
    assert link is not None
    return link


def _echo(
    *,
    owner_message_id: int,
    text: str | None = None,
    media: tuple[str, str] | None = None,
    bot_id: int = BOT,
    bridge: str = BRIDGE,
    first_seen_ms: int = 1 << 62,
) -> dict[str, Any]:
    """The payload of one durable echo-binding job."""
    if media is not None:
        kind, caption = media
        fingerprint = media_echo_fingerprint(kind, caption=caption)
    else:
        fingerprint = text_echo_fingerprint(text or "")
    return {
        "bot_id": bot_id,
        "bridge_name": bridge,
        "account_id": ACCOUNT,
        "owner_message_id": owner_message_id,
        "fingerprint": fingerprint,
        # Far in the future by default, so the patience has not run out and a
        # blocked head defers rather than being given up on mid-test.
        "first_seen_ms": first_seen_ms,
    }


async def _bind(db: Database, payload: dict[str, Any]) -> None:
    await resolve_echo(
        messages=MessageMapRepository(db), state=BridgeStateRepository(db), payload=payload
    )


# --------------------------------------------------------------- text binding


async def test_a_text_echo_binds_the_owner_side_id(db: Database) -> None:
    messages = MessageMapRepository(db)
    link = await _sent(messages, text="[30/07] привет", max_message_id=1)

    await _bind(db, _echo(owner_message_id=900, text="[30/07] привет"))

    bound = await messages.by_owner_account_message(ACCOUNT, 900)
    assert bound is not None and bound.id == link


async def test_two_identical_texts_bind_in_order(db: Database) -> None:
    """The case a fingerprint cannot decide on its own: same words twice. The
    order they were sent in is what tells them apart, and it is durable."""
    messages = MessageMapRepository(db)
    first = await _sent(messages, text="ок", max_message_id=1)
    second = await _sent(messages, text="ок", max_message_id=2)

    await _bind(db, _echo(owner_message_id=900, text="ок"))
    await _bind(db, _echo(owner_message_id=901, text="ок"))

    assert (await messages.by_owner_account_message(ACCOUNT, 900)).id == first  # type: ignore[union-attr]
    assert (await messages.by_owner_account_message(ACCOUNT, 901)).id == second  # type: ignore[union-attr]


async def test_a_newer_matching_row_is_never_taken_before_the_head(db: Database) -> None:
    """A later row matches the echo exactly; the head does not. Binding the
    obvious-looking one is precisely the mistake head-of-line exists to stop."""
    messages = MessageMapRepository(db)
    head = await _sent(messages, text="первое", max_message_id=1)
    later = await _sent(messages, text="второе", max_message_id=2)

    with pytest.raises(DeferDelivery):
        await _bind(db, _echo(owner_message_id=900, text="второе"))

    assert await messages.by_owner_account_message(ACCOUNT, 900) is None
    for link_id in (head, later):
        row = await messages.by_max_message(MAX_CHAT, 1 if link_id == head else 2, BOT)
        assert row is not None and row.echo_fingerprint is not None, "neither row was touched"


async def test_the_same_text_in_another_bridge_is_not_touched(db: Database) -> None:
    messages = MessageMapRepository(db)
    ours = await _sent(messages, text="ок", max_message_id=1)
    theirs = await _sent(
        messages, text="ок", bot_id=OTHER_BOT, bridge=OTHER_BRIDGE, max_message_id=2
    )

    await _bind(db, _echo(owner_message_id=900, text="ок"))

    assert (await messages.by_owner_account_message(ACCOUNT, 900)).id == ours  # type: ignore[union-attr]
    other = await messages.by_max_message(MAX_CHAT, 2, OTHER_BOT)
    assert other is not None and other.id == theirs
    assert other.telegram_owner_message_id is None, "another bridge's queue is separate"


async def test_an_echo_with_nothing_waiting_is_ignored(db: Database) -> None:
    """A status line, a refusal notice, anything the bot posts that is not a
    carried MAX message. Silence, not an incident — these arrive all day."""
    await _bind(db, _echo(owner_message_id=900, text="статус: на связи"))
    assert await MessageMapRepository(db).by_owner_account_message(ACCOUNT, 900) is None


async def test_a_replayed_echo_binds_once(db: Database) -> None:
    messages = MessageMapRepository(db)
    link = await _sent(messages, text="привет", max_message_id=1)
    await _sent(messages, text="привет", max_message_id=2)

    await _bind(db, _echo(owner_message_id=900, text="привет"))
    await _bind(db, _echo(owner_message_id=900, text="привет"))  # a catch-up replay

    assert (await messages.by_owner_account_message(ACCOUNT, 900)).id == link  # type: ignore[union-attr]
    second = await messages.by_max_message(MAX_CHAT, 2, BOT)
    assert second is not None and second.telegram_owner_message_id is None


# -------------------------------------------------------------- media binding


async def test_a_single_photo_echo_binds(db: Database) -> None:
    messages = MessageMapRepository(db)
    link = await _sent(messages, media=("photo", "[30/07]"), max_message_id=1)

    await _bind(db, _echo(owner_message_id=900, media=("photo", "[30/07]")))

    assert (await messages.by_owner_account_message(ACCOUNT, 900)).id == link  # type: ignore[union-attr]


async def test_two_identical_photos_bind_in_order(db: Database) -> None:
    messages = MessageMapRepository(db)
    first = await _sent(messages, media=("photo", ""), max_message_id=1)
    second = await _sent(messages, media=("photo", ""), max_message_id=2)

    await _bind(db, _echo(owner_message_id=900, media=("photo", "")))
    await _bind(db, _echo(owner_message_id=901, media=("photo", "")))

    assert (await messages.by_owner_account_message(ACCOUNT, 900)).id == first  # type: ignore[union-attr]
    assert (await messages.by_owner_account_message(ACCOUNT, 901)).id == second  # type: ignore[union-attr]


async def test_a_voice_note_does_not_bind_to_a_photo(db: Database) -> None:
    """Structure is all there is to go on — MTProto has no `file_unique_id` — so
    it has to be enough to refuse with, not only to accept with."""
    messages = MessageMapRepository(db)
    await _sent(messages, media=("photo", ""), max_message_id=1)

    with pytest.raises(DeferDelivery):
        await _bind(db, _echo(owner_message_id=900, media=("voice", "")))

    assert await messages.by_owner_account_message(ACCOUNT, 900) is None


async def test_an_unbindable_kind_never_becomes_a_candidate(db: Database) -> None:
    """A sticker is rebuilt on the way out, so it does not come back as itself.
    Its row is claimed with no fingerprint, which means skip — not block."""
    messages = MessageMapRepository(db)
    assert media_echo_fingerprint("sticker", caption="") is None
    sticker = await messages.claim_from_max(
        bridge_name=BRIDGE,
        max_chat_id=MAX_CHAT,
        max_message_id=1,
        telegram_bot_id=BOT,
        telegram_chat_id=OWNER_CHAT,
        echo_fingerprint=None,
    )
    text_link = await _sent(messages, text="после стикера", max_message_id=2)

    await _bind(db, _echo(owner_message_id=900, text="после стикера"))

    assert (await messages.by_owner_account_message(ACCOUNT, 900)).id == text_link  # type: ignore[union-attr]
    assert sticker is not None


# ------------------------------------------------------- a head that never binds


async def test_a_blocked_head_waits_rather_than_being_stepped_around(db: Database) -> None:
    messages = MessageMapRepository(db)
    await _sent(messages, text="потерянное", max_message_id=1)
    await _sent(messages, text="следующее", max_message_id=2)

    with pytest.raises(DeferDelivery):
        await _bind(db, _echo(owner_message_id=900, text="следующее"))


async def test_a_lost_echo_is_given_up_on_loudly_and_the_queue_moves(db: Database) -> None:
    """The head's echo never arrived. It is not silently skipped: the bridge
    records the failure once, the row is marked unbindable so it stops holding
    everything up, and only then does the waiting echo take its own row."""
    messages, state = MessageMapRepository(db), BridgeStateRepository(db)
    lost = await _sent(messages, text="потерянное", max_message_id=1)
    mine = await _sent(messages, text="следующее", max_message_id=2)

    await resolve_echo(
        messages=messages,
        state=state,
        payload=_echo(owner_message_id=900, text="следующее", first_seen_ms=0),
    )

    assert (await messages.by_owner_account_message(ACCOUNT, 900)).id == mine  # type: ignore[union-attr]
    abandoned = await messages.by_max_message(MAX_CHAT, 1, BOT)
    assert abandoned is not None and abandoned.id == lost
    assert abandoned.echo_fingerprint is None, "given up on, so it blocks nothing"
    assert abandoned.telegram_owner_message_id is None, "and never bound to someone else"
    recorded = await state.snapshot(BRIDGE) or {}
    assert "owner echo binding" in str(recorded.get("last_error"))


async def test_giving_up_says_nothing_about_the_message_itself(db: Database) -> None:
    messages, state = MessageMapRepository(db), BridgeStateRepository(db)
    await _sent(messages, text="секрет клиента", max_message_id=1)
    await _sent(messages, text="ответ", max_message_id=2)

    await resolve_echo(
        messages=messages,
        state=state,
        payload=_echo(owner_message_id=900, text="ответ", first_seen_ms=0),
    )

    recorded = str((await state.snapshot(BRIDGE) or {}).get("last_error") or "")
    assert "секрет" not in recorded and "ответ" not in recorded
    assert "t1:" in recorded, "hashes and ids only"


async def test_an_echo_matching_nothing_at_all_needs_attention(db: Database) -> None:
    messages = MessageMapRepository(db)
    await _sent(messages, text="единственное", max_message_id=1)

    with pytest.raises(UnconfirmedDeliveryError):
        await resolve_echo(
            messages=messages,
            state=BridgeStateRepository(db),
            payload=_echo(owner_message_id=900, text="чужое", first_seen_ms=0),
        )


# ------------------------------------------------------------ the two orders


async def test_an_echo_arriving_before_the_send_result_still_binds(db: Database) -> None:
    """The row is claimed before the first Telegram call, so the echo cannot
    outrun it — however early it arrives, there is already something to match."""
    messages = MessageMapRepository(db)
    link = await _sent(messages, text="привет", max_message_id=1)
    unsent = await messages.by_max_message(MAX_CHAT, 1, BOT)
    assert unsent is not None and unsent.telegram_message_id is None

    await _bind(db, _echo(owner_message_id=900, text="привет"))

    bound = await messages.by_owner_account_message(ACCOUNT, 900)
    assert bound is not None and bound.id == link
    await messages.attach_telegram_message(link, 7001)  # the Bot API result, late
    both = await messages.by_owner_account_message(ACCOUNT, 900)
    assert both is not None
    assert both.telegram_message_id == 7001 and both.telegram_owner_message_id == 900


async def test_a_send_result_arriving_first_changes_nothing(db: Database) -> None:
    messages = MessageMapRepository(db)
    link = await _sent(messages, text="привет", max_message_id=1)
    await messages.attach_telegram_message(link, 7001)

    await _bind(db, _echo(owner_message_id=900, text="привет"))

    both = await messages.by_owner_account_message(ACCOUNT, 900)
    assert both is not None
    assert both.telegram_message_id == 7001 and both.telegram_owner_message_id == 900


async def test_the_bot_side_id_is_written_once(db: Database) -> None:
    """Two senders call it now — the inline path and the worker, through the hook
    they share — and the second must not move an id the first already proved."""
    messages = MessageMapRepository(db)
    link = await _sent(messages, text="привет", max_message_id=1)

    await messages.attach_telegram_message(link, 7001)
    await messages.attach_telegram_message(link, 7002)

    row = await messages.by_max_message(MAX_CHAT, 1, BOT)
    assert row is not None and row.telegram_message_id == 7001


async def test_a_conflicting_owner_id_is_not_overwritten(db: Database) -> None:
    messages = MessageMapRepository(db)
    link = await _sent(messages, text="привет", max_message_id=1)
    assert await messages.bind_owner_message(link, account_id=ACCOUNT, owner_message_id=900)

    assert await messages.bind_owner_message(link, account_id=ACCOUNT, owner_message_id=900), (
        "the same identity again is success, not a conflict"
    )
    assert not await messages.bind_owner_message(
        link, account_id=ACCOUNT, owner_message_id=901
    ), "a different owner message must not take a row that is already proved"


# ------------------------------------------------------------- one send order


async def test_the_inline_path_waits_behind_an_older_undelivered_send(db: Database) -> None:
    """The reordering this closes: an earlier message back on the queue after a
    failure while the next one goes straight out inline."""
    outbox = OutboxRepository(db)
    stuck = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={"text": "первое"},
        source_key="max:555:1",
    )

    pipe = DeliveryPipe(outbox=outbox, send=_never_sends)
    job_id, ours = await pipe.submit_in_order(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={"text": "второе"},
        source_key="max:555:2",
    )

    assert ours is False, "it does not send: something older has not gone out"
    later = await outbox.by_source_key("max:555:2")
    assert later is not None and later.id == job_id
    assert later.state is OutboxState.PENDING, "durable and waiting its turn"
    assert await outbox.older_undelivered(
        BRIDGE, direction=Direction.MAX_TO_TG, before_id=job_id
    )
    assert not await outbox.older_undelivered(
        BRIDGE, direction=Direction.MAX_TO_TG, before_id=stuck
    ), "the head itself is free to go"


async def test_the_inline_path_sends_when_it_is_first_in_line(db: Database) -> None:
    outbox = OutboxRepository(db)
    pipe = DeliveryPipe(outbox=outbox, send=_never_sends)

    _job, ours = await pipe.submit_in_order(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={"text": "первое"},
        source_key="max:555:1",
    )

    assert ours is True, "an empty queue still sends without waiting for a poll"


async def test_a_delivered_predecessor_does_not_hold_anything_back(db: Database) -> None:
    outbox = OutboxRepository(db)
    done = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={"text": "первое"},
        source_key="max:555:1",
    )
    await outbox.mark_done(done, remote_message_id=7001)

    pipe = DeliveryPipe(outbox=outbox, send=_never_sends)
    _job, ours = await pipe.submit_in_order(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={"text": "второе"},
        source_key="max:555:2",
    )
    assert ours is True


async def test_the_other_direction_is_a_separate_order(db: Database) -> None:
    outbox = OutboxRepository(db)
    await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.TG_TO_MAX,
        kind="tg_to_max_text",
        payload={"text": "владелец"},
        source_key="tg:1:1",
    )
    pipe = DeliveryPipe(outbox=outbox, send=_never_sends)
    _job, ours = await pipe.submit_in_order(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={"text": "контакт"},
        source_key="max:555:1",
    )
    assert ours is True, "a message going the other way is not ahead of this one"


async def _never_sends(
    kind: str, direction: Direction, payload: dict[str, Any], sending: Any
) -> int | None:
    raise AssertionError("no send should happen in an ordering test")


# ------------------------------------------------- what the binding unlocks


async def test_a_bound_incoming_copy_is_deletable_through_inc_2(db: Database) -> None:
    """The point of the whole increment: deleting the Telegram copy from the
    owner's own client now finds the MAX message behind it, through the delete
    path Inc 2 already built. No second delete implementation."""
    messages, outbox = MessageMapRepository(db), OutboxRepository(db)
    link = await _sent(messages, text="от контакта", max_message_id=4242)
    await messages.attach_telegram_message(link, 7001)
    await _bind(db, _echo(owner_message_id=900, text="от контакта"))

    deleted: list[tuple[int, list[int], bool]] = []

    class _Max:
        async def delete_messages(
            self, chat_id: int, message_ids: list[int], *, for_everyone: bool = True
        ) -> None:
            deleted.append((chat_id, message_ids, for_everyone))

    await resolve_delete(
        outbox=outbox,
        messages=messages,
        max_sender=_Max(),
        payload={
            "max_chat_id": MAX_CHAT,
            "account_id": ACCOUNT,
            "owner_message_id": 900,
            "send_source_key": "tg-owner-msg:100000001:900",
        },
    )

    assert deleted == [(MAX_CHAT, [4242], True)], "one operation, MAX scopes it"
    assert delete_source_key(ACCOUNT, 900).startswith("tg-owner-delete:")


async def test_an_unbound_copy_resolves_to_nothing_rather_than_guessing(db: Database) -> None:
    messages = MessageMapRepository(db)
    await _sent(messages, text="от контакта", max_message_id=4242)

    assert await messages.by_owner_account_message(ACCOUNT, 900) is None


# ------------------------------------------------------- the durable echo job


async def test_the_echo_job_is_keyed_by_the_owner_side_message(db: Database) -> None:
    outbox = OutboxRepository(db)
    key = echo_source_key(ACCOUNT, 900)
    assert key == f"tg-owner-echo:{ACCOUNT}:900"

    first = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_OWNER_ECHO_BIND,
        payload=_echo(owner_message_id=900, text="привет"),
        source_key=key,
    )
    again = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_OWNER_ECHO_BIND,
        payload=_echo(owner_message_id=900, text="привет"),
        source_key=key,
    )
    assert first == again, "a replayed update finds its own job"


async def test_waiting_for_a_head_spends_no_delivery_budget(db: Database) -> None:
    """A binding that waits must not walk anything toward failure, and the
    delivery it is about stays delivered."""
    messages, outbox = MessageMapRepository(db), OutboxRepository(db)
    await _sent(messages, text="потерянное", max_message_id=1)
    await _sent(messages, text="следующее", max_message_id=2)
    delivery = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={"text": "следующее"},
        source_key="max:555:2",
    )
    await outbox.mark_done(delivery, remote_message_id=7002)

    payload = _echo(owner_message_id=900, text="следующее")
    job = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_OWNER_ECHO_BIND,
        payload=payload,
        source_key=echo_source_key(ACCOUNT, 900),
    )

    async def send(kind: str, direction: Direction, load: dict[str, Any], sending: Any) -> Any:
        return await resolve_echo(
            messages=messages, state=BridgeStateRepository(db), payload=load
        )

    settled = await DeliveryPipe(outbox=outbox, send=send).attempt(
        job_id=job,
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_OWNER_ECHO_BIND,
        payload=payload,
    )

    assert settled.deferred is True
    row = await outbox.by_source_key(echo_source_key(ACCOUNT, 900))
    assert row is not None and row.state is OutboxState.PENDING and row.attempts == 0
    carried = await outbox.by_source_key("max:555:2")
    assert carried is not None and carried.state is OutboxState.DONE, "delivery is untouched"


async def test_an_unbindable_echo_leaves_the_delivery_done(db: Database) -> None:
    messages, outbox = MessageMapRepository(db), OutboxRepository(db)
    await _sent(messages, text="единственное", max_message_id=1)
    delivery = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={"text": "единственное"},
        source_key="max:555:1",
    )
    await outbox.mark_done(delivery, remote_message_id=7001)

    payload = _echo(owner_message_id=900, text="чужое", first_seen_ms=0)
    job = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_OWNER_ECHO_BIND,
        payload=payload,
        source_key=echo_source_key(ACCOUNT, 900),
    )

    async def send(kind: str, direction: Direction, load: dict[str, Any], sending: Any) -> Any:
        return await resolve_echo(
            messages=messages, state=BridgeStateRepository(db), payload=load
        )

    settled = await DeliveryPipe(outbox=outbox, send=send).attempt(
        job_id=job,
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_OWNER_ECHO_BIND,
        payload=payload,
    )

    assert settled.ambiguous is True, "the owner is told, once"
    assert await outbox.needing_attention(BRIDGE), "through the existing list"
    carried = await outbox.by_source_key("max:555:1")
    assert carried is not None and carried.state is OutboxState.DONE
    assert json.loads(carried.payload_json) == {}, "delivered, and still delivered"


def test_the_give_up_window_outlasts_a_send(_unused: None = None) -> None:
    """Long enough that an ordinary send in flight is never given up on, which
    is what would turn a slow message into a wrongly bound one."""
    assert GIVE_UP_MS >= 30_000


# ------------------------------------------------- replies, and the shape of it


class _Lookup:
    def bridge_for_bot(self, bot_id: int) -> Any:
        from bridge.routing.router import BridgeTarget

        return BridgeTarget(name=BRIDGE, max_chat_id=MAX_CHAT, bot_id=BOT)

    def bridge_for_max_chat(self, max_chat_id: int) -> Any:
        return None


class _RecordingMax:
    def __init__(self) -> None:
        self.sent: list[tuple[str, int | None]] = []

    async def send_text(self, chat_id: int, text: str, *, reply_to: int | None = None) -> int:
        self.sent.append((text, reply_to))
        return 111411200589824009


class _Silent:
    async def send_text(self, *a: Any, **k: Any) -> int | None:
        return None

    async def edit_text(self, *a: Any, **k: Any) -> bool:
        return True

    async def delete(self, *a: Any, **k: Any) -> bool:
        return True


async def _router(db: Database, mx: _RecordingMax) -> Any:
    from bridge.routing.router import BridgeRouter

    return BridgeRouter(
        lookup=_Lookup(),
        telegram=_Silent(),
        max_sender=mx,
        messages=MessageMapRepository(db),
        state=BridgeStateRepository(db),
        owner_chat_id=OWNER_CHAT,
    )


async def test_a_reply_to_a_bound_message_reaches_the_right_max_message(db: Database) -> None:
    """What the binding is for. The owner answers the contact from their own
    client, quoting the owner-side id — a number the bot has never seen."""
    messages, mx = MessageMapRepository(db), _RecordingMax()
    await _sent(messages, text="вопрос контакта", max_message_id=4242)
    await _bind(db, _echo(owner_message_id=900, text="вопрос контакта"))

    await (await _router(db, mx)).on_telegram_text(
        bot_id=BOT,
        telegram_chat_id=BOT,
        telegram_message_id=1001,
        text="ответ",
        reply_to_telegram_message_id=900,
        owner_account_id=ACCOUNT,
    )

    assert mx.sent == [("ответ", 4242)], "threaded onto the MAX message behind it"


async def test_a_reply_to_a_bound_photo_resolves_the_same_way(db: Database) -> None:
    messages, mx = MessageMapRepository(db), _RecordingMax()
    await _sent(messages, media=("photo", "[30/07]"), max_message_id=4243)
    await _bind(db, _echo(owner_message_id=900, media=("photo", "[30/07]")))

    await (await _router(db, mx)).on_telegram_text(
        bot_id=BOT,
        telegram_chat_id=BOT,
        telegram_message_id=1001,
        text="красиво",
        reply_to_telegram_message_id=900,
        owner_account_id=ACCOUNT,
    )

    assert mx.sent == [("красиво", 4243)]


async def test_a_reply_to_something_unbound_still_carries_the_message(db: Database) -> None:
    """The existing fallback, unchanged: a thread that cannot be resolved is not
    a message worth dropping."""
    messages, mx = MessageMapRepository(db), _RecordingMax()
    await _sent(messages, text="вопрос контакта", max_message_id=4242)

    await (await _router(db, mx)).on_telegram_text(
        bot_id=BOT,
        telegram_chat_id=BOT,
        telegram_message_id=1001,
        text="ответ",
        reply_to_telegram_message_id=900,
        owner_account_id=ACCOUNT,
    )

    assert mx.sent == [("ответ", None)], "delivered plain rather than lost"


async def test_a_bot_api_reply_still_uses_the_bot_side_id(db: Database) -> None:
    """Two id spaces, and the Bot API one must keep working exactly as before."""
    messages, mx = MessageMapRepository(db), _RecordingMax()
    link = await _sent(messages, text="вопрос контакта", max_message_id=4242)
    await messages.attach_telegram_message(link, 7001)

    await (await _router(db, mx)).on_telegram_text(
        bot_id=BOT,
        telegram_chat_id=BOT,
        telegram_message_id=1001,
        text="ответ",
        reply_to_telegram_message_id=7001,
    )

    assert mx.sent == [("ответ", 4242)]


# ------------------------------------------------------ the worker's half of it


async def test_the_worker_defers_a_job_that_is_not_first_in_line(db: Database) -> None:
    """The other sender obeys the same order. Claiming the head of the *pending*
    queue is not the same as being first: an older job may be in flight inline."""
    from bridge.retry.worker import OutboxWorker

    outbox = OutboxRepository(db)
    inflight, _ours = await outbox.claim_for_attempt(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={"text": "первое"},
        source_key="max:555:1",
    )
    queued = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={"text": "второе"},
        source_key="max:555:2",
    )

    delivered: list[str] = []

    async def deliver(kind: str, direction: Direction, payload: dict[str, Any], hook: Any) -> int:
        delivered.append(payload["text"])
        return 7002

    worker = OutboxWorker(
        bridge_name=BRIDGE,
        outbox=outbox,
        state=BridgeStateRepository(db),
        deliver=deliver,
    )
    await worker.drain_once()

    assert delivered == [], "it waits for the one already going out"
    waiting = await outbox.by_source_key("max:555:2")
    assert waiting is not None and waiting.id == queued
    assert waiting.state is OutboxState.PENDING and waiting.attempts == 0, "no attempt spent"
    assert inflight != queued


async def test_the_worker_delivers_once_the_older_send_is_done(db: Database) -> None:
    from bridge.retry.worker import OutboxWorker

    outbox = OutboxRepository(db)
    first, _ours = await outbox.claim_for_attempt(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={"text": "первое"},
        source_key="max:555:1",
    )
    await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={"text": "второе"},
        source_key="max:555:2",
    )
    await outbox.mark_done(first, remote_message_id=7001)

    delivered: list[str] = []

    async def deliver(kind: str, direction: Direction, payload: dict[str, Any], hook: Any) -> int:
        delivered.append(payload["text"])
        return 7002

    await OutboxWorker(
        bridge_name=BRIDGE,
        outbox=outbox,
        state=BridgeStateRepository(db),
        deliver=deliver,
    ).drain_once()

    assert delivered == ["второе"], "the order held, and nothing was dropped"


async def test_a_worker_delivery_leaves_the_bot_side_id_on_the_mapping(db: Database) -> None:
    """The settle hook's contract: written while the payload still holds
    `link_id`, because `mark_done` clears the payload the moment it lands."""
    messages, outbox = MessageMapRepository(db), OutboxRepository(db)
    link = await _sent(messages, text="от контакта", max_message_id=1)
    job = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={"text": "от контакта", "link_id": link},
        source_key="max:555:1",
    )

    async def deliver_like_the_runtime(
        kind: str, direction: Direction, payload: dict[str, Any], hook: Any
    ) -> int:
        sent = 7001
        await messages.attach_telegram_message(int(payload["link_id"]), sent)
        return sent

    from bridge.retry.worker import OutboxWorker

    await OutboxWorker(
        bridge_name=BRIDGE,
        outbox=outbox,
        state=BridgeStateRepository(db),
        deliver=deliver_like_the_runtime,
    ).drain_once()

    row = await messages.by_max_message(MAX_CHAT, 1, BOT)
    assert row is not None and row.telegram_message_id == 7001
    settled = await outbox.by_source_key("max:555:1")
    assert settled is not None and settled.id == job
    assert settled.state is OutboxState.DONE
    assert json.loads(settled.payload_json) == {}, "the payload is gone by then"


# ----------------------------------------------------------- what did not change


async def test_the_schema_gained_a_column_and_not_a_table(tmp_path: Path) -> None:
    """No echo table, no echo store: the binding lives in the row the delivery
    already had. V14 is one column and one index over it, nothing else."""
    from unittest.mock import patch

    from bridge.storage.migrations import MIGRATIONS

    async def _tables(database: Database) -> set[str]:
        rows = await database.query(
            "SELECT name FROM sqlite_master WHERE type = 'table'", ()
        )
        return {str(row["name"]) for row in rows}

    v13 = tuple(step for step in MIGRATIONS if step[0] <= 13)
    with (
        patch("bridge.storage.database.MIGRATIONS", v13),
        patch("bridge.storage.database.LATEST_VERSION", 13),
    ):
        before = await Database.connect(tmp_path / "before.db")
        try:
            older = await _tables(before)
        finally:
            await before.close()

    # Pinned to V14 rather than to HEAD: the claim is about what *that* step
    # does, and a later migration adding a table of its own is not this test
    # failing — it is this test being asked the wrong question.
    v14 = tuple(step for step in MIGRATIONS if step[0] <= 14)
    with (
        patch("bridge.storage.database.MIGRATIONS", v14),
        patch("bridge.storage.database.LATEST_VERSION", 14),
    ):
        after = await Database.connect(tmp_path / "after.db")
    try:
        assert await _tables(after) == older, "V14 adds no table"
        columns = await after.query("PRAGMA table_info(message_map)", ())
        assert "echo_fingerprint" in {str(row["name"]) for row in columns}
    finally:
        await after.close()


async def test_a_shape_this_increment_cannot_bind_is_dropped_at_intake() -> None:
    """An album or a sticker reaches the intake as None and stops there — before
    a job is created, and without the router hearing about it."""
    from bridge.telegram.mtproto_intake import MtprotoIntake

    class _Router:
        def __init__(self) -> None:
            self.echoes: list[dict[str, Any]] = []

        async def on_contact_echo(self, **kwargs: Any) -> None:
            self.echoes.append(kwargs)

    async def allow() -> set[int]:
        return {BOT}

    router = _Router()
    intake = MtprotoIntake(router=router, allowed_bots=allow)  # type: ignore[arg-type]

    await intake.on_contact_echo(bot_id=BOT, account_id=ACCOUNT, message_id=900, fingerprint=None)
    assert router.echoes == []

    await intake.on_contact_echo(
        bot_id=BOT, account_id=ACCOUNT, message_id=901, fingerprint="t1:abc"
    )
    assert router.echoes == [
        {
            "bot_id": BOT,
            "owner_account_id": ACCOUNT,
            "owner_message_id": 901,
            "fingerprint": "t1:abc",
        }
    ]
    assert await intake.allowed_bots() == {BOT}, "the transport filters on the live set"


def test_an_album_echo_is_not_fingerprinted() -> None:
    """Grouped parts are Inc 4. Until then they describe themselves as nothing,
    which is what keeps them from being bound to a single-media row by mistake."""
    from types import SimpleNamespace

    from bridge.telegram.mtproto_media import echo_fingerprint_of

    album_part = SimpleNamespace(grouped_id=778899, message="подпись", media=object())
    assert echo_fingerprint_of(album_part) is None


def test_there_is_no_intake_gate_to_persist() -> None:
    """The onboarding record used to carry `owner_mtproto_intake_enabled`, which
    chose between two owner ingresses. There is one, so there is nothing to
    choose and nothing to persist. Old state files still load — `_read` keeps
    only known keys — and drop the value on their next save."""
    from bridge.onboarding.state import OnboardingRecord

    assert not hasattr(OnboardingRecord(), "owner_mtproto_intake_enabled")
