"""What the sixteen-hour lockout costs, and what stops it happening again.

Five `/newbot` walks back to back, and @BotFather stopped answering for 58000
seconds. That incident is why bot creation moved to Managed Bots at all — and
Managed Bots has since failed twice on the one step nothing can route around,
Telegram's own Create button, which is why creation is back on the owner's
session.

So the incident has to be answered here rather than avoided. Three things do it,
and every one of them is pinned below: one walk at a time, a gap between walks,
and a refusal that sticks for exactly as long as @BotFather asked.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bridge.provisioning.mtproto import BotFatherTooSoonError, CreatedBot
from bridge.provisioning.provisioner import (
    NEWBOT_MIN_INTERVAL_SECONDS,
    FloodWaitError,
    MtprotoProvisioner,
    ProvisioningFailure,
    classify_failure,
)

pytestmark = pytest.mark.asyncio


class Session:
    """A @BotFather that counts walks and can refuse on cue.

    `owns` is what the account already has: a username in it is reused rather
    than created, so an empty set is "every walk is a real creation".
    """

    def __init__(
        self, *, refuse: BaseException | None = None, owns: set[str] | None = None
    ) -> None:
        self.walks: list[str] = []
        self.refuse = refuse
        self.owns = owns or set()
        self.tokens_taken: list[str] = []

    async def admined_bots(self) -> list[Any]:
        from bridge.provisioning.mtproto import OwnedBot

        return [
            OwnedBot(bot_id=index, username=name)
            for index, name in enumerate(sorted(self.owns), start=1)
        ]

    async def token_of(self, username: str) -> str:
        self.tokens_taken.append(username)
        return f"9:{username}"

    async def create_bot(self, *, name: str, username: str) -> CreatedBot:
        self.walks.append(username)
        if self.refuse is not None:
            raise self.refuse
        return CreatedBot(username=username, token=f"1:{username}")


class Clock:
    """A sleep that records instead of waiting."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


def _provisioner(session: Any, clock: Any) -> MtprotoProvisioner:
    return MtprotoProvisioner(session, sleep=clock)


# ------------------------------------------------------------------- pacing


async def test_the_first_walk_waits_for_nothing() -> None:
    clock = Clock()
    await _provisioner(Session(), clock).create_bot(name="A", username="a_max_bot")

    assert clock.slept == []


async def test_the_second_walk_waits_out_the_interval() -> None:
    """The whole defect: five of these went out with no gap at all."""
    clock = Clock()
    provisioner = _provisioner(Session(), clock)

    await provisioner.create_bot(name="A", username="a_max_bot")
    await provisioner.create_bot(name="B", username="b_max_bot")

    assert len(clock.slept) == 1
    assert 0 < clock.slept[0] <= NEWBOT_MIN_INTERVAL_SECONDS


async def test_a_refused_walk_still_counts_as_a_conversation() -> None:
    """@BotFather has been spoken to either way; the next one still waits."""
    clock = Clock()
    session = Session(refuse=BotFatherTooSoonError(1))
    provisioner = _provisioner(session, clock)

    with pytest.raises(FloodWaitError):
        await provisioner.create_bot(name="A", username="a_max_bot")

    assert provisioner._last_newbot is not None


async def test_two_walks_never_interleave() -> None:
    """BotFather is one conversation with one position in it. Two walks at once
    answer each other's questions, and it looks like a bot with the wrong name."""
    inside = 0
    seen = 0

    class Slow(Session):
        async def create_bot(self, *, name: str, username: str) -> CreatedBot:
            nonlocal inside, seen
            inside += 1
            seen = max(seen, inside)
            await asyncio.sleep(0)
            inside -= 1
            return await super().create_bot(name=name, username=username)

    provisioner = _provisioner(Slow(), Clock())
    await asyncio.gather(
        provisioner.create_bot(name="A", username="a_max_bot"),
        provisioner.create_bot(name="B", username="b_max_bot"),
    )

    assert seen == 1


# ----------------------------------------------------------- the sticky stop


async def test_the_refusal_is_honoured_for_as_long_as_it_asked() -> None:
    """58000 seconds is a real answer from this account, and so is 62. A single
    cooldown cannot be right for both, so neither is invented."""
    session = Session(refuse=BotFatherTooSoonError(58000))
    provisioner = _provisioner(session, Clock())

    with pytest.raises(FloodWaitError) as first:
        await provisioner.create_bot(name="A", username="a_max_bot")
    assert first.value.seconds == 58000

    with pytest.raises(FloodWaitError) as second:
        await provisioner.create_bot(name="B", username="b_max_bot")

    assert session.walks == ["a_max_bot"], "the second contact never reached BotFather"
    assert "мин" in str(second.value)


