"""Rebuilding the guardian bot: the one deletion setup is allowed to perform.

A second `python -m bridge setup` finds a bot already sitting at the guardian's
deterministic username. Nine times out of ten it is the one the first run made,
and the right thing is to delete it and create it again so there is exactly one
live token. The tenth time it belongs to somebody else, and the right thing is
to stop — which is what most of this file is about, because a wrong answer there
deletes a stranger's bot and there is no undo.

The other rule tested here is ordering: the runtime holding the old token must
be stopped *before* the bot is deleted. Two guardians polling the same chat is a
state with no honest recovery.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bridge.bootstrap.plan import read_plan
from bridge.bootstrap.telegram import GuardianUsernameTakenError, ensure_guardian
from bridge.provisioning.mtproto import MtprotoError
from bridge.provisioning.naming_v2 import guardian_bot_username_v3
from tests.fake_console import FakeUi

OWNER = 100000001


class Session:
    """A user session that owns some bots and can be told about strangers."""

    def __init__(
        self,
        *,
        owned: dict[str, int] | None = None,
        foreign: set[str] | None = None,
        never_released: set[str] | None = None,
    ) -> None:
        self.owned = dict(owned or {})
        self.foreign = set(foreign or set())
        self.never_released = set(never_released or set())
        self.deleted: list[str] = []
        self.created: list[str] = []
        self.events: list[str] = []
        self._next_id = 900

    async def own_user_id(self) -> int:
        return OWNER

    async def admined_bots(self) -> list[Any]:
        from bridge.provisioning.mtproto import OwnedBot

        return [
            OwnedBot(bot_id=identifier, username=name)
            for name, identifier in self.owned.items()
        ]

    async def username_holder(self, username: str) -> int | None:
        if username in self.owned:
            return self.owned[username]
        if username in self.foreign:
            return 4242
        return None

    async def delete_bot(self, username: str) -> None:
        self.events.append(f"delete:{username}")
        self.deleted.append(username)
        if username not in self.never_released:
            self.owned.pop(username, None)

    async def create_bot(self, *, name: str, username: str) -> Any:
        from bridge.provisioning.mtproto import CreatedBot

        self.events.append(f"create:{username}")
        self.created.append(username)
        self._next_id += 1
        self.owned[username] = self._next_id
        return CreatedBot(username=username, token=f"{self._next_id}:{'A' * 35}")


@pytest.fixture
def plan(tmp_path: Path, monkeypatch: Any) -> Any:
    made = read_plan(tmp_path / "config.yaml")
    for name in (made.guardian_token_env, f"{made.guardian_token_env}_USERNAME"):
        monkeypatch.delenv(name, raising=False)
    return made


def expected_username(plan: Any) -> str:
    return guardian_bot_username_v3(OWNER)


# ------------------------------------------------------------------ first run


async def test_a_fresh_account_gets_the_deterministic_bot(plan: Any) -> None:
    session = Session()
    account = await ensure_guardian(plan, FakeUi(), session)  # type: ignore[arg-type]

    assert account.guardian_username == expected_username(plan)
    assert session.created == [expected_username(plan)]
    assert session.deleted == []


async def test_the_token_and_username_are_remembered_in_env(plan: Any) -> None:
    session = Session()
    account = await ensure_guardian(plan, FakeUi(), session)  # type: ignore[arg-type]

    written = plan.env_path.read_text(encoding="utf-8")
    assert f"{plan.guardian_token_env}={account.guardian_token}" in written
    assert f"_USERNAME={account.guardian_username}" in written
    assert (plan.env_path.stat().st_mode & 0o777) == 0o600


# ----------------------------------------------------------------- second run


async def test_an_owned_guardian_is_found_through_telegram(plan: Any) -> None:
    """Not through the local YAML: a copied config claims bots it does not own."""
    username = expected_username(plan)
    session = Session(owned={username: 111})

    await ensure_guardian(plan, FakeUi(), session)  # type: ignore[arg-type]

    assert session.deleted == [username]


async def test_the_bot_is_recreated_under_the_very_same_username(plan: Any) -> None:
    username = expected_username(plan)
    session = Session(owned={username: 111})

    account = await ensure_guardian(plan, FakeUi(), session)  # type: ignore[arg-type]

    assert account.guardian_username == username
    assert session.created == [username]


async def test_the_old_runtime_is_stopped_before_anything_is_deleted(plan: Any) -> None:
    """Otherwise the old token keeps long-polling next to the new one."""
    username = expected_username(plan)
    session = Session(owned={username: 111})
    order: list[str] = []

    def stop() -> None:
        order.append("stop")

    session.events = order  # the session appends delete/create to the same list
    await ensure_guardian(plan, FakeUi(), session, stop_runtime=stop)  # type: ignore[arg-type]

    assert order[0] == "stop"
    assert order[1].startswith("delete:")
    assert order[2].startswith("create:")


async def test_the_new_token_replaces_the_old_one(plan: Any, monkeypatch: Any) -> None:
    username = expected_username(plan)
    monkeypatch.setenv(plan.guardian_token_env, "111111:stale-token-from-the-last-run")
    session = Session(owned={username: 111})

    account = await ensure_guardian(plan, FakeUi(), session)  # type: ignore[arg-type]

    written = plan.env_path.read_text(encoding="utf-8")
    assert "stale-token-from-the-last-run" not in written
    assert account.guardian_token in written


async def test_only_one_guardian_token_is_ever_live(plan: Any) -> None:
    """A deleted bot's token cannot come back, so the file holds exactly one."""
    username = expected_username(plan)
    session = Session(owned={username: 111})

    await ensure_guardian(plan, FakeUi(), session)  # type: ignore[arg-type]

    lines = plan.env_path.read_text(encoding="utf-8").splitlines()
    tokens = [line for line in lines if line.startswith(f"{plan.guardian_token_env}=")]
    assert len(tokens) == 1


