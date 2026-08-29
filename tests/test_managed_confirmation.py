"""The confirmation is registered before its link is drawn, and a timeout asks.

Two defects are pinned here, both measured against `ManagedBotProvisioner`.

The expectation used to be registered *after* the link reached the screen and
after two Bot API round trips. An owner who tapped Create inside that window hit
`on_managed_bot` with nobody waiting; the update was dropped, the run sat out the
whole ten-minute timeout and failed — on a bot that by then existed in Telegram,
holding a slot, with nothing local naming it.

And a timeout was treated as proof that nothing was created. It is not proof of
anything: the three things it can mean need three different answers.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from bridge.provisioning.batch import ProvisioningBatch
from bridge.provisioning.journal import ItemState, JournalEntry, ProvisioningJournal
from bridge.provisioning.managed import ManagedBotProvisioner
from bridge.provisioning.mtproto import OwnedBot
from bridge.provisioning.provisioner import ConfirmationTimeoutError, UsernameState

pytestmark = pytest.mark.asyncio

TOKEN = "999999999:TTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTT"


class Manager:
    """A manager bot whose Bot API calls take time, as real ones do.

    `getManagedBotToken` answers only for bots this manager owns — which is the
    measured behaviour, and the thing "is this bot mine" is decided by.
    """

    def __init__(self, *, can_manage: bool = True, owns: set[int] | None = None) -> None:
        self.can_manage = can_manage
        self.calls: list[str] = []
        self.owns = owns if owns is not None else set()

    async def get_me(self) -> Any:
        await asyncio.sleep(0.01)
        return type("Me", (), {"can_manage_bots": self.can_manage, "id": 1})()

    async def __call__(self, method: Any) -> str:
        self.calls.append(type(method).__name__)
        await asyncio.sleep(0.01)
        wanted = getattr(method, "user_id", None)
        if wanted is not None and int(wanted) not in self.owns:
            raise RuntimeError(f"Bad Request: bot {wanted} is not managed by this bot")
        return TOKEN


class Owned:
    """The account's bots, plus a hook that fires while they are being listed."""

    def __init__(self, bots: list[OwnedBot] | None = None) -> None:
        self.bots = list(bots or [])
        self.on_list: Any = None
        self.holder: int | None = None
        self.listings = 0

    async def admined_bots(self) -> list[OwnedBot]:
        self.listings += 1
        await asyncio.sleep(0.01)
        if self.on_list is not None:
            self.on_list()
        return list(self.bots)

    async def username_holder(self, username: str) -> int | None:
        return self.holder

    async def bot_creation_limit(self) -> int | None:
        return 20

    async def is_premium(self) -> bool:
        return False

    async def send_start(self, username: str, bot_id: int | None = None) -> None:
        return None


def provisioner_for(
    owned: Owned, *, timeout: float = 0.2, owns: set[int] | None = None
) -> ManagedBotProvisioner:
    managed = {bot.bot_id for bot in owned.bots} | {4242, 7, 77, 78, 79} | (owns or set())
    return ManagedBotProvisioner(
        manager=Manager(owns=managed),
        manager_username="guard_bot",
        owned=owned,
        confirmation_timeout=timeout,
    )


# ------------------------------------------------------------------ ordering


async def test_prepare_registers_the_expectation_before_anything_is_drawn() -> None:
    owned = Owned()
    provisioner = provisioner_for(owned)

    needed_no_confirmation = await provisioner.prepare("p_max_bot")

    assert needed_no_confirmation is False
    assert provisioner.on_managed_bot(4242, "p_max_bot") is True, (
        "the update has somewhere to land before the link exists"
    )


async def test_prepare_says_so_when_the_bot_is_already_there() -> None:
    owned = Owned([OwnedBot(bot_id=7, username="p_max_bot")])
    provisioner = provisioner_for(owned)

    assert await provisioner.prepare("p_max_bot") is True
    assert provisioner.on_managed_bot(7, "p_max_bot") is False, "nothing was awaited"


async def test_a_confirmation_that_beats_the_screen_is_not_lost() -> None:
    """The race, end to end: Create is tapped while the round trips are in flight."""
    owned = Owned()
    provisioner = provisioner_for(owned, timeout=0.3)

    def tap() -> None:
        provisioner.on_managed_bot(4242, "p_max_bot")

    await provisioner.prepare("p_max_bot")
    owned.on_list = tap  # the owner taps during `create_bot`'s own listing

    created = await provisioner.create_bot(name="P", username="p_max_bot")

    assert created.bot_id == 4242
    assert created.token == TOKEN


