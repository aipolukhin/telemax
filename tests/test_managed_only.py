"""`provisioning.mode: managed` — provisioning with no account credential at all.

The fixtures cover username resolution, managed token retrieval and the fact
that Bot API cannot open a private chat on the owner's behalf.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from bridge.config import ProvisioningMode
from bridge.provisioning import (
    BotApiOwnedBots,
    OwnerMustOpenChatError,
    RepositoryKnownBots,
    start_link,
)
from bridge.provisioning.owned import ASSUMED_BOT_LIMIT


class NotFoundError(Exception):
    """Shaped like aiogram's error, which is all the code reads."""

    def __str__(self) -> str:
        return "Telegram server says - Bad Request: chat not found"


@dataclass
class FakeChat:
    id: int


@dataclass
class FakeManager:
    """A guardian bot, as much of one as this module touches."""

    usernames: dict[str, int] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)

    async def get_chat(self, chat_id: str) -> FakeChat:
        self.asked.append(chat_id)
        found = self.usernames.get(chat_id.lstrip("@").lower())
        if found is None:
            raise NotFoundError
        return FakeChat(id=found)


@dataclass
class FakeRecord:
    telegram_bot_id: int | None
    expected_username: str
    title: str = "Мама"


def build(manager: FakeManager, records: list[FakeRecord]) -> BotApiOwnedBots:
    async def active() -> list[Any]:
        return list(records)

    return BotApiOwnedBots(manager=manager, known=RepositoryKnownBots(active))


async def test_a_free_username_is_free_not_foreign() -> None:
    """`chat not found` is the one error that must not read as "taken"."""
    owned = build(FakeManager(), [])

    assert await owned.username_holder("telemax_nothing_here_bot") is None


async def test_a_taken_username_names_its_holder() -> None:
    manager = FakeManager(usernames={"botfather": 93372553})
    owned = build(manager, [])

    assert await owned.username_holder("@BotFather") == 93372553
    assert manager.asked == ["@BotFather"]


async def test_the_bot_count_comes_from_this_installs_own_bridges() -> None:
    """Bot API cannot list the account's bots, so the count is a lower bound."""
    owned = build(
        FakeManager(),
        [
            FakeRecord(telegram_bot_id=1, expected_username="a_max_bot"),
            FakeRecord(telegram_bot_id=2, expected_username="b_max_bot"),
            # A bridge whose bot never came into existence is not a bot.
            FakeRecord(telegram_bot_id=None, expected_username="c_max_bot"),
        ],
    )

    bots = await owned.admined_bots()

    assert [bot.username for bot in bots] == ["a_max_bot", "b_max_bot"]


async def test_the_limit_is_assumed_because_it_cannot_be_read() -> None:
    owned = build(FakeManager(), [])

    assert await owned.bot_creation_limit() == ASSUMED_BOT_LIMIT
    assert await owned.is_premium() is False, "Premium is not visible to a bot"


async def test_opening_a_chat_is_the_owners_job_and_says_so() -> None:
    """A bot may not write first, and Bot API has no way around it."""
    owned = build(FakeManager(), [])

    with pytest.raises(OwnerMustOpenChatError) as raised:
        await owned.send_start("mom_max_bot")

    assert raised.value.username == "mom_max_bot"
    assert raised.value.link == start_link("mom_max_bot")
    assert "?start=" in raised.value.link


def test_managed_is_a_mode_of_its_own() -> None:
    """`managed` and `auto_mtproto` differ by exactly one thing: the session."""
    assert ProvisioningMode.MANAGED.value == "managed"
    assert ProvisioningMode.MANAGED is not ProvisioningMode.AUTO_MTPROTO


# ------------------------------------------------- a disabled bridge is ours


