"""Commands stay with the bot, and the bot count is about the account.

Two defects the owner found in use.

`/start` typed in a bridge chat was answered by the bot *and* delivered to the
contact in MAX. Two paths read that chat — the Bot API router and the owner's
own MTProto session — and only one of them filtered commands.

And the picker counted the bridges this install had made, printed the number as
if it were the account's, and took the limit from configuration. The owner had
six bots against a screen reading five, and a Premium cap of forty against a
configured twenty.
"""

from __future__ import annotations

from typing import Any

import pytest

from bridge.telegram.commands import is_bot_command

pytestmark = pytest.mark.asyncio


# ----------------------------------------------------------------- the rule


@pytest.mark.parametrize(
    "text",
    ["/start", "/start payload", "/status", "/guard@my_max_bot", "  /help", "/a"],
)
async def test_a_command_is_recognised(text: str) -> None:
    assert is_bot_command(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        None,
        "привет",
        "/etc/passwd",  # a path, and Telegram does not call it a command either
        "/привет",  # Cyrillic: not a command to Telegram
        "//",
        "/",
        "/1start",  # must begin with a letter
        "и /start в середине",
        "текст\n/start",  # only a *leading* command counts
    ],
)
async def test_ordinary_text_is_not(text: str | None) -> None:
    assert not is_bot_command(text)


async def test_the_rule_is_one_object_shared_by_both_paths() -> None:
    """The defect was two rules, not a missing one."""
    from bridge.routing import adapters
    from bridge.telegram import mtproto_intake

    assert adapters.is_bot_command is is_bot_command
    assert mtproto_intake.is_bot_command is is_bot_command


# ------------------------------------------------- the session intake path


class Carried:
    """A router that records what the intake decided to carry."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.media: list[Any] = []

    def bridge_name_for_bot(self, bot_id: int) -> str | None:
        return "b"

    async def on_telegram_text(self, *, text: str, **_: Any) -> None:
        self.texts.append(text)

    async def on_telegram_media(self, *, caption: str | None = None, **_: Any) -> None:
        self.media.append(caption)

    async def on_telegram_contact(self, **_: Any) -> None:
        return None


def _intake(router: Any) -> Any:
    from bridge.telegram.mtproto_intake import MtprotoIntake

    async def allowed() -> set[int]:
        return {77}

    return MtprotoIntake(router=router, allowed_bots=allowed)


def _owner_message(text: str, *, media: Any = None) -> Any:
    from bridge.telegram.mtproto_intake import OwnerMessage

    return OwnerMessage(
        account_id=1,
        peer_id=77,
        message_id=5,
        text=text,
        media=media,
        grouped_id=None,
        reply_to_message_id=None,
    )


async def test_a_command_does_not_reach_the_contact() -> None:
    router = Carried()
    await _intake(router).on_owner_message(_owner_message("/start"))

    assert router.texts == [], "the contact must not be sent «/start»"


async def test_ordinary_text_still_reaches_the_contact() -> None:
    router = Carried()
    await _intake(router).on_owner_message(_owner_message("привет"))

    assert router.texts == ["привет"]


async def test_a_photo_whose_caption_starts_with_a_slash_is_still_carried() -> None:
    """Refusing it would lose the picture to save a word."""
    from bridge.telegram.mtproto_intake import OwnerMedia

    router = Carried()
    media = OwnerMedia(kind="photo", reference={"id": 5})
    await _intake(router).on_owner_message(_owner_message("/start", media=media))

    assert router.media == ["/start"]
    assert router.texts == []


# --------------------------------------------------------------- the count


class Account:
    """The owner's session, as far as capacity is concerned."""

    def __init__(self, *, count: int | None = None, limit: int | None = None) -> None:
        self._count = count
        self._limit = limit

    async def count(self) -> int | None:
        return self._count

    async def creation_limit(self) -> int | None:
        return self._limit

    async def premium(self) -> bool:
        return True


class Rows:
    def __init__(self, bots: list[Any]) -> None:
        self._bots = bots

    async def known_bots(self) -> list[Any]:
        return list(self._bots)


