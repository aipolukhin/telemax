"""The owner's own words, placed by the owner's own account.

Secretary Mode did this first and asked for a lot to do it: Premium, Business
switched on, a connection granted to the guardian, `can_reply` still set, and
none of it revoked since. The session the bridge already runs asks for one thing
— to be connected — and the message it sends is not "as if" the owner's, it is
the owner's.

The property the whole thing rests on is measured rather than assumed: a session
receives **no update for a message it sent itself**. So a placement made through
the intake session is invisible to the intake handlers on that same client and
cannot travel back into MAX. Sent from any other session of the account it would
be seen, carried, and the owner would read their own line twice — which is why
the wiring test below checks *which* session the sender is given.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from bridge.max_client import AttachmentKind
from bridge.media.delivery import OutgoingMedia
from bridge.routing.owner_voice import (
    MtprotoOwnerSender,
    OwnerTransportUnavailableError,
    OwnerVoice,
    PartialOwnerAlbumError,
)
from bridge.telegram.user_session import TelegramUserSession, mtproto_entities

BOT = 9000000001


class FakeSession:
    """A connected owner session, recording what it was asked to place."""

    def __init__(self, *, connected: bool = True, fails: bool = False) -> None:
        self.is_connected = connected
        self.fails = fails
        #: The transport is away — raised before anything reaches Telegram, so
        #: the job is owed rather than unknown.
        self.session_gone = False
        #: Telegram took the message and said nothing identifiable back. The
        #: message *is* in the chat; only the id is missing.
        self.answers_nothing = False
        self.messages: list[dict[str, Any]] = []
        self.files: list[dict[str, Any]] = []
        #: One grouped upload, recorded whole — the album path makes a single
        #: `sendMultiMedia` rather than a call per file.
        self.albums: list[dict[str, Any]] = []
        self.next_id = 5000

    def _check(self) -> None:
        if self.session_gone:
            raise OwnerTransportUnavailableError("the owner's Telegram session is not connected")
        if self.fails:
            raise RuntimeError("session went away mid-send")

    async def send_own_message(
        self, peer_id: int, text: str, *, entities: list[dict[str, Any]] | None = None
    ) -> int | None:
        self._check()
        self.messages.append({"peer": peer_id, "text": text, "entities": entities})
        if self.answers_nothing:
            return None
        self.next_id += 1
        return self.next_id

    async def send_own_file(self, peer_id: int, path: str, **kwargs: Any) -> int | None:
        self._check()
        self.files.append({"peer": peer_id, "path": path, **kwargs})
        if self.answers_nothing:
            return None
        self.next_id += 1
        return self.next_id

    async def send_own_album(
        self,
        peer_id: int,
        paths: list[str],
        *,
        caption: str | None = None,
        entities: list[dict[str, Any]] | None = None,
    ) -> list[int | None]:
        self._check()
        self.albums.append(
            {"peer": peer_id, "paths": list(paths), "caption": caption, "entities": entities}
        )
        if self.answers_nothing:
            return [None for _ in paths]
        placed: list[int | None] = []
        for _ in paths:
            self.next_id += 1
            placed.append(self.next_id)
        return placed


def _media(kind: AttachmentKind, **over: Any) -> OutgoingMedia:
    base: dict[str, Any] = {
        "kind": kind,
        "path": Path("/tmp/clip.bin"),  # noqa: S108 - never opened here
        "file_name": "clip.bin",
    }
    base.update(over)
    return OutgoingMedia(**base)


# ------------------------------------------------------------------ availability


async def test_a_connected_session_is_all_it_takes() -> None:
    """No Premium, no business connection, no `can_reply` — one condition."""
    session = FakeSession()
    voice = OwnerVoice(session=lambda: session)
    assert voice.available is True


async def test_a_disconnected_session_falls_back_to_the_bot_line() -> None:
    voice = OwnerVoice(session=lambda: FakeSession(connected=False))
    assert voice.available is False
    assert OwnerVoice(session=lambda: None).available is False


async def test_the_session_is_resolved_on_every_call() -> None:
    """The router is built before the session opens, and a reconnect replaces the
    client — so a cached reference would be wrong in both directions."""
    box: dict[str, Any] = {"session": None}
    voice = OwnerVoice(session=lambda: box["session"])

    assert voice.available is False
    box["session"] = FakeSession()
    assert voice.available is True


# ----------------------------------------------------------------- placing text


async def test_a_line_is_placed_on_the_contact_bot_as_the_owner() -> None:
    """From the owner's account the bot *is* the other side of the chat, so it is
    both the peer and the chat — the addressing the business path used."""
    session = FakeSession()
    sent = await OwnerVoice(session=lambda: session).send_as_owner(
        BOT, "[03/08 19:20] привет", entities=[{"type": "bold", "offset": 0, "length": 5}]
    )

    assert sent == 5001, "the owner's own id for it, which is what the mapping stores"
    assert session.messages == [
        {
            "peer": BOT,
            "text": "[03/08 19:20] привет",
            "entities": [{"type": "bold", "offset": 0, "length": 5}],
        }
    ]


async def test_a_failed_placement_travels_rather_than_being_reported_as_none() -> None:
    """The queue is what decides what a failure means, and it needs the exception.

    Answering None made every outcome look like "nothing was sent", including a
    timeout that arrived *after* Telegram had accepted the message — and the
    caller's fallback then put the same line in the chat a second time, signed
    `Вы: …`.
    """
    voice = OwnerVoice(session=lambda: FakeSession(fails=True))
    with pytest.raises(RuntimeError, match="went away"):
        await voice.send_as_owner(BOT, "привет")


async def test_a_session_that_is_away_is_a_named_retryable_condition() -> None:
    """Not a failure of the message: the transport is absent and will come back."""
    voice = OwnerVoice(session=lambda: None)
    with pytest.raises(OwnerTransportUnavailableError):
        await voice.send_as_owner(BOT, "привет")


# ---------------------------------------------------------------- placing media


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (AttachmentKind.PHOTO, "photo"),
        (AttachmentKind.VIDEO, "video"),
        (AttachmentKind.VIDEO_NOTE, "video_note"),
        (AttachmentKind.VOICE, "voice"),
        (AttachmentKind.MUSIC, "audio"),
        (AttachmentKind.FILE, "document"),
    ],
)
async def test_each_kind_keeps_its_shape(kind: AttachmentKind, expected: str) -> None:
    """A voice sent as a file loses its waveform and a circle stops being round —
    the same mapping the Bot API side spells out one method at a time."""
    session = FakeSession()
    sender = MtprotoOwnerSender(session=lambda: session)
    method = {
        "photo": sender.send_photo,
        "video": sender.send_video,
        "video_note": sender.send_video_note,
        "voice": sender.send_voice,
        "audio": sender.send_audio,
        "document": sender.send_document,
    }[expected]

    await method(BOT, BOT, _media(kind), reply_to=None)

    assert session.files[0]["kind"] == expected
    assert session.files[0]["peer"] == BOT


async def test_an_album_of_photos_is_one_grouped_upload() -> None:
    """Telethon makes one `sendMultiMedia` for a list, so the group is a group.

    The caption travels once, with the request, and Telegram puts it on the first
    item — the same rule the Bot API path follows and the one the aliases were
    written down expecting.
    """
    session = FakeSession()
    sender = MtprotoOwnerSender(session=lambda: session)
    items = [
        _media(AttachmentKind.PHOTO, caption="подпись"),
        _media(AttachmentKind.PHOTO, caption="подпись"),
    ]

    receipt = await sender.send_album(BOT, BOT, items, reply_to=None)

    assert receipt is not None and len(receipt.message_ids) == 2
    assert session.files == [], "a grouped album makes no per-file call"
    assert len(session.albums) == 1
    assert session.albums[0]["caption"] == "подпись"
    assert len(session.albums[0]["paths"]) == 2


async def test_an_album_with_a_video_keeps_its_dimensions_instead() -> None:
    """The measured limit of the grouped path, and why the split exists.

    Telethon's album call takes no per-file `attributes` and no `thumb`, and with
    `hachoir` absent it infers `DocumentAttributeVideo(0, 1, 1)` — a clip that
    arrives as one pixel. So a group holding a video goes file by file, with its
    duration, its dimensions and its upright cover intact.
    """
    session = FakeSession()
    sender = MtprotoOwnerSender(session=lambda: session)
    items = [
        _media(AttachmentKind.PHOTO, caption="подпись"),
        _media(AttachmentKind.VIDEO, width=1280, height=720, duration_seconds=12),
    ]

    receipt = await sender.send_album(BOT, BOT, items, reply_to=None)

    assert receipt is not None and len(receipt.message_ids) == 2
    assert session.albums == []
    assert session.files[0]["caption"] == "подпись"
    assert session.files[1]["caption"] is None, "only the head carries it"
    assert session.files[1]["width"] == 1280
    assert session.files[1]["duration"] == 12


async def test_a_media_notice_is_spoken_in_the_owners_voice_too() -> None:
    """«не удалось скачать» beside the owner's own photo must not arrive from the
    bot: it would sit on the other side of the screen from what it describes."""
    session = FakeSession()
    sender = MtprotoOwnerSender(session=lambda: session)
    await sender.send_text(BOT, BOT, "[видео: не удалось скачать]")
    assert session.messages[0]["text"] == "[видео: не удалось скачать]"


async def test_a_missing_session_is_raised_not_reported_as_a_placement() -> None:
    sender = MtprotoOwnerSender(session=lambda: None)
    with pytest.raises(OwnerTransportUnavailableError):
        await sender.send_photo(BOT, BOT, _media(AttachmentKind.PHOTO), reply_to=None)
    with pytest.raises(OwnerTransportUnavailableError):
        await sender.send_text(BOT, BOT, "нечего")


async def test_a_half_placed_album_is_a_question_not_a_delivery() -> None:
    """Telethon places these one at a time, so a short group is a real outcome.

    A receipt naming two ids for a three-part album would be settled by position
    and would map somebody's third photo onto their second. The count is the
    contract.
    """
    class Flaky(FakeSession):
        async def send_own_album(
            self,
            peer_id: int,
            paths: list[str],
            *,
            caption: str | None = None,
            entities: list[dict[str, Any]] | None = None,
        ) -> list[int | None]:
            placed = await super().send_own_album(
                peer_id, paths, caption=caption, entities=entities
            )
            # One `sendMultiMedia` made the group; the answer named one part of
            # it and not the rest.
            return [placed[0], None, None]

    flaky = Flaky()
    sender = MtprotoOwnerSender(session=lambda: flaky)
    items = [_media(AttachmentKind.PHOTO) for _ in range(3)]

    with pytest.raises(PartialOwnerAlbumError):
        await sender.send_album(BOT, BOT, items, reply_to=None)


# ------------------------------------------------------------------- entities


def test_formatting_survives_the_change_of_transport() -> None:
    """Both sides count offsets in UTF-16 units, so only the wrapper differs."""
    built = mtproto_entities(
        [
            {"type": "bold", "offset": 0, "length": 4},
            {"type": "text_link", "offset": 5, "length": 3, "url": "https://example.org"},
            {"type": "code", "offset": 9, "length": 2},
        ]
    )
    assert built is not None and len(built) == 3
    assert [type(item).__name__ for item in built] == [
        "MessageEntityBold",
        "MessageEntityTextUrl",
        "MessageEntityCode",
    ]
    assert built[1].url == "https://example.org"
    assert (built[0].offset, built[0].length) == (0, 4)


def test_an_entity_telegram_would_refuse_is_dropped_not_approximated() -> None:
    """One entity Telegram rejects takes the whole message with it, and losing a
    bold range is not worth losing the line."""
    assert mtproto_entities([{"type": "text_link", "offset": 0, "length": 3}]) is None
    assert mtproto_entities([{"type": "custom_emoji", "offset": 0, "length": 1}]) is None
    assert mtproto_entities([{"type": "bold", "offset": 0, "length": 0}]) is None
    assert mtproto_entities(None) is None
    assert mtproto_entities([]) is None


# ------------------------------------------------------- the session's own send


async def test_the_session_refuses_to_place_while_disconnected() -> None:
    async def connect() -> Any:
        raise AssertionError("unused")

    session = TelegramUserSession(
        connect=connect,
        owner_user_id=1,
        health=SimpleNamespace(),
        allowed_bot_ids=lambda: None,  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="not connected"):
        await session.send_own_message(BOT, "привет")


async def test_the_session_places_through_its_own_client() -> None:
    calls: list[tuple[int, str, Any]] = []

    class FakeClient:
        async def send_message(
            self,
            peer: int,
            text: str,
            *,
            formatting_entities: Any = None,
            parse_mode: Any = "md",
        ) -> Any:
            calls.append((peer, text, formatting_entities, parse_mode))
            return SimpleNamespace(id=777)

    async def connect() -> Any:
        raise AssertionError("unused")

    session = TelegramUserSession(
        connect=connect,
        owner_user_id=1,
        health=SimpleNamespace(),
        allowed_bot_ids=lambda: None,  # type: ignore[arg-type]
    )
    session._client = FakeClient()  # the client under test, reached directly

    assert await session.send_own_message(BOT, "*привет*") == 777
    # `parse_mode=None` and an explicit (empty) entity list: the body Telegram
    # stores is the body that went in, asterisks and all. With a parse mode
    # Telethon would read those asterisks as bold, store `привет`, and the
    # baseline the placement writes for this message would describe something
    # that is not there.
    assert calls == [(BOT, "*привет*", [], None)]


# ------------------------------------------------------------ the loop invariant


def test_the_placement_uses_the_intake_session_and_no_other() -> None:
    """The whole design rests on it, and nothing in the types can hold it.

    A session gets no update for its own sends, so placing through the *intake*
    session is invisible to the intake. Placed through a second client of the
    same account it would be seen, carried into MAX, and shown to the owner
    twice. So the sender must be handed `_owner_session` — the very object the
    update handlers are registered on.
    """
    import ast

    source = Path(__file__).resolve().parent.parent / "bridge" / "service" / "runtime.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    built = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None)
        if name not in ("OwnerVoice", "MtprotoOwnerSender"):
            continue
        built += 1
        attributes = {
            inner.attr for inner in ast.walk(node) if isinstance(inner, ast.Attribute)
        }
        assert "_owner_session" in attributes, f"{name} was given some other session"
    assert built == 2, "both the voice and the media sender are wired here"


# ------------------------------------------ the account behind a placed message


OWNER = 100000001
MAX_CHAT = 555


class _Lookup:
    def bridge_for_bot(self, bot_id: int) -> Any:
        from bridge.routing.router import BridgeTarget

        return BridgeTarget("mom", MAX_CHAT, BOT)

    def bridge_for_max_chat(self, max_chat_id: int) -> Any:
        from bridge.routing.router import BridgeTarget

        return BridgeTarget("mom", MAX_CHAT, BOT)

    def target_for_bot(self, bot_id: int) -> Any:
        return self.bridge_for_bot(bot_id)


class _BotApi:
    """The contact bot. Every call here after the owner branch is a duplicate."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, bot_id: int, chat_id: int, text: str, **kw: Any) -> int | None:
        self.sent.append(text)
        return 900 + len(self.sent)

    async def send_media(self, *a: Any, **k: Any) -> int | None:
        return 1


