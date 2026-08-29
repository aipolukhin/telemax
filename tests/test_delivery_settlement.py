"""The one moment a delivered MAX→TG message gets its bot-side id, held shut.

The regression this guards is not hypothetical: `attach_telegram_message` used to
run in the router, on the inline path only, so anything the retry worker
delivered after a failed first attempt sat in the chat with no Telegram id in the
map at all — and a reply to it resolved to nothing for ever. It was fixed by
moving the write inside the sender both paths share, and a fix that is only held
up by types and a green suite is one refactor away from coming back.

So this reads the production sender to prove it still routes through the named
helper, and then drives the real worker through that same helper: the ordering it
depends on, its idempotence, and what each crash window actually leaves behind.
"""

from __future__ import annotations

import ast
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from bridge.max_client import AttachmentKind
from bridge.media.delivery import DeliveredPart, DeliveryReceipt
from bridge.retry.worker import OutboxWorker
from bridge.routing.delivery import KIND_MAX_TO_TG_TEXT, UnconfirmedDeliveryError
from bridge.routing.echo import album_part_fingerprint, expected_album_namespace
from bridge.service.runtime import settle_telegram_delivery_mapping
from bridge.storage import (
    AlbumSettlementRepository,
    BridgeStateRepository,
    Database,
    Direction,
    MediaGroupRepository,
    MessageMapRepository,
    OutboxRepository,
    OutboxState,
)

BRIDGE = "mom"
BOT = 9000000001
MAX_CHAT = 555
OWNER_CHAT = 4242
_SOURCE = Path(__file__).resolve().parent.parent / "bridge"
RUNTIME = _SOURCE / "service" / "runtime.py"
DELIVERY = _SOURCE / "routing" / "delivery.py"
WORKER = _SOURCE / "retry" / "worker.py"


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.connect(tmp_path / "bridge.db")
    try:
        yield database
    finally:
        await database.close()


async def _claimed(messages: MessageMapRepository, *, max_message_id: int = 1) -> int:
    link = await messages.claim_from_max(
        bridge_name=BRIDGE,
        max_chat_id=MAX_CHAT,
        max_message_id=max_message_id,
        telegram_bot_id=BOT,
        telegram_chat_id=OWNER_CHAT,
        echo_fingerprint="t1:whatever",
    )
    assert link is not None
    return link


def _function(name: str, tree: ast.AST) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not in {RUNTIME.name} any more — has it been renamed?")


def _first_call_line(node: ast.AST, name: str) -> int | None:
    """Where a call to `name` first appears inside `node`, by line."""
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Call):
            continue
        target = inner.func
        called = (
            target.id
            if isinstance(target, ast.Name)
            else target.attr
            if isinstance(target, ast.Attribute)
            else None
        )
        if called == name:
            return int(inner.lineno)
    return None


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call):
            target = inner.func
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


# ------------------------------------------------- the production call path


def test_the_real_sender_settles_through_the_named_helper() -> None:
    """`send_job` is the only sender the queue has — the inline attempt and the
    retry worker both go through it. Whatever it does about the mapping is what
    production does, so this reads it rather than trusting a description of it."""
    tree = ast.parse(RUNTIME.read_text(encoding="utf-8"))
    sender = _function("send_job", tree)
    settle = _function("settled_max_to_tg", sender)

    assert "settle_telegram_delivery_mapping" in _called_names(settle), (
        "the sender must settle through the importable helper, not inline the write"
    )
    assert "attach_telegram_message" not in _called_names(sender), (
        "no second way to write the mapping alongside the helper"
    )


