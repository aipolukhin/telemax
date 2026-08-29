"""`setup` with no Telegram account credential: a pasted token and one tap.

The old console had to log into the owner's account, and the two reasons for it
are gone: Managed Bots create the contact bots, and the owner's own tap on the
guardian identifies them. What is left is the one thing no API can do — the *first*
bot is made by hand — and this file is about refusing to proceed on anything less
than proof that it was made correctly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from bridge.bootstrap.managed import ManagedGuardian, adopt_guardian
from bridge.bootstrap.plan import read_plan
from bridge.bootstrap.ui import SetupCancelled
from tests.fake_console import FakeUi

TOKEN = "9000000006:AAH-fake-token-for-tests"
OWNER = 100000001


@dataclass
class FakeCheck:
    """Telegram's answers, scripted.

    `replies` is consumed in order when it is set, so a retry can be answered
    differently from the first attempt — which is the whole point of the loop.
    """

    #: token -> (bot_id, username, can_manage_bots)
    bots: dict[str, tuple[int, str, bool]] = field(default_factory=dict)
    replies: list[tuple[int, str, bool] | None] = field(default_factory=list)
    starts: int | None = OWNER
    identified: list[str] = field(default_factory=list)
    waited: int = 0

    async def identify(self, token: str) -> tuple[int, str, bool] | None:
        self.identified.append(token)
        if self.replies:
            return self.replies.pop(0)
        return self.bots.get(token)

    async def await_start(
        self,
        token: str,
        *,
        timeout: float,  # noqa: ASYNC109 - mirrors the real checker's signature
    ) -> int | None:
        self.waited += 1
        return self.starts


def plan_for(tmp_path: Path):  # type: ignore[no-untyped-def]
    return read_plan(tmp_path / "config.yaml")


async def test_a_token_and_a_tap_are_the_whole_of_it(tmp_path: Path) -> None:
    ui = FakeUi(answers={"Токен бота-стража": TOKEN})
    check = FakeCheck(bots={TOKEN: (9000000006, "guard_bot", True)})

    guardian = await adopt_guardian(plan_for(tmp_path), ui, checker=check)

    assert guardian == ManagedGuardian(
        token=TOKEN, bot_id=9000000006, username="guard_bot", owner_user_id=OWNER
    )
    assert ui.hidden == ["Токен бота-стража"], "a token is never echoed"


async def test_the_token_lands_in_env_at_once(tmp_path: Path) -> None:
    """A re-run must not ask again, and the bot must not be stranded."""
    plan = plan_for(tmp_path)
    ui = FakeUi(answers={"Токен бота-стража": TOKEN})
    check = FakeCheck(bots={TOKEN: (1, "guard_bot", True)})

    await adopt_guardian(plan, ui, checker=check)

    assert plan.guardian_token_env in plan.env_path.read_text(encoding="utf-8")


async def test_a_bot_without_management_mode_is_refused_with_the_fix(
    tmp_path: Path,
) -> None:
    """The toggle is the whole difference, so the message is the toggle.

    And the token is not taken on trust in the meantime: the owner flips the
    switch, the console asks Telegram again, and only then proceeds.
    """
    plan = plan_for(tmp_path)
    ui = FakeUi(answers={"Токен бота-стража": TOKEN})
    check = FakeCheck(replies=[(1, "guard_bot", False), (1, "guard_bot", True)])

    guardian = await adopt_guardian(plan, ui, checker=check)

    assert "Bot Management Mode" in ui.transcript
    assert len(check.identified) == 2, "asked again after the owner said they enabled it"
    assert guardian.owner_user_id == OWNER


async def test_a_refused_bot_never_gets_the_owner_asked_for_a_tap(tmp_path: Path) -> None:
    """Cancelling at the "did you enable it?" question stops the whole run."""
    plan = plan_for(tmp_path)
    ui = FakeUi(
        answers={"Токен бота-стража": TOKEN},
        cancel_on={"Включили?"},
    )
    check = FakeCheck(replies=[(1, "guard_bot", False)])

    with pytest.raises(SetupCancelled):
        await adopt_guardian(plan, ui, checker=check)

    assert check.waited == 0
    assert not plan.env_path.exists(), "a bot that cannot manage bots is not written down"


async def test_a_token_telegram_refuses_is_asked_for_again(tmp_path: Path) -> None:
    """A typo is a typo: ask again rather than fail the install."""
    plan = plan_for(tmp_path)
    ui = FakeUi(answers={"Токен бота-стража": TOKEN})
    check = FakeCheck(replies=[None, (1, "guard_bot", True)])

    guardian = await adopt_guardian(plan, ui, checker=check)

    assert "Telegram не принял" in ui.transcript
    assert guardian.username == "guard_bot"


async def test_nobody_pressing_start_cancels_rather_than_guesses(tmp_path: Path) -> None:
    """The owner's id has exactly one source here, and no fallback is honest."""
    ui = FakeUi(answers={"Токен бота-стража": TOKEN})
    check = FakeCheck(bots={TOKEN: (1, "guard_bot", True)}, starts=None)

    with pytest.raises(SetupCancelled):
        await adopt_guardian(plan_for(tmp_path), ui, checker=check)


async def test_the_owner_is_told_which_link_to_open(tmp_path: Path) -> None:
    ui = FakeUi(answers={"Токен бота-стража": TOKEN})
    check = FakeCheck(bots={TOKEN: (1, "guard_bot", True)})

    await adopt_guardian(plan_for(tmp_path), ui, checker=check)

    assert "https://t.me/guard_bot" in ui.transcript
    assert check.waited == 1