class _MaxSender:
    async def delete_messages(self, *a: Any, **k: Any) -> None: ...
    async def edit_text(self, *a: Any, **k: Any) -> None: ...


@dataclass
class _Stand:
    """A router wired to a real queue, and the real owner sender behind it."""

    database: Any
    messages: Any
    outbox: Any
    router: Any
    bot: _BotApi
    session: FakeSession
    echoes: Any

    async def jobs(self) -> list[Any]:
        return list(await self.database.query("SELECT * FROM outbox ORDER BY id"))


async def _stand(tmp_path: Path, *, session: FakeSession | None = None) -> _Stand:
    """Production's own wiring, minus the parts that talk to a network.

    The sender under test is `place_owner_message` itself — the function the
    queue calls — so what is exercised is the delivery, not a copy of it.
    """
    from bridge.routing.delivery import DeliveryPipe
    from bridge.routing.echo import OwnEchoes
    from bridge.routing.router import BridgeRouter
    from bridge.service.runtime import place_owner_message
    from bridge.storage import (
        BridgeStateRepository,
        Database,
        MediaGroupRepository,
        MessageMapRepository,
        OutboxRepository,
    )

    live = session or FakeSession()
    database = await Database.connect(tmp_path / "bridge.db")
    messages = MessageMapRepository(database)
    albums = MediaGroupRepository(database)
    outbox = OutboxRepository(database)
    bot = _BotApi()
    echoes = OwnEchoes()
    voice = OwnerVoice(session=lambda: live)

    async def send(kind: str, direction: Any, payload: dict[str, Any], sending: Any) -> int | None:
        assert kind == "max_to_tg_owner", f"nothing else should reach this stand: {kind}"
        return await place_owner_message(
            payload,
            sending=sending,
            voice=voice,
            media=cast("Any", None),
            messages=messages,
            albums=albums,
            echoes=echoes,
        )

    pipe = DeliveryPipe(outbox=outbox, send=send)
    router = BridgeRouter(
        lookup=_Lookup(),
        telegram=bot,
        max_sender=_MaxSender(),
        messages=messages,
        state=BridgeStateRepository(database),
        owner_chat_id=OWNER,
        own_voice=voice,
        own_echoes=echoes,
        albums=albums,
        pipe=pipe,
    )
    return _Stand(database, messages, outbox, router, bot, live, echoes)


