"""Inc 1 — owner→MAX intake for new messages, over the existing durable pipeline.

Three levels, all through fakes: the normaliser's routing decisions; the media
classification by MTProto attributes; and the router + repository, where owner
identity is persisted atomically at intake and the source_key dedups a replay.
No real MAX send, no PyMax, and the persisted gate is never touched.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from telethon.tl import types

from bridge.media.store import TempFiles
from bridge.routing.delivery import DeliveryPipe
from bridge.routing.router import BridgeRouter, BridgeTarget
from bridge.storage import (
    BridgeStateRepository,
    Database,
    Direction,
    MessageMapRepository,
    OutboxRepository,
)
from bridge.telegram.mtproto_intake import MtprotoIntake, OwnerMedia, OwnerMessage
from bridge.telegram.mtproto_media import (
    MediaUnavailableError,
    MtprotoMediaSource,
    _classify,
    owner_message_from,
)

ACCOUNT = 100000001
BOT = 9000000001
OTHER_BOT = 9000000002
STRANGER_PEER = 12345
EDIT_PTS = 4471


# --------------------------------------------------------------- normaliser


class FakeRouter:
    def __init__(self) -> None:
        self.texts: list[dict[str, Any]] = []
        self.medias: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.deletes: list[dict[str, Any]] = []

    async def on_telegram_text(self, **kwargs: Any) -> None:
        self.texts.append(kwargs)

    async def on_telegram_media(self, **kwargs: Any) -> None:
        self.medias.append(kwargs)

    async def on_owner_edit(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)

    async def on_owner_delete(self, **kwargs: Any) -> None:
        self.deletes.append(kwargs)

    def bridge_name_for_bot(self, bot_id: int) -> str | None:
        return "mom" if bot_id == BOT else None


class FakeReadMarker:
    def __init__(self) -> None:
        self.reads: list[tuple[str, int, int]] = []

    async def on_owner_read(
        self, bridge_name: str, *, owner_account_id: int, owner_message_id: int
    ) -> bool:
        self.reads.append((bridge_name, owner_account_id, owner_message_id))
        return True


def _intake(
    router: FakeRouter,
    allowed: set[int],
    read_marker: FakeReadMarker | None = None,
    updates: Any = None,
) -> MtprotoIntake:
    async def allow() -> set[int]:
        return allowed

    return MtprotoIntake(
        router=router, allowed_bots=allow, read_marker=read_marker, updates=updates
    )


def _msg(**over: Any) -> OwnerMessage:
    base: dict[str, Any] = {
        "account_id": ACCOUNT,
        "peer_id": BOT,
        "message_id": 100,
        "text": "привет",
        "media": None,
        "grouped_id": None,
        "reply_to_message_id": None,
    }
    base.update(over)
    return OwnerMessage(**base)


async def test_owner_text_enters_the_router_with_owner_identity() -> None:
    router = FakeRouter()
    await _intake(router, {BOT}).on_owner_message(_msg(text="привет"))

    assert len(router.texts) == 1
    call = router.texts[0]
    assert call["owner_account_id"] == ACCOUNT
    assert call["telegram_message_id"] == 100
    assert call["bot_id"] == BOT
    assert call["text"] == "привет"


async def test_each_contact_bot_routes_to_its_own_bridge() -> None:
    router = FakeRouter()
    await _intake(router, {BOT, OTHER_BOT}).on_owner_message(_msg(peer_id=OTHER_BOT))

    assert router.texts[0]["bot_id"] == OTHER_BOT


async def test_an_unrelated_chat_is_ignored_entirely() -> None:
    router = FakeRouter()
    await _intake(router, {BOT}).on_owner_message(_msg(peer_id=STRANGER_PEER))

    assert router.texts == [] and router.medias == []


async def test_media_takes_the_media_path_with_a_retry_reference() -> None:
    router = FakeRouter()
    ref = {"account_id": ACCOUNT, "peer": BOT, "owner_message_id": 100, "kind": "photo"}
    await _intake(router, {BOT}).on_owner_message(
        _msg(media=OwnerMedia(kind="photo", reference=ref), text="подпись")
    )

    assert router.texts == []
    assert len(router.medias) == 1
    call = router.medias[0]
    assert call["items"] == [], "no perishable bytes are carried; the worker fetches by reference"
    assert call["mtproto"] == [ref]
    assert call["caption"] == "подпись"
    assert call["owner_account_id"] == ACCOUNT


async def test_an_album_with_nowhere_to_assemble_is_refused_whole() -> None:
    """No durable store, no album — and still never N separate messages.

    Assembly needs the parts on disk: the group has no last-part marker, so the
    only honest way to know it ended is that nothing else arrived for a moment,
    and surviving that moment is what the store is for. Without one the group is
    refused as a whole rather than fragmented, which is the failure that cannot
    be undone. `tests/test_owner_albums.py` drives the wired path.
    """
    router = FakeRouter()
    ref = {"owner_message_id": 100, "kind": "photo", "grouped_id": 42}
    await _intake(router, {BOT}).on_owner_message(
        _msg(media=OwnerMedia(kind="photo", reference=ref), grouped_id=42)
    )

    assert router.texts == [] and router.medias == [], "one album must not split into N sends"


async def test_an_owner_update_respects_the_allowlist() -> None:
    """`UpdateEditMessage` is an edit, a reaction, or both, and which of those it
    was needs durable state the transport does not have. What the transport does
    decide — the one thing it is for — is that the dialog is ours."""

    class Dispatch:
        def __init__(self) -> None:
            self.seen: list[dict[str, object]] = []

        async def on_owner_update(self, **kwargs: object) -> None:
            self.seen.append(kwargs)

    dispatch = Dispatch()
    intake = _intake(FakeRouter(), {BOT}, updates=dispatch)
    message = SimpleNamespace(id=100)
    await intake.on_owner_update(
        account_id=ACCOUNT, bot_id=BOT, message=message,
        pts=EDIT_PTS, text="v2", outgoing=True,
    )
    await intake.on_owner_update(
        account_id=ACCOUNT, bot_id=STRANGER_PEER, message=message,
        pts=EDIT_PTS, text="v2", outgoing=True,
    )

    assert len(dispatch.seen) == 1, "only the bridge chat's update is dispatched"
    assert dispatch.seen[0]["bot_id"] == BOT
    assert dispatch.seen[0]["pts"] == EDIT_PTS


def test_the_version_rides_on_the_update_itself() -> None:
    """No helper to unwrap it any more: the handler reads `UpdateEditMessage`
    raw, and `pts` is a required field of that constructor."""
    update = types.UpdateEditMessage(message=SimpleNamespace(id=100), pts=EDIT_PTS, pts_count=1)
    assert update.pts == EDIT_PTS and update.pts_count == 1


async def test_owner_delete_routes_ids_to_the_router() -> None:
    router = FakeRouter()
    await _intake(router, {BOT}).on_owner_delete(account_id=ACCOUNT, message_ids=[100, 101])

    assert router.deletes == [{"owner_account_id": ACCOUNT, "owner_message_ids": [100, 101]}]


async def test_an_empty_delete_batch_does_nothing() -> None:
    router = FakeRouter()
    await _intake(router, {BOT}).on_owner_delete(account_id=ACCOUNT, message_ids=[])
    assert router.deletes == []


async def test_a_read_chat_becomes_a_read_mark_for_its_bridge() -> None:
    """Reading the bot chat is what tells MAX to draw the contact's second tick."""
    marker = FakeReadMarker()
    await _intake(FakeRouter(), {BOT}, marker).on_owner_read(
        bot_id=BOT, owner_account_id=ACCOUNT, owner_message_id=1_121_077
    )

    assert marker.reads == [("mom", ACCOUNT, 1_121_077)]


