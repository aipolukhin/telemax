"""How many more bots the account may have, and what each choice costs.

The arithmetic is small and every part of it has a way of being wrong that hurts:

* counting only Telemax's own bots hides the ones the owner made themselves, and
  the shortfall only shows up as a failure halfway through provisioning;
* counting a rebuild as a new bot understates capacity, so the owner is told
  they cannot connect somebody they can;
* defaulting the limit to twenty is right until it is not, and when it is not it
  invites a run that deletes working bots and cannot recreate them.

So: the limit comes from Telegram or is `None`, ownership comes from Telegram,
and a replacement costs zero.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bridge.provisioning.capacity import (
    FRESH_SECONDS,
    SOURCE_TELEGRAM,
    Capacity,
    PeerBotStatus,
    PeerPlan,
    classify,
    slot_cost,
)
from bridge.provisioning.flow import DialogFlow
from bridge.provisioning.journal import ProvisioningJournal
from bridge.provisioning.picker import DialogOption
from bridge.provisioning.provisioner import (
    BotLimit,
    CreationLimitError,
    MtprotoProvisioner,
    UsernameState,
    is_limit_error,
)
from tests.fake_provisioning import FakeProvisioner


def plan(
    chat_id: int, status: PeerBotStatus, *, username: str | None = "aaaa_max_bot"
) -> PeerPlan:
    return PeerPlan(
        max_chat_id=chat_id,
        max_peer_id=chat_id * 2,
        title=f"Контакт {chat_id}",
        expected_username=username,
        status=status,
    )


def capacity(*plans: PeerPlan, limit: int | None = 20, owned: int = 13) -> Capacity:
    return Capacity(
        limit=BotLimit(value=limit), owned_bot_count=owned, plans=tuple(plans)
    )


# ------------------------------------------------------------- where it comes from


async def test_owned_bots_come_from_telegram_not_from_local_state() -> None:
    """A copied config claims bots it does not own; `getAdminedBots` cannot."""
    provisioner = FakeProvisioner(owned={"aaaa_max_bot": 1}, strangers=2)

    bots = await provisioner.list_owned_bots()

    assert len(bots) == 3
    assert await provisioner.check_username("aaaa_max_bot") is UsernameState.OWNED


async def test_bots_the_owner_made_themselves_are_counted() -> None:
    """They occupy slots too. Ignoring them is a figure that lies until it fails."""
    provisioner = FakeProvisioner(owned={"aaaa_max_bot": 1}, strangers=5)

    assert len(await provisioner.list_owned_bots()) == 6


async def test_the_limit_is_never_hardcoded_as_twenty() -> None:
    class Session:
        premium = False

        async def is_premium(self) -> bool:
            return self.premium

        async def bot_creation_limit(self) -> int | None:
            return 40 if self.premium else 17

        async def admined_bots(self) -> list[object]:
            return []

    session = Session()
    provisioner = MtprotoProvisioner(session)
    assert (await provisioner.get_creation_limit()).value == 17

    session.premium = True
    assert (await provisioner.get_creation_limit()).value == 40


async def test_premium_and_non_premium_may_differ() -> None:
    assert BotLimit(value=20, premium=False).value != BotLimit(value=40, premium=True).value
    assert BotLimit(value=None).known is False


async def test_an_unpublished_limit_is_reported_as_unknown() -> None:
    """Not as a number. `None` is an answer the whole flow handles."""
    from bridge.provisioning.mtproto import MtprotoError

    class Silent:
        async def is_premium(self) -> bool:
            return False

        async def bot_creation_limit(self) -> int | None:
            raise MtprotoError("no such key")

    limit = await MtprotoProvisioner(Silent()).get_creation_limit()
    assert limit.value is None
    assert not limit.known


# -------------------------------------------------------------------- the maths


def test_free_slots_are_the_limit_minus_what_is_owned() -> None:
    assert capacity(limit=20, owned=13).free_new_slots == 7
    assert capacity(limit=20, owned=20).free_new_slots == 0
    # Never negative: an account over its own limit has no slots, not -3.
    assert capacity(limit=20, owned=23).free_new_slots == 0


def test_a_new_contact_bot_costs_one_slot() -> None:
    assert slot_cost(PeerBotStatus.NOT_CREATED) == 1
    assert slot_cost(PeerBotStatus.LOCAL_STATE_MISMATCH) == 1


def test_rebuilding_an_owned_bot_costs_nothing() -> None:
    """The number this whole module exists to get right."""
    assert slot_cost(PeerBotStatus.OWNED_REPLACEABLE) == 0

    state = capacity(
        plan(1, PeerBotStatus.OWNED_REPLACEABLE),
        plan(2, PeerBotStatus.NOT_CREATED),
    ).with_selection({1, 2})

    assert state.selected_new_count == 1
    assert state.replacement_count == 1


def test_a_foreign_username_is_not_replaceable() -> None:
    state = capacity(plan(1, PeerBotStatus.FOREIGN_USERNAME_COLLISION))

    assert not state.plans[0].selectable
    assert not state.can_select(1)


def test_local_state_is_not_evidence_of_ownership() -> None:
    """Telegram is the source of truth; a stale row does not make a bot ours."""
    assert (
        classify(UsernameState.FREE, locally_claimed=True)
        is PeerBotStatus.LOCAL_STATE_MISMATCH
    )
    assert classify(UsernameState.OWNED, locally_claimed=False) is PeerBotStatus.OWNED_REPLACEABLE
    assert (
        classify(UsernameState.FOREIGN, locally_claimed=True)
        is PeerBotStatus.FOREIGN_USERNAME_COLLISION
    )
    # And a mismatch still costs a slot: the old token is not reused.
    assert slot_cost(PeerBotStatus.LOCAL_STATE_MISMATCH) == 1


def test_more_new_bots_than_slots_cannot_be_selected() -> None:
    state = capacity(
        *[plan(index, PeerBotStatus.NOT_CREATED) for index in range(1, 5)],
        limit=15,
        owned=13,
    )

    assert state.can_select(1)
    two = state.with_selection({1, 2})
    assert two.selected_new_count == 2
    assert not two.can_select(3), "two free slots, two taken"


def test_at_the_limit_only_zero_cost_replacements_remain() -> None:
    state = capacity(
        plan(1, PeerBotStatus.OWNED_REPLACEABLE),
        plan(2, PeerBotStatus.NOT_CREATED),
        limit=20,
        owned=20,
    )

    assert state.at_limit
    assert state.can_select(1)
    assert not state.can_select(2)


def test_an_unknown_limit_permits_rebuilds_and_nothing_else() -> None:
    state = capacity(
        plan(1, PeerBotStatus.OWNED_REPLACEABLE),
        plan(2, PeerBotStatus.NOT_CREATED),
        limit=None,
    )

    assert state.free_new_slots is None
    assert state.can_select(1)
    assert not state.can_select(2)


def test_deselecting_is_always_allowed() -> None:
    """Otherwise a full account would trap the owner in their own selection."""
    state = capacity(plan(1, PeerBotStatus.NOT_CREATED), limit=20, owned=20)
    assert state.with_selection({1}).can_select(1)


# ---------------------------------------------------------------- the server


def test_the_server_error_is_recognised_whatever_it_is_called() -> None:
    """`BOT_CREATE_LIMIT_EXCEEDED` is the final word, after any local preflight."""
    for text in ("BOT_CREATE_LIMIT_EXCEEDED", "BOTS_TOO_MUCH", "too many bots"):
        assert is_limit_error(RuntimeError(text)), text
    assert not is_limit_error(RuntimeError("network unreachable"))


async def test_creation_stops_safely_when_telegram_refuses() -> None:
    provisioner = FakeProvisioner(owned={f"bot{index}_max_bot": index for index in range(3)})
    provisioner.limit = 3

    with pytest.raises(CreationLimitError):
        await provisioner.create_bot(name="Мама", username="new_max_bot")

    assert provisioner.created == []
    assert provisioner.deleted == [], "nothing is destroyed on the way to refusing"


# ------------------------------------------------------------ one snapshot


def _flow(tmp_path: Path, provisioner: FakeProvisioner) -> DialogFlow:
    from tests.fake_provisioning import (
        FakeBridgeRepository,
        FakeDialogPicker,
        FakeGateway,
    )

    options = [
        DialogOption(max_chat_id=1000, title="Мама", last_activity=1, max_user_id=2000)
    ]
    return DialogFlow(
        picker=FakeDialogPicker(options),  # type: ignore[arg-type]
        provisioner=provisioner,
        gateway=FakeGateway(),
        journal=ProvisioningJournal.for_data_dir(tmp_path),
        bridges=FakeBridgeRepository(),
        telegram_owner_user_id=100000001,
    )


async def test_the_home_screen_and_the_picker_read_one_snapshot(tmp_path: Path) -> None:
    """The bug this file was extended for: two screens, two counts, one account.

    The home screen used to ask Telegram for the numbers itself. That is how it
    came to promise free slots beside a picker that had already been refused —
    both were right about a different moment.
    """
    from bridge.provisioning.selection import capacity_lines, picker_text

    provisioner = FakeProvisioner(strangers=1, limit=40)
    flow = _flow(tmp_path, provisioner)

    picker, _ = await flow.open()
    snapshot = await flow.capacity_snapshot()

    assert snapshot is not None
    assert provisioner.owned_reads == 1, "one reading serves both screens"
    header = capacity_lines(snapshot, selection=False)
    assert header == ["Боты Telegram: 1 из 40", "Свободно новых слотов: 39"]
    # The picker no longer prints them — thirty-nine free slots is not a fact
    # that changes what the next tap does — but it is the same snapshot behind
    # both, which is the property this test exists for.
    assert "Выберите диалоги MAX" in picker
    assert picker_text(snapshot).count("Боты Telegram") == 0


async def test_a_snapshot_carries_when_and_where_it_came_from(tmp_path: Path) -> None:
    """A number with no timestamp is a number nobody can decide to distrust."""
    provisioner = FakeProvisioner(limit=40)
    snapshot = await _flow(tmp_path, provisioner).capacity_snapshot()

    assert snapshot is not None
    assert snapshot.source == SOURCE_TELEGRAM
    assert snapshot.checked_at > 0
    assert snapshot.is_fresh()
    assert not snapshot.is_fresh(now=snapshot.checked_at + FRESH_SECONDS + 1)


async def test_a_stale_snapshot_is_taken_again_before_it_is_used(tmp_path: Path) -> None:
    provisioner = FakeProvisioner(limit=40)
    flow = _flow(tmp_path, provisioner)

    await flow.capacity_snapshot()
    await flow.capacity_snapshot()
    assert provisioner.owned_reads == 1, "a fresh answer is not asked for twice"

    await flow.capacity_snapshot(refresh=True)
    assert provisioner.owned_reads == 2, "anything about to act asks again"