def _own_message(message_id: int = 42, text: str = "своё") -> Any:
    from bridge.max_client import normalize_message

    base = normalize_message(
        {
            "id": message_id,
            "chatId": MAX_CHAT,
            "sender": 1,
            "text": text,
            "time": 1_785_000_000_000,
        },
        own_user_id=1,
    )
    return replace(base, is_outgoing=True)


async def test_a_placed_message_is_a_durable_job_and_records_its_account(
    tmp_path: Path,
) -> None:
    """One job, settled, with both the id and the account it belongs to.

    The account used to be left null, because the business connection never said
    whose it was — and `by_owner_account_message` is keyed by both, so an owner
    deleting or editing their own placed message resolved to nothing.
    """
    stand = await _stand(tmp_path)
    try:
        await stand.router.on_max_message(_own_message())

        jobs = await stand.jobs()
        assert len(jobs) == 1, "a placement is a job, not a send beside the queue"
        assert jobs[0]["kind"] == "max_to_tg_owner"
        assert jobs[0]["state"] == "done"
        assert jobs[0]["source_key"] == f"max:{MAX_CHAT}:42"

        placed = await stand.messages.by_owner_account_message(OWNER, 5001)
        assert placed is not None, "an owner-side delete must be able to find it"
        assert placed.max_message_id == 42
        assert stand.session.messages, "and it really went through the owner's session"
        assert stand.bot.sent == [], "the contact bot must not also carry it"
    finally:
        await stand.database.close()


