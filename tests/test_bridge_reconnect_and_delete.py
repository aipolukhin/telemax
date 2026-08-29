"""Disconnecting is not forgetting, and a deleted bot has to be forgettable.

The owner deleted a bot in @BotFather, disconnected its bridge, then chose the
same contact again. The guardian created nothing and opened a chat with a
Deleted Account.

Nothing failed. The journal still read `healthy` for that contact, a terminal
entry is skipped, so the run did nothing at all — and the result screen drew a
link to the bot the journal remembered.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bridge.provisioning.batch import ProvisioningBatch
from bridge.provisioning.journal import ItemState, JournalEntry, ProvisioningJournal
from tests.fake_provisioning import FakeGateway, FakeProvisioner

pytestmark = pytest.mark.asyncio


def _journal(tmp_path: Path, *, state: ItemState) -> ProvisioningJournal:
    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.begin(
        [
            JournalEntry(
                max_chat_id=1,
                max_peer_id=2,
                expected_username="c1_max_bot",
                title="Наталья",
            )
        ]
    )
    journal.note(1, state=state)
    return journal


def _batch(journal: ProvisioningJournal, gateway: FakeGateway, prov: Any) -> ProvisioningBatch:
    return ProvisioningBatch(
        provisioner=prov,
        gateway=gateway,
        journal=journal,
        display_name_of=lambda entry: entry.title,
    )


# ------------------------------------------------ the entry against reality


async def test_a_finished_entry_whose_bridge_is_gone_is_walked_again(
    tmp_path: Path,
) -> None:
    """The reported bug. `healthy` is what the last run achieved, not what is."""
    journal = _journal(tmp_path, state=ItemState.HEALTHY)
    gateway = FakeGateway()
    gateway.unhealthy.add(1)  # disconnected, or its bot deleted
    prov = FakeProvisioner()

    await _batch(journal, gateway, prov).run()

    assert prov.created == ["c1_max_bot"], "a new bot, because there is no bridge"
    assert gateway.started == [1]


async def test_a_finished_entry_with_a_live_bridge_is_left_alone(
    tmp_path: Path,
) -> None:
    """Idempotence still holds: choosing a working contact does nothing."""
    journal = _journal(tmp_path, state=ItemState.HEALTHY)
    gateway = FakeGateway()
    gateway.running.add(1)  # the bridge is up, which is what `healthy` claimed
    prov = FakeProvisioner()

    await _batch(journal, gateway, prov).run()

    assert prov.created == []
    assert gateway.started == []


async def test_a_decision_is_not_re_examined_by_a_health_check(
    tmp_path: Path,
) -> None:
    """A permanent failure and an abandoned attempt are decisions, not
    observations. Reopening those walks into the same wall on every tap."""
    for state in (ItemState.FAILED_PERMANENT, ItemState.ABANDONED):
        journal = _journal(tmp_path / state.value, state=state)
        gateway = FakeGateway()
        gateway.unhealthy.add(1)
        prov = FakeProvisioner()

        await _batch(journal, gateway, prov).run()

        assert prov.created == [], state.value


async def test_an_unanswerable_health_check_leaves_the_entry_standing(
    tmp_path: Path,
) -> None:
    """Re-provisioning a working bridge over a failed check is worse."""
    journal = _journal(tmp_path, state=ItemState.HEALTHY)
    prov = FakeProvisioner()

    class Broken(FakeGateway):
        async def is_healthy(self, max_chat_id: int) -> bool:
            raise RuntimeError("registry is not up")

    await _batch(journal, Broken(), prov).run()

    assert prov.created == []


# --------------------------------------------------- disconnect retires it


class Bridges:
    """Just enough of `BridgeRepository` for the two flows below."""

    def __init__(self, record: Any) -> None:
        self.record = record
        self.states: list[Any] = []
        self.forgotten: list[str] = []

    async def by_max_chat(self, max_chat_id: int) -> Any:
        return self.record

    async def set_state(self, bridge_name: str, state: Any) -> None:
        self.states.append(state)

    async def forget_bot(self, bridge_name: str) -> None:
        self.forgotten.append(bridge_name)


def _record() -> Any:
    return type(
        "Row",
        (),
        {
            "bridge_name": "c1",
            "max_chat_id": 1,
            "expected_username": "c1_max_bot",
            "telegram_bot_id": 500,
            "title": "Наталья",
        },
    )()


def _flow(tmp_path: Path, *, provisioner: Any, journal: ProvisioningJournal) -> Any:
    from bridge.provisioning.flow import DialogFlow

    flow = DialogFlow.__new__(DialogFlow)
    flow._bridges = Bridges(_record())
    flow._gateway = FakeGateway()
    flow._journal = journal
    flow._provisioner = provisioner
    return flow


async def test_disconnecting_retires_the_journal_entry(tmp_path: Path) -> None:
    """Without this the contact cannot be connected again at all: the entry
    still says `healthy`, and a terminal entry is skipped."""
    journal = _journal(tmp_path, state=ItemState.HEALTHY)
    flow = _flow(tmp_path, provisioner=FakeProvisioner(), journal=journal)

    assert await flow.disconnect(1)

    assert journal.get(1) is None


# ------------------------------------------------------------- deleting it


class Deleting(FakeProvisioner):
    deletes_bots = True


async def test_deleting_the_bot_unbinds_the_row_from_it(tmp_path: Path) -> None:
    """A row still naming a deleted bot is what makes the *next* bridge fail.

    `_expected_bot` reads `telegram_bot_id`, so a rebuilt bridge at the same
    username — with a new bot behind it — is refused as somebody else's.
    """
    journal = _journal(tmp_path, state=ItemState.HEALTHY)
    prov = Deleting(owned={"c1_max_bot": 500})
    flow = _flow(tmp_path, provisioner=prov, journal=journal)

    deleted = await flow.delete_bot(1)

    assert deleted == "c1_max_bot"
    assert prov.deleted == ["c1_max_bot"]
    assert flow._bridges.forgotten == ["c1"]
    assert journal.get(1) is None, "the attempt it belonged to is gone too"


async def test_the_screen_only_offers_deletion_when_something_can_delete(
    tmp_path: Path,
) -> None:
    """Bot API has no `deleteManagedBot` at all, so on that path the screen is
    instructions — never a button that pretends."""
    journal = _journal(tmp_path, state=ItemState.HEALTHY)

    assert _flow(tmp_path, provisioner=Deleting(), journal=journal).can_delete_bots
    assert not _flow(tmp_path, provisioner=FakeProvisioner(), journal=journal).can_delete_bots


async def test_the_managed_provisioner_says_it_cannot_delete() -> None:
    from bridge.provisioning.managed import ManagedBotProvisioner
    from bridge.provisioning.provisioner import MtprotoProvisioner

    assert MtprotoProvisioner.deletes_bots is True
    assert ManagedBotProvisioner.deletes_bots is False


async def test_the_free_slot_screen_has_two_shapes() -> None:
    from bridge.onboarding import screens

    # The screen takes a card, not a row: it has a username, not an
    # `expected_username`.
    item = type("Card", (), {"max_chat_id": 1, "title": "Наталья", "username": "c1_max_bot"})()

    button, markup = screens.free_slot(item, can_delete=True, can_wipe=True, revision=3)
    assert "Удалить мост «Наталья»?" in button
    assert "чат Telegram" in button, "the wipe is stated, not implied"
    assert "Переписка в MAX останется" in button
    assert "нельзя отменить" in button
    # The confirmation button names the action. «Да» under a heading somebody
    # has stopped reading is how a bot gets deleted by accident.
    assert "🗑 Удалить мост" in str(markup)
    assert screens.BRIDGE_FREE_YES in str(markup)

    # A session that can delete a bot but not empty a chat says the second part
    # plainly rather than letting the owner assume it happened.
    no_wipe, _ = screens.free_slot(item, can_delete=True, can_wipe=False, revision=3)
    assert "останется" in no_wipe

    recipe, links = screens.free_slot(item, can_delete=False)
    assert "/deletebot" in recipe
    assert screens.BRIDGE_FREE_YES not in str(links)
    assert "t.me/BotFather" in str(links)


async def test_a_new_bot_supersedes_the_one_the_row_remembered(tmp_path: Path) -> None:
    """The owner deletes a bot by hand and rebuilds the bridge.

    The row still names the deleted bot, `_expected_bot` reads that id, and the
    identity check refuses the new bot as somebody else's. So a creation unbinds
    the row before the worker starts.
    """
    journal = _journal(tmp_path, state=ItemState.PENDING)
    prov = FakeProvisioner()

    class Remembering(FakeGateway):
        def __init__(self) -> None:
            super().__init__()
            self.asked: list[tuple[int, int | None]] = []

        async def forget_bot(self, max_chat_id: int, *, keeping: int | None = None) -> None:
            self.asked.append((max_chat_id, keeping))

    gateway = Remembering()
    await _batch(journal, gateway, prov).run()

    assert prov.created == ["c1_max_bot"]
    assert gateway.asked == [(1, None)], "a creation names no bot to keep"


async def test_a_reused_bot_leaves_the_row_alone(tmp_path: Path) -> None:
    """Nothing was created, so nothing the row remembers has been superseded."""
    journal = _journal(tmp_path, state=ItemState.PENDING)
    prov = FakeProvisioner(owned={"c1_max_bot": 77})

    class Remembering(FakeGateway):
        def __init__(self) -> None:
            super().__init__()
            self.asked: list[tuple[int, int | None]] = []

        async def forget_bot(self, max_chat_id: int, *, keeping: int | None = None) -> None:
            self.asked.append((max_chat_id, keeping))

    gateway = Remembering()
    await _batch(journal, gateway, prov).run()

    assert prov.created == []
    assert prov.reused == ["c1_max_bot"]
    assert gateway.asked == [(1, 77)], "the reused bot is named, so the row is kept"


async def test_a_gateway_that_cannot_unbind_does_not_stop_the_bridge(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path, state=ItemState.PENDING)

    class Refusing(FakeGateway):
        async def forget_bot(self, max_chat_id: int, *, keeping: int | None = None) -> None:
            raise RuntimeError("database is busy")

    gateway = Refusing()
    await _batch(journal, gateway, FakeProvisioner()).run()

    assert gateway.started == [1]


async def test_the_gateway_keeps_a_row_that_already_names_the_right_bot() -> None:
    """Where the keep-or-unbind decision actually lives."""
    from bridge.service.runtime import LiveBridgeGateway

    class Rows:
        def __init__(self) -> None:
            self.forgotten: list[str] = []

        async def by_max_chat(self, max_chat_id: int) -> Any:
            return _record()  # telegram_bot_id = 500

        async def forget_bot(self, bridge_name: str) -> None:
            self.forgotten.append(bridge_name)

    gateway = LiveBridgeGateway.__new__(LiveBridgeGateway)

    rows = Rows()
    gateway._bridges = rows
    await gateway.forget_bot(1, keeping=500)
    assert rows.forgotten == [], "the row already names this bot"

    rows = Rows()
    gateway._bridges = rows
    await gateway.forget_bot(1, keeping=999)
    assert rows.forgotten == ["c1"], "a different bot supersedes it"

    rows = Rows()
    gateway._bridges = rows
    await gateway.forget_bot(1, keeping=None)
    assert rows.forgotten == ["c1"], "an unnamed bot cannot be proven the same"


# ------------------------------------ what the row remembered about that bot


async def test_unbinding_drops_everything_the_bot_owned(tmp_path: Path) -> None:
    """A row that outlives its bot must not hand its claims to the next one.

    Measured on the live install after a bot was deleted and rebuilt at the same
    username: the new bot arrived with the default name and no avatar, and
    «подтянуть историю» brought nothing. Both were the row telling the truth
    about a bot that no longer existed — the profile signature said "already
    dressed", the history cursor said "already imported".
    """
    from bridge.storage.database import Database
    from bridge.storage.models import BridgeRecord, BridgeState
    from bridge.storage.repositories import BridgeRepository

    database = await Database.connect(tmp_path / "b.db")
    bridges = BridgeRepository(database)
    await bridges.upsert(
        BridgeRecord(
            bridge_name="c1",
            max_chat_id=1,
            token_env="TELEMAX_BOT_C1",
            telegram_bot_id=500,
            title="Наталья",
            state=BridgeState.ACTIVE,
            expected_username="c1_max_bot",
        )
    )
    await bridges.set_profile_signature("c1", "dressed")
    await bridges.set_history_cursor("c1", 999)
    await bridges.set_pinned_status("c1", message_id=7, text="pinned")

    await bridges.forget_bot("c1")

    row = await bridges.get("c1")
    assert row is not None
    assert row.telegram_bot_id is None, "the identity check would refuse the new bot"
    assert await bridges.history_cursor("c1") is None, "the new chat is empty"
    assert await bridges.profile_signature("c1") is None, "the new bot is undressed"
    assert await bridges.pinned_status("c1") == (None, None)

    # What the contact owns, not the bot, stays.
    assert row.expected_username == "c1_max_bot"
    assert row.title == "Наталья"
    assert row.token_env == "TELEMAX_BOT_C1"
    await database.close()


# ------------------------------------------------- the whole chain, not one link


async def test_deleting_is_reachable_from_the_router_not_only_the_flow() -> None:
    """The button existed in tests and never on screen.

    `delete_bot` was written onto `DialogFlow`, which the tests drive directly.
    The router does not: it talks to `TelemaxRuntime`, which talks to
    `BridgeService`, which talks to the flow. Two of those three had no such
    method, so `getattr` found nothing and the screen silently fell back to
    printing instructions.

    Same failure as `account_bot_count` on the Protocol a few hours earlier, and
    the same shape of test: assert the whole chain, because every link is where
    it can break.
    """
    from bridge.provisioning.flow import DialogFlow
    from bridge.service.runtime import BridgeService
    from bridge.service.telemax import TelemaxRuntime

    for link in (TelemaxRuntime, BridgeService, DialogFlow):
        assert hasattr(link, "delete_bot"), link.__name__
        assert hasattr(link, "can_delete_bots"), link.__name__


async def test_bridge_settings_offers_deletion_beside_disconnecting() -> None:
    """Both live one screen under the card, and neither is on the card itself.

    Deletion used to be reachable only *after* disconnecting, which is a strange
    place to keep the more final of the two; then it moved onto the card, next
    to «Открыть чат». Its home is the bridge's settings screen.
    """
    from bridge.onboarding import screens
    from tests.fake_provisioning import fake_bridge_view

    item = type("Card", (), {"max_chat_id": 1, "title": "Наталья", "username": "c1_max_bot"})()
    view = fake_bridge_view(item)

    with_delete = str(screens.bridge_settings_screen(view, can_delete=True)[1])
    assert "Удалить мост…" in with_delete
    assert "Отключить мост" in with_delete
    assert screens.BRIDGE_FREE in with_delete

    without = str(screens.bridge_settings_screen(view, can_delete=False)[1])
    assert "Удалить мост…" not in without
    assert "Отключить мост" in without, "disconnecting never depends on a session"

    card = str(screens.bridge_screen(view, can_delete=True)[1])
    assert "Удалить" not in card and "Отключить" not in card


async def test_a_disconnected_bridge_stays_in_the_list() -> None:
    """Otherwise its bot is the one thing the menu cannot reach.

    The list was `active()` only, so switching a bridge off removed it from
    «Мосты», taking its card with it — and with the card the delete button. The
    bot of a disconnected bridge is exactly the one worth deleting.
    """
    from bridge.onboarding import screens
    from bridge.provisioning.flow import BridgeSummary
    from tests.fake_provisioning import fake_bridge_view

    on = BridgeSummary(title="Мама", username="m_max_bot", bridge_name="m", max_chat_id=1)
    off = BridgeSummary(
        title="Наталья",
        username="n_max_bot",
        bridge_name="n",
        max_chat_id=2,
        running=False,
    )

    text, markup = screens.bridges_screen([fake_bridge_view(on), fake_bridge_view(off)])
    rendered = str(markup)

    assert screens.bridge_callback(2) in rendered, "the card must be reachable"
    assert "⚪ Наталья · отключён" in rendered, "and it must not look like it is running"
    assert "1 работает · 1 отключён" in text


async def test_nothing_says_paused_when_everything_runs() -> None:
    from bridge.onboarding import screens
    from bridge.provisioning.flow import BridgeSummary
    from tests.fake_provisioning import fake_bridge_view

    text, _ = screens.bridges_screen(
        [
            fake_bridge_view(
                BridgeSummary(
                    title="Мама", username="m_max_bot", bridge_name="m", max_chat_id=1
                )
            )
        ]
    )

    assert "1 работает" in text
    assert "отключ" not in text