def _source(**kwargs: Any) -> Any:
    from bridge.provisioning.mtproto import OwnedBot
    from bridge.provisioning.owned import BotApiOwnedBots

    return BotApiOwnedBots(
        manager=object(),
        known=Rows([OwnedBot(bot_id=index, username=f"b{index}_max_bot") for index in (1, 2)]),
        assumed_limit=20,
        guardian=OwnedBot(bot_id=99, username="guard_telemax_bot"),
        **kwargs,
    )


async def test_with_no_session_the_account_total_is_unknown() -> None:
    """None, not a guess. It is what makes the screen say whose bots it counted."""
    source = _source()

    assert await source.account_bot_count() is None
    assert await source.bot_creation_limit() == 20
    assert await source.is_premium() is False


async def test_with_a_session_the_account_answers() -> None:
    source = _source(account=Account(count=6, limit=40))

    assert await source.account_bot_count() == 6
    assert await source.bot_creation_limit() == 40
    assert await source.is_premium() is True


async def test_a_session_that_cannot_answer_the_limit_falls_back() -> None:
    source = _source(account=Account(count=6, limit=None))

    assert await source.account_bot_count() == 6
    assert await source.bot_creation_limit() == 20


async def test_a_broken_session_never_breaks_the_screen() -> None:
    class Broken:
        async def count(self) -> int | None:
            raise RuntimeError("session is down")

        async def creation_limit(self) -> int | None:
            raise RuntimeError("session is down")

        async def premium(self) -> bool:
            raise RuntimeError("session is down")

    source = _source(account=Broken())

    assert await source.account_bot_count() is None
    assert await source.bot_creation_limit() == 20
    assert await source.is_premium() is False


async def test_the_account_total_never_becomes_the_ownership_list() -> None:
    """The two are different questions and folding them would undo UNMANAGEABLE.

    `admined_bots` decides whether a username is ours to *drive*. A bot the
    account owns but this manager cannot manage must stay out of it, or
    `check_username` calls it OWNED and hands out a token that never arrives.
    """
    source = _source(account=Account(count=6, limit=40))

    listed = await source.admined_bots()

    assert len(listed) == 3, "two bridges and the guardian — not the account's six"
    assert await source.account_bot_count() == 6


# ------------------------------------------------------- and on the screen


class Provisioner:
    """Just enough of a provisioner for the capacity snapshot."""

    def __init__(self, *, account: int | None) -> None:
        self._account = account

    async def list_owned_bots(self) -> list[Any]:
        from bridge.provisioning.mtproto import OwnedBot

        return [OwnedBot(bot_id=index, username=f"b{index}_max_bot") for index in (1, 2, 3, 4, 5)]

    async def get_creation_limit(self) -> Any:
        from bridge.provisioning.provisioner import BotLimit

        return BotLimit(value=40, premium=True)

    async def account_bot_count(self) -> int | None:
        return self._account


async def _snapshot(account: int | None) -> Any:
    from bridge.provisioning.flow import DialogFlow

    flow = DialogFlow.__new__(DialogFlow)
    flow._provisioner = Provisioner(account=account)
    flow._capacity = None
    capacity, _ = await flow._read_capacity()
    return capacity


async def test_the_snapshot_prefers_the_account_total() -> None:
    from bridge.provisioning.capacity import SOURCE_TELEGRAM

    capacity = await _snapshot(6)

    assert capacity.owned_bot_count == 6, "not the five this install can name"
    assert capacity.source == SOURCE_TELEGRAM
    assert not capacity.counts_only_ours
    assert capacity.free_new_slots == 34


async def test_without_an_answer_it_falls_back_and_says_so() -> None:
    """The bug this replaced: the local count was reported as Telegram's."""
    from bridge.provisioning.capacity import SOURCE_LOCAL

    capacity = await _snapshot(None)

    assert capacity.owned_bot_count == 5
    assert capacity.source == SOURCE_LOCAL
    assert capacity.counts_only_ours


# ------------------------------------------------- greeting on the first open