def test_every_delivered_max_to_tg_send_goes_through_the_settle_step() -> None:
    """Both MAX→TG kinds, both success paths. A branch that returns the id
    without settling is a message delivered into a chat and lost to the map."""
    tree = ast.parse(RUNTIME.read_text(encoding="utf-8"))
    sender = _function("send_job", tree)

    settled = 0
    for node in ast.walk(sender):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        if isinstance(node.value, ast.Await) and isinstance(node.value.value, ast.Call):
            call = node.value.value
            if isinstance(call.func, ast.Name) and call.func.id == "settled_max_to_tg":
                settled += 1
    assert settled == 2, "text and media, each settling exactly once before returning"


def test_the_helper_is_reachable_without_starting_the_service() -> None:
    """It is a module-level function precisely so it can be called by a test;
    a closure inside `start()` could only ever be checked indirectly."""
    assert callable(settle_telegram_delivery_mapping)


# ------------------------------------------------------- the worker's delivery


async def _worker(db: Database, deliver: Any) -> OutboxWorker:
    return OutboxWorker(
        bridge_name=BRIDGE,
        outbox=OutboxRepository(db),
        state=BridgeStateRepository(db),
        deliver=deliver,
    )


async def _queued(outbox: OutboxRepository, link_id: int, *, source_key: str = "max:555:1") -> int:
    return await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={"text": "от контакта", "link_id": link_id, "bot_id": BOT},
        source_key=source_key,
    )


async def test_a_worker_delivery_fills_in_the_bot_side_id(db: Database) -> None:
    """The defect itself: delivered by the worker, and mapped for it."""
    messages, outbox = MessageMapRepository(db), OutboxRepository(db)
    link = await _claimed(messages)
    await _queued(outbox, link)

    async def deliver(kind: str, direction: Direction, payload: dict[str, Any], hook: Any) -> int:
        return await settle_telegram_delivery_mapping(
            messages, payload, telegram_message_id=7001
        )

    await (await _worker(db, deliver)).drain_once()

    row = await messages.by_max_message(MAX_CHAT, 1, BOT)
    assert row is not None and row.telegram_message_id == 7001
    job = await outbox.by_source_key("max:555:1")
    assert job is not None and job.state is OutboxState.DONE
    assert job.remote_message_id == 7001


async def test_the_mapping_is_written_before_the_payload_is_cleared(db: Database) -> None:
    """`link_id` lives in the payload, and `mark_done` clears the payload in the
    same statement that records the delivery. Settling after that would have
    nothing left to say *which* mapping the message belonged to."""
    messages, outbox = MessageMapRepository(db), OutboxRepository(db)
    link = await _claimed(messages)
    await _queued(outbox, link)
    seen: list[dict[str, Any]] = []

    async def deliver(kind: str, direction: Direction, payload: dict[str, Any], hook: Any) -> int:
        seen.append(dict(payload))
        during = await messages.by_max_message(MAX_CHAT, 1, BOT)
        assert during is not None and during.telegram_message_id is None
        return await settle_telegram_delivery_mapping(
            messages, payload, telegram_message_id=7001
        )

    await (await _worker(db, deliver)).drain_once()

    assert seen and seen[0]["link_id"] == link, "the sender still had it in hand"
    job = await outbox.by_source_key("max:555:1")
    assert job is not None and json.loads(job.payload_json) == {}, "and only then dropped"
    row = await messages.by_max_message(MAX_CHAT, 1, BOT)
    assert row is not None and row.telegram_message_id == 7001


async def test_a_delivery_with_no_mapping_row_settles_quietly(db: Database) -> None:
    """Notices and the guardian's own messages have nothing to attach to."""
    messages = MessageMapRepository(db)
    assert await settle_telegram_delivery_mapping(
        messages, {"text": "статус"}, telegram_message_id=7001
    ) == 7001
    assert await settle_telegram_delivery_mapping(
        None, {"link_id": 1}, telegram_message_id=7001
    ) == 7001


# ------------------------------------------------------------- idempotence