async def test_the_batch_prepares_before_it_draws(tmp_path: Path) -> None:
    """Ordering asserted where it matters: through the real walk."""
    owned = Owned()
    provisioner = provisioner_for(owned, timeout=0.5)
    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.begin([JournalEntry(max_chat_id=10, expected_username="p_max_bot", title="P")])
    drawn: list[str] = []

    async def progress(entries: list[JournalEntry]) -> None:
        drawn.append(entries[0].state.value)
        if entries[0].state is ItemState.AWAITING_CONFIRMATION:
            # The screen with the Create link is on the owner's phone now.
            provisioner.on_managed_bot(4242, "p_max_bot")

    class Gateway:
        async def stop_bridge(self, max_chat_id: int) -> None: ...

        async def save_token(self, *, max_chat_id: int, username: str, token: str) -> str:
            return "TELEMAX_BOT_P"

        async def start_worker(self, **kwargs: Any) -> str:
            return "p"

        async def is_healthy(self, max_chat_id: int) -> bool:
            return True

        async def mark_active(self, max_chat_id: int) -> None: ...

    result = await ProvisioningBatch(
        provisioner=provisioner,
        gateway=Gateway(),  # type: ignore[arg-type]
        journal=journal,
        display_name_of=lambda entry: entry.title,
        progress=progress,
        chat_ids=[10],
    ).run()

    assert ItemState.AWAITING_CONFIRMATION.value in drawn
    assert result.entries[0].state is ItemState.HEALTHY
    assert result.entries[0].telegram_bot_id == 4242


# ------------------------------------------------------- timeout is a question


async def test_a_timeout_on_a_bot_that_exists_adopts_it() -> None:
    """The lost-update case. The old code reported "nobody confirmed"."""
    owned = Owned()
    provisioner = provisioner_for(owned, timeout=0.05)
    # Telegram will say the bot is ours the moment anybody asks again.
    owned.bots = [OwnedBot(bot_id=4242, username="p_max_bot")]

    created = await provisioner.create_bot(name="P", username="p_max_bot")

    assert created.bot_id == 4242, "a timeout is not evidence that nothing happened"


async def test_a_timeout_on_a_free_username_stays_a_timeout() -> None:
    owned = Owned()
    provisioner = provisioner_for(owned, timeout=0.05)

    with pytest.raises(ConfirmationTimeoutError):
        await provisioner.create_bot(name="P", username="p_max_bot")


async def test_a_timeout_on_a_name_somebody_holds_is_not_a_timeout() -> None:
    """A manager cannot tell a stranger's bot from the owner's unmanaged one.

    Both answer `BOT_ACCESS_FORBIDDEN`, so this does not guess: it reports the
    state whose remedy fits either, and never reports a timeout for a name that
    is demonstrably taken.
    """
    from bridge.provisioning.provisioner import BotNotManageableError

    owned = Owned()
    owned.holder = 5150
    provisioner = provisioner_for(owned, timeout=0.05)

    with pytest.raises(BotNotManageableError):
        await provisioner.create_bot(name="P", username="p_max_bot")


async def test_an_unreachable_telegram_after_a_timeout_is_retryable() -> None:
    owned = Owned()
    provisioner = provisioner_for(owned, timeout=0.05)
    real = owned.admined_bots

    async def fails_the_second_time() -> list[OwnedBot]:
        if owned.listings >= 1:
            raise TimeoutError("telegram is not answering")
        return await real()

    owned.admined_bots = fails_the_second_time  # type: ignore[method-assign]

    with pytest.raises(ConfirmationTimeoutError):
        await provisioner.create_bot(name="P", username="p_max_bot")


async def test_an_unreachable_telegram_before_the_wait_is_typed() -> None:
    """The batch catches `ProvisionerError`; a raw one would abandon the walk."""
    from bridge.provisioning.provisioner import ProvisionerError

    owned = Owned()
    provisioner = provisioner_for(owned, timeout=0.05)

    async def unreachable() -> list[OwnedBot]:
        raise TimeoutError("telegram is not answering")

    owned.admined_bots = unreachable  # type: ignore[method-assign]

    with pytest.raises(ProvisionerError):
        await provisioner.create_bot(name="P", username="p_max_bot")


async def test_no_second_username_is_ever_invented() -> None:
    owned = Owned()
    provisioner = provisioner_for(owned, timeout=0.05)

    with pytest.raises(ConfirmationTimeoutError):
        await provisioner.create_bot(name="P", username="p_max_bot")

    assert await provisioner.check_username("p_max_bot") is UsernameState.FREE


# ------------------------------------------------------------- expectations


async def test_a_foreign_update_is_not_accepted() -> None:
    provisioner = provisioner_for(Owned())
    await provisioner.prepare("p_max_bot")

    assert provisioner.on_managed_bot(1, "somebody_else_bot") is False
    assert provisioner.on_managed_bot(4242, "p_max_bot") is True


