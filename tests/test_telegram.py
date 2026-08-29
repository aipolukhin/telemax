"""WP4 — the bot registry, the owner filter and command isolation."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest
from aiogram import Dispatcher, Router
from aiogram.types import Chat, Message, Update, User
from pydantic import SecretStr

from bridge.config import ResolvedBridge
from bridge.telegram import (
    STRANGER_REPLY,
    BotRegistry,
    BridgeRegistryError,
    build_dispatcher,
)
from bridge.telegram.registry import DEFAULT_ALLOWED_UPDATES
from tests.fake_telegram import FakeBot, FakeBotFactory

OWNER = 111
STRANGER = 222


def bridge(name: str, token: str, max_chat_id: int) -> ResolvedBridge:
    return ResolvedBridge(
        name=name,
        max_chat_id=max_chat_id,
        token_env=f"TOK_{name.upper()}",
        token=SecretStr(token),
    )


def make_update(
    update_id: int,
    text: str,
    *,
    user_id: int = OWNER,
    chat_id: int | None = None,
    chat_type: str = "private",
) -> Update:
    """One update, shaped the way Telegram actually shapes it.

    In a private chat the chat id *is* the user id — there is no third number.
    Using one made the owner filter untestable in the direction that mattered:
    a bot added to a group sees the owner's id with a chat id that is not
    theirs, which is exactly what must be refused.
    """
    user = User(id=user_id, is_bot=False, first_name="Someone")
    message = Message(
        message_id=update_id,
        date=__import__("datetime").datetime.now(tz=__import__("datetime").UTC),
        chat=Chat(id=chat_id if chat_id is not None else user_id, type=chat_type),
        from_user=user,
        text=text,
    )
    return Update(update_id=update_id, message=message)


async def test_registry_starts_a_bot_and_maps_it(dispatcher: Dispatcher) -> None:
    factory = FakeBotFactory()
    registry = BotRegistry(dispatcher, bot_factory=factory)  # type: ignore[arg-type]

    live = await registry.add(bridge("mom", "1:aaa", 777), start=False)

    assert live.identity.bot_id == 100
    assert registry.by_name("mom") is live
    assert registry.by_bot_id(100) is live
    assert registry.by_max_chat(777) is live

    await registry.close()
    assert factory.bots["1:aaa"].session.closed is True


async def test_same_bot_behind_two_variables_is_refused(dispatcher: Dispatcher) -> None:
    """Only getMe can catch this — the config loader sees two different names."""
    factory = FakeBotFactory(bot_ids={"1:aaa": 500, "2:bbb": 500})
    registry = BotRegistry(dispatcher, bot_factory=factory)  # type: ignore[arg-type]

    await registry.add(bridge("mom", "1:aaa", 777), start=False)

    with pytest.raises(BridgeRegistryError, match="same Telegram bot"):
        await registry.add(bridge("dad", "2:bbb", 888), start=False)

    await registry.close()


async def test_two_bridges_cannot_claim_one_dialog(dispatcher: Dispatcher) -> None:
    factory = FakeBotFactory()
    registry = BotRegistry(dispatcher, bot_factory=factory)  # type: ignore[arg-type]

    await registry.add(bridge("mom", "1:aaa", 777), start=False)

    with pytest.raises(BridgeRegistryError, match="exactly one dialog"):
        await registry.add(bridge("dad", "2:bbb", 777), start=False)

    await registry.close()


async def test_invalid_token_names_the_bridge(dispatcher: Dispatcher) -> None:
    factory = FakeBotFactory(unauthorized_tokens={"1:aaa"})
    registry = BotRegistry(dispatcher, bot_factory=factory)  # type: ignore[arg-type]

    with pytest.raises(BridgeRegistryError, match=r"bridge 'mom'.*TOK_MOM"):
        await registry.add(bridge("mom", "1:aaa", 777), start=False)

    # The failed bot must not linger in the registry or hold a session open.
    assert registry.live == ()
    assert factory.bots["1:aaa"].session.closed is True


async def test_bridges_can_be_added_and_removed_while_running(dispatcher: Dispatcher) -> None:
    """WP19 depends on this: no restart when a new contact appears."""
    factory = FakeBotFactory()
    registry = BotRegistry(dispatcher, bot_factory=factory)  # type: ignore[arg-type]

    await registry.add(bridge("mom", "1:aaa", 777))
    await registry.add(bridge("dad", "2:bbb", 888))
    assert len(registry.live) == 2

    await registry.remove("mom")
    assert registry.by_name("mom") is None
    assert registry.by_max_chat(777) is None
    assert len(registry.live) == 1

    # The removed bridge's slot is free again.
    await registry.add(bridge("mom", "3:ccc", 777))
    assert registry.by_max_chat(777) is not None

    await registry.close()


async def test_the_allowlist_is_what_the_bots_actually_poll_for(
    dispatcher: Dispatcher,
) -> None:
    """Telegram sends nothing that is not named here, and `message_reaction` is
    no longer named: the owner's reactions come from their puppet session, so
    asking a contact bot for them would only invite a second ingress."""
    assert "message_reaction" not in DEFAULT_ALLOWED_UPDATES
    assert "edited_message" in DEFAULT_ALLOWED_UPDATES  # still needed for echoes

    factory = FakeBotFactory()
    registry = BotRegistry(dispatcher, bot_factory=factory)  # type: ignore[arg-type]
    await registry.add(bridge("mom", "1:aaa", 777))

    bot = factory.bots["1:aaa"]
    for _ in range(50):
        if bot.method_calls("get_updates"):
            break
        await asyncio.sleep(0.01)

    assert bot.method_calls("get_updates")[0]["allowed_updates"] == DEFAULT_ALLOWED_UPDATES
    await registry.close()


# ----------------------------------------------------------------- owner filter


@pytest.fixture
def dispatcher() -> Dispatcher:
    return build_dispatcher(owner_user_id=OWNER)


async def test_stranger_never_reaches_a_handler() -> None:
    seen: list[str] = []
    dispatcher = build_dispatcher(owner_user_id=OWNER)

    @dispatcher.message()
    async def _catch_all(message: Message) -> None:
        seen.append(message.text or "")

    bot = FakeBot("1:aaa")

    await dispatcher.feed_update(bot, make_update(1, "hello", user_id=OWNER))  # type: ignore[arg-type]
    await dispatcher.feed_update(bot, make_update(2, "who are you", user_id=STRANGER))  # type: ignore[arg-type]

    assert seen == ["hello"]

    # The stranger gets a bland refusal that mentions nothing.
    replies = [call["text"] for call in bot.method_calls("send_message")]
    assert replies == [STRANGER_REPLY]
    assert "MAX" not in STRANGER_REPLY


async def test_commands_are_handled_before_forwarding() -> None:
    """`/status` must answer the owner, not end up typed at their contact."""
    forwarded: list[str] = []

    async def status(bot_id: int) -> str:
        return f"bridge for bot {bot_id} is fine"

    dispatcher = build_dispatcher(owner_user_id=OWNER, status_provider=status)

    # Registered the way the real bridge does it: as a router included *after*
    # the command router, so commands are consumed first.
    forwarding = Router(name="forwarding")

    @forwarding.message()
    async def _forward_to_max(message: Message) -> None:
        forwarded.append(message.text or "")

    dispatcher.include_router(forwarding)

    bot = FakeBot("1:aaa", bot_id=100)
    await dispatcher.feed_update(bot, make_update(1, "/status"))  # type: ignore[arg-type]
    await dispatcher.feed_update(bot, make_update(2, "/start"))  # type: ignore[arg-type]
    await dispatcher.feed_update(bot, make_update(3, "regular text"))  # type: ignore[arg-type]

    assert forwarded == ["regular text"], "commands must not be forwarded to MAX"

    answers = [call["text"] for call in bot.method_calls("send_message")]
    assert "bridge for bot 100 is fine" in answers[0]


async def test_updates_are_dispatched_per_bot(dispatcher: Dispatcher) -> None:
    """Each bot polls on its own task, and its updates carry its own identity."""
    seen: list[tuple[int, str]] = []

    dispatcher = build_dispatcher(owner_user_id=OWNER)

    @dispatcher.message()
    async def _record(message: Message, bot: Any) -> None:
        seen.append((bot.id, message.text or ""))

    factory = FakeBotFactory()
    registry = BotRegistry(dispatcher, bot_factory=factory)  # type: ignore[arg-type]
    await registry.add(bridge("mom", "1:aaa", 777), start=False)
    await registry.add(bridge("dad", "2:bbb", 888), start=False)

    factory.bots["1:aaa"].updates.append([make_update(1, "to mom")])
    factory.bots["2:bbb"].updates.append([make_update(2, "to dad")])

    for live in registry.live:
        live.runner.start()

    for _ in range(100):
        if len(seen) == 2:
            break
        await asyncio.sleep(0.01)

    assert sorted(seen) == [(100, "to mom"), (101, "to dad")]
    await registry.close()


def test_every_update_type_with_a_handler_is_allowed() -> None:
    """Telegram omits update types absent from `allowed_updates`, silently.

    This has cost two debugging sessions already: a `message_reaction` handler
    that looked like "reactions are unsupported in private chats", and a
    `managed_bot` handler that looked like "the owner never pressed Create".
    Both were registered correctly and simply never invoked. So the assertion
    is structural — whatever the routers listen for, the poller must ask for.
    """
    from bridge.onboarding.fsm import MaxOnboarding
    from bridge.onboarding.router import build_onboarding_router
    from bridge.onboarding.state import StateStore
    from bridge.provisioning import GuardianContext, build_guardian_router

    async def nothing(*_: object, **__: object) -> None:
        return None

    class Control:
        async def status_lines(self) -> list[str]:
            return []

        async def restart_bridge(self) -> bool:
            return True

    with tempfile.TemporaryDirectory() as directory:
        store = StateStore.for_data_dir(Path(directory))
        routers = [
            build_guardian_router(GuardianContext()),
            build_onboarding_router(
                owner_user_id=1,
                store=store,
                onboarding=MaxOnboarding(
                    store=store,
                    show=nothing,
                    connect=nothing,
                    persist=nothing,
                    launch=nothing,
                ),
                control=Control(),  # type: ignore[arg-type]
                is_guardian=lambda _: True,
                show=nothing,
            ),
        ]

    listened = {
        name
        for router in routers
        for name, observer in router.observers.items()
        if observer.handlers and name != "update"
    }
    missing = listened - set(DEFAULT_ALLOWED_UPDATES)
    assert not missing, f"handlers exist for {sorted(missing)}, but nothing asks Telegram for them"