async def test_reading_an_unrelated_chat_tells_max_nothing() -> None:
    marker = FakeReadMarker()
    intake = _intake(FakeRouter(), {BOT}, marker)

    await intake.on_owner_read(
        bot_id=STRANGER_PEER, owner_account_id=ACCOUNT, owner_message_id=1_121_077
    )
    # A contact bot on the allowlist whose bridge is gone: still nothing to mark.
    await _intake(FakeRouter(), {BOT, OTHER_BOT}, marker).on_owner_read(
        bot_id=OTHER_BOT, owner_account_id=ACCOUNT, owner_message_id=1_121_077
    )

    assert marker.reads == []


async def test_without_a_read_marker_a_read_is_simply_ignored() -> None:
    """Presence can be off; the transport must not care."""
    await _intake(FakeRouter(), {BOT}).on_owner_read(
        bot_id=BOT, owner_account_id=ACCOUNT, owner_message_id=1_121_077
    )


# ------------------------------------------------------- media classification


def _doc(*attributes: Any) -> Any:
    return SimpleNamespace(
        media=types.MessageMediaDocument(document=SimpleNamespace(attributes=list(attributes))),
        id=7,
        message="",
        entities=[],
        grouped_id=None,
        reply_to=None,
    )


def test_media_is_classified_by_attributes_not_the_constructor() -> None:
    assert _classify(SimpleNamespace(media=types.MessageMediaPhoto(photo=None), id=7))[0] == "photo"
    # A voice and a video note now reach their own pipeline (R7), not a degraded one.
    assert _classify(_doc(types.DocumentAttributeAudio(duration=1, voice=True)))[0] == "voice"
    assert _classify(_doc(types.DocumentAttributeAudio(duration=1, voice=False)))[0] == "file"
    assert _classify(_doc(types.DocumentAttributeVideo(duration=1, w=1, h=1)))[0] == "video"
    assert (
        _classify(_doc(types.DocumentAttributeVideo(duration=1, w=1, h=1, round_message=True)))[0]
        == "circle"
    )
    assert _classify(_doc(types.DocumentAttributeFilename(file_name="a.pdf"))) == ("file", "a.pdf")
    assert _classify(SimpleNamespace(media=None, id=7)) is None