async def test_settling_twice_with_the_same_id_changes_nothing(db: Database) -> None:
    messages = MessageMapRepository(db)
    link = await _claimed(messages)
    payload = {"link_id": link}

    await settle_telegram_delivery_mapping(messages, payload, telegram_message_id=7001)
    await settle_telegram_delivery_mapping(messages, payload, telegram_message_id=7001)

    row = await messages.by_max_message(MAX_CHAT, 1, BOT)
    assert row is not None and row.telegram_message_id == 7001


async def test_a_conflicting_id_never_replaces_one_already_proved(db: Database) -> None:
    """The column is filled only while empty. A disagreeing id is left alone —
    the unique index on (bot, message id) would refuse it anyway, and quietly
    moving a mapping is worse than not writing one."""
    messages = MessageMapRepository(db)
    link = await _claimed(messages)

    await settle_telegram_delivery_mapping(messages, {"link_id": link}, telegram_message_id=7001)
    await settle_telegram_delivery_mapping(messages, {"link_id": link}, telegram_message_id=7002)

    row = await messages.by_max_message(MAX_CHAT, 1, BOT)
    assert row is not None and row.telegram_message_id == 7001


# --------------------------------------------------------------- crash windows


async def test_a_crash_between_the_send_and_the_attach_never_resends(db: Database) -> None:
    """Died with Telegram's answer in hand and nothing written down. The send had
    started, so lease recovery does *not* put it back on the queue: it becomes
    AMBIGUOUS for the owner to judge. That is the point — the message is in the
    chat, and a second copy would be worse than a missing mapping."""
    messages, outbox = MessageMapRepository(db), OutboxRepository(db)
    link = await _claimed(messages)
    job = await _queued(outbox, link)

    sends = 0

    async def deliver(kind: str, direction: Direction, payload: dict[str, Any], hook: Any) -> int:
        nonlocal sends
        sends += 1
        await hook()  # Telegram was asked
        raise SystemExit("killed with the answer in hand, before settling")

    worker = await _worker(db, deliver)
    try:
        await worker.drain_once()
    except SystemExit:
        pass  # the process died here

    await outbox.reclaim_expired_leases()  # what the next process does
    async with db.transaction() as connection:
        await connection.execute("UPDATE outbox SET lease_expires_at = 1 WHERE id = ?", (job,))
    requeued, ambiguous = await outbox.reclaim_expired_leases()

    assert (requeued, ambiguous) == (0, 1), "the send had started: judged, not repeated"
    recovered = await outbox.by_source_key("max:555:1")
    assert recovered is not None and recovered.state is OutboxState.AMBIGUOUS
    row = await messages.by_max_message(MAX_CHAT, 1, BOT)
    assert row is not None and row.telegram_message_id is None, "unmapped, and honestly so"

    await worker.drain_once()
    assert sends == 1, "never sent a second time"


async def test_a_crash_between_the_attach_and_done_leaves_the_mapping_right(
    db: Database,
) -> None:
    """The other side of the window: the mapping landed, the job did not. The
    write survives, and settling it again on the way through is a no-op."""
    messages, outbox = MessageMapRepository(db), OutboxRepository(db)
    link = await _claimed(messages)
    await _queued(outbox, link)

    async def deliver(kind: str, direction: Direction, payload: dict[str, Any], hook: Any) -> int:
        await hook()
        await settle_telegram_delivery_mapping(messages, payload, telegram_message_id=7001)
        raise SystemExit("killed after settling, before the job was closed")

    worker = await _worker(db, deliver)
    try:
        await worker.drain_once()
    except SystemExit:
        pass

    row = await messages.by_max_message(MAX_CHAT, 1, BOT)
    assert row is not None and row.telegram_message_id == 7001, "durable across the crash"

    # A later pass settles the same message again, as a recovery would.
    await settle_telegram_delivery_mapping(
        messages, {"link_id": link}, telegram_message_id=7001
    )
    again = await messages.by_max_message(MAX_CHAT, 1, BOT)
    assert again is not None and again.telegram_message_id == 7001
    job = await outbox.by_source_key("max:555:1")
    assert job is not None and job.state is OutboxState.INFLIGHT, "still owed a verdict"


