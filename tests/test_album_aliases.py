"""V15: an album part as an alias of one canonical message.

Telegram treats an album as N messages — it will let one part be replied to,
edited or deleted on its own — while MAX carries the whole group as a single
message with N attachments and offers no way to touch one of them. So the two
sides can only be joined by giving every Telegram part a durable identity of its
own and pointing all of them at one `message_map` row.

That is what `media_group_part` became in V15, and these tests hold the parts of
it that a future change could quietly break: the rebuild loses nothing, the old
dedup still refuses a replayed Bot API part, the new logical identity refuses a
duplicate position, and each of the four lookups the later increments are built
on returns the row it claims to.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest_asyncio

from bridge.max_client import AttachmentKind, MaxAttachment, normalize_message
from bridge.routing.delivery import DeliveryPipe
from bridge.routing.echo import (
    album_part_fingerprint,
    expected_album_namespace,
    owner_album_namespace,
)
from bridge.routing.router import BridgeRouter, BridgeTarget
from bridge.storage import (
    BridgeStateRepository,
    Database,
    Direction,
    MediaGroupRepository,
    MessageMapRepository,
    OutboxRepository,
)

BOT = 9000000001
OWNER = 5150
PEER = 9000000001


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


async def _expected(
    groups: MediaGroupRepository,
    *,
    link_id: int,
    index: int,
    kind: str = "photo",
    caption: str | None = None,
) -> bool:
    """One MAX→TG alias, written the way the sender will write it: before the
    send, with a position and a structure and no Telegram id yet."""
    return await groups.add_part(
        media_group_id=expected_album_namespace(link_id),
        bridge_name="mom",
        bot_id=BOT,
        payload={"kind": kind},
        link_id=link_id,
        direction=Direction.MAX_TO_TG,
        part_index=index,
        media_kind=kind,
        caption_present=caption is not None,
        part_fingerprint=album_part_fingerprint(kind, part_index=index, caption=caption),
    )


# ------------------------------------------------------------------ the legacy shape


async def test_the_bot_api_collector_still_works_exactly_as_before(
    database: Database,
) -> None:
    """The rebuild is invisible to the transport the table was built for."""
    groups = MediaGroupRepository(database)
    for message_id in (11, 12, 13):
        assert await groups.add_part(
            media_group_id="17983",
            bridge_name="dad",
            bot_id=BOT,
            telegram_message_id=message_id,
            payload={"file_id": f"f{message_id}"},
        )

    parts = await groups.parts("17983")
    assert [part.telegram_message_id for part in parts] == [11, 12, 13]
    assert await groups.open_groups() == ["17983"]
    # Nothing about a legacy part claims to be an alias.
    assert all(part.link_id is None and part.part_index is None for part in parts)
    assert all(part.direction is None for part in parts)


async def test_a_replayed_bot_api_part_is_still_refused(database: Database) -> None:
    """V8's dedup key, kept intact as a partial index."""
    groups = MediaGroupRepository(database)
    common = {
        "media_group_id": "17983",
        "bridge_name": "dad",
        "bot_id": BOT,
        "telegram_message_id": 11,
        "payload": {"file_id": "f11"},
    }
    assert await groups.add_part(**common) is True  # type: ignore[arg-type]
    assert await groups.add_part(**common) is False  # type: ignore[arg-type]
    assert len(await groups.parts("17983")) == 1


async def test_the_same_telegram_id_in_another_namespace_is_allowed(
    database: Database,
) -> None:
    """Two albums may hold the same message id — they are different groups.

    The dedup index is (group, id) and not (id): a Bot API album the owner sent
    and an alias of something the bridge delivered are different rows about
    different messages, and one must not refuse the other.
    """
    groups = MediaGroupRepository(database)
    assert await groups.add_part(
        media_group_id="17983",
        bridge_name="dad",
        bot_id=BOT,
        telegram_message_id=7001,
        payload={},
    )
    assert await groups.add_part(
        media_group_id="17984",
        bridge_name="dad",
        bot_id=BOT,
        telegram_message_id=7001,
        payload={},
    )
    assert len(await groups.parts("17983")) == 1
    assert len(await groups.parts("17984")) == 1


# --------------------------------------------------------------- logical identity