# ---------------------------------------------------------- somebody else's


async def test_a_foreign_bot_is_never_deleted(plan: Any) -> None:
    username = expected_username(plan)
    session = Session(foreign={username})

    with pytest.raises(GuardianUsernameTakenError):
        await ensure_guardian(plan, FakeUi(), session)  # type: ignore[arg-type]

    assert session.deleted == []
    assert session.created == []


async def test_a_taken_username_stops_setup_with_a_readable_reason(plan: Any) -> None:
    username = expected_username(plan)
    session = Session(foreign={username})

    with pytest.raises(GuardianUsernameTakenError) as caught:
        await ensure_guardian(plan, FakeUi(), session)  # type: ignore[arg-type]

    message = str(caught.value)
    assert "занято другим Telegram-аккаунтом" in message
    assert f"@{username}" in message
    assert "Traceback" not in message


async def test_no_fallback_username_is_invented(plan: Any) -> None:
    """A near-miss name would work today and break every saved link tomorrow."""
    username = expected_username(plan)
    session = Session(foreign={username})

    with pytest.raises(GuardianUsernameTakenError):
        await ensure_guardian(plan, FakeUi(), session)  # type: ignore[arg-type]

    assert session.created == []


# -------------------------------------------------------------- the awkward


async def test_a_username_telegram_has_not_released_is_not_worked_around(
    plan: Any,
) -> None:
    """Waiting is the answer. Renaming is not, and neither is pretending."""
    username = expected_username(plan)
    session = Session(owned={username: 111}, never_released={username})
    waited: list[float] = []

    async def nap(delay: float) -> None:
        waited.append(delay)

    with pytest.raises(MtprotoError) as caught:
        await ensure_guardian(plan, FakeUi(), session, sleep=nap)  # type: ignore[arg-type]

    assert waited == sorted(waited), "the retry backs off rather than hammering"

    assert "не освободил" in str(caught.value)
    assert session.created == []


async def test_a_fresh_guardian_does_not_create_a_load_bearing_secret(plan: Any) -> None:
    session = Session()
    await ensure_guardian(plan, FakeUi(), session)  # type: ignore[arg-type]

    assert not (plan.secrets_dir / "naming-secret").exists()


# ---------------------------------------------------------------- adoption


async def test_a_working_token_for_the_right_bot_is_reused(
    plan: Any, monkeypatch: Any
) -> None:
    """@BotFather rate-limits creation by the hour. Rebuilding an identical bot
    for no reason is how a re-run ends with no guardian at all."""
    from bridge.bootstrap import telegram as step

    username = expected_username(plan)
    session = Session(owned={username: 111})
    monkeypatch.setenv(plan.guardian_token_env, "111:already-working")
    monkeypatch.setattr(step, "identify_bot", _identifies(111, username))

    account = await ensure_guardian(plan, FakeUi(), session)  # type: ignore[arg-type]

    assert account.guardian_token == "111:already-working"
    assert session.deleted == []
    assert session.created == [], "nothing was asked of @BotFather"


async def test_a_token_for_a_different_bot_is_not_adopted(
    plan: Any, monkeypatch: Any
) -> None:
    """Adopting it would hand the owner a link to the wrong bot, permanently."""
    from bridge.bootstrap import telegram as step

    username = expected_username(plan)
    session = Session(owned={username: 111})
    monkeypatch.setenv(plan.guardian_token_env, "222:someone-elses")
    monkeypatch.setattr(step, "identify_bot", _identifies(222, "other_bot"))

    account = await ensure_guardian(plan, FakeUi(), session)  # type: ignore[arg-type]

    assert account.guardian_username == username
    assert session.deleted == [username], "the real bot is rebuilt instead"
    assert session.created == [username]


async def test_a_dead_token_falls_back_to_rebuilding(plan: Any, monkeypatch: Any) -> None:
    from bridge.bootstrap import telegram as step

    username = expected_username(plan)
    session = Session(owned={username: 111})
    monkeypatch.setenv(plan.guardian_token_env, "111:revoked")
    monkeypatch.setattr(step, "identify_bot", _identifies(None, None))

    await ensure_guardian(plan, FakeUi(), session)  # type: ignore[arg-type]

    assert session.created == [username]


async def test_the_botfather_limit_says_what_to_do_about_it(
    plan: Any, monkeypatch: Any
) -> None:
    """A bare BOT_CREATE_LIMIT_EXCEEDED in a terminal is not an instruction."""
    from bridge.bootstrap.telegram import BotFatherLimitError
    from bridge.provisioning.mtproto import MtprotoError

    username = expected_username(plan)
    session = Session()

    async def refuse(*, name: str, username: str) -> Any:
        raise MtprotoError("BOT_CREATE_LIMIT_EXCEEDED")

    session.create_bot = refuse  # type: ignore[assignment]

    with pytest.raises(BotFatherLimitError) as caught:
        await ensure_guardian(plan, FakeUi(), session)  # type: ignore[arg-type]

    message = str(caught.value)
    assert f"@{username}" in message, "it names the username to create by hand"
    assert "adopt_guardian" in message
    assert "Traceback" not in message


def _identifies(bot_id: int | None, username: str | None) -> Any:
    async def identify(token: str) -> tuple[int, str] | None:
        return None if bot_id is None or username is None else (bot_id, username)

    return identify