async def test_a_duplicate_update_is_idempotent() -> None:
    provisioner = provisioner_for(Owned())
    await provisioner.prepare("p_max_bot")

    assert provisioner.on_managed_bot(4242, "p_max_bot") is True
    assert provisioner.on_managed_bot(4242, "p_max_bot") is False


async def test_an_expectation_is_dropped_when_the_bot_turns_out_to_exist() -> None:
    owned = Owned()
    provisioner = provisioner_for(owned)
    await provisioner.prepare("p_max_bot")
    owned.bots = [OwnedBot(bot_id=7, username="p_max_bot")]

    await provisioner.create_bot(name="P", username="p_max_bot")

    assert provisioner.on_managed_bot(7, "p_max_bot") is False, "nothing is left waiting"


# ------------------------------------------- a bot the owner made by hand


async def test_a_bot_created_by_hand_is_adopted_not_waited_for() -> None:
    """Telegram's own Create dialog refuses sometimes; @BotFather still works.

    Measured in production: three managed bots in forty seconds and the fourth
    dialog's Create button went grey. The owner made that bot in @BotFather at
    the same V2 username — and provisioning went on waiting for a confirmation
    of a bot that already existed, because the local inventory had no row for it.
    """
    owned = Owned()
    owned.holder = 9000000004  # what `getChat` answers for the hand-made bot
    provisioner = provisioner_for(owned, timeout=0.05, owns={9000000004})

    assert await provisioner.bot_id_for("example_managed_max_bot") == 9000000004
    created = await provisioner.create_bot(name="Папа", username="example_managed_max_bot")
    assert created.bot_id == 9000000004


async def test_a_stranger_holding_the_name_is_still_not_ours() -> None:
    """The fallback proves ownership; it does not assume it."""
    owned = Owned()
    owned.holder = 5150
    provisioner = provisioner_for(owned, timeout=0.05, owns=set())

    assert await provisioner.bot_id_for("somebody_else_bot") is None


async def test_a_bot_the_guardian_cannot_manage_is_not_called_a_collision() -> None:
    """Measured 2026-08-06: `BOT_ACCESS_FORBIDDEN` on a hand-made bot.

    The same account owns the bot and the guardian, and the guardian still
    cannot drive it — management comes from the creation flow, not ownership.
    Reporting that as "somebody else has the name" sends the owner to fix a
    collision that does not exist.
    """
    owned = Owned()
    owned.holder = 9000000004

    class Forbidding(Manager):
        async def __call__(self, method: Any) -> str:
            raise RuntimeError("Telegram server says - Bad Request: BOT_ACCESS_FORBIDDEN")

    provisioner = ManagedBotProvisioner(
        manager=Forbidding(), manager_username="guard_bot", owned=owned
    )

    assert await provisioner.check_username("x_max_bot") is UsernameState.UNMANAGEABLE


async def test_a_lost_packet_is_not_a_verdict() -> None:
    """"Refused" and "could not ask" need opposite handling."""
    from bridge.provisioning.provisioner import ProvisionerError

    owned = Owned()
    owned.holder = 9000000004

    class Unreachable(Manager):
        async def __call__(self, method: Any) -> str:
            raise TimeoutError("telegram is not answering")

    provisioner = ManagedBotProvisioner(
        manager=Unreachable(), manager_username="guard_bot", owned=owned
    )

    with pytest.raises(ProvisionerError):
        await provisioner.check_username("x_max_bot")


async def test_an_unmanageable_bot_fails_the_walk_with_its_own_sentence(
    tmp_path: Path,
) -> None:
    from bridge.provisioning.batch import NOT_MANAGEABLE_MESSAGE, ProvisioningBatch
    from bridge.provisioning.journal import ProvisioningJournal

    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.begin([JournalEntry(max_chat_id=10, expected_username="x_max_bot", title="Папа")])

    class Port:
        async def check_username(self, username: str) -> UsernameState:
            return UsernameState.UNMANAGEABLE

    class Gateway:
        async def stop_bridge(self, max_chat_id: int) -> None: ...
        async def save_token(self, **kwargs: Any) -> str: ...
        async def start_worker(self, **kwargs: Any) -> str: ...
        async def is_healthy(self, max_chat_id: int) -> bool: ...
        async def mark_active(self, max_chat_id: int) -> None: ...

    result = await ProvisioningBatch(
        provisioner=Port(),
        gateway=Gateway(),  # type: ignore[arg-type]
        journal=journal,
        display_name_of=lambda entry: entry.title,
        chat_ids=[10],
    ).run()

    assert result.entries[0].failure == "bot_not_manageable"
    assert result.entries[0].error == NOT_MANAGEABLE_MESSAGE
    assert "другим Telegram-аккаунтом" not in (result.entries[0].error or "")
