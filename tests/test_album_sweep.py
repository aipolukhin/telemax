"""Deleting one part of an album takes the rest of it out of Telegram too.

Reported from the live bridge and reproduced in compatibility fixtures before this existed: an
album of three, one part deleted in Telegram, and the whole MAX message vanished
while the other two parts stayed in the Telegram chat. Half of that is not a bug
and cannot be — MAX carries a group as one message with N attachments and has no
way to remove one of them, so the group goes as a whole or not at all. The half
that *was* wrong is what stayed behind: two photos still sitting in Telegram as a
group whose counterpart no longer exists anywhere, and which the owner never
chose to keep.

So the collapse now runs in both directions. The MAX message goes, as it must,
and the remaining Telegram parts follow it.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from bridge.routing.delivery import (
    KIND_TG_ALBUM_SWEEP,
    KIND_TG_TO_MAX_DELETE,
    DeliveryPipe,
)
from bridge.routing.echo import album_part_fingerprint, expected_album_namespace
from bridge.routing.owner_mutation import album_sweep_source_key, resolve_delete
from bridge.routing.router import BridgeRouter, BridgeTarget
from bridge.storage import (
    BridgeStateRepository,
    Database,
    Direction,
    MediaGroupRepository,
    MessageMapRepository,
    OutboxRepository,
)

ACCOUNT = 100000001
BOT = 9000000001
MAX_CHAT = 555
MAX_MESSAGE = 111411200720896011


class MaxSpy:
    def __init__(self) -> None:
        self.deletes: list[tuple[int, list[int], bool]] = []

    async def send_text(self, *a: Any, **k: Any) -> int | None:
        return 1

    async def send_media(self, *a: Any, **k: Any) -> int | None:
        return 1

    async def edit_text(self, *a: Any, **k: Any) -> None:
        return None

    async def delete_messages(
        self, chat_id: int, message_ids: list[int], *, for_everyone: bool = True
    ) -> None:
        self.deletes.append((chat_id, list(message_ids), for_everyone))


class _Lookup:
    def bridge_for_bot(self, bot_id: int) -> BridgeTarget | None:
        return BridgeTarget("mom", MAX_CHAT, BOT) if bot_id == BOT else None

    def bridge_for_max_chat(self, max_chat_id: int) -> BridgeTarget | None:
        return None


class Live:
    """Router, repositories and a sender shaped like the runtime's `send_job`."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.messages = MessageMapRepository(database)
        self.albums = MediaGroupRepository(database)
        self.outbox = OutboxRepository(database)
        self.max = MaxSpy()
        #: What the owner's session was asked to remove from Telegram.
        self.swept: list[tuple[int, list[int]]] = []
        self.sweep_fails: Exception | None = None
        self.router = BridgeRouter(
            lookup=_Lookup(),
            telegram=MaxSpy(),
            max_sender=self.max,
            messages=self.messages,
            state=BridgeStateRepository(database),
            owner_chat_id=ACCOUNT,
            pipe=DeliveryPipe(outbox=self.outbox, send=self._send),
            albums=self.albums,
        )

    async def _send(
        self, kind: str, direction: Direction, payload: dict[str, Any], sending: Any = None
    ) -> int | None:
        if kind == KIND_TG_TO_MAX_DELETE:
            return await resolve_delete(
                outbox=self.outbox,
                messages=self.messages,
                max_sender=self.max,
                payload=payload,
            )
        if kind == KIND_TG_ALBUM_SWEEP:
            if self.sweep_fails is not None:
                raise self.sweep_fails
            self.swept.append(
                (int(payload["peer_id"]), [int(v) for v in payload["owner_message_ids"]])
            )
            return None
        return 1

    async def album(self, *owner_ids: int, max_message_id: int = MAX_MESSAGE) -> int:
        """One delivered album: a canonical row plus a bound alias per part."""
        link_id = await self.messages.record_from_telegram(
            bridge_name="mom",
            max_chat_id=MAX_CHAT,
            telegram_bot_id=BOT,
            telegram_chat_id=BOT,
            telegram_message_id=None,
            max_message_id=max_message_id,
            telegram_owner_message_id=owner_ids[0],
            telegram_owner_account_id=ACCOUNT,
        )
        namespace = expected_album_namespace(link_id)
        for index, owner_id in enumerate(owner_ids):
            await self.albums.add_part(
                media_group_id=namespace,
                bridge_name="mom",
                bot_id=BOT,
                payload={"kind": "photo"},
                link_id=link_id,
                direction=Direction.TG_TO_MAX,
                part_index=index,
                media_kind="photo",
                caption_present=index == 0,
                part_fingerprint=album_part_fingerprint("photo", part_index=index, caption=None),
                telegram_owner_account_id=ACCOUNT,
                telegram_owner_message_id=owner_id,
            )
        return link_id

    async def single(self, owner_id: int, *, max_message_id: int) -> int:
        return await self.messages.record_from_telegram(
            bridge_name="mom",
            max_chat_id=MAX_CHAT,
            telegram_bot_id=BOT,
            telegram_chat_id=BOT,
            telegram_message_id=None,
            max_message_id=max_message_id,
            telegram_owner_message_id=owner_id,
            telegram_owner_account_id=ACCOUNT,
        )

    async def jobs(self, kind: str) -> list[dict[str, Any]]:
        rows = await self.database.query(
            "SELECT id, state, source_key, payload_json FROM outbox WHERE kind = ? ORDER BY id",
            (kind,),
        )
        return [dict(row) for row in rows]


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> Any:
    """Just the storage, for the sweep's own guards — no router in the way."""
    db = await Database.connect(tmp_path / "sweep.db")
    try:
        yield db
    finally:
        await db.close()