async def test_a_crash_before_the_send_leaves_the_message_deliverable(
    tmp_path: Path,
) -> None:
    """The claim used to be all that survived, and it suppressed every replay.

    Here the process dies while the transport is away: the job is on the queue,
    PENDING, and the worker finishes it when the session returns. Nothing is lost
    and nothing is placed twice.
    """
    away = FakeSession()
    away.session_gone = True
    stand = await _stand(tmp_path, session=away)
    try:
        await stand.router.on_max_message(_own_message())

        jobs = await stand.jobs()
        assert len(jobs) == 1
        assert jobs[0]["state"] == "pending", "owed, not failed and not delivered"
        assert stand.session.messages == []
        assert stand.bot.sent == [], "and never rerouted through the bot"

        # The session comes back and the queue finishes what it started.
        away.session_gone = False
        item = await stand.outbox.claim_due("mom", limit=1)
        assert item
        settled = await _drain(stand, item[0])
        assert settled.delivered is True
        assert len(stand.session.messages) == 1
    finally:
        await stand.database.close()


async def _drain(stand: _Stand, item: Any) -> Any:
    from bridge.routing.delivery import Direction

    return await stand.router._pipe.attempt(  # the queue's own step, driven by hand
        job_id=item.id,
        bridge_name="mom",
        direction=Direction.MAX_TO_TG,
        kind=item.kind,
        payload=json.loads(item.payload_json),
    )