async def test_two_parts_cannot_claim_the_same_position(database: Database) -> None:
    groups = MediaGroupRepository(database)
    assert await _expected(groups, link_id=42, index=0)
    assert await _expected(groups, link_id=42, index=0) is False
    assert len(await groups.parts(expected_album_namespace(42))) == 1


async def test_positions_are_only_unique_inside_one_album(database: Database) -> None:
    groups = MediaGroupRepository(database)
    assert await _expected(groups, link_id=42, index=0)
    assert await _expected(groups, link_id=43, index=0)


async def test_parts_without_a_position_do_not_collide(database: Database) -> None:
    """Assembly writes parts before the order is known, so many hold no index.

    A unique index over a nullable column would make the second such part
    impossible; partial is what keeps the constraint about real positions.
    """
    groups = MediaGroupRepository(database)
    namespace = owner_album_namespace(OWNER, PEER, 9911)
    for owner_message_id in (500, 501, 502):
        assert await groups.add_part(
            media_group_id=namespace,
            bridge_name="mom",
            bot_id=PEER,
            payload={"kind": "photo"},
            direction=Direction.TG_TO_MAX,
            telegram_owner_account_id=OWNER,
            telegram_owner_message_id=owner_message_id,
        )
    parts = await groups.parts(namespace)
    assert len(parts) == 3
    assert all(part.part_index is None for part in parts)


async def test_an_owner_message_belongs_to_one_part(database: Database) -> None:
    """A replayed owner update must not add its photo to the album twice."""
    groups = MediaGroupRepository(database)
    namespace = owner_album_namespace(OWNER, PEER, 9911)
    part = {
        "media_group_id": namespace,
        "bridge_name": "mom",
        "bot_id": PEER,
        "payload": {"kind": "photo"},
        "direction": Direction.TG_TO_MAX,
        "telegram_owner_account_id": OWNER,
        "telegram_owner_message_id": 500,
    }
    assert await groups.add_part(**part) is True  # type: ignore[arg-type]
    assert await groups.add_part(**part) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------- lookups


async def test_the_owner_lookup_finds_the_part_a_delete_names(database: Database) -> None:
    groups = MediaGroupRepository(database)
    namespace = owner_album_namespace(OWNER, PEER, 9911)
    await groups.add_part(
        media_group_id=namespace,
        bridge_name="mom",
        bot_id=PEER,
        payload={},
        direction=Direction.TG_TO_MAX,
        telegram_owner_account_id=OWNER,
        telegram_owner_message_id=501,
    )

    found = await groups.by_owner_message(OWNER, 501)
    assert found is not None and found.media_group_id == namespace
    # Keyed by account: another account's id 501 is another message entirely.
    assert await groups.by_owner_message(OWNER + 1, 501) is None


async def test_the_bot_lookup_finds_the_alias_a_reply_quotes(database: Database) -> None:
    groups = MediaGroupRepository(database)
    await _expected(groups, link_id=42, index=1)
    alias = await groups.part_at(expected_album_namespace(42), 1)
    assert alias is not None
    assert await groups.attach_bot_message(alias.id, 7002)

    found = await groups.by_bot_message(BOT, 7002)
    assert found is not None and found.id == alias.id
    assert await groups.by_bot_message(BOT + 1, 7002) is None


async def test_the_link_lookup_returns_every_part_of_one_message(
    database: Database,
) -> None:
    """What a delete reads: N aliases, one canonical row, one MAX delete."""
    groups = MediaGroupRepository(database)
    for index in (0, 1, 2):
        await _expected(groups, link_id=42, index=index)
    await _expected(groups, link_id=99, index=0)
    assert await groups.bind_link(expected_album_namespace(42), 42) == 3

    parts = await groups.parts_of_link(42)
    assert [part.part_index for part in parts] == [0, 1, 2]
    assert {part.link_id for part in parts} == {42}


async def test_ordered_parts_follow_the_canonical_order(database: Database) -> None:
    """Ascending Telegram message id, however the rows happened to be written.

    Written deliberately out of order here, because that is what the transport
    does: an owner album arrives one `UpdateNewMessage` at a time and a catch-up
    can replay it in any interleaving. The order that comes back is the one both
    sides agree on, never the insertion order.
    """
    groups = MediaGroupRepository(database)
    namespace = owner_album_namespace(OWNER, PEER, 9911)
    for owner_message_id in (502, 500, 501):
        await groups.add_part(
            media_group_id=namespace,
            bridge_name="mom",
            bot_id=PEER,
            payload={"n": owner_message_id},
            direction=Direction.TG_TO_MAX,
            telegram_owner_account_id=OWNER,
            telegram_owner_message_id=owner_message_id,
        )

    ordered = await groups.ordered_parts(namespace)
    assert [part.telegram_owner_message_id for part in ordered] == [500, 501, 502]

    # And once the positions are fixed, they are what the order follows.
    for index, part in enumerate(ordered):
        assert await groups.assign_part_index(part.id, index)
    assert [part.part_index for part in await groups.ordered_parts(namespace)] == [0, 1, 2]