async def test_settling_after_the_job_is_done_is_a_no_op(db: Database) -> None:
    messages, outbox = MessageMapRepository(db), OutboxRepository(db)
    link = await _claimed(messages)
    job = await _queued(outbox, link)
    await settle_telegram_delivery_mapping(messages, {"link_id": link}, telegram_message_id=7001)
    await outbox.mark_done(job, remote_message_id=7001)

    stored = await outbox.by_source_key("max:555:1")
    assert stored is not None
    await settle_telegram_delivery_mapping(
        messages, json.loads(stored.payload_json), telegram_message_id=7001
    )

    row = await messages.by_max_message(MAX_CHAT, 1, BOT)
    assert row is not None and row.telegram_message_id == 7001
    assert stored.state is OutboxState.DONE


async def test_both_paths_settle_through_one_contract(db: Database) -> None:
    """Inline and worker are the same function with the same payload, so the
    mapping cannot depend on which one happened to carry the message."""
    messages = MessageMapRepository(db)
    inline_link = await _claimed(messages, max_message_id=1)
    worker_link = await _claimed(messages, max_message_id=2)

    await settle_telegram_delivery_mapping(
        messages, {"link_id": inline_link, "text": "inline"}, telegram_message_id=7001
    )
    await settle_telegram_delivery_mapping(
        messages, {"link_id": worker_link, "text": "worker"}, telegram_message_id=7002
    )

    first = await messages.by_max_message(MAX_CHAT, 1, BOT)
    second = await messages.by_max_message(MAX_CHAT, 2, BOT)
    assert first is not None and first.telegram_message_id == 7001
    assert second is not None and second.telegram_message_id == 7002


# ------------------------------------------------------ albums: part by part

ALBUM_KINDS = (AttachmentKind.PHOTO, AttachmentKind.PHOTO, AttachmentKind.PHOTO)


async def _expected_album(
    albums: MediaGroupRepository, link_id: int, *, parts: int = 3
) -> str:
    """The aliases the sender writes before the first Telegram call."""
    namespace = expected_album_namespace(link_id)
    for index in range(parts):
        await albums.add_part(
            media_group_id=namespace,
            bridge_name=BRIDGE,
            bot_id=BOT,
            payload={"kind": "photo"},
            link_id=link_id,
            direction=Direction.MAX_TO_TG,
            part_index=index,
            media_kind="photo",
            caption_present=index == 0,
            part_fingerprint=album_part_fingerprint(
                "photo", part_index=index, caption="подпись" if index == 0 else None
            ),
        )
    return namespace


def _receipt(*message_ids: int) -> DeliveryReceipt:
    return DeliveryReceipt(
        head=message_ids[0] if message_ids else None,
        album=tuple(
            DeliveredPart(message_id, AttachmentKind.PHOTO, index)
            for index, message_id in enumerate(message_ids)
        ),
        media_group_id="13984172040192",
    )


async def test_three_ids_land_on_the_three_expected_parts(db: Database) -> None:
    """The array Telegram returns, matched to the aliases by position.

    Position is the only thing that can match them: an album's parts can be
    byte-identical, and the probe found nothing in the content that separates
    them. What binds part 2 is that it is part 2.
    """
    messages, albums = MessageMapRepository(db), MediaGroupRepository(db)
    settlement = AlbumSettlementRepository(db)
    link = await _claimed(messages)
    namespace = await _expected_album(albums, link)

    await settle_telegram_delivery_mapping(
        messages,
        {"link_id": link},
        telegram_message_id=7001,
        receipt=_receipt(7001, 7002, 7003),
        albums=settlement,
    )

    parts = await albums.parts_of_link(link)
    assert [part.part_index for part in parts] == [0, 1, 2]
    assert [part.telegram_message_id for part in parts] == [7001, 7002, 7003]
    # And the canonical row still carries the head, exactly as before.
    row = await messages.by_max_message(MAX_CHAT, 1, BOT)
    assert row is not None and row.telegram_message_id == 7001
    assert await albums.link_of(namespace) == link