async def test_an_unconfirmed_send_becomes_ambiguous_and_never_a_bot_line(
    tmp_path: Path,
) -> None:
    """The defect this whole change exists for.

    Telegram accepted the message and the answer was lost. The old path swallowed
    that, answered None, and let the durable Bot API branch place the same line
    again as `Вы: …` — two copies in the owner's chat. Now the job stops and the
    owner decides.
    """
    unsure = FakeSession()
    unsure.answers_nothing = True
    stand = await _stand(tmp_path, session=unsure)
    try:
        await stand.router.on_max_message(_own_message())

        jobs = await stand.jobs()
        assert len(jobs) == 1
        assert jobs[0]["state"] == "ambiguous", "an unknown outcome is not a retry"
        assert stand.bot.sent == [], "and above all, not a second copy"
    finally:
        await stand.database.close()


async def test_a_replay_of_the_same_max_message_makes_no_second_job(
    tmp_path: Path,
) -> None:
    """MAX replays on reconnect; the source_key is what makes that a no-op."""
    stand = await _stand(tmp_path)
    try:
        await stand.router.on_max_message(_own_message())
        await stand.router.on_max_message(_own_message())

        assert len(await stand.jobs()) == 1
        assert len(stand.session.messages) == 1
    finally:
        await stand.database.close()