# ------------------------------------------------------------------ the two scopes


async def test_each_transport_only_sees_its_own_open_groups(database: Database) -> None:
    """A restart must not hand one transport another's album.

    Before V15 there was one population and one query. Now three kinds of row
    share the table, and a Bot API restore that picked up an owner→MAX group
    mid-assembly would upload it into MAX a second time.
    """
    groups = MediaGroupRepository(database)
    await groups.add_part(
        media_group_id="17983",
        bridge_name="dad",
        bot_id=BOT,
        telegram_message_id=11,
        payload={},
    )
    owner_namespace = owner_album_namespace(OWNER, PEER, 9911)
    await groups.add_part(
        media_group_id=owner_namespace,
        bridge_name="mom",
        bot_id=PEER,
        payload={},
        direction=Direction.TG_TO_MAX,
        telegram_owner_account_id=OWNER,
        telegram_owner_message_id=500,
    )
    await _expected(groups, link_id=42, index=0)

    assert await groups.open_groups() == ["17983"]
    assert await groups.open_owner_groups() == [owner_namespace]


async def test_a_settled_owner_group_stops_being_open(database: Database) -> None:
    groups = MediaGroupRepository(database)
    namespace = owner_album_namespace(OWNER, PEER, 9911)
    await groups.add_part(
        media_group_id=namespace,
        bridge_name="mom",
        bot_id=PEER,
        payload={},
        direction=Direction.TG_TO_MAX,
        telegram_owner_account_id=OWNER,
        telegram_owner_message_id=500,
    )
    assert await groups.open_owner_groups() == [namespace]

    assert await groups.bind_link(namespace, 7) == 1
    assert await groups.open_owner_groups() == []


# ----------------------------------------------------------- conditional binding


async def test_binding_the_same_owner_id_twice_is_a_success(database: Database) -> None:
    """A catch-up replays the echo; the second pass must change nothing."""
    groups = MediaGroupRepository(database)
    await _expected(groups, link_id=42, index=0)
    alias = await groups.part_at(expected_album_namespace(42), 0)
    assert alias is not None

    assert await groups.attach_owner_message(alias.id, account_id=OWNER, owner_message_id=900)
    assert await groups.attach_owner_message(alias.id, account_id=OWNER, owner_message_id=900)
    again = await groups.part_at(expected_album_namespace(42), 0)
    assert again is not None and again.telegram_owner_message_id == 900


async def test_a_different_owner_id_is_refused_rather_than_moved(
    database: Database,
) -> None:
    groups = MediaGroupRepository(database)
    await _expected(groups, link_id=42, index=0)
    alias = await groups.part_at(expected_album_namespace(42), 0)
    assert alias is not None
    assert await groups.attach_owner_message(alias.id, account_id=OWNER, owner_message_id=900)

    assert (
        await groups.attach_owner_message(alias.id, account_id=OWNER, owner_message_id=901)
        is False
    )
    again = await groups.part_at(expected_album_namespace(42), 0)
    assert again is not None and again.telegram_owner_message_id == 900


async def test_one_owner_message_cannot_bind_two_parts(database: Database) -> None:
    """The index, not a convention: a second alias claiming it is refused."""
    groups = MediaGroupRepository(database)
    await _expected(groups, link_id=42, index=0)
    await _expected(groups, link_id=42, index=1)
    namespace = expected_album_namespace(42)
    first = await groups.part_at(namespace, 0)
    second = await groups.part_at(namespace, 1)
    assert first is not None and second is not None

    assert await groups.attach_owner_message(first.id, account_id=OWNER, owner_message_id=900)
    assert (
        await groups.attach_owner_message(second.id, account_id=OWNER, owner_message_id=900)
        is False
    )
    unchanged = await groups.part_at(namespace, 1)
    assert unchanged is not None and unchanged.telegram_owner_message_id is None


