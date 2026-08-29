"""Inc 4 — binding the owner's own ids onto an album the bridge delivered.

A contact bot puts an album in the owner's Telegram chat over the Bot API, and
the answer names N messages in the bot's id space. The owner's own client sees
those very messages with N *different* ids, in the owner's space, and those are
the ones a reply, an edit or a delete made from that client speaks. Nothing joins
the two: the ids appear in different places, arrive over different transports,
and race each other freely.

What the live probe settled is what the join can rest on. The order is ascending
Telegram message id and it holds on both sides, through a reconnect's catch-up
and through a replay. Byte-identical photos carry no signal that separates them,
so nothing but that order can. The caption sits wherever the sender typed it, so
its position is part of the structure and not an assumption. Timestamps prove
nothing and are not used.

These tests drive the whole of it: the album goes out, the echo comes back — in
both orders and interleaved — and then a reply, an edit and a delete arrive on
every part of it in turn.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from bridge.config import TimestampStyle
from bridge.formatting import format_stamp
from bridge.max_client import AttachmentKind, MaxAttachment, normalize_message
from bridge.media.delivery import DeliveredPart, DeliveryReceipt, split_caption
from bridge.routing.delivery import (
    KIND_MAX_TO_TG_MEDIA,
    KIND_OWNER_ECHO_BIND,
    KIND_TG_TO_MAX_DELETE,
    KIND_TG_TO_MAX_EDIT,
    DeferDelivery,
    DeliveryPipe,
    UnconfirmedDeliveryError,
)
from bridge.routing.echo import echo_album_namespace
from bridge.routing.owner_echo import resolve_album_echo
from bridge.routing.owner_mutation import resolve_delete, resolve_edit
from bridge.routing.router import BridgeRouter, BridgeTarget
from bridge.service.runtime import settle_telegram_delivery_mapping
from bridge.storage import (
    AlbumSettlementRepository,
    BridgeStateRepository,
    Database,
    Direction,
    MediaGroupRepository,
    MessageMapRepository,
    OutboxRepository,
)
from bridge.telegram.mtproto_intake import MtprotoIntake

ACCOUNT = 100000001
BOT = 9000000001
OTHER_BOT = 9000000002
MAX_CHAT = 555
OWNER_CHAT = 100000001
GROUPED = 13984172040192
SENT_AT = 1_785_000_000_000


def caption_as_sent(text: str) -> str | None:
    """What the album's head part will actually carry into Telegram.

    Built the way the router builds it — the MAX timestamp stamp in front of the
    body — because the fingerprint is over what was *sent*, and an echo carrying
    anything else is, correctly, a different album.
    """
    stamp = format_stamp(SENT_AT, TimestampStyle.COMPACT, tz=None)
    return split_caption(f"{stamp}{text}" if text else stamp)[0]


class MaxSpy:
    def __init__(self) -> None:
        self.edits: list[tuple[int, int, str]] = []
        self.deletes: list[tuple[int, list[int], bool]] = []
        self.sent: list[dict[str, Any]] = []

    async def send_text(self, *a: Any, **k: Any) -> int | None:
        return 1

    async def send_media(self, *a: Any, **k: Any) -> int | None:
        return 1

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> None:
        self.edits.append((chat_id, message_id, text))

    async def delete_messages(
        self, chat_id: int, message_ids: list[int], *, for_everyone: bool = True
    ) -> None:
        self.deletes.append((chat_id, list(message_ids), for_everyone))


class _Lookup:
    def bridge_for_bot(self, bot_id: int) -> BridgeTarget | None:
        if bot_id == BOT:
            return BridgeTarget(name="mom", max_chat_id=MAX_CHAT, bot_id=BOT)
        if bot_id == OTHER_BOT:
            return BridgeTarget(name="dad", max_chat_id=666, bot_id=OTHER_BOT)
        return None

    def bridge_for_max_chat(self, max_chat_id: int) -> BridgeTarget | None:
        if max_chat_id == MAX_CHAT:
            return BridgeTarget(name="mom", max_chat_id=MAX_CHAT, bot_id=BOT)
        if max_chat_id == 666:
            return BridgeTarget(name="dad", max_chat_id=666, bot_id=OTHER_BOT)
        return None


class Live:
    """The three moving parts wired the way the runtime wires them."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.messages = MessageMapRepository(database)
        self.albums = MediaGroupRepository(database)
        self.album_settlement = AlbumSettlementRepository(database)
        self.outbox = OutboxRepository(database)
        self.state = BridgeStateRepository(database)
        self.max = MaxSpy()
        #: Telegram's next id for a bot-side message, and what it returned.
        self.next_bot_id = 7000
        self.telegram_sends: list[int] = []
        #: What the last delivered album put on its head part.
        self.head_caption: str | None = None
        #: Fired inside the send, before its answer is settled — how an echo that
        #: beats the Bot API response is reproduced without a clock.
        self.during_send: Any = None
        self.router = BridgeRouter(
            lookup=_Lookup(),
            telegram=MaxSpy(),
            max_sender=self.max,
            messages=self.messages,
            state=self.state,
            owner_chat_id=OWNER_CHAT,
            pipe=DeliveryPipe(outbox=self.outbox, send=self._send),
            media=MaxSpy(),
            albums=self.albums,
        )

        async def allow() -> set[int]:
            return {BOT, OTHER_BOT}

        self.intake = MtprotoIntake(
            router=self.router,
            allowed_bots=allow,
            album_parts=self.albums,
            album_window_seconds=60.0,
        )

    async def _send(
        self, kind: str, direction: Direction, payload: dict[str, Any], sending: Any = None
    ) -> int | None:
        if kind == KIND_OWNER_ECHO_BIND:
            return await resolve_album_echo(
                messages=self.messages,
                albums=self.albums,
                state=self.state,
                payload=payload,
            )
        if kind == KIND_TG_TO_MAX_EDIT:
            return await resolve_edit(
                outbox=self.outbox,
                messages=self.messages,
                max_sender=self.max,
                payload=payload,
            )
        if kind == KIND_TG_TO_MAX_DELETE:
            return await resolve_delete(
                outbox=self.outbox,
                messages=self.messages,
                max_sender=self.max,
                payload=payload,
            )
        if kind != KIND_MAX_TO_TG_MEDIA:
            return 1
        if sending is not None:
            await sending()
        count = len(payload.get("attachments") or [])
        ids = [self.next_bot_id + step for step in range(1, count + 1)]
        self.next_bot_id += count
        self.telegram_sends.extend(ids)
        if self.during_send is not None:
            hook, self.during_send = self.during_send, None
            await hook()
        receipt = DeliveryReceipt(
            head=ids[0],
            album=tuple(
                DeliveredPart(message_id, AttachmentKind.PHOTO, index)
                for index, message_id in enumerate(ids)
            ),
            media_group_id=str(GROUPED),
        )
        return await settle_telegram_delivery_mapping(
            self.messages,
            payload,
            telegram_message_id=receipt.head or 0,
            receipt=receipt,
            albums=self.album_settlement,
        )

    async def deliver_album(
        self, *, max_message_id: int = 42, parts: int = 3, text: str = "", chat: int = MAX_CHAT
    ) -> int:
        """One MAX album into Telegram, exactly as `on_max_message` does it."""
        base = normalize_message(
            {
                "id": max_message_id,
                "chatId": chat,
                "sender": 99,
                "text": text,
                "time": SENT_AT,
            },
            own_user_id=1,
        )
        self.head_caption = caption_as_sent(text)
        from dataclasses import replace

        await self.router.on_max_message(
            replace(
                base,
                attachments=tuple(
                    MaxAttachment(kind=AttachmentKind.PHOTO, raw={}) for _ in range(parts)
                ),
            )
        )
        target = _Lookup().bridge_for_max_chat(chat)
        assert target is not None
        link = await self.messages.by_max_message(chat, max_message_id, target.bot_id)
        assert link is not None
        return link.id

    async def echo(
        self,
        *owner_message_ids: int,
        bot_id: int = BOT,
        account_id: int = ACCOUNT,
        grouped_id: int = GROUPED,
        caption_on: int | None = -1,
        caption: str | None = None,
        kind: str = "photo",
        flush: bool = True,
    ) -> None:
        """The owner's client seeing those messages arrive, one part at a time.

        By default the caption comes back where the bridge put it: on the head,
        which is the lowest owner-side id of the group and not necessarily the
        first one to arrive.
        """
        if caption_on == -1:
            caption_on = min(owner_message_ids)
        if caption is None:
            caption = self.head_caption
        for owner_message_id in owner_message_ids:
            await self.intake.on_contact_echo(
                bot_id=bot_id,
                account_id=account_id,
                message_id=owner_message_id,
                fingerprint=None,
                album={
                    "grouped_id": grouped_id,
                    "kind": kind,
                    "caption": caption if owner_message_id == caption_on else None,
                },
            )
        if flush:
            await self.intake.flush_echo_albums()

    async def owner_ids(self, link_id: int) -> list[int | None]:
        return [part.telegram_owner_message_id for part in await self.albums.parts_of_link(link_id)]

    async def bot_ids(self, link_id: int) -> list[int | None]:
        return [part.telegram_message_id for part in await self.albums.parts_of_link(link_id)]