async def test_settlement_is_idempotent_across_a_repeated_attempt(
    tmp_path: Path,
) -> None:
    """A crash between the placement and `mark_done` is finished, not duplicated.

    The second attempt writes what the first one did, and `attach_owner_message`
    only fills a column while it is empty — so the mapping ends up with one id
    and the job ends up done.
    """
    from bridge.service.runtime import settle_owner_delivery_mapping

    stand = await _stand(tmp_path)
    try:
        link_id = await stand.messages.claim_from_max(
            bridge_name="mom",
            max_chat_id=MAX_CHAT,
            max_message_id=42,
            telegram_bot_id=BOT,
            telegram_chat_id=OWNER,
        )
        payload = {"link_id": link_id, "owner_account_id": OWNER}
        for _ in range(2):
            assert (
                await settle_owner_delivery_mapping(
                    stand.messages, payload, owner_message_id=5001
                )
                == 5001
            )
        link = await stand.messages.by_owner_account_message(OWNER, 5001)
        assert link is not None and link.id == link_id
    finally:
        await stand.database.close()


async def test_an_album_settles_every_part_it_actually_placed(tmp_path: Path) -> None:
    """Each alias gets the owner's own id for the message it became."""
    from bridge.media.delivery import DeliveredPart, DeliveryReceipt
    from bridge.service.runtime import settle_owner_delivery_mapping
    from bridge.storage import AlbumSettlementRepository, Direction, MediaGroupRepository

    stand = await _stand(tmp_path)
    albums = MediaGroupRepository(stand.database)
    settlement = AlbumSettlementRepository(stand.database)
    try:
        link_id = await stand.messages.claim_from_max(
            bridge_name="mom",
            max_chat_id=MAX_CHAT,
            max_message_id=42,
            telegram_bot_id=BOT,
            telegram_chat_id=OWNER,
        )
        assert link_id is not None
        for index in range(3):
            await albums.add_part(
                media_group_id=f"max:{link_id}",
                bridge_name="mom",
                bot_id=BOT,
                payload={"kind": "photo"},
                link_id=link_id,
                direction=Direction.MAX_TO_TG,
                part_index=index,
                media_kind="photo",
                caption_present=index == 0,
                part_fingerprint=f"a1:{index}",
            )
        receipt = DeliveryReceipt(
            head=6001,
            album=tuple(
                DeliveredPart(6001 + index, AttachmentKind.PHOTO, index) for index in range(3)
            ),
        )

        await settle_owner_delivery_mapping(
            stand.messages,
            {"link_id": link_id, "owner_account_id": OWNER},
            owner_message_id=6001,
            receipt=receipt,
            albums=settlement,
        )

        parts = await albums.parts_of_link(link_id)
        assert [part.telegram_owner_message_id for part in parts] == [6001, 6002, 6003]
        assert all(part.telegram_owner_account_id == OWNER for part in parts)
    finally:
        await stand.database.close()