async def test_a_repeated_settlement_binds_nothing_new(db: Database) -> None:
    """The crash between the attach and `mark_done` is survived by this."""
    messages, albums = MessageMapRepository(db), MediaGroupRepository(db)
    settlement = AlbumSettlementRepository(db)
    link = await _claimed(messages)
    await _expected_album(albums, link)

    for _pass in range(2):
        await settle_telegram_delivery_mapping(
            messages,
            {"link_id": link},
            telegram_message_id=7001,
            receipt=_receipt(7001, 7002, 7003),
            albums=settlement,
        )

    parts = await albums.parts_of_link(link)
    assert [part.telegram_message_id for part in parts] == [7001, 7002, 7003]


async def test_a_disagreeing_id_is_refused_rather_than_moved(db: Database) -> None:
    """A part already bound was bound from evidence this call does not have."""
    messages, albums = MessageMapRepository(db), MediaGroupRepository(db)
    settlement = AlbumSettlementRepository(db)
    link = await _claimed(messages)
    await _expected_album(albums, link)
    await settle_telegram_delivery_mapping(
        messages,
        {"link_id": link},
        telegram_message_id=7001,
        receipt=_receipt(7001, 7002, 7003),
        albums=settlement,
    )

    with pytest.raises(UnconfirmedDeliveryError):
        await settle_telegram_delivery_mapping(
            messages,
            {"link_id": link},
            telegram_message_id=7001,
            receipt=_receipt(7001, 9002, 9003),
            albums=settlement,
        )

    parts = await albums.parts_of_link(link)
    assert [part.telegram_message_id for part in parts] == [7001, 7002, 7003]


async def test_a_short_answer_binds_nothing_and_asks_for_the_owner(db: Database) -> None:
    """Two ids for three parts: which alias went missing is not guessable.

    Binding the first two in order would map somebody's third photo to their
    second. The count is checked before a single write, so the aliases are left
    untouched and the job reaches the owner's attention instead.
    """
    messages, albums = MessageMapRepository(db), MediaGroupRepository(db)
    settlement = AlbumSettlementRepository(db)
    link = await _claimed(messages)
    await _expected_album(albums, link)

    with pytest.raises(UnconfirmedDeliveryError, match="2 message"):
        await settle_telegram_delivery_mapping(
            messages,
            {"link_id": link},
            telegram_message_id=7001,
            receipt=_receipt(7001, 7002),
            albums=settlement,
        )

    parts = await albums.parts_of_link(link)
    assert [part.telegram_message_id for part in parts] == [None, None, None]


async def test_a_longer_answer_is_refused_too(db: Database) -> None:
    messages, albums = MessageMapRepository(db), MediaGroupRepository(db)
    settlement = AlbumSettlementRepository(db)
    link = await _claimed(messages)
    await _expected_album(albums, link, parts=2)

    with pytest.raises(UnconfirmedDeliveryError):
        await settle_telegram_delivery_mapping(
            messages,
            {"link_id": link},
            telegram_message_id=7001,
            receipt=_receipt(7001, 7002, 7003),
            albums=settlement,
        )


