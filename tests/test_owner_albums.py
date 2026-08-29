"""Inc 4 — an owner-side Telegram album, carried into MAX as one message.

Telegram sends a media group as one `UpdateNewMessage` per part with nothing
marking the last one, and the live probe settled what the parts can and cannot be
matched by: the order both sides always agree on is ascending Telegram message
id; `pts` steps by an amount that is not the part count; the caption sits on
whichever part the sender typed it on — it was on the second of three; and two
byte-identical photos carry no signal that separates them at all.

So the assembly here is durable and the order is read at the end, never guessed
at the start. These tests drive the whole path through the real repository, the
real router and the real outbox — one album, one job, one MAX message with N
attachments — and then take the process away at each point it can be taken away.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from bridge.routing.delivery import (
    KIND_TG_TO_MAX_DELETE,
    KIND_TG_TO_MAX_EDIT,
    DeliveryPipe,
)
from bridge.routing.echo import owner_album_namespace, owner_album_source_key
from bridge.routing.owner_mutation import resolve_delete, resolve_edit
from bridge.routing.router import BridgeRouter, BridgeTarget
from bridge.storage import (
    BridgeStateRepository,
    Database,
    Direction,
    MediaGroupRepository,
    MessageMapRepository,
    OutboxRepository,
    OutboxState,
)
from bridge.telegram.mtproto_intake import MtprotoIntake, OwnerMedia, OwnerMessage

ACCOUNT = 100000001
BOT = 9000000001
MAX_CHAT = 555
GROUPED = 13984172040192
NAMESPACE = owner_album_namespace(ACCOUNT, BOT, GROUPED)
SEND_KEY = owner_album_source_key(NAMESPACE)


class RecordingSender:
    """The one sender the queue has, shaped like the runtime's `send_job`.

    Sends are recorded rather than performed; edits and deletes are dispatched to
    the very resolvers production dispatches them to, because what these tests
    are about is the resolution — which job an album's mutation finds, and what
    it does to it.
    """

    def __init__(self, *, outbox: OutboxRepository, messages: Any, max_sender: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail: Exception | None = None
        self._outbox = outbox
        self._messages = messages
        self._max = max_sender

    async def __call__(
        self, kind: str, direction: Direction, payload: dict[str, Any], sending: Any = None
    ) -> int | None:
        if kind == KIND_TG_TO_MAX_EDIT:
            return await resolve_edit(
                outbox=self._outbox,
                messages=self._messages,
                max_sender=self._max,
                payload=payload,
            )
        if kind == KIND_TG_TO_MAX_DELETE:
            return await resolve_delete(
                outbox=self._outbox,
                messages=self._messages,
                max_sender=self._max,
                payload=payload,
            )
        self.calls.append(payload)
        if self.fail is not None:
            raise self.fail
        if sending is not None:
            await sending()
        return 5000 + len(self.calls)


class Lookup:
    def bridge_for_bot(self, bot_id: int) -> BridgeTarget | None:
        return BridgeTarget(name="mom", max_chat_id=MAX_CHAT, bot_id=BOT) if bot_id == BOT else None

    def bridge_for_max_chat(self, max_chat_id: int) -> BridgeTarget | None:
        return None


class MaxSpy:
    """Records what MAX was actually asked to do, in order."""

    def __init__(self) -> None:
        self.edits: list[tuple[int, int, str]] = []
        self.deletes: list[tuple[int, list[int], bool]] = []

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


def _part(
    message_id: int,
    *,
    caption: str = "",
    kind: str = "photo",
    reply_to: int | None = None,
) -> OwnerMessage:
    """One part of the album, as the transport hands it over."""
    return OwnerMessage(
        account_id=ACCOUNT,
        peer_id=BOT,
        message_id=message_id,
        text=caption,
        media=OwnerMedia(
            kind=kind,
            reference={
                "account_id": ACCOUNT,
                "peer": BOT,
                "owner_message_id": message_id,
                "part_index": 0,
                "grouped_id": GROUPED,
                "kind": kind,
                "name": f"{kind}.jpg",
            },
        ),
        grouped_id=GROUPED,
        reply_to_message_id=reply_to,
    )


async def _wire(path: Path, *, window: float = 60.0) -> Any:
    database = await Database.connect(path)
    messages = MessageMapRepository(database)
    albums = MediaGroupRepository(database)
    outbox = OutboxRepository(database)
    max_sender = MaxSpy()
    sender = RecordingSender(outbox=outbox, messages=messages, max_sender=max_sender)
    router = BridgeRouter(
        lookup=Lookup(),
        telegram=MaxSpy(),
        max_sender=max_sender,
        messages=messages,
        state=BridgeStateRepository(database),
        owner_chat_id=ACCOUNT,
        pipe=DeliveryPipe(outbox=outbox, send=sender),
        albums=albums,
    )

    async def allow() -> set[int]:
        return {BOT}

    intake = MtprotoIntake(
        router=router,
        allowed_bots=allow,
        album_parts=albums,
        album_window_seconds=window,
    )
    return SimpleNamespace(
        database=database,
        messages=messages,
        albums=albums,
        outbox=outbox,
        sender=sender,
        max_sender=max_sender,
        router=router,
        intake=intake,
    )


@pytest_asyncio.fixture
async def wired(tmp_path: Path) -> Any:
    live = await _wire(tmp_path / "bridge.db")
    try:
        yield live
    finally:
        await live.database.close()


async def _album(live: Any, *parts: OwnerMessage) -> None:
    for part in parts:
        await live.intake.on_owner_message(part)
    await live.intake.flush_albums()


# ------------------------------------------------------------------- assembly


async def test_an_album_becomes_one_job_with_every_part_in_order(wired: Any) -> None:
    await _album(wired, _part(500), _part(501, caption="подпись"), _part(502))

    assert len(wired.sender.calls) == 1, "one album is one MAX message, not three"
    payload = wired.sender.calls[0]
    assert [ref["owner_message_id"] for ref in payload["mtproto"]] == [500, 501, 502]
    assert [ref["part_index"] for ref in payload["mtproto"]] == [0, 1, 2]
    assert payload["caption"] == "подпись"

    job = await wired.outbox.by_source_key(SEND_KEY)
    assert job is not None and job.state is OutboxState.DONE


async def test_parts_arriving_out_of_order_still_go_out_in_order(wired: Any) -> None:
    """A catch-up replays an album in whatever interleaving it likes.

    Insertion order is an accident of which update was processed first; ascending
    message id is the album. The probe proved the two sides agree on the second
    and never on the first.
    """
    await _album(wired, _part(502), _part(500), _part(501))

    payload = wired.sender.calls[0]
    assert [ref["owner_message_id"] for ref in payload["mtproto"]] == [500, 501, 502]


async def test_identical_photos_keep_their_places(wired: Any) -> None:
    """Three parts that describe themselves identically are still three parts.

    Nothing in the content separates them — the probe confirmed byte-identical
    images are indistinguishable — so what separates them is the order, and the
    order is the only thing that must survive.
    """
    same = {"kind": "photo"}
    await _album(wired, _part(500, **same), _part(501, **same), _part(502, **same))

    payload = wired.sender.calls[0]
    assert [ref["owner_message_id"] for ref in payload["mtproto"]] == [500, 501, 502]
    assert len(await wired.albums.parts(NAMESPACE)) == 3


@pytest.mark.parametrize("carrier", [500, 501, 502])
async def test_the_caption_is_found_wherever_it_was_typed(
    tmp_path: Path, carrier: int
) -> None:
    """First, middle or last: Telegram puts it where the sender put it.

    The probe found it on index 1 of a three-part album, which is exactly the
    case an implementation that reads the head would get wrong.
    """
    live = await _wire(tmp_path / f"bridge-{carrier}.db")
    try:
        await _album(
            live,
            *(
                _part(message_id, caption="подпись" if message_id == carrier else "")
                for message_id in (500, 501, 502)
            ),
        )
        assert live.sender.calls[0]["caption"] == "подпись"
    finally:
        await live.database.close()


async def test_a_replayed_part_is_not_added_twice(wired: Any) -> None:
    await _album(wired, _part(500), _part(501), _part(501), _part(502))

    payload = wired.sender.calls[0]
    assert [ref["owner_message_id"] for ref in payload["mtproto"]] == [500, 501, 502]


async def test_a_reply_on_any_part_travels_with_the_album(wired: Any) -> None:
    await _album(wired, _part(500), _part(501, reply_to=404), _part(502))

    # Unknown target: delivered plain rather than dropped, the existing fallback.
    assert wired.sender.calls[0]["reply_to"] is None
    parts = await wired.albums.ordered_parts(NAMESPACE)
    assert json.loads(parts[1].payload_json)["reply_to"] == 404


async def test_the_quiet_window_closes_the_group_on_its_own(tmp_path: Path) -> None:
    """No explicit flush: the pause is what says the album ended."""
    live = await _wire(tmp_path / "bridge.db", window=0.02)
    try:
        await live.intake.on_owner_message(_part(500))
        await live.intake.on_owner_message(_part(501))
        assert live.sender.calls == []
        await asyncio.sleep(0.2)
        assert len(live.sender.calls) == 1
        assert len(live.sender.calls[0]["mtproto"]) == 2
    finally:
        await live.database.close()


# -------------------------------------------------------------------- restarts


async def test_a_restart_before_the_window_carries_the_album_afterwards(
    tmp_path: Path,
) -> None:
    """The crash between part two and part three costs nothing.

    Telegram will not send those parts again, so if they lived only in the
    process they were gone. They live on disk, and the next start makes the same
    guess about when the album ended that the last one was about to make.
    """
    path = tmp_path / "bridge.db"
    first = await _wire(path)
    try:
        await first.intake.on_owner_message(_part(500))
        await first.intake.on_owner_message(_part(501))
        assert first.sender.calls == []
    finally:
        await first.database.close()

    second = await _wire(path)
    try:
        assert await second.intake.restore_albums() == 1
        await second.intake.flush_albums()
        assert len(second.sender.calls) == 1
        assert [
            ref["owner_message_id"] for ref in second.sender.calls[0]["mtproto"]
        ] == [500, 501]
    finally:
        await second.database.close()


async def test_a_restart_after_the_send_does_not_send_it_again(tmp_path: Path) -> None:
    path = tmp_path / "bridge.db"
    first = await _wire(path)
    try:
        await _album(first, _part(500), _part(501))
        assert len(first.sender.calls) == 1
    finally:
        await first.database.close()

    second = await _wire(path)
    try:
        assert await second.intake.restore_albums() == 0, "a settled group is not open"
        # And even if it were flushed by hand, the source key holds.
        await second.intake._albums.flush(NAMESPACE)
        assert second.sender.calls == []
        assert len(await second.messages.recent_max_messages(MAX_CHAT, BOT)) <= 1
    finally:
        await second.database.close()


async def test_a_crash_between_the_mapping_and_the_binding_writes_one_row(
    tmp_path: Path,
) -> None:
    """The narrow window: the row exists, the parts do not know about it yet.

    Re-running the carry has to find that row rather than write a second mapping
    for the same album — two rows for one message is how a delete resolves to
    half of it.
    """
    live = await _wire(tmp_path / "bridge.db")
    try:
        await live.intake.on_owner_message(_part(500))
        await live.intake.on_owner_message(_part(501))
        # The mapping, written the way the carry writes it, and nothing more.
        link_id = await live.messages.record_from_telegram(
            bridge_name="mom",
            max_chat_id=MAX_CHAT,
            telegram_bot_id=BOT,
            telegram_chat_id=BOT,
            telegram_message_id=None,
            telegram_owner_message_id=500,
            telegram_owner_account_id=ACCOUNT,
        )
        assert await live.albums.link_of(NAMESPACE) is None

        await live.intake.flush_albums()

        assert await live.albums.link_of(NAMESPACE) == link_id
        rows = await live.database.query(
            "SELECT id FROM message_map WHERE telegram_owner_account_id = ?", (ACCOUNT,)
        )
        assert len(rows) == 1, "one album, one canonical mapping row"
    finally:
        await live.database.close()


async def test_the_source_key_is_the_album_not_a_message_id(wired: Any) -> None:
    """A partial group that closes on a different head must not enqueue twice."""
    await _album(wired, _part(501), _part(502))
    assert await wired.outbox.by_source_key(SEND_KEY) is not None
    # The head-keyed form is deliberately not what the job is filed under.
    assert await wired.outbox.by_source_key(f"tg-owner-msg:{ACCOUNT}:501") is None


# ------------------------------------------------------------- what is stored


async def test_nothing_perishable_reaches_the_database(wired: Any) -> None:
    """No bytes, no temp paths, no signed URL — in the parts or in the job.

    A retry re-fetches over the session from the reference, so anything that
    could expire between the first attempt and the second has no business being
    written down.
    """
    await _album(wired, _part(500), _part(501))

    for part in await wired.albums.parts(NAMESPACE):
        payload = json.loads(part.payload_json)
        assert set(payload) == {"reference", "caption", "reply_to"}
        assert set(payload["reference"]) == {
            "account_id",
            "peer",
            "owner_message_id",
            "part_index",
            "grouped_id",
            "kind",
            "name",
        }

    job_payload = wired.sender.calls[0]
    assert job_payload["items"] == []
    assert not job_payload.get("sources")
    rows = await wired.database.query("SELECT payload_json FROM media_group_part")
    temp_root = tempfile.gettempdir()
    for row in rows:
        assert temp_root not in row["payload_json"], "a path that outlives nothing"
        assert "http" not in row["payload_json"], "a URL MAX or Telegram will expire"


async def test_every_part_points_at_one_canonical_message(wired: Any) -> None:
    await _album(wired, _part(500), _part(501), _part(502))

    parts = await wired.albums.ordered_parts(NAMESPACE)
    assert len({part.link_id for part in parts}) == 1
    link_id = parts[0].link_id
    assert link_id is not None
    link = await wired.messages.by_id(link_id)
    assert link is not None
    assert link.telegram_owner_message_id == 500, "the head is the canonical identity"
    assert [part.part_index for part in parts] == [0, 1, 2]


# ------------------------------------------------------------------- mutations


async def test_an_edit_before_the_send_coalesces_into_the_pending_job(
    tmp_path: Path,
) -> None:
    """Nothing has gone out yet, so the album simply goes out with the new words."""
    live = await _wire(tmp_path / "bridge.db")
    try:
        live.sender.fail = ConnectionError("MAX is down")
        with pytest.raises(ConnectionError):
            await _album(live, _part(500), _part(501, caption="первая"))
        job = await live.outbox.by_source_key(SEND_KEY)
        assert job is not None and job.state is OutboxState.PENDING

        # Edited on the *second* part — the one the caption is on.
        await live.router.on_owner_edit(
            owner_account_id=ACCOUNT, owner_message_id=501, text="вторая", edit_pts=7
        )

        job = await live.outbox.by_source_key(SEND_KEY)
        assert job is not None
        assert json.loads(job.payload_json)["caption"] == "вторая"
        assert live.max_sender.edits == [], "nothing was sent, so nothing is edited remotely"
    finally:
        await live.database.close()


async def test_a_delete_before_the_send_cancels_the_whole_album(tmp_path: Path) -> None:
    live = await _wire(tmp_path / "bridge.db")
    try:
        live.sender.fail = ConnectionError("MAX is down")
        with pytest.raises(ConnectionError):
            await _album(live, _part(500), _part(501), _part(502))

        # One part deleted; MAX has no way to remove one attachment of a message,
        # so the only honest reading is that the album is not to be sent.
        await live.router.on_owner_delete(owner_account_id=ACCOUNT, owner_message_ids=[502])

        job = await live.outbox.by_source_key(SEND_KEY)
        assert job is not None and job.state is OutboxState.ARCHIVED
        assert live.max_sender.deletes == []
    finally:
        await live.database.close()


async def test_a_mutation_waits_while_the_send_is_in_flight(wired: Any) -> None:
    """INFLIGHT is not an answer, and guessing at one is how a message is lost."""
    from bridge.routing.delivery import DeferDelivery

    await wired.intake.on_owner_message(_part(500))
    await wired.intake.on_owner_message(_part(501))
    job_id = await wired.outbox.enqueue(
        bridge_name="mom",
        direction=Direction.TG_TO_MAX,
        kind="tg_to_max_media",
        payload={"max_chat_id": MAX_CHAT, "caption": "", "mtproto": []},
        source_key=SEND_KEY,
    )
    await wired.outbox.claim_due("mom")
    await wired.intake.flush_albums()

    job = await wired.outbox.by_source_key(SEND_KEY)
    assert job is not None and job.id == job_id and job.state is OutboxState.INFLIGHT

    with pytest.raises(DeferDelivery):
        await resolve_delete(
            outbox=wired.outbox,
            messages=wired.messages,
            max_sender=wired.max_sender,
            payload={
                "account_id": ACCOUNT,
                "owner_message_id": 500,
                "send_source_key": SEND_KEY,
            },
        )
    assert wired.max_sender.deletes == []


async def test_an_edit_after_delivery_edits_the_one_max_message(wired: Any) -> None:
    await _album(wired, _part(500), _part(501, caption="первая"), _part(502))
    link_id = await wired.albums.link_of(NAMESPACE)
    assert link_id is not None
    await wired.messages.attach_max_message(link_id, 9001)

    await wired.router.on_owner_edit(
        owner_account_id=ACCOUNT, owner_message_id=502, text="вторая", edit_pts=8
    )

    assert wired.max_sender.edits == [(MAX_CHAT, 9001, "вторая")]


async def test_deleting_one_part_deletes_the_one_max_message(wired: Any) -> None:
    await _album(wired, _part(500), _part(501), _part(502))
    link_id = await wired.albums.link_of(NAMESPACE)
    assert link_id is not None
    await wired.messages.attach_max_message(link_id, 9001)

    await wired.router.on_owner_delete(owner_account_id=ACCOUNT, owner_message_ids=[501])

    assert wired.max_sender.deletes == [(MAX_CHAT, [9001], True)]


async def test_deleting_every_part_is_still_one_delete(wired: Any) -> None:
    """A batch of three ids is one album, and MAX holds one message for it."""
    await _album(wired, _part(500), _part(501), _part(502))
    link_id = await wired.albums.link_of(NAMESPACE)
    assert link_id is not None
    await wired.messages.attach_max_message(link_id, 9001)

    await wired.router.on_owner_delete(
        owner_account_id=ACCOUNT, owner_message_ids=[500, 501, 502]
    )
    # And again, the way a catch-up replays the update.
    await wired.router.on_owner_delete(
        owner_account_id=ACCOUNT, owner_message_ids=[502, 500, 501]
    )

    assert wired.max_sender.deletes == [(MAX_CHAT, [9001], True)]
    keys = await wired.database.query(
        "SELECT source_key FROM outbox WHERE kind = ?", (KIND_TG_TO_MAX_DELETE,)
    )
    assert len(keys) == 1, "one album, one delete job, however many parts were removed"


async def test_edits_of_different_parts_are_versioned_not_merged(wired: Any) -> None:
    """Two edit events are two events even when they name different parts.

    They collapse onto one canonical message — there is only one — but each keeps
    its own `pts`, so a replay of either is a no-op and neither swallows the
    other.
    """
    await _album(wired, _part(500), _part(501, caption="первая"))
    link_id = await wired.albums.link_of(NAMESPACE)
    assert link_id is not None
    await wired.messages.attach_max_message(link_id, 9001)

    await wired.router.on_owner_edit(
        owner_account_id=ACCOUNT, owner_message_id=500, text="вторая", edit_pts=8
    )
    await wired.router.on_owner_edit(
        owner_account_id=ACCOUNT, owner_message_id=501, text="третья", edit_pts=9
    )
    await wired.router.on_owner_edit(
        owner_account_id=ACCOUNT, owner_message_id=501, text="третья", edit_pts=9
    )

    assert wired.max_sender.edits == [
        (MAX_CHAT, 9001, "вторая"),
        (MAX_CHAT, 9001, "третья"),
    ]
    keys = await wired.database.query(
        "SELECT source_key FROM outbox WHERE kind = ?", (KIND_TG_TO_MAX_EDIT,)
    )
    assert len(keys) == 2


async def test_an_ambiguous_send_withholds_the_mutation(wired: Any) -> None:
    """Unknown remote outcome is never guessed through — it is for the owner.

    The album went out and MAX never answered, so nobody knows whether it is in
    the dialog. Editing it would address a message that may not exist; not
    editing it may leave a stale one. Neither is chosen here.
    """
    from bridge.routing.delivery import UnconfirmedDeliveryError

    wired.sender.fail = UnconfirmedDeliveryError("MAX returned no message id")
    await _album(wired, _part(500), _part(501))
    job = await wired.outbox.by_source_key(SEND_KEY)
    assert job is not None and job.state is OutboxState.AMBIGUOUS

    with pytest.raises(UnconfirmedDeliveryError):
        await resolve_edit(
            outbox=wired.outbox,
            messages=wired.messages,
            max_sender=wired.max_sender,
            payload={
                "account_id": ACCOUNT,
                "owner_message_id": 500,
                "send_source_key": SEND_KEY,
                "text": "поздно",
            },
        )
    assert wired.max_sender.edits == []


# --------------------------------------------------------------------- refetch


class FakeTelethon:
    """The owner session, as far as re-fetching a part is concerned."""

    def __init__(self, *, missing: set[int] | None = None) -> None:
        self.missing = missing or set()
        self.fetched: list[int] = []

    async def get_messages(self, peer: Any, ids: int) -> Any:
        if ids in self.missing:
            return None
        self.fetched.append(ids)
        return SimpleNamespace(media=object())

    async def download_media(self, message: Any, file: str) -> None:
        Path(file).write_bytes(b"pixels")  # noqa: ASYNC240


async def test_every_part_is_refetched_in_order_on_each_attempt(
    wired: Any, tmp_path: Path
) -> None:
    """The job holds references, so the second attempt fetches the same album.

    Not the same bytes: the files from the first attempt are long gone, deleted
    when it returned. That is the whole reason nothing perishable is stored, and
    it is what makes retrying an album upload mean anything.
    """
    from contextlib import AsyncExitStack

    from bridge.media.store import TempFiles
    from bridge.telegram.mtproto_media import MtprotoMediaSource

    await _album(wired, _part(500), _part(501), _part(502))
    references = wired.sender.calls[0]["mtproto"]

    client = FakeTelethon()
    source = MtprotoMediaSource(
        client_provider=lambda: client, temp_files=TempFiles(tmp_path / "tmp")
    )
    for _attempt in range(2):
        async with AsyncExitStack() as stack:
            fetched = [await source.fetch(stack, ref) for ref in references]
            assert [path.exists() for _, path, _ in fetched] == [True, True, True]
        # Left the block: the files are gone again, exactly as after a real send.
        assert all(not path.exists() for _, path, _ in fetched)

    assert client.fetched == [500, 501, 502, 500, 501, 502]


async def test_a_part_whose_source_is_gone_fails_honestly(
    wired: Any, tmp_path: Path
) -> None:
    """A deleted source is an error the retry policy handles, never a silent gap."""
    from contextlib import AsyncExitStack

    from bridge.media.store import TempFiles
    from bridge.telegram.mtproto_media import MediaUnavailableError, MtprotoMediaSource

    await _album(wired, _part(500), _part(501))
    references = wired.sender.calls[0]["mtproto"]

    client = FakeTelethon(missing={501})
    source = MtprotoMediaSource(
        client_provider=lambda: client, temp_files=TempFiles(tmp_path / "tmp")
    )
    with pytest.raises(MediaUnavailableError):
        async with AsyncExitStack() as stack:
            for ref in references:
                await source.fetch(stack, ref)
