"""Start-up finishes what the last run started, and never starts anything new.

The journal recorded every step and nothing read it. A process that died between
creating a bot and writing its token down left the bot in Telegram, the entry at
`bot_created`, and no way back that did not begin with the owner opening
`/dialogs` and pressing a button again — for a failure they were never told
about.

The other half is what start-up must *not* do. A `pending` attempt has no remote
effect behind it; creating a bot for one would be a machine spending a slot on a
decision nobody made.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fake_provisioning import FakeGateway, FakeProvisioner

from bridge.provisioning.coordinator import ProvisioningCoordinator
from bridge.provisioning.journal import ItemState, JournalEntry, ProvisioningJournal
from bridge.provisioning.reconcile import Outcome, ProvisioningReconciler, Verdict

pytestmark = pytest.mark.asyncio

CHAT = 10
PEER = 4242
USERNAME = "p_max_bot"


def journal_at(tmp_path: Path, state: ItemState, **fields: Any) -> ProvisioningJournal:
    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.begin(
        [
            JournalEntry(
                max_chat_id=CHAT,
                expected_username=USERNAME,
                title="Папа",
                max_peer_id=PEER,
            )
        ]
    )
    if state is not ItemState.PENDING or fields:
        journal.note(CHAT, state=state, **fields)
    return journal


def reconciler_for(
    journal: ProvisioningJournal,
    provisioner: FakeProvisioner,
    gateway: FakeGateway,
    *,
    incident: Any = None,
) -> ProvisioningReconciler:
    return ProvisioningReconciler(
        journal=journal,
        provisioner=provisioner,
        gateway=gateway,
        coordinator=ProvisioningCoordinator(),
        display_name_of=lambda entry: entry.title,
        own_user_id=None,
        incident=incident,
    )


# ------------------------------------------------------- resuming, per state


async def test_a_crash_after_the_bot_and_before_the_token_is_finished(
    tmp_path: Path,
) -> None:
    """The orphan-maker, resumed with no owner tap and no second bot."""
    journal = journal_at(tmp_path, ItemState.BOT_CREATED, telegram_bot_id=99)
    provisioner = FakeProvisioner(owned={USERNAME: 99})
    gateway = FakeGateway()

    report = await reconciler_for(journal, provisioner, gateway).run()

    assert report.resumed == 1
    settled = journal.get(CHAT)
    assert settled is not None and settled.state is ItemState.HEALTHY
    assert provisioner.created == [], "no second bot"
    assert gateway.started == [CHAT]
    assert gateway.active == {CHAT}


async def test_a_crash_after_the_token_resumes_at_the_worker(tmp_path: Path) -> None:
    journal = journal_at(
        tmp_path, ItemState.TOKEN_SAVED, token_env="TELEMAX_BOT_P", telegram_bot_id=99
    )
    provisioner = FakeProvisioner(owned={USERNAME: 99})
    gateway = FakeGateway()

    await reconciler_for(journal, provisioner, gateway).run()

    settled = journal.get(CHAT)
    assert settled is not None and settled.state is ItemState.HEALTHY
    assert provisioner.created == []


async def test_a_crash_after_the_worker_started_does_not_start_a_second(
    tmp_path: Path,
) -> None:
    journal = journal_at(
        tmp_path,
        ItemState.WORKER_STARTED,
        token_env="TELEMAX_BOT_P",
        bridge_name="p",
        telegram_bot_id=99,
    )
    provisioner = FakeProvisioner(owned={USERNAME: 99})
    gateway = FakeGateway()

    await reconciler_for(journal, provisioner, gateway).run()

    assert gateway.started == [CHAT], "one worker, and it replaces itself"
    settled = journal.get(CHAT)
    assert settled is not None and settled.state is ItemState.HEALTHY


async def test_a_retryable_failure_with_a_bot_behind_it_is_resumed(
    tmp_path: Path,
) -> None:
    journal = journal_at(
        tmp_path,
        ItemState.FAILED_RETRYABLE,
        telegram_bot_id=99,
        error="мост не запустился",
    )
    provisioner = FakeProvisioner(owned={USERNAME: 99})

    await reconciler_for(journal, provisioner, FakeGateway()).run()

    settled = journal.get(CHAT)
    assert settled is not None and settled.state is ItemState.HEALTHY


# ------------------------------------------------- what start-up must not do


async def test_a_pending_attempt_creates_nothing(tmp_path: Path) -> None:
    """No remote effect behind it, and nobody has said to make one."""
    journal = journal_at(tmp_path, ItemState.PENDING)
    provisioner = FakeProvisioner()

    report = await reconciler_for(journal, provisioner, FakeGateway()).run()

    assert provisioner.created == [], "a restart never spends a slot on its own"
    assert report.awaiting_owner == 1
    settled = journal.get(CHAT)
    assert settled is not None and settled.state is ItemState.PENDING


async def test_an_unconfirmed_creation_is_left_for_the_owner(tmp_path: Path) -> None:
    journal = journal_at(tmp_path, ItemState.AWAITING_CONFIRMATION)
    provisioner = FakeProvisioner()

    report = await reconciler_for(journal, provisioner, FakeGateway()).run()

    assert provisioner.created == []
    assert [item.verdict for item in report.outcomes] == [Verdict.AWAITING_OWNER]


async def test_an_unconfirmed_creation_whose_bot_exists_is_adopted(
    tmp_path: Path,
) -> None:
    """The lost-update case: the owner did press Create, and nothing heard it."""
    journal = journal_at(tmp_path, ItemState.AWAITING_CONFIRMATION)
    provisioner = FakeProvisioner(owned={USERNAME: 77})

    report = await reconciler_for(journal, provisioner, FakeGateway()).run()

    assert report.resumed == 1
    assert provisioner.created == [], "adopted, not created"
    settled = journal.get(CHAT)
    assert settled is not None and settled.state is ItemState.HEALTHY


async def test_a_foreign_username_is_permanent_and_touches_nothing(
    tmp_path: Path,
) -> None:
    journal = journal_at(tmp_path, ItemState.BOT_CREATED)
    provisioner = FakeProvisioner()
    provisioner.foreign.add(USERNAME)

    report = await reconciler_for(journal, provisioner, FakeGateway()).run()

    assert [item.verdict for item in report.outcomes] == [Verdict.COLLISION]
    settled = journal.get(CHAT)
    assert settled is not None and settled.state is ItemState.FAILED_PERMANENT
    assert provisioner.created == []


async def test_a_settled_attempt_is_not_touched(tmp_path: Path) -> None:
    journal = journal_at(tmp_path, ItemState.HEALTHY, bridge_name="p")
    provisioner = FakeProvisioner()

    report = await reconciler_for(journal, provisioner, FakeGateway()).run()

    assert report.outcomes == []
    assert provisioner.created == []


# --------------------------------------------------------------- robustness


async def test_an_unreachable_telegram_is_retried_next_time(tmp_path: Path) -> None:
    journal = journal_at(tmp_path, ItemState.BOT_CREATED, telegram_bot_id=99)

    class Unreachable(FakeProvisioner):
        async def check_username(self, username: str) -> Any:
            raise TimeoutError("telegram is not answering")

    report = await reconciler_for(journal, Unreachable(), FakeGateway()).run()

    assert [item.verdict for item in report.outcomes] == [Verdict.UNAVAILABLE]
    settled = journal.get(CHAT)
    assert settled is not None and settled.state is ItemState.BOT_CREATED, "left as it was"


async def test_one_attempt_failing_does_not_stop_the_others(tmp_path: Path) -> None:
    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.begin(
        [
            JournalEntry(max_chat_id=1, expected_username="a_max_bot", title="A", max_peer_id=1),
            JournalEntry(max_chat_id=2, expected_username="b_max_bot", title="B", max_peer_id=2),
        ]
    )
    journal.note(1, state=ItemState.BOT_CREATED, telegram_bot_id=1)
    journal.note(2, state=ItemState.BOT_CREATED, telegram_bot_id=2)

    class Half(FakeProvisioner):
        async def check_username(self, username: str) -> Any:
            if username == "a_max_bot":
                raise RuntimeError("boom")
            return await super().check_username(username)

    provisioner = Half(owned={"b_max_bot": 2})
    report = await reconciler_for(journal, provisioner, FakeGateway()).run()

    verdicts = {item.max_chat_id: item.verdict for item in report.outcomes}
    assert verdicts[1] is Verdict.UNAVAILABLE
    assert verdicts[2] is Verdict.RESUMED


async def test_a_stuck_attempt_raises_one_incident_with_no_secret_in_it(
    tmp_path: Path,
) -> None:
    journal = journal_at(tmp_path, ItemState.AWAITING_CONFIRMATION, telegram_bot_id=77)
    raised: list[Outcome] = []

    async def incident(outcome: Outcome) -> None:
        raised.append(outcome)

    await reconciler_for(
        journal, FakeProvisioner(), FakeGateway(), incident=incident
    ).run()

    assert len(raised) == 1
    assert raised[0].expected_username == USERNAME
    assert raised[0].telegram_bot_id == 77, "manual cleanup needs the id"
    assert ":" not in raised[0].detail, "nothing that could be a token"


async def test_reconciliation_and_a_tap_do_not_walk_one_contact_twice(
    tmp_path: Path,
) -> None:
    """The coordinator is shared, so a tap during start-up waits rather than races."""
    journal = journal_at(tmp_path, ItemState.BOT_CREATED, telegram_bot_id=99)
    provisioner = FakeProvisioner(owned={USERNAME: 99})
    gateway = FakeGateway()
    coordinator = ProvisioningCoordinator()

    from bridge.provisioning.batch import ProvisioningBatch

    reconciler = ProvisioningReconciler(
        journal=journal,
        provisioner=provisioner,
        gateway=gateway,
        coordinator=coordinator,
        display_name_of=lambda entry: entry.title,
    )
    tap = ProvisioningBatch(
        provisioner=provisioner,
        gateway=gateway,
        journal=journal,
        display_name_of=lambda entry: entry.title,
        chat_ids=[CHAT],
        coordinator=coordinator,
    )

    await asyncio.gather(reconciler.run(), tap.run())

    assert gateway.started == [CHAT], "one worker between them"
    assert provisioner.created == []
