"""Building bridges one at a time, so a failure costs one bridge.

The obvious implementation deletes every old bot and then creates the
replacements. It is shorter and it is catastrophic: a failure on the third
contact has already destroyed the first two. So the batch walks each contact to
the end on its own, and most of this file is about what happens when one of them
does not make it.

The other half is restart. Every step is written down before the next begins, so
a resumed run knows whether it crashed *before* creating the new bot or *after*
— which need opposite recoveries, and are indistinguishable from the outside.
"""

from __future__ import annotations

from pathlib import Path

from bridge.provisioning import selection as ui
from bridge.provisioning.batch import ProvisioningBatch
from bridge.provisioning.journal import ItemState, JournalEntry, ProvisioningJournal
from bridge.provisioning.owned import OwnerMustOpenChatError, start_link
from bridge.provisioning.provisioner import CreationLimitError
from tests.fake_provisioning import FakeGateway, FakeProvisioner


def entries(*chat_ids: int) -> list[JournalEntry]:
    return [
        JournalEntry(
            max_chat_id=chat_id,
            max_peer_id=chat_id * 2,
            expected_username=f"c{chat_id}_max_bot",
            title=f"Контакт {chat_id}",
        )
        for chat_id in chat_ids
    ]


def make(
    tmp_path: Path,
    *chat_ids: int,
    provisioner: FakeProvisioner | None = None,
) -> tuple[ProvisioningBatch, FakeProvisioner, FakeGateway, ProvisioningJournal]:
    prov = provisioner or FakeProvisioner()
    gateway = FakeGateway()
    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.begin(entries(*chat_ids))

    batch = ProvisioningBatch(
        provisioner=prov,
        gateway=gateway,
        journal=journal,
        display_name_of=lambda entry: f"{entry.title} [Max]",
    )
    return batch, prov, gateway, journal


# ----------------------------------------------------------------- the happy path


async def test_a_new_contact_gets_a_bot_a_worker_and_a_start(tmp_path: Path) -> None:
    batch, prov, gateway, journal = make(tmp_path, 1)

    result = await batch.run()

    assert prov.created == ["c1_max_bot"]
    assert gateway.started == [1]
    assert prov.started == ["c1_max_bot"], "the owner's own account opens the chat"
    assert result.complete
    assert journal.get(1).state is ItemState.HEALTHY


async def test_an_existing_bot_is_reused_not_rebuilt(tmp_path: Path) -> None:
    """The change Managed Bots make: its token is fetched, not re-earned.

    The old flow deleted the bot and created it again to recover a token it had
    not written down. That burnt a creation, freed a username the owner's saved
    links pointed at, and — on a live account — rate-limited @BotFather for
    sixteen hours.
    """
    prov = FakeProvisioner(owned={"c1_max_bot": 77})
    batch, prov, _, journal = make(tmp_path, 1, provisioner=prov)

    await batch.run()

    assert prov.deleted == [], "nothing is deleted, ever"
    assert prov.created == [], "and nothing is created either"
    assert prov.reused == ["c1_max_bot"]
    assert journal.get(1).state is ItemState.HEALTHY


async def test_no_username_release_is_waited_for(tmp_path: Path) -> None:
    """Nothing was freed, so there is nothing to wait for."""
    prov = FakeProvisioner(owned={"c1_max_bot": 77})
    batch, prov, _, _ = make(tmp_path, 1, provisioner=prov)

    await batch.run()

    assert prov.releases == []


async def test_a_running_bridge_lets_go_before_the_new_worker_starts(
    tmp_path: Path,
) -> None:
    """One poller per bot: the old token must stop long-polling first."""
    prov = FakeProvisioner(owned={"c1_max_bot": 77})
    batch, _, gateway, _ = make(tmp_path, 1, provisioner=prov)

    await batch.run()

    assert gateway.stopped == [1]
    assert gateway.started == [1]