@pytest_asyncio.fixture
async def live(tmp_path: Path) -> Any:
    database = await Database.connect(tmp_path / "bridge.db")
    try:
        yield Live(database)
    finally:
        await database.close()


# --------------------------------------------------------------- the two orders


async def test_the_echo_after_the_bot_api_answer_binds_every_part(live: Any) -> None:
    link = await live.deliver_album(parts=3, text="три фото")
    assert await live.bot_ids(link) == [7001, 7002, 7003]

    await live.echo(900, 901, 902)

    assert await live.owner_ids(link) == [900, 901, 902]


async def test_the_echo_before_the_bot_api_answer_binds_every_part(live: Any) -> None:
    """The aliases exist before the first Telegram call, so the race has no bad side.

    This is the ordering that used to be unrepresentable: the owner's session can
    see the album arrive before the HTTP response carrying its bot-side ids has
    come back, and with nothing written down beforehand there was nothing for the
    echo to bind to.
    """
    live.during_send = lambda: live.echo(900, 901)
    link = await live.deliver_album(parts=2, text="пара")

    assert await live.owner_ids(link) == [900, 901]
    assert await live.bot_ids(link) == [7001, 7002]


async def test_an_echo_interleaved_with_the_answer_binds_once(live: Any) -> None:
    """Half the group before the answer, half after. One binding either way."""

    async def half() -> None:
        await live.echo(900, flush=False)

    live.during_send = half
    link = await live.deliver_album(parts=2)
    # The caption rode on the head, which arrived in the first half.
    await live.echo(901, caption_on=None)

    assert await live.owner_ids(link) == [900, 901]