async def test_the_open_is_subscribed_to() -> None:
    """Without this line the update is omitted and the handler never runs.

    The same silent omission has cost two debugging sessions before —
    `message_reaction` and `managed_bot` — which is why `registry.py` says so
    out loud. This is the third.
    """
    from bridge.telegram.registry import DEFAULT_ALLOWED_UPDATES

    assert "my_chat_member" in DEFAULT_ALLOWED_UPDATES


class Opened:
    """A `ChatMemberUpdated`, and the bot that would answer it."""

    def __init__(self, *, old: str, new: str, chat_type: str = "private") -> None:
        self.old_chat_member = type("M", (), {"status": old})()
        self.new_chat_member = type("M", (), {"status": new})()
        self.chat = type("C", (), {"id": 42, "type": chat_type})()


class Answering:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []

    async def send_message(self, *, chat_id: int, text: str, reply_markup: Any = None) -> None:
        self.sent.append((text, reply_markup))


async def _open_chat(event: Any, bot: Any, guardian: Any = "guard_telemax_bot") -> None:
    from bridge.telegram.app import build_commands_router

    router = build_commands_router(None, lambda: guardian)
    await router.my_chat_member.handlers[0].callback(event, bot)


async def test_opening_the_chat_greets_without_waiting_for_a_start() -> None:
    """The whole point: the first tap on Start sends no message, and the
    greeting carries the only link back to the guardian."""
    from bridge.telegram.app import BACK_TO_GUARDIAN, START_TEXT

    bot = Answering()
    await _open_chat(Opened(old="left", new="member"), bot)

    assert len(bot.sent) == 1
    text, markup = bot.sent[0]
    assert text == START_TEXT
    assert markup.inline_keyboard[0][0].text == BACK_TO_GUARDIAN


async def test_being_blocked_is_not_a_greeting() -> None:
    bot = Answering()
    await _open_chat(Opened(old="member", new="kicked"), bot)

    assert bot.sent == []


async def test_a_status_that_did_not_move_says_nothing() -> None:
    bot = Answering()
    await _open_chat(Opened(old="member", new="member"), bot)

    assert bot.sent == []


async def test_a_group_is_not_a_bridge_chat() -> None:
    bot = Answering()
    await _open_chat(Opened(old="left", new="member", chat_type="supergroup"), bot)

    assert bot.sent == []


async def test_a_greeting_telegram_refuses_is_not_fatal() -> None:
    class Refusing:
        async def send_message(self, **_: Any) -> None:
            raise RuntimeError("Forbidden: bot was blocked by the user")

    await _open_chat(Opened(old="left", new="member"), Refusing())  # no raise


async def test_every_provisioner_can_be_asked_about_the_account() -> None:
    """A `Protocol` method with a body is a method nobody inherits.

    `account_bot_count` was written into `BotProvisioner` instead of onto the
    class beneath it. Nothing failed: the caller looks it up with `getattr`,
    found nothing, and fell back to counting locally — so the screen showed the
    right number under the wrong label, and only the live log said so.
    """
    from bridge.provisioning.managed import ManagedBotProvisioner
    from bridge.provisioning.provisioner import BotProvisioner, MtprotoProvisioner

    for implementation in (MtprotoProvisioner, ManagedBotProvisioner):
        assert "account_bot_count" in implementation.__dict__, implementation.__name__

    # And the Protocol only declares it. A body there is the trap above.
    declared = BotProvisioner.__dict__["account_bot_count"]
    source = declared.__doc__ or ""
    assert "return" not in source


async def test_the_session_path_counts_the_account_without_asking_twice() -> None:
    """One snapshot, one `getAdminedBots`. Telegram rate-limits it hard."""
    from bridge.provisioning.mtproto import OwnedBot
    from bridge.provisioning.provisioner import MtprotoProvisioner

    class Session:
        def __init__(self) -> None:
            self.reads = 0

        async def admined_bots(self) -> list[OwnedBot]:
            self.reads += 1
            return [OwnedBot(bot_id=index, username=f"b{index}_bot") for index in range(7)]

    session = Session()
    provisioner = MtprotoProvisioner(session)

    assert len(await provisioner.list_owned_bots()) == 7
    assert await provisioner.account_bot_count() == 7
    assert session.reads == 1, "the count reuses the listing it was taken beside"