@pytest_asyncio.fixture
async def live(tmp_path: Path) -> Any:
    database = await Database.connect(tmp_path / "bridge.db")
    try:
        yield Live(database)
    finally:
        await database.close()


# ------------------------------------------------------------ the reported bug


async def test_deleting_one_part_removes_the_rest_from_telegram(live: Any) -> None:
    """The whole of the fix, in the shape it was reported in.

    Before this, MAX lost the album and Telegram kept two thirds of it. Now the
    two sides end up saying the same thing.
    """
    await live.album(1002043, 1002044, 1002045)

    await live.router.on_owner_delete(owner_account_id=ACCOUNT, owner_message_ids=[1002044])

    assert live.max.deletes == [(MAX_CHAT, [MAX_MESSAGE], True)], "MAX loses it whole"
    assert live.swept == [(BOT, [1002043, 1002044, 1002045])], "and so does Telegram"


async def test_the_part_that_started_it_is_listed_too(live: Any) -> None:
    """Deleting a message that is already gone is a no-op, and a list that tried
    to be clever about which parts survive would be wrong the moment two
    deletions raced."""
    await live.album(1002043, 1002044, 1002045)
    await live.router.on_owner_delete(owner_account_id=ACCOUNT, owner_message_ids=[1002043])

    assert live.swept[0][1] == [1002043, 1002044, 1002045]


async def test_the_sweep_is_a_durable_job_not_a_side_effect(live: Any) -> None:
    """It is a remote effect, so it survives the process that decided on it."""
    link = await live.album(1002043, 1002044)
    await live.router.on_owner_delete(owner_account_id=ACCOUNT, owner_message_ids=[1002043])

    jobs = await live.jobs(KIND_TG_ALBUM_SWEEP)
    assert len(jobs) == 1
    assert jobs[0]["source_key"] == album_sweep_source_key(expected_album_namespace(link))
    assert json.loads(jobs[0]["payload_json"] or "{}").get("peer_id", BOT) == BOT


# --------------------------------------------------------------- no runaway


async def test_the_sweeps_own_deletions_do_not_ask_for_another(live: Any) -> None:
    """The sweep deletes; those deletions come back as owner-side updates and
    resolve to the same album. The source key is what ends the loop."""
    await live.album(1002043, 1002044, 1002045)
    await live.router.on_owner_delete(owner_account_id=ACCOUNT, owner_message_ids=[1002044])

    # Telegram now reports the two the sweep removed, and a catch-up repeats it.
    await live.router.on_owner_delete(
        owner_account_id=ACCOUNT, owner_message_ids=[1002043, 1002045]
    )
    await live.router.on_owner_delete(
        owner_account_id=ACCOUNT, owner_message_ids=[1002045, 1002043, 1002044]
    )

    assert len(await live.jobs(KIND_TG_ALBUM_SWEEP)) == 1
    assert len(await live.jobs(KIND_TG_TO_MAX_DELETE)) == 1
    assert live.max.deletes == [(MAX_CHAT, [MAX_MESSAGE], True)]
    assert len(live.swept) == 1


async def test_a_whole_batch_sweeps_once(live: Any) -> None:
    await live.album(1002043, 1002044, 1002045)
    await live.router.on_owner_delete(
        owner_account_id=ACCOUNT, owner_message_ids=[1002043, 1002044, 1002045]
    )

    assert len(live.swept) == 1
    assert len(await live.jobs(KIND_TG_ALBUM_SWEEP)) == 1


# ------------------------------------------------------- what must not sweep