def test_text_and_entities_survive_as_markdown() -> None:
    message = SimpleNamespace(
        message="hi",
        entities=[types.MessageEntityBold(offset=0, length=2)],
        id=9,
        media=None,
        grouped_id=None,
        reply_to=None,
    )
    owner = owner_message_from(message, account_id=ACCOUNT, peer_id=BOT)
    assert owner.text == "**hi**"
    assert owner.media is None


def test_a_media_reference_carries_only_a_locator() -> None:
    message = _doc(types.DocumentAttributeFilename(file_name="a.pdf"))
    owner = owner_message_from(message, account_id=ACCOUNT, peer_id=BOT)
    assert owner.media is not None
    ref = owner.media.reference
    assert ref["owner_message_id"] == 7 and ref["peer"] == BOT and ref["account_id"] == ACCOUNT
    assert ref["kind"] == "file" and "part_index" in ref
    assert not any(isinstance(v, (bytes, bytearray)) for v in ref.values())


# ------------------------------------------------- router + repository (durable)


class RecordingSender:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    async def __call__(
        self, kind: str, direction: Direction, payload: dict[str, Any], sending: Any = None
    ) -> int | None:
        self.calls.append(payload)
        if self.fail is not None:
            raise self.fail
        if sending is not None:
            await sending()
        return 5000 + len(self.calls)


class Lookup:
    def bridge_for_bot(self, bot_id: int) -> BridgeTarget | None:
        return BridgeTarget(name="mom", max_chat_id=555, bot_id=BOT) if bot_id == BOT else None

    def bridge_for_max_chat(self, max_chat_id: int) -> BridgeTarget | None:
        return None


class Dummy:
    async def send_text(self, *a: Any, **k: Any) -> int | None:
        return 1

    async def send_media(self, *a: Any, **k: Any) -> int | None:
        return 1