async def test_a_bot_id_is_attached_once_and_never_replaced(database: Database) -> None:
    groups = MediaGroupRepository(database)
    await _expected(groups, link_id=42, index=0)
    alias = await groups.part_at(expected_album_namespace(42), 0)
    assert alias is not None

    assert await groups.attach_bot_message(alias.id, 7001)
    assert await groups.attach_bot_message(alias.id, 7001)  # replayed settlement
    assert await groups.attach_bot_message(alias.id, 7002) is False
    again = await groups.part_at(expected_album_namespace(42), 0)
    assert again is not None and again.telegram_message_id == 7001


async def test_giving_up_on_a_part_only_clears_its_fingerprint(
    database: Database,
) -> None:
    groups = MediaGroupRepository(database)
    await _expected(groups, link_id=42, index=0)
    namespace = expected_album_namespace(42)
    alias = await groups.part_at(namespace, 0)
    assert alias is not None and alias.part_fingerprint is not None

    assert await groups.stop_binding(alias.id)
    after = await groups.part_at(namespace, 0)
    assert after is not None
    assert after.part_fingerprint is None
    # The alias itself survives: the album is delivered and still mapped.
    assert after.link_id == 42 and after.part_index == 0

    # A bound part is not something to give up on.
    await _expected(groups, link_id=42, index=1)
    bound = await groups.part_at(namespace, 1)
    assert bound is not None
    await groups.attach_owner_message(bound.id, account_id=OWNER, owner_message_id=900)
    assert await groups.stop_binding(bound.id) is False


# ------------------------------------------------------------------- fingerprints


def test_the_caption_position_is_part_of_the_structure() -> None:
    """The live probe found the caption on part 1 of 3, not on part 0.

    A fingerprint that ignored where the caption sits would call two different
    albums the same shape, which is exactly the mismatch this guards.
    """
    with_caption_first = album_part_fingerprint("photo", part_index=0, caption="привет")
    with_caption_second = album_part_fingerprint("photo", part_index=1, caption="привет")
    without = album_part_fingerprint("photo", part_index=0, caption=None)

    assert with_caption_first != with_caption_second
    assert with_caption_first != without
    # An empty caption is not the same as no caption at all.
    assert album_part_fingerprint("photo", part_index=0, caption="") != without


def test_identical_parts_are_deliberately_identical() -> None:
    """Two byte-identical photos in one album hash the same, and must.

    The probe confirmed there is no content signal that separates them, so the
    fingerprint does not pretend to be one: it confirms a candidate's structure,
    and `part_index` is what says which of the two this is.
    """
    assert album_part_fingerprint("photo", part_index=0, caption=None) == (
        album_part_fingerprint("photo", part_index=0, caption=None)
    )
    assert album_part_fingerprint("video", part_index=0, caption=None) != (
        album_part_fingerprint("photo", part_index=0, caption=None)
    )


def test_the_namespaces_separate_what_could_otherwise_collide() -> None:
    """`grouped_id` is unique per chat, not per account, and never across both."""
    assert owner_album_namespace(1, 2, 9911) != owner_album_namespace(1, 3, 9911)
    assert owner_album_namespace(1, 2, 9911) != owner_album_namespace(4, 2, 9911)
    assert expected_album_namespace(42) != owner_album_namespace(1, 2, 42)


async def test_a_part_keeps_what_was_written_about_it(database: Database) -> None:
    """The payload is the caller's, stored and returned unchanged."""
    groups = MediaGroupRepository(database)
    await groups.add_part(
        media_group_id="17983",
        bridge_name="dad",
        bot_id=BOT,
        telegram_message_id=11,
        payload={"file_id": "AgACAgIAAx", "file_name": "фото.jpg"},
    )
    part = (await groups.parts("17983"))[0]
    assert json.loads(part.payload_json) == {
        "file_id": "AgACAgIAAx",
        "file_name": "фото.jpg",
    }


# ------------------------------------- MAX→TG: the aliases written before a send


class _Lookup:
    def bridge_for_bot(self, bot_id: int) -> BridgeTarget | None:
        return BridgeTarget(name="mom", max_chat_id=555, bot_id=BOT) if bot_id == BOT else None

    def bridge_for_max_chat(self, max_chat_id: int) -> BridgeTarget | None:
        return BridgeTarget(name="mom", max_chat_id=555, bot_id=BOT)


