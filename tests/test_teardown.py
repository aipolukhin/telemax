"""Tearing one bridge down: conversation, bot, rows — and the order.

The order is the feature. Every step depends on something the next one
destroys, so getting it wrong does not fail loudly — it strands a resource
nobody can reach. That is not hypothetical here: local destructive cleanup
running before the remote work it described cost this install eighty-eight
minutes of delivery on 2026-08-06.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bridge.provisioning import teardown
from bridge.provisioning.teardown import Step

pytestmark = pytest.mark.asyncio


class Recorder:
    """Every effect, in the order it actually happened."""

    def __init__(self, *, fail_at: str | None = None) -> None:
        self.order: list[str] = []
        self.fail_at = fail_at

    def _step(self, name: str) -> None:
        self.order.append(name)
        if self.fail_at == name:
            raise RuntimeError(f"{name} refused")


def _row() -> Any:
    return type("Row", (), {
        "bridge_name": "c1", "max_chat_id": 1, "expected_username": "c1_max_bot",
        "telegram_bot_id": 500, "title": "Наталья", "token_env": "TELEMAX_BOT_C1",
    })()


def _flow(
    tmp_path: Path,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    *,
    wipes: bool = True,
) -> Any:
    from bridge.provisioning.flow import DialogFlow
    from bridge.provisioning.journal import JournalEntry, ProvisioningJournal

    class Provisioner:
        wipes_dialogs = wipes
        deletes_bots = True

        async def wipe_dialog(self, bot_id: int) -> int:
            recorder._step("wipe")
            return bot_id

        async def delete_owned_bot(self, username: str) -> None:
            recorder._step("delete_bot")

    class Gateway:
        data_dir = tmp_path

        async def stop_bridge(self, max_chat_id: int) -> None:
            recorder._step("stop")

        async def forget_token(self, token_env: str) -> str:
            recorder._step("forget_token")
            return token_env

    class Bridges:
        database = None

        async def by_max_chat(self, max_chat_id: int) -> Any:
            return _row()

    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.begin([JournalEntry(max_chat_id=1, expected_username="c1_max_bot", title="Наталья")])

    flow = DialogFlow.__new__(DialogFlow)
    flow._bridges = Bridges()
    flow._gateway = Gateway()
    flow._journal = journal
    flow._provisioner = Provisioner()

    async def purge(**_: Any) -> dict[str, int]:
        recorder._step("purge")
        return {"message_map": 12, "outbox": 3}

    # Through monkeypatch, never by assignment: the suite runs in random order,
    # and a module attribute replaced for good is a mine under every later test.
    monkeypatch.setattr(teardown, "purge_rows", purge)
    return flow, journal


async def test_the_order_is_stop_wipe_delete_purge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each step needs something the next one takes away."""
    recorder = Recorder()
    flow, journal = _flow(tmp_path, recorder, monkeypatch)

    outcome = await flow.tear_down(1)

    assert recorder.order == ["stop", "wipe", "delete_bot", "purge", "forget_token"]
    assert outcome.complete
    assert outcome.dialog_wiped and outcome.bot_deleted
    assert outcome.row_count == 15
    assert journal.get(1) is None


async def test_a_failed_wipe_stops_before_the_bot_is_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting the bot first would leave the conversation unreachable for ever:
    there is no peer to resolve once the bot is gone."""
    recorder = Recorder(fail_at="wipe")
    flow, journal = _flow(tmp_path, recorder, monkeypatch)

    with pytest.raises(RuntimeError):
        await flow.tear_down(1)

    assert recorder.order == ["stop", "wipe"]
    assert "delete_bot" not in recorder.order
    assert "purge" not in recorder.order
    assert journal.get(1) is not None, "the local record survives a stalled walk"


async def test_a_failed_delete_never_purges_local_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact mistake that cost eighty-eight minutes: local cleanup running
    ahead of the remote effect it describes. A row is the only record that the
    resource ever existed."""
    recorder = Recorder(fail_at="delete_bot")
    flow, journal = _flow(tmp_path, recorder, monkeypatch)

    with pytest.raises(RuntimeError):
        await flow.tear_down(1)

    assert recorder.order == ["stop", "wipe", "delete_bot"]
    assert "purge" not in recorder.order
    assert "forget_token" not in recorder.order
    assert journal.get(1) is not None