async def test_the_token_is_stored_by_name_never_in_the_journal(tmp_path: Path) -> None:
    batch, _, _, journal = make(tmp_path, 1)

    await batch.run()

    entry = journal.get(1)
    assert entry.token_env == "TELEMAX_BOT_C1"
    written = journal.path.read_text(encoding="utf-8")
    assert ":AAAAA" not in written, "a token never lands in the journal"
    assert "TELEMAX_BOT_C1" in written


async def test_the_worker_starts_exactly_once(tmp_path: Path) -> None:
    batch, _, gateway, _ = make(tmp_path, 1)

    await batch.run()
    await batch.run()

    assert gateway.started == [1], "a second run finds it finished and does nothing"


async def test_a_bridge_is_only_ready_after_its_health_check(tmp_path: Path) -> None:
    batch, prov, gateway, journal = make(tmp_path, 1)
    gateway.unhealthy.add(1)

    result = await batch.run()

    assert not result.complete
    assert journal.get(1).state is ItemState.FAILED_RETRYABLE
    assert prov.started == ["c1_max_bot"], "the /start happened; the answer did not"


async def test_a_repeated_start_creates_no_second_bridge(tmp_path: Path) -> None:
    """Idempotent by construction: a finished entry is skipped entirely."""
    batch, prov, gateway, journal = make(tmp_path, 1)
    await batch.run()

    await batch.run()

    assert prov.started == ["c1_max_bot"]
    assert gateway.started == [1]
    assert len(journal.entries()) == 1


# --------------------------------------------------------------- partial failure


async def test_one_failure_does_not_destroy_the_others(tmp_path: Path) -> None:
    """The reason nothing is deleted up front."""
    prov = FakeProvisioner(owned={"c1_max_bot": 1, "c2_max_bot": 2, "c3_max_bot": 3})
    prov.fail_create["c2_max_bot"] = CreationLimitError("BOT_CREATE_LIMIT_EXCEEDED")
    batch, prov, _, journal = make(tmp_path, 1, 2, 3, provisioner=prov)

    result = await batch.run()

    assert [entry.state for entry in journal.entries()] == [
        ItemState.HEALTHY,
        ItemState.FAILED_PERMANENT,
        ItemState.HEALTHY,
    ]
    assert not result.complete
    assert len(result.healthy) == 2
    assert "c3_max_bot" in prov.reused, "the third contact was still provisioned"
    assert prov.deleted == [], "and the two that worked were never destroyed"


async def test_a_foreign_username_is_never_deleted_and_fails_permanently(
    tmp_path: Path,
) -> None:
    prov = FakeProvisioner(foreign={"c1_max_bot"})
    batch, prov, gateway, journal = make(tmp_path, 1, provisioner=prov)

    await batch.run()

    assert prov.deleted == []
    assert prov.created == []
    assert gateway.stopped == []
    entry = journal.get(1)
    assert entry.state is ItemState.FAILED_PERMANENT
    assert "занят другим" in (entry.error or "")


async def test_the_limit_stops_the_run_without_breaking_anything(tmp_path: Path) -> None:
    prov = FakeProvisioner()
    prov.fail_create["c1_max_bot"] = CreationLimitError("BOT_CREATE_LIMIT_EXCEEDED")
    batch, _, gateway, journal = make(tmp_path, 1, provisioner=prov)

    await batch.run()

    entry = journal.get(1)
    assert entry.state is ItemState.FAILED_PERMANENT
    assert "лимит" in (entry.error or "")
    assert gateway.started == []


async def test_a_worker_that_will_not_start_is_retryable(tmp_path: Path) -> None:
    batch, prov, gateway, journal = make(tmp_path, 1)
    gateway.fail_start.add(1)

    await batch.run()

    assert journal.get(1).state is ItemState.FAILED_RETRYABLE
    assert prov.started == [], "no /start into a bot nothing is polling"