async def test_a_single_message_sweeps_nothing(live: Any) -> None:
    """Nothing was collapsed, so nothing trails behind it."""
    await live.single(1002050, max_message_id=111411200655360010)
    await live.router.on_owner_delete(owner_account_id=ACCOUNT, owner_message_ids=[1002050])

    assert live.max.deletes == [(MAX_CHAT, [111411200655360010], True)]
    assert live.swept == []
    assert await live.jobs(KIND_TG_ALBUM_SWEEP) == []


async def test_an_album_with_no_owner_ids_is_left_alone(live: Any) -> None:
    """Its echo never arrived, so there is nothing this could name.

    Deleting by guess is the one thing worse than leaving the parts: the ids
    would have to come from somewhere, and the only somewhere is a guess.
    """
    link_id = await live.messages.record_from_telegram(
        bridge_name="mom",
        max_chat_id=MAX_CHAT,
        telegram_bot_id=BOT,
        telegram_chat_id=BOT,
        telegram_message_id=None,
        max_message_id=MAX_MESSAGE,
        telegram_owner_message_id=1002060,
        telegram_owner_account_id=ACCOUNT,
    )
    namespace = expected_album_namespace(link_id)
    for index in range(2):
        await live.albums.add_part(
            media_group_id=namespace,
            bridge_name="mom",
            bot_id=BOT,
            payload={"kind": "photo"},
            link_id=link_id,
            direction=Direction.MAX_TO_TG,
            part_index=index,
            media_kind="photo",
            caption_present=index == 0,
            part_fingerprint=album_part_fingerprint("photo", part_index=index, caption=None),
        )

    await live.router.on_owner_delete(owner_account_id=ACCOUNT, owner_message_ids=[1002060])

    assert live.swept == []
    assert await live.jobs(KIND_TG_ALBUM_SWEEP) == []


async def test_an_unmapped_id_sweeps_nothing(live: Any) -> None:
    await live.router.on_owner_delete(owner_account_id=ACCOUNT, owner_message_ids=[999999])
    assert live.swept == [] and live.max.deletes == []


# ------------------------------------------------------------- when it fails


async def test_a_missing_session_leaves_the_sweep_owed(live: Any) -> None:
    """Retryable, not failed: the deletion is still owed once the session is back.

    Landing it on the owner's `/failed` list would ask them to act on something
    that fixes itself the moment the transport reconnects.
    """
    from bridge.retry.worker import classify

    live.sweep_fails = RuntimeError("the Telegram user session is not connected")
    await live.album(1002043, 1002044)
    await live.router.on_owner_delete(owner_account_id=ACCOUNT, owner_message_ids=[1002043])

    jobs = await live.jobs(KIND_TG_ALBUM_SWEEP)
    assert len(jobs) == 1 and jobs[0]["state"] == "pending"
    retryable, _forced = classify(live.sweep_fails)
    assert retryable, "a disconnected session must not burn the job"
    # And MAX was still cleared: the two effects are independent jobs.
    assert live.max.deletes == [(MAX_CHAT, [MAX_MESSAGE], True)]


async def test_the_session_refuses_to_sweep_while_disconnected() -> None:
    """The port raises rather than reporting a success it did not perform."""
    from bridge.telegram.user_session import TelegramUserSession

    async def connect() -> Any:
        raise AssertionError("not connected in this test")

    session = TelegramUserSession(
        connect=connect,
        owner_user_id=ACCOUNT,
        health=SimpleNamespace(
            note_tg_session_connected=None, note_tg_session_disconnected=None
        ),
        allowed_bot_ids=lambda: None,  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="not connected"):
        await session.delete_own_messages(BOT, [1, 2])


async def test_the_session_revokes_for_both_sides() -> None:
    """A half-deleted album on the contact's side is the same untidiness one
    message further along."""
    from bridge.telegram.user_session import TelegramUserSession

    calls: list[tuple[int, list[int], bool]] = []

    class FakeClient:
        async def delete_messages(
            self, peer: int, ids: list[int], *, revoke: bool = False
        ) -> None:
            calls.append((peer, list(ids), revoke))

    async def connect() -> Any:
        raise AssertionError("unused")

    session = TelegramUserSession(
        connect=connect,
        owner_user_id=ACCOUNT,
        health=SimpleNamespace(),
        allowed_bot_ids=lambda: None,  # type: ignore[arg-type]
    )
    session._client = FakeClient()  # the port under test, reached directly
    await session.delete_own_messages(BOT, [1002043, 1002045])

    assert calls == [(BOT, [1002043, 1002045], True)]


# ------------------------------------------------- what the sweep may delete


