"""The journal keeps every attempt, and knows which attempt an update belongs to.

Two defects are pinned here. `begin` used to assign `self._entries = fresh`, so
starting a one-contact run threw away the record of every attempt still in
flight — including the `telegram_bot_id` and `token_env` of a bot that already
existed in Telegram, which turned a recoverable interruption into an orphan with
no local trace. And two `ProvisioningJournal` objects over one path each held
their own cache and overwrote each other.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bridge.provisioning.journal import (
    TERMINAL,
    ItemState,
    JournalEntry,
    ProvisioningJournal,
    StaleGenerationError,
)


def entry(chat_id: int, *, title: str = "") -> JournalEntry:
    return JournalEntry(
        max_chat_id=chat_id,
        expected_username=f"c{chat_id}_max_bot",
        title=title or f"contact {chat_id}",
    )


@pytest.fixture
def journal(tmp_path: Path) -> ProvisioningJournal:
    return ProvisioningJournal.for_data_dir(tmp_path)


# ----------------------------------------------------------------- merging


def test_beginning_one_contact_keeps_another_unfinished_attempt(
    journal: ProvisioningJournal,
) -> None:
    """The orphan-maker: a bot existed, and the record of it disappeared."""
    journal.begin([entry(1), entry(2)])
    journal.note(1, state=ItemState.BOT_CREATED, telegram_bot_id=4242)

    journal.begin([entry(3)])

    survivor = journal.get(1)
    assert survivor is not None
    assert survivor.state is ItemState.BOT_CREATED
    assert survivor.telegram_bot_id == 4242, "the id of a bot that exists is evidence"
    assert {item.max_chat_id for item in journal.entries()} == {1, 2, 3}


def test_a_finished_entry_is_left_alone(journal: ProvisioningJournal) -> None:
    journal.begin([entry(1)])
    journal.note(1, state=ItemState.HEALTHY, bridge_name="c1")

    journal.begin([entry(1)])

    settled = journal.get(1)
    assert settled is not None
    assert settled.state is ItemState.HEALTHY
    assert settled.generation == 1, "nothing was retried, so nothing is a new attempt"


def test_an_attempt_in_flight_resumes_rather_than_restarting(
    journal: ProvisioningJournal,
) -> None:
    journal.begin([entry(1)])
    journal.note(1, state=ItemState.TOKEN_SAVED, token_env="TELEMAX_BOT_C1")

    journal.begin([entry(1, title="Новое имя")])

    resumed = journal.get(1)
    assert resumed is not None
    assert resumed.state is ItemState.TOKEN_SAVED
    assert resumed.token_env == "TELEMAX_BOT_C1"
    assert resumed.title == "Новое имя", "the title is display and may have moved"


def test_a_permanent_failure_chosen_again_is_a_new_generation_that_keeps_evidence(
    journal: ProvisioningJournal,
) -> None:
    journal.begin([entry(1)])
    journal.note(1, state=ItemState.BOT_CREATED, telegram_bot_id=99, token_env="E_C1")
    journal.note(1, state=ItemState.FAILED_PERMANENT)

    journal.begin([entry(1)])

    fresh = journal.get(1)
    assert fresh is not None
    assert fresh.state is ItemState.PENDING
    assert fresh.generation == 2
    assert fresh.telegram_bot_id == 99, "a permanent failure does not unmake a bot"
    assert fresh.token_env == "E_C1"


def test_begin_returns_only_what_was_asked_for(journal: ProvisioningJournal) -> None:
    journal.begin([entry(1)])
    returned = journal.begin([entry(2)])
    assert [item.max_chat_id for item in returned] == [2]


def test_selected_walks_the_run_not_the_file(journal: ProvisioningJournal) -> None:
    journal.begin([entry(1), entry(2), entry(3)])
    assert [item.max_chat_id for item in journal.selected([3, 1])] == [3, 1]
    assert journal.selected([99]) == []


# -------------------------------------------------------------- generations


def test_a_superseded_attempt_cannot_settle_the_current_one(
    journal: ProvisioningJournal,
) -> None:
    journal.begin([entry(1)])
    stale = journal.get(1)
    assert stale is not None
    journal.note(1, state=ItemState.FAILED_PERMANENT)
    journal.begin([entry(1)])

    with pytest.raises(StaleGenerationError):
        journal.note(1, generation=stale.generation, state=ItemState.HEALTHY)

    current = journal.get(1)
    assert current is not None
    assert current.state is ItemState.PENDING


def test_the_current_generation_may_settle(journal: ProvisioningJournal) -> None:
    journal.begin([entry(1)])
    current = journal.get(1)
    assert current is not None
    journal.note(1, generation=current.generation, state=ItemState.HEALTHY)
    settled = journal.get(1)
    assert settled is not None
    assert settled.state is ItemState.HEALTHY


# ------------------------------------------------------------ two objects


def test_two_objects_over_one_path_do_not_overwrite_each_other(
    tmp_path: Path,
) -> None:
    first = ProvisioningJournal.for_data_dir(tmp_path)
    second = ProvisioningJournal.for_data_dir(tmp_path)

    first.begin([entry(1)])
    second.begin([entry(2)])
    first.note(1, state=ItemState.BOT_CREATED)
    second.note(2, state=ItemState.TOKEN_SAVED)

    third = ProvisioningJournal.for_data_dir(tmp_path)
    states = {item.max_chat_id: item.state for item in third.entries()}
    assert states == {1: ItemState.BOT_CREATED, 2: ItemState.TOKEN_SAVED}


def test_a_stale_cache_does_not_travel_into_a_write(tmp_path: Path) -> None:
    first = ProvisioningJournal.for_data_dir(tmp_path)
    second = ProvisioningJournal.for_data_dir(tmp_path)
    first.begin([entry(1)])
    first.entries()  # warm the cache

    second.note(1, state=ItemState.WORKER_STARTED, bridge_name="c1")
    first.note(1, needs_open=True)

    reread = ProvisioningJournal.for_data_dir(tmp_path).get(1)
    assert reread is not None
    assert reread.state is ItemState.WORKER_STARTED
    assert reread.bridge_name == "c1"
    assert reread.needs_open is True


# --------------------------------------------------------------- cleanup


def test_forget_is_the_only_thing_that_removes_an_entry(
    journal: ProvisioningJournal,
) -> None:
    journal.begin([entry(1), entry(2)])
    assert journal.forget(1) is True
    assert journal.forget(1) is False
    assert [item.max_chat_id for item in journal.entries()] == [2]


def test_pruning_only_takes_finished_entries(journal: ProvisioningJournal) -> None:
    journal.begin([entry(1), entry(2)])
    journal.note(1, state=ItemState.HEALTHY)
    journal.note(2, state=ItemState.BOT_CREATED)
    # Age both of them by rewriting the file: `note` stamps `updated_at` itself,
    # which is the behaviour that makes the watermark trustworthy.
    aged = json.loads(journal.path.read_text(encoding="utf-8"))
    for item in aged:
        item["updated_at"] = int(time.time()) - 10_000
    journal.path.write_text(json.dumps(aged), encoding="utf-8")

    assert journal.prune_finished(older_than_seconds=3600) == 1
    assert [item.max_chat_id for item in journal.entries()] == [2], (
        "an unfinished attempt is never pruned, however old it is"
    )


def test_abandoned_is_terminal_and_keeps_the_username(
    journal: ProvisioningJournal,
) -> None:
    journal.begin([entry(1)])
    journal.note(1, state=ItemState.ABANDONED, telegram_bot_id=7)
    left = journal.get(1)
    assert left is not None
    assert left.state in TERMINAL
    assert left.expected_username == "c1_max_bot"
    assert left.telegram_bot_id == 7, "manual cleanup needs the id"
    assert journal.unfinished == []


# ------------------------------------------------------------- robustness


def test_a_corrupt_file_starts_a_new_journal(tmp_path: Path) -> None:
    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.begin([entry(1)])
    journal.path.write_text("{not json", encoding="utf-8")

    assert ProvisioningJournal.for_data_dir(tmp_path).entries() == []


def test_one_unreadable_entry_does_not_take_its_neighbours(tmp_path: Path) -> None:
    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.path.parent.mkdir(parents=True, exist_ok=True)
    journal.path.write_text(
        json.dumps(
            [
                {"max_chat_id": 1, "expected_username": "c1_max_bot", "state": "moon"},
                {"max_chat_id": 2, "expected_username": "c2_max_bot", "state": "pending"},
            ]
        ),
        encoding="utf-8",
    )

    assert [item.max_chat_id for item in journal.entries()] == [2]


def test_a_journal_written_before_managed_bots_still_loads(tmp_path: Path) -> None:
    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.path.parent.mkdir(parents=True, exist_ok=True)
    journal.path.write_text(
        json.dumps(
            [
                {
                    "max_chat_id": 1,
                    "expected_username": "c1_max_bot",
                    "state": "old_bot_deleted",
                }
            ]
        ),
        encoding="utf-8",
    )

    loaded = journal.entries()
    assert loaded[0].state is ItemState.OLD_BOT_DELETED
    assert loaded[0].generation == 1, "a file without generations reads as the first"


def test_nothing_secret_is_ever_written(journal: ProvisioningJournal) -> None:
    journal.begin([entry(1)])
    journal.note(1, token_env="TELEMAX_BOT_C1", telegram_bot_id=4242)
    text = journal.path.read_text(encoding="utf-8")
    assert "TELEMAX_BOT_C1" in text
    assert ":" not in text.split("TELEMAX_BOT_C1")[1].split(",")[0]