# --------------------------------------------------------------------- restart


async def test_a_crash_after_creating_and_before_the_token_just_fetches_it(
    tmp_path: Path,
) -> None:
    """The crash that used to force a rebuild. Now it is one method call.

    A bot exists whose token nobody wrote down — previously unrecoverable
    without deleting it. `getManagedBotToken` makes the recovery free.
    """
    prov = FakeProvisioner()
    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.begin(entries(1))
    journal.note(1, state=ItemState.BOT_CREATED)
    prov.owned["c1_max_bot"] = 77  # the bot exists; nobody wrote its token down

    batch = ProvisioningBatch(
        provisioner=prov,
        gateway=FakeGateway(),
        journal=journal,
        display_name_of=lambda entry: entry.title,
    )
    await batch.run()

    assert prov.deleted == []
    assert prov.created == []
    assert prov.reused == ["c1_max_bot"]
    assert journal.get(1).state is ItemState.HEALTHY


async def test_a_crash_after_the_token_resumes_without_deleting(tmp_path: Path) -> None:
    """The opposite crash, and the opposite recovery."""
    prov = FakeProvisioner(owned={"c1_max_bot": 77})
    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.begin(entries(1))
    journal.note(1, state=ItemState.TOKEN_SAVED, token_env="TELEMAX_BOT_C1")

    gateway = FakeGateway()
    batch = ProvisioningBatch(
        provisioner=prov,
        gateway=gateway,
        journal=journal,
        display_name_of=lambda entry: entry.title,
    )
    await batch.run()

    assert prov.deleted == [], "the bot is fine; only the worker was missing"
    assert prov.created == []
    assert gateway.started == [1]
    assert journal.get(1).state is ItemState.HEALTHY


async def test_the_journal_survives_a_new_process(tmp_path: Path) -> None:
    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.begin(entries(1, 2))
    journal.note(1, state=ItemState.HEALTHY)

    fresh = ProvisioningJournal.for_data_dir(tmp_path)

    assert {entry.max_chat_id: entry.state for entry in fresh.entries()} == {
        1: ItemState.HEALTHY,
        2: ItemState.PENDING,
    }
    assert [entry.max_chat_id for entry in fresh.unfinished] == [2]


async def test_pressing_done_twice_does_not_duplicate_anything(tmp_path: Path) -> None:
    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.begin(entries(1))
    journal.note(1, state=ItemState.HEALTHY, bridge_name="c1")

    journal.begin(entries(1))

    assert journal.get(1).state is ItemState.HEALTHY, "a finished entry is left alone"
    assert len(journal.entries()) == 1