class SweepSession:
    """The owner's session, as the sweep sees it."""

    def __init__(self, *, connected: bool = True, peers: set[int] | None = None) -> None:
        self.is_connected = connected
        self._peers = {BOT} if peers is None else peers
        self.deleted: list[tuple[int, list[int]]] = []

    async def allowed_peers(self) -> set[int]:
        return self._peers

    async def delete_own_messages(self, peer_id: int, message_ids: list[int]) -> None:
        self.deleted.append((peer_id, list(message_ids)))


async def _album_parts(
    database: Any, *, owner_ids: list[int], account_id: int = ACCOUNT, bot_id: int = BOT
) -> int:
    """One delivered album with bound aliases, and the link they belong to."""
    messages = MessageMapRepository(database)
    albums = MediaGroupRepository(database)
    link_id = await messages.record_from_telegram(
        bridge_name="mom",
        max_chat_id=MAX_CHAT,
        telegram_bot_id=bot_id,
        telegram_chat_id=bot_id,
        telegram_message_id=None,
        max_message_id=MAX_MESSAGE,
        telegram_owner_message_id=owner_ids[0],
        telegram_owner_account_id=account_id,
    )
    for index, owner_id in enumerate(owner_ids):
        await albums.add_part(
            media_group_id=expected_album_namespace(link_id),
            bridge_name="mom",
            bot_id=bot_id,
            payload={"kind": "photo"},
            link_id=link_id,
            direction=Direction.TG_TO_MAX,
            part_index=index,
            media_kind="photo",
            caption_present=index == 0,
            part_fingerprint=album_part_fingerprint("photo", part_index=index, caption=None),
            telegram_owner_account_id=account_id,
            telegram_owner_message_id=owner_id,
        )
    return link_id


def _payload(link_id: int, ids: list[int], *, peer: int = BOT, account: int = ACCOUNT) -> dict:
    return {
        "account_id": account,
        "peer_id": peer,
        "link_id": link_id,
        "owner_message_ids": ids,
    }


async def test_the_sweep_deletes_exactly_what_the_album_still_claims(
    database: Any,
) -> None:
    """The payload is re-derived, not trusted. It has been on disk since it was
    written, and "the producer was right at the time" is not something the
    executor can check."""
    from bridge.service.runtime import sweep_album_in_telegram

    link_id = await _album_parts(database, owner_ids=[9001, 9002, 9003])
    session = SweepSession()

    await sweep_album_in_telegram(
        _payload(link_id, [9001, 9002, 9003]),
        session=session,
        albums=MediaGroupRepository(database),
    )

    assert session.deleted == [(BOT, [9001, 9002, 9003])]


async def test_an_id_the_album_does_not_claim_is_not_deleted(database: Any) -> None:
    """The one that matters. An id from another chat, in a payload, deletes nothing."""
    from bridge.service.runtime import sweep_album_in_telegram

    link_id = await _album_parts(database, owner_ids=[9001, 9002])
    session = SweepSession()

    await sweep_album_in_telegram(
        _payload(link_id, [9001, 9002, 1234567]),
        session=session,
        albums=MediaGroupRepository(database),
    )

    assert session.deleted == [(BOT, [9001, 9002])], "the stranger's id is dropped"


async def test_a_payload_naming_nothing_the_album_claims_deletes_nothing(
    database: Any,
) -> None:
    """Not a retry and not a guess: the owner sees the job instead."""
    from bridge.retry import PermanentDeliveryError
    from bridge.service.runtime import sweep_album_in_telegram

    link_id = await _album_parts(database, owner_ids=[9001, 9002])
    session = SweepSession()

    with pytest.raises(PermanentDeliveryError, match="names no id"):
        await sweep_album_in_telegram(
            _payload(link_id, [111, 222]),
            session=session,
            albums=MediaGroupRepository(database),
        )
    assert session.deleted == []


async def test_another_owner_accounts_ids_are_not_this_albums(database: Any) -> None:
    """An owner-side id means nothing outside the account that issued it."""
    from bridge.retry import PermanentDeliveryError
    from bridge.service.runtime import sweep_album_in_telegram

    link_id = await _album_parts(database, owner_ids=[9001, 9002])
    session = SweepSession()

    with pytest.raises(PermanentDeliveryError):
        await sweep_album_in_telegram(
            _payload(link_id, [9001, 9002], account=ACCOUNT + 1),
            session=session,
            albums=MediaGroupRepository(database),
        )
    assert session.deleted == []


