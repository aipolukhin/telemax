"""Two readings of one message must not interleave, and an old one must not win.

The compare-and-set keeps the *row* honest and says nothing about the order in
which remote calls were made. Telethon dispatches with `sequential_updates=False`
by default and the bridge does not override it, so two `UpdateEditMessage` for
one message really are two concurrent tasks. Measured on the build before this:

    previous pts=9, nothing showing
    pts=10 read the state, began setting 👍
    pts=11 read the same state, set ❤, committed
    pts=10 finished, setting 👍
    → row says pts 11 / ❤, MAX shows 👍

Two things fix it and both are needed. A lock per identity makes read, decide,
effect and commit one section, so the *enqueue* order is the version order. And
a generation check in the worker makes a job that waited out a MAX outage step
aside rather than putting back a reaction the owner has replaced since.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest_asyncio
from telethon.tl.types import MessageReactions, ReactionCount, ReactionEmoji

from bridge.routing.keyed_lock import KeyedLock
from bridge.routing.owner_mutation import resolve_reaction
from bridge.routing.owner_updates import OwnerUpdateDispatch
from bridge.storage import (
    Database,
    MessageMapRepository,
    OwnerMessageStateRepository,
    ReactionStateRepository,
)

ACCOUNT, BOT, MESSAGE = 100000001, 9000000001, 1002319
MAX_CHAT, MAX_MESSAGE = 236856064, 111411200851968013


class Message:
    def __init__(self, text: str = "привет", chosen: tuple[str, ...] = ()) -> None:
        self.id = MESSAGE
        self.message = text
        self.entities: list[object] | None = None
        self.reactions = MessageReactions(
            results=[
                ReactionCount(reaction=ReactionEmoji(emoticon=e), count=1, chosen_order=i)
                for i, e in enumerate(chosen)
            ],
            min=False, can_see_list=False, reactions_as_tags=False, recent_reactions=[],
        )


class GatedReactions:
    """A MAX that can be made to finish its calls out of order."""

    def __init__(self) -> None:
        self.begun: list[tuple[int, str | None]] = []
        self.applied: list[tuple[int, str | None]] = []
        self.gates: dict[int, asyncio.Event] = {}

    async def apply_owner_reaction(self, **kwargs: Any) -> None:
        pts, emoji = kwargs["pts"], kwargs["emoji"]
        self.begun.append((pts, emoji))
        gate = self.gates.get(pts)
        if gate is not None:
            await gate.wait()
        self.applied.append((pts, emoji))


class GatedEdits:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []
        self.gates: dict[int, asyncio.Event] = {}

    async def on_owner_edit(self, **kwargs: Any) -> None:
        gate = self.gates.get(kwargs["edit_pts"])
        if gate is not None:
            await gate.wait()
        self.calls.append((kwargs["edit_pts"], kwargs["text"]))


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


async def _wired(
    database: Database,
) -> tuple[OwnerUpdateDispatch, GatedEdits, GatedReactions, OwnerMessageStateRepository]:
    messages = MessageMapRepository(database)
    link = await messages.claim_from_max(
        bridge_name="mom", max_chat_id=MAX_CHAT, max_message_id=MAX_MESSAGE,
        telegram_bot_id=BOT, telegram_chat_id=ACCOUNT,
    )
    assert link is not None
    await messages.attach_owner_message(link, MESSAGE, telegram_owner_account_id=ACCOUNT)
    edits, reactions = GatedEdits(), GatedReactions()
    state = OwnerMessageStateRepository(database)
    dispatch = OwnerUpdateDispatch(
        state=state, messages=messages, edits=edits, reactions=reactions
    )
    await dispatch.seed_baseline(
        account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE,
        content_fingerprint="seed", chosen_json="[]",
    )
    return dispatch, edits, reactions, state


async def _feed(dispatch: OwnerUpdateDispatch, message: Message, pts: int) -> None:
    await dispatch.on_owner_update(
        account_id=ACCOUNT, bot_id=BOT, message=message, pts=pts,
        text=message.message, outgoing=True,
    )


# --------------------------------------------------------------- the interleave


async def test_the_row_and_max_cannot_disagree(database: Database) -> None:
    """The measured divergence, as a test. Whichever update takes the lock first,
    the last reaction to reach MAX is the one the row says."""
    dispatch, _, reactions, state = await _wired(database)
    reactions.gates[10] = asyncio.Event()

    slow = asyncio.create_task(_feed(dispatch, Message(chosen=("👍",)), 10))
    await asyncio.sleep(0.05)
    later = asyncio.create_task(_feed(dispatch, Message(chosen=("❤",)), 11))
    await asyncio.sleep(0.05)
    reactions.gates[10].set()
    await asyncio.gather(slow, later)

    row = await state.get(account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE)
    assert row is not None
    last_applied = reactions.applied[-1][1] if reactions.applied else None
    assert last_applied == "❤", f"MAX ended on {last_applied}, the row says ❤"
    assert row.pts == 11


async def test_the_later_update_can_win_the_lock_and_the_earlier_becomes_stale(
    database: Database,
) -> None:
    """The other admissible order: 11 goes first, 10 finds itself old and does
    nothing at all."""
    dispatch, _, reactions, _state = await _wired(database)

    await _feed(dispatch, Message(chosen=("❤",)), 11)
    await _feed(dispatch, Message(chosen=("👍",)), 10)

    assert [emoji for _, emoji in reactions.applied] == ["❤"]
    assert dispatch.counts.stale_update == 1


async def test_an_edit_is_never_enqueued_out_of_version_order(
    database: Database,
) -> None:
    """The same defect on the other half: two edits enqueued in the wrong order
    leave MAX on the older text, because the queue is first-in first-out."""
    dispatch, edits, _, _ = await _wired(database)
    edits.gates[10] = asyncio.Event()

    slow = asyncio.create_task(_feed(dispatch, Message("v10"), 10))
    await asyncio.sleep(0.05)
    later = asyncio.create_task(_feed(dispatch, Message("v11"), 11))
    await asyncio.sleep(0.05)
    edits.gates[10].set()
    await asyncio.gather(slow, later)

    assert [pts for pts, _ in edits.calls] == sorted(pts for pts, _ in edits.calls)
    assert edits.calls[-1][1] == "v11"


async def test_a_hundred_concurrent_updates_leave_one_consistent_answer(
    database: Database,
) -> None:
    dispatch, _, reactions, state = await _wired(database)
    order = [7, 3, 99, 12, 55, *range(100, 196)]

    await asyncio.gather(
        *(_feed(dispatch, Message(chosen=("👍" if pts % 2 else "❤",)), pts) for pts in order)
    )

    row = await state.get(account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE)
    assert row is not None and row.pts == max(order)
    if reactions.applied:
        assert reactions.applied[-1][0] == max(
            pts for pts, _ in reactions.applied
        ), "the last reaction applied is the newest one"


# ------------------------------------------------------------ the lock itself


async def test_different_messages_do_not_wait_on_each_other() -> None:
    locks = KeyedLock()
    started = asyncio.Event()

    async def hold_one() -> None:
        async with locks.hold((ACCOUNT, BOT, 1)):
            started.set()
            await asyncio.sleep(0.2)

    async def take_another() -> None:
        await started.wait()
        async with locks.hold((ACCOUNT, BOT, 2)):
            pass

    await asyncio.wait_for(asyncio.gather(hold_one(), take_another()), timeout=1.0)


async def test_the_same_message_id_in_two_dialogs_is_two_locks() -> None:
    locks = KeyedLock()
    started = asyncio.Event()

    async def hold_one() -> None:
        async with locks.hold((ACCOUNT, BOT, MESSAGE)):
            started.set()
            await asyncio.sleep(0.2)

    async def take_another() -> None:
        await started.wait()
        async with locks.hold((ACCOUNT, BOT + 1, MESSAGE)):
            pass

    await asyncio.wait_for(asyncio.gather(hold_one(), take_another()), timeout=1.0)


async def test_a_cancelled_holder_does_not_keep_the_lock() -> None:
    """Cancellation is how a shutdown reaches a handler. A lock left held would
    stop every later update on that message for the life of the process."""
    locks = KeyedLock()
    inside = asyncio.Event()

    async def held() -> None:
        async with locks.hold((ACCOUNT, BOT, MESSAGE)):
            inside.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(held())
    await inside.wait()
    task.cancel()
    with __import__("contextlib").suppress(asyncio.CancelledError):
        await task

    async with locks.hold((ACCOUNT, BOT, MESSAGE)):
        pass
    assert locks.held == 0


async def test_the_map_does_not_grow_for_ever() -> None:
    locks = KeyedLock()
    for message_id in range(200):
        async with locks.hold((ACCOUNT, BOT, message_id)):
            pass
    assert locks.held == 0


async def test_a_seed_and_a_reading_of_one_message_do_not_overlap(
    database: Database,
) -> None:
    """The bootstrap writes baselines while the dispatcher is live. A seed that
    landed mid-reading would be a third opinion about what came before."""
    dispatch, _, reactions, state = await _wired(database)
    reactions.gates[10] = asyncio.Event()

    reading = asyncio.create_task(_feed(dispatch, Message(chosen=("👍",)), 10))
    await asyncio.sleep(0.05)
    seeding = asyncio.create_task(
        dispatch.seed_baseline(
            account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE,
            content_fingerprint="fetched", chosen_json='[["e","❤"]]',
        )
    )
    await asyncio.sleep(0.05)
    assert not seeding.done(), "the seed is waiting for the reading, not racing it"
    reactions.gates[10].set()
    await asyncio.gather(reading, seeding)

    row = await state.get(account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE)
    assert row is not None and row.pts == 10  # the live version, not the fetch


# ------------------------------------------------------- the generation check


async def _run_job(database: Database, max_sender: Any, **payload: Any) -> None:
    await resolve_reaction(
        state=OwnerMessageStateRepository(database),
        snapshots=ReactionStateRepository(database),
        max_sender=max_sender,
        payload={
            "max_chat_id": MAX_CHAT, "max_message_id": MAX_MESSAGE,
            "account_id": ACCOUNT, "bot_id": BOT, "owner_message_id": MESSAGE,
            **payload,
        },
    )


class MaxSpy:
    def __init__(self) -> None:
        self.added: list[str] = []
        self.removed: int = 0

    async def add_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        self.added.append(emoji)

    async def remove_reaction(self, chat_id: int, message_id: int) -> None:
        self.removed += 1


async def test_a_job_that_waited_out_an_outage_does_not_put_back_an_old_reaction(
    database: Database,
) -> None:
    """The case the version in the payload exists for. The job was made when the
    owner wanted 👍; by the time MAX came back they wanted ❤."""
    state = OwnerMessageStateRepository(database)
    await state.advance(
        account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE,
        content_fingerprint="x", chosen_json='[["e","❤"]]', pts=11,
    )
    spy = MaxSpy()

    await _run_job(database, spy, emoji="👍", pts=10)

    assert spy.added == [] and spy.removed == 0


async def test_the_current_version_is_applied(database: Database) -> None:
    state = OwnerMessageStateRepository(database)
    await state.advance(
        account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE,
        content_fingerprint="x", chosen_json='[["e","👍"]]', pts=10,
    )
    spy = MaxSpy()

    await _run_job(database, spy, emoji="👍", pts=10)

    assert spy.added == ["👍"]


async def test_a_clear_is_applied_as_a_removal(database: Database) -> None:
    spy = MaxSpy()
    await _run_job(database, spy, emoji=None, pts=10)
    assert spy.removed == 1


async def test_running_the_job_twice_is_the_same_as_running_it_once(
    database: Database,
) -> None:
    """A crash after the remote call and before the job is marked done. Setting
    replaces, so the repeat means what the first attempt meant."""
    spy = MaxSpy()
    await _run_job(database, spy, emoji="👍", pts=10)
    await _run_job(database, spy, emoji="👍", pts=10)
    assert spy.added == ["👍", "👍"]

    snapshot = await ReactionStateRepository(database).get(MAX_CHAT, MAX_MESSAGE)
    assert snapshot is not None and snapshot.your_reaction == "👍"


async def test_what_max_shows_is_recorded_so_its_echo_is_not_mirrored_back(
    database: Database,
) -> None:
    """MAX pushes our own reaction straight back as a chat update. Without this
    it reads as the contact having made it."""
    await _run_job(database, MaxSpy(), emoji="🔥", pts=10)
    snapshot = await ReactionStateRepository(database).get(MAX_CHAT, MAX_MESSAGE)
    assert snapshot is not None and snapshot.your_reaction == "🔥"


def test_the_worker_is_given_a_sender_that_can_react() -> None:
    """The defect the live smoke found: the worker was handed `MaxTextSender`,
    which has no `add_reaction` at all, so every reaction job failed on an
    `AttributeError` and retried until it gave up.

    The parameter is typed now, so this cannot come back silently — but the
    wiring is worth naming too, because `Any` is what let it through."""
    import ast

    tree = ast.parse(Path("bridge/service/runtime.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "resolve_owner_reaction":
            continue
        sender = next(kw.value for kw in node.keywords if kw.arg == "max_sender")
        assert isinstance(sender, ast.Call) and isinstance(sender.func, ast.Name)
        assert sender.func.id == "MaxReactionAdapter", (
            f"the reaction worker is given {ast.dump(sender)[:60]}"
        )
        return
    raise AssertionError("the reaction job is no longer wired into the worker")