# ------------------------------------------------------------ what cannot be told apart


async def test_identical_photos_are_separated_by_order_alone(live: Any) -> None:
    """Three parts with nothing to tell them apart but where they sit.

    The probe confirmed byte-identical images are indistinguishable, so a scheme
    that needed to tell them apart by content would have nothing to work with.
    Position is what binds them, and position is what this checks.
    """
    link = await live.deliver_album(parts=3)
    await live.echo(902, 900, 901)  # arriving out of order, as a catch-up may

    assert await live.owner_ids(link) == [900, 901, 902]


async def test_two_albums_back_to_back_do_not_cross(live: Any) -> None:
    """Same bot, same shape, two sends. Head-of-line keeps them apart."""
    first = await live.deliver_album(max_message_id=42, parts=2)
    second = await live.deliver_album(max_message_id=43, parts=2)

    await live.echo(900, 901, grouped_id=GROUPED)
    await live.echo(902, 903, grouped_id=GROUPED + 1)

    assert await live.owner_ids(first) == [900, 901]
    assert await live.owner_ids(second) == [902, 903]


async def test_the_same_grouped_id_in_two_chats_is_two_albums(live: Any) -> None:
    """`grouped_id` is unique within a chat and nothing more."""
    mine = await live.deliver_album(max_message_id=42, parts=2, chat=MAX_CHAT)
    theirs = await live.deliver_album(max_message_id=42, parts=2, chat=666)

    await live.echo(900, 901, bot_id=BOT, grouped_id=GROUPED)
    await live.echo(910, 911, bot_id=OTHER_BOT, grouped_id=GROUPED)

    assert await live.owner_ids(mine) == [900, 901]
    assert await live.owner_ids(theirs) == [910, 911]
    assert (
        echo_album_namespace(ACCOUNT, BOT, GROUPED)
        != echo_album_namespace(ACCOUNT, OTHER_BOT, GROUPED)
    )


