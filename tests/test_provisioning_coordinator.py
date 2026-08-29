"""One walk per contact, whatever tapped it, and different contacts in parallel.

The lock this replaces lived on `ProvisioningBatch`, which is built per tap. Two
taps on «Создать» therefore took two different locks and produced two complete
walks: two `create_bot` calls, two `save_token`s, two `start_worker`s and two
`stop_bridge`s — the second of which tore down the worker the first had just
brought up. That is measured here against the production batch, not a model of
it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bridge.provisioning.batch import ProvisioningBatch
from bridge.provisioning.coordinator import ContactKey, ProvisioningCoordinator
from bridge.provisioning.journal import ItemState, JournalEntry, ProvisioningJournal
from bridge.provisioning.mtproto import CreatedBot
from bridge.provisioning.provisioner import UsernameState

pytestmark = pytest.mark.asyncio


# ------------------------------------------------------------------- the key


async def test_a_chat_id_folds_onto_the_peer_it_belongs_to() -> None:
    """`own ^ chat` is the peer, so both entry points guard the same person."""
    own, peer = 200000002, 200000004
    chat = own ^ peer
    assert ContactKey.for_chat(chat, own_user_id=own) == ContactKey.for_user(peer)


async def test_without_the_owner_id_a_chat_keeps_a_key_of_its_own() -> None:
    assert ContactKey.for_chat(4242) != ContactKey.for_user(4242)


async def test_a_group_is_never_folded_onto_a_user() -> None:
    """A negative id is not `own ^ peer` of anything; folding it would be a lie."""
    key = ContactKey.for_chat(-400000000004, own_user_id=200000002)
    assert key.value.startswith("c:")


# -------------------------------------------------------------- the claiming


async def test_a_second_caller_waits_and_is_told_the_work_was_not_theirs() -> None:
    coordinator = ProvisioningCoordinator()
    key = ContactKey.for_user(1)
    entered = asyncio.Event()
    release = asyncio.Event()
    outcomes: list[bool] = []
    finished = False

    async def first() -> None:
        nonlocal finished
        async with coordinator.claim(key, doing="creating") as mine:
            outcomes.append(mine)
            entered.set()
            await release.wait()
            finished = True

    async def second() -> None:
        await entered.wait()
        async with coordinator.claim(key) as mine:
            outcomes.append(mine)
            assert finished, "the loser reads the outcome, not a run in progress"

    task = asyncio.create_task(first())
    waiter = asyncio.create_task(second())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(task, waiter)

    assert outcomes == [True, False]


async def test_different_contacts_do_not_wait_for_each_other() -> None:
    coordinator = ProvisioningCoordinator()
    inside = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async with coordinator.claim(ContactKey.for_user(1)):
            inside.set()
            await release.wait()

    task = asyncio.create_task(hold())
    await inside.wait()
    async with coordinator.claim(ContactKey.for_user(2)) as mine:
        assert mine is True
    release.set()
    await task


async def test_the_lock_map_returns_to_empty() -> None:
    coordinator = ProvisioningCoordinator()
    for index in range(50):
        async with coordinator.claim(ContactKey.for_user(index)) as mine:
            assert mine is True
    assert coordinator.tracked == 0


async def test_cancellation_releases_the_contact() -> None:
    coordinator = ProvisioningCoordinator()
    key = ContactKey.for_user(1)
    inside = asyncio.Event()

    async def hold() -> None:
        async with coordinator.claim(key):
            inside.set()
            await asyncio.sleep(60)

    task = asyncio.create_task(hold())
    await inside.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert coordinator.busy(key) is False
    async with coordinator.claim(key) as mine:
        assert mine is True


async def test_what_is_running_is_visible_and_says_nothing_private() -> None:
    coordinator = ProvisioningCoordinator()
    inside = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async with coordinator.claim(ContactKey.for_user(7), doing="creating a bot"):
            inside.set()
            await release.wait()

    task = asyncio.create_task(hold())
    await inside.wait()
    assert coordinator.running() == {"u:7": "creating a bot"}
    release.set()
    await task
    assert coordinator.running() == {}


# ------------------------------------------------------- against the batch


class SlowPort:
    """A provisioner port that actually awaits, so the walks can interleave."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.starts: list[str] = []

    async def check_username(self, username: str) -> UsernameState:
        await asyncio.sleep(0.01)
        return UsernameState.OWNED if username in self.created else UsernameState.FREE

    async def bot_id_for(self, username: str) -> int | None:
        await asyncio.sleep(0)
        return 4242 if username in self.created else None

    async def create_bot(self, *, name: str, username: str) -> CreatedBot:
        await asyncio.sleep(0.05)
        self.created.append(username)
        return CreatedBot(username=username, token="123:AAA", bot_id=4242)

    async def send_start(self, username: str, bot_id: int | None = None) -> None:
        self.starts.append(username)