async def test_a_mismatched_album_is_never_sent_again(db: Database) -> None:
    """AMBIGUOUS, not PENDING: the album is in the chat, and only the mapping is
    incomplete. Retrying would put a second copy of it in front of a person."""
    messages, albums = MessageMapRepository(db), MediaGroupRepository(db)
    settlement = AlbumSettlementRepository(db)
    outbox = OutboxRepository(db)
    link = await _claimed(messages)
    await _expected_album(albums, link)
    await _queued(outbox, link)
    sends = 0

    async def deliver(kind: str, direction: Direction, payload: dict[str, Any], hook: Any) -> int:
        nonlocal sends
        sends += 1
        await hook()
        return await settle_telegram_delivery_mapping(
            messages,
            payload,
            telegram_message_id=7001,
            receipt=_receipt(7001, 7002),
            albums=settlement,
        )

    worker = await _worker(db, deliver)
    await worker.drain_once()

    job = await outbox.by_source_key("max:555:1")
    assert job is not None and job.state is OutboxState.AMBIGUOUS
    await worker.drain_once()
    assert sends == 1


async def test_a_single_message_receipt_touches_no_aliases(db: Database) -> None:
    """Text and one attachment settle exactly as they did before the receipt."""
    messages, albums = MessageMapRepository(db), MediaGroupRepository(db)
    settlement = AlbumSettlementRepository(db)
    link = await _claimed(messages)

    assert (
        await settle_telegram_delivery_mapping(
            messages,
            {"link_id": link},
            telegram_message_id=7001,
            receipt=DeliveryReceipt(head=7001),
            albums=settlement,
        )
        == 7001
    )
    row = await messages.by_max_message(MAX_CHAT, 1, BOT)
    assert row is not None and row.telegram_message_id == 7001
    assert await albums.parts_of_link(link) == []


async def test_a_crash_after_the_parts_are_bound_keeps_them(db: Database) -> None:
    """Bound before `mark_done`, so the window between them costs nothing."""
    messages, albums = MessageMapRepository(db), MediaGroupRepository(db)
    settlement = AlbumSettlementRepository(db)
    outbox = OutboxRepository(db)
    link = await _claimed(messages)
    await _expected_album(albums, link)
    await _queued(outbox, link)

    async def deliver(kind: str, direction: Direction, payload: dict[str, Any], hook: Any) -> int:
        await hook()
        await settle_telegram_delivery_mapping(
            messages,
            payload,
            telegram_message_id=7001,
            receipt=_receipt(7001, 7002, 7003),
            albums=settlement,
        )
        raise SystemExit("killed after the parts were bound, before the job closed")

    worker = await _worker(db, deliver)
    try:
        await worker.drain_once()
    except SystemExit:
        pass

    parts = await albums.parts_of_link(link)
    assert [part.telegram_message_id for part in parts] == [7001, 7002, 7003]
    job = await outbox.by_source_key("max:555:1")
    assert job is not None and job.state is OutboxState.INFLIGHT


async def test_closing_the_job_does_not_disturb_the_bound_parts(db: Database) -> None:
    """`mark_done` clears the payload; the aliases are rows of their own."""
    messages, albums = MessageMapRepository(db), MediaGroupRepository(db)
    settlement = AlbumSettlementRepository(db)
    outbox = OutboxRepository(db)
    link = await _claimed(messages)
    await _expected_album(albums, link)
    job = await _queued(outbox, link)

    await settle_telegram_delivery_mapping(
        messages,
        {"link_id": link},
        telegram_message_id=7001,
        receipt=_receipt(7001, 7002, 7003),
        albums=settlement,
    )
    await outbox.mark_done(job, remote_message_id=7001)

    parts = await albums.parts_of_link(link)
    assert [part.telegram_message_id for part in parts] == [7001, 7002, 7003]
    assert await albums.by_bot_message(BOT, 7003) is not None