async def test_the_caption_position_is_part_of_the_match(live: Any) -> None:
    """Where the caption sits is structure, not decoration.

    Outgoing, the caption always rides on the head — Telegram shows an album's
    caption there — so that is where the sender wrote it down. An echo carrying
    it on the second part describes a different album, and is not this one.
    """
    link = await live.deliver_album(parts=2, text="подпись")
    await live.echo(900, 901, caption_on=901)

    assert await live.owner_ids(link) == [None, None], "the wrong shape binds nothing"

    await live.echo(900, 901)  # the caption where it was actually sent
    assert await live.owner_ids(link) == [900, 901]


# ------------------------------------------------------------------ replays


async def test_a_replayed_echo_part_binds_nothing_twice(live: Any) -> None:
    link = await live.deliver_album(parts=2)
    await live.echo(900, 901)
    # The catch-up sends the whole group again.
    await live.echo(900, 901)

    assert await live.owner_ids(link) == [900, 901]
    jobs = await live.database.query(
        "SELECT source_key FROM outbox WHERE kind = ?", (KIND_OWNER_ECHO_BIND,)
    )
    assert len(jobs) == 1, "one album echo, one binding job"


async def test_binding_the_same_ids_again_is_a_success(live: Any) -> None:
    link = await live.deliver_album(parts=2)
    await live.echo(900, 901)
    parts = await live.albums.parts_of_link(link)

    for part, owner_id in zip(parts, (900, 901), strict=True):
        assert await live.albums.attach_owner_message(
            part.id, account_id=ACCOUNT, owner_message_id=owner_id
        )
    assert await live.owner_ids(link) == [900, 901]


async def test_a_conflicting_owner_id_is_refused(live: Any) -> None:
    """Two aliases cannot hold one owner-side message, and the index says so."""
    link = await live.deliver_album(parts=2)
    await live.echo(900, 901)
    parts = await live.albums.parts_of_link(link)

    assert (
        await live.albums.attach_owner_message(
            parts[1].id, account_id=ACCOUNT, owner_message_id=900
        )
        is False
    )
    assert await live.owner_ids(link) == [900, 901]


async def test_a_partial_echo_group_survives_a_restart(tmp_path: Path) -> None:
    """The parts are buffered as they arrive, so the gap between them is safe."""
    path = tmp_path / "bridge.db"
    first_db = await Database.connect(path)
    first = Live(first_db)
    link = await first.deliver_album(parts=3)
    await first.echo(900, 901, flush=False)
    assert await first.owner_ids(link) == [None, None, None]
    await first_db.close()

    second_db = await Database.connect(path)
    second = Live(second_db)
    try:
        await second.echo(902, flush=False)
        assert await second.intake.restore_echo_albums() == 1
        await second.intake.flush_echo_albums()
        assert await second.owner_ids(link) == [900, 901, 902]
    finally:
        await second_db.close()


# --------------------------------------------------------------- blocked head


def _payload(
    *owner_ids: int, first_seen_ms: int, namespace: str, head_caption: str | None = None
) -> dict[str, Any]:
    from bridge.routing.echo import album_part_fingerprint

    return {
        "bot_id": BOT,
        "bridge_name": "mom",
        "account_id": ACCOUNT,
        "namespace": namespace,
        "first_seen_ms": first_seen_ms,
        "parts": [
            {
                "owner_message_id": owner_id,
                "part_index": index,
                "kind": "photo",
                "fingerprint": album_part_fingerprint(
                    "photo",
                    part_index=index,
                    caption=head_caption if index == 0 else None,
                ),
            }
            for index, owner_id in enumerate(owner_ids)
        ],
    }