async def test_a_peer_the_album_never_reached_is_refused(database: Any) -> None:
    from bridge.retry import PermanentDeliveryError
    from bridge.service.runtime import sweep_album_in_telegram

    link_id = await _album_parts(database, owner_ids=[9001, 9002])
    session = SweepSession()

    with pytest.raises(PermanentDeliveryError, match="not"):
        await sweep_album_in_telegram(
            _payload(link_id, [9001, 9002], peer=424242),
            session=session,
            albums=MediaGroupRepository(database),
        )
    assert session.deleted == []


async def test_a_peer_that_is_no_longer_a_live_bridge_is_refused(
    database: Any,
) -> None:
    """The allowlist *is* the live bridge set, so a disabled bridge drops out of it."""
    from bridge.retry import PermanentDeliveryError
    from bridge.service.runtime import sweep_album_in_telegram

    link_id = await _album_parts(database, owner_ids=[9001, 9002])
    session = SweepSession(peers=set())

    with pytest.raises(PermanentDeliveryError, match="live bridge"):
        await sweep_album_in_telegram(
            _payload(link_id, [9001, 9002]),
            session=session,
            albums=MediaGroupRepository(database),
        )
    assert session.deleted == []


async def test_a_session_that_is_away_defers_and_spends_no_attempt(
    database: Any,
) -> None:
    """Forty minutes offline used to become a permanent FAILED.

    Each attempt spent one of twelve, so a session down for twenty minutes turned
    an owed deletion into a message the owner had to chase by hand — and the
    album stayed half-gone for ever. Deferring costs nothing.
    """
    from bridge.routing.delivery import DeferDelivery
    from bridge.service.runtime import sweep_album_in_telegram

    link_id = await _album_parts(database, owner_ids=[9001, 9002])

    for session in (None, SweepSession(connected=False)):
        with pytest.raises(DeferDelivery):
            await sweep_album_in_telegram(
                _payload(link_id, [9001, 9002]),
                session=session,
                albums=MediaGroupRepository(database),
            )


async def test_a_deferred_sweep_finishes_when_the_session_returns(
    database: Any,
) -> None:
    from bridge.routing.delivery import DeferDelivery
    from bridge.service.runtime import sweep_album_in_telegram

    link_id = await _album_parts(database, owner_ids=[9001, 9002])
    session = SweepSession(connected=False)
    payload = _payload(link_id, [9001, 9002])

    with pytest.raises(DeferDelivery):
        await sweep_album_in_telegram(
            payload, session=session, albums=MediaGroupRepository(database)
        )

    session.is_connected = True
    await sweep_album_in_telegram(
        payload, session=session, albums=MediaGroupRepository(database)
    )
    assert session.deleted == [(BOT, [9001, 9002])]


def test_the_album_sweep_never_announces_a_remote_boundary() -> None:
    """The contract, pinned rather than left as an omission (option A).

    Every other kind with a remote effect calls `sending()` immediately before
    the request, so a crash after it becomes AMBIGUOUS and the owner adjudicates.
    This one does not, and must not: deleting a message that is already gone is a
    no-op in Telegram, so a repeat after a timeout is exactly as correct as the
    first attempt. Asking the owner "did the delete land?" would be asking them
    to decide something that does not matter.
    """
    import ast

    runtime = Path(__file__).resolve().parent.parent / "bridge" / "service" / "runtime.py"
    tree = ast.parse(runtime.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.AsyncFunctionDef)
            or node.name != "sweep_album_in_telegram"
        ):
            continue
        calls = {
            inner.func.id
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
        }
        assert "sending" not in calls, (
            "the sweep announced a remote boundary. If that is now wanted, this "
            "kind also needs a retryable-idempotent classification so a timeout "
            "does not strand the album as AMBIGUOUS."
        )
        return
    raise AssertionError("sweep_album_in_telegram is gone; this test needs updating")


async def test_one_album_is_one_sweep_key_whichever_part_resolved(live: Any) -> None:
    """The key comes from the group, not from the id that happened to resolve.

    A part deleted through its alias answers with the album's namespace; one
    resolved through the canonical row answers with its own number. Filing the
    sweep under the second would make a second job for the same album.
    """
    link_id = await live.album(1002043, 1002044, 1002045)
    namespace = expected_album_namespace(link_id)

    # The head resolves through the canonical row (it is the one `message_map`
    # holds), the tail through its alias. Both must file the same sweep.
    await live.router.on_owner_delete(owner_account_id=ACCOUNT, owner_message_ids=[1002043])
    await live.router.on_owner_delete(owner_account_id=ACCOUNT, owner_message_ids=[1002045])

    jobs = await live.jobs(KIND_TG_ALBUM_SWEEP)
    assert len(jobs) == 1
    assert jobs[0]["source_key"] == album_sweep_source_key(namespace)