@pytest_asyncio.fixture
async def wired(tmp_path: Path) -> Any:
    database = await Database.connect(tmp_path / "bridge.db")
    messages = MessageMapRepository(database)
    sender = RecordingSender()
    pipe = DeliveryPipe(outbox=OutboxRepository(database), send=sender)
    router = BridgeRouter(
        lookup=Lookup(),
        telegram=Dummy(),
        max_sender=Dummy(),
        messages=messages,
        state=BridgeStateRepository(database),
        owner_chat_id=ACCOUNT,
        pipe=pipe,
    )
    try:
        yield SimpleNamespace(router=router, messages=messages, sender=sender, database=database)
    finally:
        await database.close()


async def test_owner_ids_are_persisted_atomically_at_intake(wired: Any) -> None:
    """The mapping carries the owner identity before the send, not after.

    The sender raises, so the send never completes — and the row is already
    there, resolvable by account + owner-side id. That is what a later edit or
    delete will need, even for a message whose first send is still in flight.
    """
    wired.sender.fail = ConnectionError("MAX is down")
    with pytest.raises(ConnectionError):
        await wired.router.on_telegram_text(
            bot_id=BOT,
            telegram_chat_id=BOT,
            telegram_message_id=1001879,
            text="привет",
            owner_account_id=ACCOUNT,
        )

    link = await wired.messages.by_owner_account_message(ACCOUNT, 1001879)
    assert link is not None
    assert link.direction is Direction.TG_TO_MAX


async def test_a_replayed_new_message_is_deduped(wired: Any) -> None:
    for _ in range(2):
        await wired.router.on_telegram_text(
            bot_id=BOT,
            telegram_chat_id=BOT,
            telegram_message_id=1001879,
            text="привет",
            owner_account_id=ACCOUNT,
        )
    assert len(wired.sender.calls) == 1, "one owner message → one job → one MAX send"


async def test_media_job_stores_a_reference_and_no_bytes(wired: Any) -> None:
    ref = {"account_id": ACCOUNT, "peer": BOT, "owner_message_id": 1001880, "kind": "photo"}
    await wired.router.on_telegram_media(
        bot_id=BOT,
        telegram_chat_id=BOT,
        telegram_message_id=1001880,
        items=[],
        caption="",
        owner_account_id=ACCOUNT,
        mtproto=[ref],
    )
    payload = wired.sender.calls[0]
    assert payload["mtproto"] == [ref]
    assert payload["items"] == []
    assert not payload.get("sources")


# -------------------------------------------------------- media refetch source


class FakeTelethon:
    def __init__(self, *, present: bool = True) -> None:
        self.present = present
        self.downloaded: list[str] = []

    async def get_messages(self, peer: Any, ids: int) -> Any:
        return SimpleNamespace(media=object()) if self.present else None

    async def download_media(self, message: Any, file: str) -> None:
        Path(file).write_bytes(b"pixels")  # noqa: ASYNC240
        self.downloaded.append(file)


async def test_media_is_refetched_by_reference(tmp_path: Path) -> None:
    client = FakeTelethon(present=True)
    source = MtprotoMediaSource(
        client_provider=lambda: client, temp_files=TempFiles(tmp_path / "tmp")
    )
    ref = {"peer": BOT, "owner_message_id": 1001880, "kind": "photo", "name": "photo.jpg"}
    async with AsyncExitStack() as stack:
        kind, path, name = await source.fetch(stack, ref)
        assert kind == "photo" and name == "photo.jpg"
        assert path.read_bytes() == b"pixels"
    assert client.downloaded, "the file came back over the session, not from SQLite"


async def test_a_deleted_source_is_an_honest_error(tmp_path: Path) -> None:
    source = MtprotoMediaSource(
        client_provider=lambda: FakeTelethon(present=False),
        temp_files=TempFiles(tmp_path / "tmp"),
    )
    ref = {"peer": BOT, "owner_message_id": 1001880, "kind": "photo", "name": "photo.jpg"}
    with pytest.raises(MediaUnavailableError):
        async with AsyncExitStack() as stack:
            await source.fetch(stack, ref)