async def test_a_head_that_has_not_echoed_yet_is_waited_for(live: Any) -> None:
    """Never stepped around: skipping ahead is how one album wears another's ids."""
    from bridge.storage.database import now_ms

    first = await live.deliver_album(max_message_id=42, parts=2)
    second = await live.deliver_album(max_message_id=43, parts=3)

    with pytest.raises(DeferDelivery):
        await resolve_album_echo(
            messages=live.messages,
            albums=live.albums,
            state=live.state,
            payload=_payload(
                910,
                911,
                912,
                first_seen_ms=now_ms(),
                namespace=echo_album_namespace(ACCOUNT, BOT, GROUPED + 1),
                head_caption=live.head_caption,
            ),
        )

    assert await live.owner_ids(first) == [None, None]
    assert await live.owner_ids(second) == [None, None, None]


async def test_a_head_that_never_echoes_is_given_up_on_loudly(live: Any) -> None:
    """One incident, the head marked unbindable, and the queue moves again.

    Without this a single lost echo would block every later binding for that
    bridge for the rest of the day — so the give-up is real, and it is never
    silent.
    """
    first = await live.deliver_album(max_message_id=42, parts=2)
    second = await live.deliver_album(max_message_id=43, parts=3)

    await resolve_album_echo(
        messages=live.messages,
        albums=live.albums,
        state=live.state,
        payload=_payload(
            910,
            911,
            912,
            first_seen_ms=1,  # long past the give-up window
            namespace=echo_album_namespace(ACCOUNT, BOT, GROUPED + 1),
            head_caption=live.head_caption,
        ),
    )

    assert await live.owner_ids(second) == [910, 911, 912], "the queue moved on"
    assert await live.owner_ids(first) == [None, None], "and the head stayed as delivered"
    snapshot = await live.state.snapshot("mom")
    assert snapshot is not None and "album echo binding" in (snapshot["last_error"] or "")

    # The abandoned head is no longer a candidate, so it blocks nothing further.
    blocked = await live.albums.parts_of_link(first)
    assert all(part.part_fingerprint is None for part in blocked)
    assert all(part.link_id == first for part in blocked), "still mapped, still delivered"


async def test_an_echo_matching_nothing_at_all_asks_for_the_owner(live: Any) -> None:
    from bridge.routing.echo import album_part_fingerprint

    await live.deliver_album(max_message_id=42, parts=2)
    payload = _payload(
        910,
        911,
        first_seen_ms=1,
        namespace=echo_album_namespace(ACCOUNT, BOT, GROUPED + 1),
        head_caption=live.head_caption,
    )
    # A structure no album of this bridge has: a different kind at position 1.
    payload["parts"][1]["fingerprint"] = album_part_fingerprint(
        "video", part_index=1, caption=None
    )

    with pytest.raises(UnconfirmedDeliveryError):
        await resolve_album_echo(
            messages=live.messages,
            albums=live.albums,
            state=live.state,
            payload=payload,
        )


# ------------------------------------------------------- replies, edits, deletes


@pytest.mark.parametrize("part", [0, 1, 2])
async def test_a_reply_to_any_part_reaches_the_one_max_message(
    tmp_path: Path, part: int
) -> None:
    database = await Database.connect(tmp_path / f"bridge-{part}.db")
    live = Live(database)
    try:
        await live.deliver_album(parts=3)
        await live.echo(900, 901, 902)

        # The owner answers the part they were looking at.
        await live.router.on_telegram_text(
            bot_id=BOT,
            telegram_chat_id=BOT,
            telegram_message_id=1000,
            text="ответ",
            reply_to_telegram_message_id=(900, 901, 902)[part],
            owner_account_id=ACCOUNT,
        )
        job = await live.outbox.by_source_key(f"tg-owner-msg:{ACCOUNT}:1000")
        assert job is not None
        assert json.loads(job.payload_json or "{}").get("reply_to", 9001) == 9001
    finally:
        await database.close()


async def test_a_bot_side_reply_to_a_middle_part_also_resolves(live: Any) -> None:
    """The Bot API quotes the bot's own id, and only the head is in the mapping."""
    await live.deliver_album(parts=3)

    await live.router.on_telegram_text(
        bot_id=BOT,
        telegram_chat_id=OWNER_CHAT,
        telegram_message_id=1000,
        text="ответ",
        reply_to_telegram_message_id=7003,
    )
    job = await live.outbox.by_source_key(f"tg:{BOT}:1000")
    assert job is not None
    assert json.loads(job.payload_json or "{}").get("reply_to", 9001) == 9001