async def test_an_album_that_placed_fewer_parts_binds_nothing(tmp_path: Path) -> None:
    """Binding the first N in order maps somebody's third photo onto their second."""
    from bridge.media.delivery import DeliveredPart, DeliveryReceipt
    from bridge.routing.delivery import UnconfirmedDeliveryError
    from bridge.service.runtime import settle_owner_delivery_mapping
    from bridge.storage import AlbumSettlementRepository, Direction, MediaGroupRepository

    stand = await _stand(tmp_path)
    albums = MediaGroupRepository(stand.database)
    settlement = AlbumSettlementRepository(stand.database)
    try:
        link_id = await stand.messages.claim_from_max(
            bridge_name="mom",
            max_chat_id=MAX_CHAT,
            max_message_id=42,
            telegram_bot_id=BOT,
            telegram_chat_id=OWNER,
        )
        assert link_id is not None
        for index in range(3):
            await albums.add_part(
                media_group_id=f"max:{link_id}",
                bridge_name="mom",
                bot_id=BOT,
                payload={"kind": "photo"},
                link_id=link_id,
                direction=Direction.MAX_TO_TG,
                part_index=index,
                media_kind="photo",
                caption_present=index == 0,
                part_fingerprint=f"a1:{index}",
            )

        with pytest.raises(UnconfirmedDeliveryError, match="nothing was bound"):
            await settle_owner_delivery_mapping(
                stand.messages,
                {"link_id": link_id, "owner_account_id": OWNER},
                owner_message_id=6001,
                receipt=DeliveryReceipt(
                    head=6001,
                    album=(
                        DeliveredPart(6001, AttachmentKind.PHOTO, 0),
                        DeliveredPart(6002, AttachmentKind.PHOTO, 1),
                    ),
                ),
                albums=settlement,
            )

        parts = await albums.parts_of_link(link_id)
        assert all(part.telegram_owner_message_id is None for part in parts)
        # And the canonical row was left alone too: the head used to be written
        # before the receipt was checked, so a short answer left a message
        # pointing at a delivery whose parts were unbound.
        canonical = await stand.messages.by_id(link_id)
        assert canonical is not None and canonical.telegram_owner_message_id is None
    finally:
        await stand.database.close()


async def test_the_bot_carries_it_when_the_session_is_away_before_any_job(
    tmp_path: Path,
) -> None:
    """The one place a fallback is honest: before the transport was chosen.

    A disconnected session means the owner branch is never entered, so the
    message goes out durably as a `Вы: …` bot line — one copy, one job, and the
    kind says which transport carried it.
    """
    stand = await _stand(tmp_path, session=FakeSession(connected=False))
    try:
        await stand.router.on_max_message(_own_message())

        jobs = await stand.jobs()
        assert [job["kind"] for job in jobs] == ["max_to_tg_text"]
        assert stand.session.messages == []
    finally:
        await stand.database.close()