class _Dummy:
    async def send_text(self, *a: Any, **k: Any) -> int | None:
        return 1

    async def send_media(self, *a: Any, **k: Any) -> int | None:
        return 1

    async def edit_text(self, *a: Any, **k: Any) -> None:
        return None

    async def delete_messages(self, *a: Any, **k: Any) -> None:
        return None


def _max_message(*kinds: AttachmentKind, text: str = "") -> Any:
    base = normalize_message(
        {"id": 42, "chatId": 555, "sender": 99, "text": text, "time": 1_785_000_000_000},
        own_user_id=1,
    )
    return replace(
        base,
        attachments=tuple(MaxAttachment(kind=kind, raw={}) for kind in kinds),
    )


async def _routed(database: Database, message: Any) -> tuple[Any, list[dict[str, Any]]]:
    """Drive `on_max_message` with a sender that records and never answers."""
    sent: list[dict[str, Any]] = []

    async def send(kind: str, direction: Direction, payload: dict[str, Any], hook: Any) -> int:
        sent.append(payload)
        raise ConnectionError("Telegram is down")

    albums = MediaGroupRepository(database)
    router = BridgeRouter(
        lookup=_Lookup(),
        telegram=_Dummy(),
        max_sender=_Dummy(),
        messages=MessageMapRepository(database),
        state=BridgeStateRepository(database),
        owner_chat_id=OWNER,
        pipe=DeliveryPipe(outbox=OutboxRepository(database), send=send),
        media=_Dummy(),
        albums=albums,
    )
    await router.on_max_message(message)
    return albums, sent


async def test_the_parts_are_written_before_the_first_telegram_call(
    database: Database,
) -> None:
    """The send fails, so nothing reached Telegram — and the aliases are there.

    That ordering is the whole guarantee, the same one the claim itself rests on:
    by the time any echo of this album can exist, the rows it will be matched
    against are already on disk, whichever side of the race lands first and
    whatever the process does in between.
    """
    albums, sent = await _routed(
        database, _max_message(AttachmentKind.PHOTO, AttachmentKind.PHOTO, AttachmentKind.VIDEO)
    )
    assert sent, "the send was attempted"

    messages = MessageMapRepository(database)
    link = await messages.by_max_message(555, 42, BOT)
    assert link is not None and link.telegram_message_id is None

    parts = await albums.parts_of_link(link.id)
    assert [part.part_index for part in parts] == [0, 1, 2]
    assert [part.media_kind for part in parts] == ["photo", "photo", "video"]
    assert all(part.direction is Direction.MAX_TO_TG for part in parts)
    assert all(part.telegram_message_id is None for part in parts)
    assert all(part.media_group_id == expected_album_namespace(link.id) for part in parts)


async def test_the_caption_rides_on_the_head_part(database: Database) -> None:
    """Outgoing is the direction where the position is known: Telegram shows an
    album's caption on its first item, so that is where the sender puts it."""
    albums, _ = await _routed(
        database, _max_message(AttachmentKind.PHOTO, AttachmentKind.PHOTO, text="привет")
    )
    link = await MessageMapRepository(database).by_max_message(555, 42, BOT)
    assert link is not None

    parts = await albums.parts_of_link(link.id)
    assert [part.caption_present for part in parts] == [True, False]
    assert parts[0].part_fingerprint != parts[1].part_fingerprint


async def test_a_lone_attachment_writes_no_aliases(database: Database) -> None:
    albums, _ = await _routed(database, _max_message(AttachmentKind.PHOTO))
    link = await MessageMapRepository(database).by_max_message(555, 42, BOT)
    assert link is not None
    assert await albums.parts_of_link(link.id) == []


async def test_re_entering_the_send_writes_no_second_set(database: Database) -> None:
    """A crash before the job leaves the aliases; the retry finds them there."""
    albums, _ = await _routed(database, _max_message(AttachmentKind.PHOTO, AttachmentKind.PHOTO))
    link = await MessageMapRepository(database).by_max_message(555, 42, BOT)
    assert link is not None
    before = await albums.parts_of_link(link.id)

    await _routed(database, _max_message(AttachmentKind.PHOTO, AttachmentKind.PHOTO))

    after = await albums.parts_of_link(link.id)
    assert [part.id for part in after] == [part.id for part in before]