class SlowGateway:
    def __init__(self) -> None:
        self.saved: list[str] = []
        self.started: list[str] = []
        self.stopped: list[int] = []
        self.active: list[int] = []

    async def stop_bridge(self, max_chat_id: int) -> None:
        await asyncio.sleep(0.01)
        self.stopped.append(max_chat_id)

    async def save_token(self, *, max_chat_id: int, username: str, token: str) -> str:
        await asyncio.sleep(0.01)
        self.saved.append(username)
        return f"TELEMAX_BOT_{username.upper()}"

    async def start_worker(
        self,
        *,
        max_chat_id: int,
        username: str,
        token_env: str,
        title: str,
        max_user_id: int | None = None,
    ) -> str:
        await asyncio.sleep(0.02)
        self.started.append(username)
        return username.removesuffix("_max_bot")

    async def is_healthy(self, max_chat_id: int) -> bool:
        return True

    async def mark_active(self, max_chat_id: int) -> None:
        self.active.append(max_chat_id)


def batch_for(
    journal: ProvisioningJournal,
    coordinator: ProvisioningCoordinator,
    port: SlowPort,
    gateway: SlowGateway,
    chat_ids: list[int],
) -> ProvisioningBatch:
    return ProvisioningBatch(
        provisioner=port,
        gateway=gateway,
        journal=journal,
        display_name_of=lambda entry: entry.title,
        chat_ids=chat_ids,
        coordinator=coordinator,
    )


async def test_two_taps_on_create_are_one_walk(tmp_path: Path) -> None:
    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.begin(
        [JournalEntry(max_chat_id=10, expected_username="p_max_bot", title="P", max_peer_id=99)]
    )
    coordinator, port, gateway = ProvisioningCoordinator(), SlowPort(), SlowGateway()

    await asyncio.gather(
        batch_for(journal, coordinator, port, gateway, [10]).run(),
        batch_for(journal, coordinator, port, gateway, [10]).run(),
    )

    assert port.created == ["p_max_bot"], "one bot"
    assert gateway.saved == ["p_max_bot"], "one token written"
    assert gateway.started == ["p_max_bot"], "one worker"
    assert gateway.stopped == [10], "and nobody's worker torn down twice"
    settled = journal.get(10)
    assert settled is not None
    assert settled.state is ItemState.HEALTHY


async def test_the_refused_caller_still_sees_the_real_state(tmp_path: Path) -> None:
    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.begin([JournalEntry(max_chat_id=10, expected_username="p_max_bot", title="P")])
    coordinator, port, gateway = ProvisioningCoordinator(), SlowPort(), SlowGateway()

    first, second = await asyncio.gather(
        batch_for(journal, coordinator, port, gateway, [10]).run(),
        batch_for(journal, coordinator, port, gateway, [10]).run(),
    )

    assert first.entries[0].state is ItemState.HEALTHY
    assert second.entries[0].state is ItemState.HEALTHY, "the journal is the answer"


async def test_two_different_contacts_are_provisioned_at_once(tmp_path: Path) -> None:
    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.begin(
        [
            JournalEntry(max_chat_id=10, expected_username="a_max_bot", title="A", max_peer_id=1),
            JournalEntry(max_chat_id=20, expected_username="b_max_bot", title="B", max_peer_id=2),
        ]
    )
    coordinator, port, gateway = ProvisioningCoordinator(), SlowPort(), SlowGateway()

    await asyncio.gather(
        batch_for(journal, coordinator, port, gateway, [10]).run(),
        batch_for(journal, coordinator, port, gateway, [20]).run(),
    )

    assert sorted(gateway.started) == ["a_max_bot", "b_max_bot"]
    assert coordinator.tracked == 0