async def test_deleting_one_part_deletes_the_one_max_message(live: Any) -> None:
    await live.deliver_album(parts=3)
    await live.echo(900, 901, 902)

    await live.router.on_owner_delete(owner_account_id=ACCOUNT, owner_message_ids=[901])

    assert live.max.deletes == [(MAX_CHAT, [42], True)]


async def test_deleting_the_whole_album_is_still_one_delete(live: Any) -> None:
    await live.deliver_album(parts=3)
    await live.echo(900, 901, 902)

    await live.router.on_owner_delete(
        owner_account_id=ACCOUNT, owner_message_ids=[900, 901, 902]
    )
    # And the catch-up replaying the same batch, in another order.
    await live.router.on_owner_delete(
        owner_account_id=ACCOUNT, owner_message_ids=[902, 900]
    )

    assert live.max.deletes == [(MAX_CHAT, [42], True)]
    jobs = await live.database.query(
        "SELECT source_key FROM outbox WHERE kind = ?", (KIND_TG_TO_MAX_DELETE,)
    )
    assert len(jobs) == 1


async def test_a_delete_before_the_head_is_bound_still_collapses(live: Any) -> None:
    """The echo can arrive after a part of the album is already gone.

    The mapping is keyed by the head's owner-side id, which does not exist yet in
    that case — so the collapse is keyed by the group instead, and the delete is
    one delete whether or not the binding has happened.
    """
    link_id = await live.deliver_album(parts=3)
    await live.echo(900, 901, 902)
    # Undo the head's binding, as if its echo were still in flight.
    async with live.database.transaction() as connection:
        await connection.execute(
            "UPDATE message_map SET telegram_owner_message_id = NULL WHERE id = ?",
            (link_id,),
        )

    await live.router.on_owner_delete(
        owner_account_id=ACCOUNT, owner_message_ids=[901, 902]
    )

    assert live.max.deletes == [(MAX_CHAT, [42], True)]


async def test_a_caption_edit_on_any_part_edits_the_one_max_message(live: Any) -> None:
    await live.deliver_album(parts=3)
    await live.echo(900, 901, 902)

    await live.router.on_owner_edit(
        owner_account_id=ACCOUNT, owner_message_id=902, text="новая подпись", edit_pts=11
    )
    # The same update replayed keeps its pts, so it is the same event.
    await live.router.on_owner_edit(
        owner_account_id=ACCOUNT, owner_message_id=902, text="новая подпись", edit_pts=11
    )

    assert live.max.edits == [(MAX_CHAT, 42, "новая подпись")]
    jobs = await live.database.query(
        "SELECT source_key FROM outbox WHERE kind = ?", (KIND_TG_TO_MAX_EDIT,)
    )
    assert len(jobs) == 1


async def test_an_edit_after_a_delete_does_not_resurrect_the_album(live: Any) -> None:
    await live.deliver_album(parts=2)
    await live.echo(900, 901)

    await live.router.on_owner_delete(owner_account_id=ACCOUNT, owner_message_ids=[901])
    await live.router.on_owner_edit(
        owner_account_id=ACCOUNT, owner_message_id=900, text="поздно", edit_pts=12
    )

    assert live.max.edits == [], "a deleted album is terminally gone"


# ------------------------------------------------------------------ invariants


def test_no_timestamp_decides_anything_in_the_binding() -> None:
    """The probe found no ordering signal in time, and none is read for one.

    `created_at` bounds the quiet window and orders whole groups; what binds a
    part is `part_index` and the owner-side message id, and this reads the source
    to keep it that way.
    """
    import ast

    source = Path(__file__).resolve().parent.parent / "bridge" / "routing" / "owner_echo.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    binders = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AST)
        and getattr(node, "name", None) in {"resolve_album_echo", "_matches", "_attach"}
    ]
    read: set[Any] = set()
    for binder in binders:
        read |= {node.attr for node in ast.walk(binder) if isinstance(node, ast.Attribute)}
        read |= {
            node.value
            for node in ast.walk(binder)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
    assert not {"created_at", "updated_at", "timestamp", "date"} & read
    assert "part_index" in read and "owner_message_id" in read