async def test_a_disabled_bridges_bot_is_still_one_of_ours() -> None:
    """The row is what proves ownership, not whether anything is polling it.

    Reading only `active()` made a disconnected contact's bot invisible; asked
    about its username, `BotApiOwnedBots` then fell through to `getChat`, found
    the bot, and reported a stranger — so the picker refused the contact with
    «занят другим Telegram-аккаунтом» and there was no way to reconnect.
    """
    from bridge.provisioning.managed import ManagedBotProvisioner
    from bridge.provisioning.provisioner import UsernameState

    manager = FakeManager(usernames={"b_max_bot": 2})
    owned = build(
        manager,
        [
            FakeRecord(telegram_bot_id=1, expected_username="a_max_bot"),
            # disabled — still a bot, still a slot, still ours
            FakeRecord(telegram_bot_id=2, expected_username="b_max_bot"),
        ],
    )
    provisioner = ManagedBotProvisioner(
        manager=manager, manager_username="guard_bot", owned=owned
    )

    assert await provisioner.check_username("b_max_bot") is UsernameState.OWNED
    assert await provisioner.bot_id_for("b_max_bot") == 2


async def test_a_name_the_manager_cannot_drive_is_not_called_a_stranger() -> None:
    """This path has no way to prove another owner, so it does not claim one.

    A stranger's bot and the owner's own hand-made bot both answer
    `BOT_ACCESS_FORBIDDEN`. `FOREIGN` belongs to the session provisioner, which
    can actually see who holds a username.
    """
    from bridge.provisioning.managed import ManagedBotProvisioner
    from bridge.provisioning.provisioner import UsernameState

    manager = FakeManager(usernames={"someone_else_bot": 9999})
    provisioner = ManagedBotProvisioner(
        manager=manager, manager_username="guard_bot", owned=build(manager, [])
    )

    assert await provisioner.check_username("someone_else_bot") is UsernameState.UNMANAGEABLE


async def test_the_guardian_occupies_a_slot_too() -> None:
    from bridge.provisioning.mtproto import OwnedBot

    manager = FakeManager()

    async def rows() -> list[Any]:
        return [FakeRecord(telegram_bot_id=1, expected_username="a_max_bot")]

    owned = BotApiOwnedBots(
        manager=manager,
        known=RepositoryKnownBots(rows),
        guardian=OwnedBot(bot_id=42, username="guard_telemax_bot"),
    )

    bots = await owned.admined_bots()

    assert sorted(bot.bot_id for bot in bots) == [1, 42]


async def test_the_guardian_is_not_counted_twice() -> None:
    from bridge.provisioning.mtproto import OwnedBot

    manager = FakeManager()

    async def rows() -> list[Any]:
        return [FakeRecord(telegram_bot_id=42, expected_username="guard_telemax_bot")]

    owned = BotApiOwnedBots(
        manager=manager,
        known=RepositoryKnownBots(rows),
        guardian=OwnedBot(bot_id=42, username="guard_telemax_bot"),
    )

    assert len(await owned.admined_bots()) == 1


async def test_a_bot_with_no_row_yet_is_ours_if_telegram_hands_us_its_token() -> None:
    """An interrupted attempt has a bot and no bridge row.

    Reading "not in my rows" as "somebody else's" would make it permanently
    un-resumable. `getManagedBotToken` settles it: it answers for a bot this
    manager created, and refuses for anything else — including, as measured on
    2026-08-06, a bot the same account made by hand. So the refusal means
    `UNMANAGEABLE`, not `FOREIGN`: this path cannot see owners at all.
    """
    from bridge.provisioning.managed import ManagedBotProvisioner
    from bridge.provisioning.provisioner import UsernameState

    class Manager(FakeManager):
        def __init__(self, owns: set[int]) -> None:
            super().__init__(usernames={"p_max_bot": 4242})
            self.owns = owns

        async def get_me(self) -> Any:
            return type("Me", (), {"can_manage_bots": True, "id": 1})()

        async def __call__(self, method: Any) -> str:
            wanted = int(getattr(method, "user_id", 0))
            if wanted not in self.owns:
                raise RuntimeError("Bad Request: bot is not managed by this bot")
            return "4242:TTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTT"

    ours = Manager({4242})
    mine = ManagedBotProvisioner(
        manager=ours, manager_username="guard_bot", owned=build(ours, [])
    )
    assert await mine.check_username("p_max_bot") is UsernameState.OWNED

    theirs = Manager(set())
    other = ManagedBotProvisioner(
        manager=theirs, manager_username="guard_bot", owned=build(theirs, [])
    )
    assert await other.check_username("p_max_bot") is UsernameState.UNMANAGEABLE