async def test_progress_is_reported_after_every_contact(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    async def progress(current: list[JournalEntry]) -> None:
        seen.append([entry.state.value for entry in current])

    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.begin(entries(1, 2))
    batch = ProvisioningBatch(
        provisioner=FakeProvisioner(),
        gateway=FakeGateway(),
        journal=journal,
        display_name_of=lambda entry: entry.title,
        progress=progress,
    )
    await batch.run()

    assert seen[-1] == ["healthy", "healthy"]
    # The waiting state is drawn *before* the call that blocks on the owner:
    # reporting afterwards would show a "confirm" button for a question that
    # has already been answered.
    assert ["awaiting_confirmation", "pending"] in seen
    assert ["healthy", "awaiting_confirmation"] in seen


async def test_a_permanent_failure_is_not_retried_on_its_own(tmp_path: Path) -> None:
    """Retrying a username that belongs to somebody else changes nothing."""
    prov = FakeProvisioner(foreign={"c1_max_bot"})
    batch, prov, _, journal = make(tmp_path, 1, 2, provisioner=prov)
    await batch.run()
    assert journal.get(1).state is ItemState.FAILED_PERMANENT

    prov.deleted.clear()
    await batch.run()

    assert prov.deleted == []
    assert journal.get(1).state is ItemState.FAILED_PERMANENT


async def test_choosing_a_failed_contact_again_is_a_fresh_attempt(
    tmp_path: Path,
) -> None:
    """The collision may have been resolved between one selection and the next."""
    prov = FakeProvisioner(foreign={"c1_max_bot"})
    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.begin(entries(1))
    journal.note(1, state=ItemState.FAILED_PERMANENT, error="занят другим")

    prov.foreign.clear()
    journal.begin(entries(1))
    assert journal.get(1).state is ItemState.PENDING

    batch = ProvisioningBatch(
        provisioner=prov,
        gateway=FakeGateway(),
        journal=journal,
        display_name_of=lambda entry: entry.title,
    )
    await batch.run()

    assert journal.get(1).state is ItemState.HEALTHY


# ---------------------------------------------- nothing reaches @BotFather


def test_only_the_bootstrap_can_reach_botfather() -> None:
    """The migration, asserted by import graph rather than by reading the diff.

    Driving @BotFather's chat is what rate-limited a live account for sixteen
    hours. It survives for exactly one job — creating the very first guardian
    bot, which by definition has no manager — and must not creep back into the
    path that makes contact bots.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "bridge"
    importers: set[str] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module, *(alias.name for alias in node.names)]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            if any("botfather" in name for name in names):
                importers.add(path.relative_to(root).as_posix())

    assert importers == {"provisioning/mtproto.py"}, sorted(importers)


def test_the_service_may_drive_botfather_only_with_a_brake_on() -> None:
    """This invariant was inverted on 2026-08-06, deliberately, and narrowed.

    It used to say the worker must never hold an `MtprotoProvisioner` at all —
    written after five unpaced `/newbot` walks locked the account out for
    58000 seconds. The ban on the *class* has since been paid for twice by the
    other side: Telegram's own Create button refused to press on a live install,
    and a creation path the owner cannot press is not a creation path.

    So the service drives @BotFather again, and what is guarded is the thing
    that actually caused the incident. Removing any of these is removing the
    brake, not tidying up.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    runtime = (root / "bridge/service/runtime.py").read_text(encoding="utf-8")
    provisioner = (root / "bridge/provisioning/provisioner.py").read_text(encoding="utf-8")

    # Both paths stay reachable: no session configured means managed bots.
    assert "MtprotoProvisioner" in runtime
    assert "ManagedBotProvisioner" in runtime
    # Borrowed, never opened: a second Telethon connection on the same session
    # file is how the account's keys get revoked.
    assert "BorrowedBotFatherSession" in runtime

    # One walk at a time, a gap between them, and a stop that outlives the walk.
    assert "NEWBOT_MIN_INTERVAL_SECONDS" in provisioner
    assert "_pace" in provisioner
    assert "_refuse_while_quiet" in provisioner
    assert "_quiet_until" in provisioner


async def test_without_a_session_the_owner_is_asked_to_open_the_chat(tmp_path: Path) -> None:
    """`managed` has nobody to press Start, and that is not a failure.

    Retrying would raise the same thing for ever: a bot cannot open a chat with
    another bot at all. So the bridge is brought up and the entry records that
    the owner still has to open it — the result screen says so once, beside the
    buttons that do it, rather than sending a message per bot.
    """
    prov = FakeProvisioner()

    async def refuse(username: str, bot_id: int | None = None) -> None:
        raise OwnerMustOpenChatError(username, start_link(username))

    prov.send_start = refuse  # type: ignore[method-assign]
    batch, _, gateway, journal = make(tmp_path, 1, provisioner=prov)

    result = await batch.run()

    assert journal.get(1).needs_open, "the reason is written down, not announced"
    assert gateway.started == [1], "the bridge still comes up"
    assert result.complete
    assert journal.get(1).state is ItemState.HEALTHY
    assert "нажмите в нём <b>Старт</b>" in ui.result_text(list(result.entries))