async def test_the_checkpoint_names_where_it_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between two irreversible effects must not leave a person guessing
    which of them happened."""
    recorder = Recorder(fail_at="delete_bot")
    flow, _ = _flow(tmp_path, recorder, monkeypatch)

    with pytest.raises(RuntimeError):
        await flow.tear_down(1)

    assert teardown.read_step(tmp_path, "c1") is Step.DIALOG_WIPED


async def test_a_finished_teardown_leaves_no_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow, _ = _flow(tmp_path, Recorder(), monkeypatch)

    await flow.tear_down(1)

    assert teardown.read_step(tmp_path, "c1") is None


async def test_without_a_session_the_bot_still_goes_but_the_chat_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Honest degradation rather than a silent skip: the screen says so too."""
    recorder = Recorder()
    flow, _ = _flow(tmp_path, recorder, monkeypatch, wipes=False)

    outcome = await flow.tear_down(1)

    assert recorder.order == ["stop", "delete_bot", "purge", "forget_token"]
    assert outcome.dialog_wiped is False
    assert outcome.bot_deleted is True


async def test_an_unknown_bridge_is_not_an_error() -> None:
    from bridge.provisioning.flow import DialogFlow

    class Empty:
        async def by_max_chat(self, max_chat_id: int) -> Any:
            return None

    flow = DialogFlow.__new__(DialogFlow)
    flow._bridges = Empty()

    assert await flow.tear_down(999) is None


# ------------------------------------------- the scope is derived, not listed


async def test_the_purge_finds_every_table_that_names_the_bridge(
    tmp_path: Path,
) -> None:
    """A hand-written table list is a list with one table missing.

    The scope comes from scanning the schema for `bridge_name`,
    `telegram_bot_id`, `bot_id` or `max_chat_id` — the same derivation the
    cutover uses — so a table added next year is covered without anybody
    remembering to add it here.
    """
    from bridge.storage.database import Database
    from bridge.storage.models import BridgeRecord, BridgeState
    from bridge.storage.repositories import BridgeRepository

    database = await Database.connect(tmp_path / "b.db")
    bridges = BridgeRepository(database)
    for name, chat, bot in (("keep", 2, 600), ("gone", 1, 500)):
        await bridges.upsert(
            BridgeRecord(
                bridge_name=name, max_chat_id=chat, token_env=f"T_{name}",
                telegram_bot_id=bot, title=name, state=BridgeState.ACTIVE,
                expected_username=f"{name}_max_bot",
            )
        )
    await bridges.set_history_cursor("gone", 42)
    await bridges.set_history_cursor("keep", 42)

    tables = {name for name, _ in await teardown.scoped_tables(database)}
    assert "bridges" in tables
    assert "message_map" in tables
    assert "outbox" in tables
    assert "sticker_cache" not in tables, "account-level state survives a contact"

    removed = await teardown.purge_rows(
        database=database, bridge_name="gone", telegram_bot_id=500, max_chat_id=1
    )

    assert removed.get("bridges") == 1
    assert await bridges.get("gone") is None
    assert await bridges.get("keep") is not None, "the other bridge is untouched"
    assert await bridges.history_cursor("keep") == 42
    await database.close()


async def test_dropping_a_floor_leaves_the_others(tmp_path: Path) -> None:
    from bridge.cutover.floors import read_floors, write_floors

    write_floors(tmp_path, {1: 100, 2: 200})

    assert teardown.drop_floor(tmp_path, 1) is True
    assert teardown.drop_floor(tmp_path, 1) is False, "already gone"
    assert read_floors(tmp_path) == {2: 200}