async def test_the_worker_binds_the_parts_the_inline_path_would_have(
    db: Database,
) -> None:
    """One album, delivered by the retry worker after a failed first attempt.

    The defect this whole file exists for, in its album form: anything the worker
    delivered used to reach the chat with nothing in the map, and for an album
    that is three messages nobody can resolve rather than one.
    """
    messages, albums = MessageMapRepository(db), MediaGroupRepository(db)
    settlement = AlbumSettlementRepository(db)
    outbox = OutboxRepository(db)
    link = await _claimed(messages)
    await _expected_album(albums, link)
    await _queued(outbox, link)

    async def deliver(kind: str, direction: Direction, payload: dict[str, Any], hook: Any) -> int:
        await hook()
        return await settle_telegram_delivery_mapping(
            messages,
            payload,
            telegram_message_id=7001,
            receipt=_receipt(7001, 7002, 7003),
            albums=settlement,
        )

    await (await _worker(db, deliver)).drain_once()

    parts = await albums.parts_of_link(link)
    assert [part.telegram_message_id for part in parts] == [7001, 7002, 7003]
    job = await outbox.by_source_key("max:555:1")
    assert job is not None and job.state is OutboxState.DONE


# ------------------------------------------- the production call path, again


def test_no_second_way_to_write_a_part_mapping_exists_beside_the_helper() -> None:
    """An album's parts are bound in one place or in none.

    The same guard the canonical mapping has, for the same reason: a per-part
    write that grew up next to the sender would be a second contract, and the
    two would agree right up until the day one of them was changed.
    """
    tree = ast.parse(RUNTIME.read_text(encoding="utf-8"))
    called = _called_names(_function("send_job", tree))

    assert "attach_bot_message" not in called
    assert "bind_link" not in called
    assert "parts_of_link" not in called
    assert "settle_bot_album" not in called
    assert "settle_owner_album" not in called


def test_only_the_named_helpers_settle_an_albums_parts() -> None:
    """The atomic settlement is reachable from the two named helpers and nowhere
    else, so "one album, one settlement" stays checkable."""
    tree = ast.parse(RUNTIME.read_text(encoding="utf-8"))
    bot = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and "settle_bot_album" in _called_names(node)
    ]
    owner = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and "settle_owner_album" in _called_names(node)
    ]
    assert bot == ["settle_telegram_delivery_mapping"]
    assert owner == ["settle_owner_delivery_mapping"]


def test_an_album_is_settled_inside_one_transaction() -> None:
    """The head and every alias land together or not at all.

    Written as a read of the repository rather than of the runtime: the runtime
    calls one method, and that method is where the atomicity has to live.
    """
    import inspect

    from bridge.storage.repositories import AlbumSettlementRepository

    for method in (
        AlbumSettlementRepository.settle_bot_album,
        AlbumSettlementRepository.settle_owner_album,
    ):
        source = inspect.getsource(method)
        assert source.count("self._db.transaction()") == 1, method.__name__
        # Never through the ordinary repositories: they take the same
        # non-reentrant lock this transaction is already holding.
        assert "attach_bot_message" not in source
        assert "attach_telegram_message" not in source
        assert "attach_owner_message" not in source


def test_the_sender_never_closes_a_job_itself() -> None:
    """`mark_done` belongs to the pipe and the worker, after the sender returns.

    That ordering is what puts every settlement before the payload is cleared. A
    sender that closed its own job could settle afterwards — against a payload
    that no longer says which mapping this was."""
    tree = ast.parse(RUNTIME.read_text(encoding="utf-8"))
    assert "mark_done" not in _called_names(_function("send_job", tree))


def test_both_delivery_paths_settle_before_they_close_the_job() -> None:
    """Read from the two files that actually do it, in source order."""
    pipe = ast.parse(DELIVERY.read_text(encoding="utf-8"))
    attempt = _function("attempt", pipe)
    sent_at = _first_call_line(attempt, "_send")
    done_at = _first_call_line(attempt, "mark_done")
    assert sent_at is not None and done_at is not None and sent_at < done_at

    worker = ast.parse(WORKER.read_text(encoding="utf-8"))
    attempt = _function("_attempt", worker)
    sent_at = _first_call_line(attempt, "_deliver")
    done_at = _first_call_line(attempt, "mark_done")
    assert sent_at is not None and done_at is not None and sent_at < done_at