async def test_a_refusal_with_no_number_still_stops_the_batch() -> None:
    session = Session(refuse=BotFatherTooSoonError(None))
    provisioner = _provisioner(session, Clock())

    with pytest.raises(FloodWaitError):
        await provisioner.create_bot(name="A", username="a_max_bot")
    with pytest.raises(FloodWaitError):
        await provisioner.create_bot(name="B", username="b_max_bot")

    assert session.walks == ["a_max_bot"]


async def test_the_stop_lifts_when_the_wait_is_over() -> None:
    session = Session(refuse=BotFatherTooSoonError(1))
    provisioner = _provisioner(session, Clock())

    with pytest.raises(FloodWaitError):
        await provisioner.create_bot(name="A", username="a_max_bot")

    # Rewind the deadline rather than living through it.
    provisioner._quiet_until = provisioner._now() - 1
    provisioner._last_newbot = None
    session.refuse = None

    created = await provisioner.create_bot(name="B", username="b_max_bot")

    assert created.username == "b_max_bot"


async def test_being_asked_to_wait_is_never_reported_as_a_full_account() -> None:
    """Waiting fixes one and only deleting a bot fixes the other. Told apart
    here, they were one reply kind before — and an owner with thirty-three free
    slots was sent to delete something."""
    session = Session(refuse=BotFatherTooSoonError(62))
    provisioner = _provisioner(session, Clock())

    with pytest.raises(FloodWaitError) as raised:
        await provisioner.create_bot(name="A", username="a_max_bot")

    failure = classify_failure(raised.value)
    assert failure is ProvisioningFailure.FLOOD_WAIT
    assert failure.retryable
    assert failure is not ProvisioningFailure.BOT_CREATE_LIMIT_EXCEEDED


# --------------------------------------------------- borrowing, not opening


async def test_the_borrowed_session_is_never_closed() -> None:
    """Closing it would take the owner's whole intake down with it.

    Provisioning shares intake's connection because a second Telethon client on
    the same session file is how an account's keys get revoked — the reason
    `AccountBots` takes a provider too.
    """
    from bridge.provisioning.mtproto import BorrowedBotFatherSession

    class Client:
        def __init__(self) -> None:
            self.disconnected = False

        async def disconnect(self) -> None:
            self.disconnected = True

    client = Client()
    session = BorrowedBotFatherSession(lambda: client, secrets_dir=None)  # type: ignore[arg-type]

    await session.close()

    assert not client.disconnected


async def test_a_client_that_is_not_up_is_a_wait_not_a_verdict() -> None:
    """Intake reconnects. A walk that starts in the gap must say "not now",
    not fail with an AttributeError three frames in."""
    from bridge.provisioning.mtproto import (
        BorrowedBotFatherSession,
        SessionUnavailableError,
    )

    session = BorrowedBotFatherSession(lambda: None, secrets_dir=None)  # type: ignore[arg-type]

    assert not session.connected
    # Through the walk itself: it raises before a single message is sent, which
    # is the property that matters — an absent client must cost nothing.
    with pytest.raises(SessionUnavailableError):
        await session.create_bot(name="A", username="a_max_bot")


async def test_a_reconnect_is_picked_up() -> None:
    """Resolved on every call, so a replaced client is not a dead provisioner."""
    from bridge.provisioning.mtproto import BorrowedBotFatherSession

    live: list[Any] = [None]
    session = BorrowedBotFatherSession(lambda: live[0], secrets_dir=None)  # type: ignore[arg-type]

    assert not session.connected
    live[0] = object()
    assert session.connected


# ----------------------------------------------- an existing bot is reused


async def test_a_bot_that_already_exists_is_never_recreated() -> None:
    """The owner disconnected a contact and reconnected it, and provisioning
    said the deterministic username belonged to somebody else.

    It belonged to *them*. `/newbot` at a name the account already holds answers
    "username is taken", and the batch reports that as a foreign collision — a
    lie, and a dead end. `/token` hands the existing one over instead, which is
    exactly what `getManagedBotToken` did on the other path.
    """
    session = Session(owns={"a_max_bot"})
    provisioner = _provisioner(session, Clock())

    created = await provisioner.create_bot(name="A", username="a_max_bot")

    assert session.walks == [], "@BotFather was never asked to create anything"
    assert session.tokens_taken == ["a_max_bot"]
    assert created.token == "9:a_max_bot"


async def test_reuse_costs_no_creation_and_no_pause() -> None:
    """Nothing was created, so nothing is owed to the rate limit."""
    session = Session(owns={"a_max_bot"})
    clock = Clock()
    provisioner = _provisioner(session, clock)

    await provisioner.create_bot(name="A", username="a_max_bot")
    await provisioner.create_bot(name="A", username="a_max_bot")

    assert clock.slept == []


async def test_a_name_the_account_does_not_hold_is_still_created() -> None:
    session = Session(owns={"someone_else_bot"})
    provisioner = _provisioner(session, Clock())

    created = await provisioner.create_bot(name="B", username="b_max_bot")

    assert session.walks == ["b_max_bot"]
    assert session.tokens_taken == []
    assert created.username == "b_max_bot"
