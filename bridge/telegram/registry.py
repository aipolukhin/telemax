"""The set of live bots, and the mapping that makes routing possible.

This is the module dynamic provisioning leans on: bridges are added and removed while the
process runs, so nothing here may assume the configuration file is the final
word. `add` is also where the one-bot-one-dialog invariant gets its last check —
the config loader compares names and variables, but only `getMe` can tell that
two variables hold tokens of the *same* bot.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramUnauthorizedError

from bridge.config import ResolvedBridge

from .app import publish_bridge_menu
from .quiet import silence_history
from .runner import BotIdentity, BotRunner, UpdateInbox

logger = logging.getLogger(__name__)

# Every update type this project uses has to be named here. Telegram omits
# anything absent from `allowed_updates`, and the omission is silent — the
# handler simply never runs, with a clean log and no error anywhere.
#
# This has now cost two debugging sessions. `message_reaction` looked exactly
# like "reactions are not supported in private chats"; `managed_bot` looked
# exactly like "the owner never pressed Create". Neither was true. If a new
# handler is ever added for an update type, add it here in the same commit.
DEFAULT_ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "callback_query",
    # How the guardian learns that the owner confirmed creating a contact bot.
    "managed_bot",
    # The connection id arrives once, in this update, and nothing can ask for it
    # again. Recorded, not acted on — see `provisioning/business.py`.
    "business_connection",
    # The owner opening the chat for the first time. Compatibility tests confirmed: the
    # first tap on Start in a bot Telegram has just created sends *no message* —
    # the bot's own greeting appeared only after a second, manual `/start`. The
    # open is reported here instead, and without this line it was omitted
    # silently, exactly as the note above warns.
    "my_chat_member",
]


class BridgeRegistryError(Exception):
    """A bridge cannot be brought up. The message names the bridge."""


class BotIdentityError(BridgeRegistryError):
    """The token works and belongs to a different bot than the one expected.

    Its own type because the answer is different: a bad token is fixed by
    fetching the token again, and this is fixed by finding out which bot the
    bridge is supposed to be. Adopting the bot the token actually names would
    file one contact's conversation under another contact's bot.
    """


@dataclass(frozen=True, slots=True)
class ExpectedBot:
    """Who a bridge's bot is supposed to be, as far as anything local knows.

    Both fields are optional and mean different things when absent. No `bot_id`
    is a bridge from before the id was written down — it may be bound once, from
    `getMe`. No `username` is the same for the deterministic name. What is
    present is checked; what is absent is learned.
    """

    bot_id: int | None = None
    username: str | None = None

    @property
    def known(self) -> bool:
        return self.bot_id is not None or bool(self.username)


@dataclass(slots=True)
class LiveBridge:
    """A bridge that is currently running."""

    name: str
    max_chat_id: int
    identity: BotIdentity
    runner: BotRunner

    @property
    def bot(self) -> Bot:
        return self.runner.bot


class BotRegistry:
    """Owns every `Bot`, and answers "which bridge is this update for?"."""

    def __init__(
        self,
        dispatcher: Dispatcher,
        *,
        allowed_updates: list[str] | None = None,
        bot_factory: type[Bot] = Bot,
        inbox: UpdateInbox | None = None,
        guardian_bot_id: int | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        # The guardian's token would pass `getMe` like any other, and a bridge
        # polling it would carry a contact's conversation on the bot that runs
        # setup — while the guardian and the bridge fought over `getUpdates`.
        self._guardian_bot_id = guardian_bot_id
        self._allowed_updates = list(allowed_updates or DEFAULT_ALLOWED_UPDATES)
        self._bot_factory = bot_factory
        # Every contact bot polls through this: an update is written down before
        # its offset is acknowledged to Telegram. Without it the runners fall
        # back to dispatching straight through, which is fine for tests and for
        # the guardian, and not fine for a bridge carrying someone's messages.
        self._inbox = inbox
        self._by_name: dict[str, LiveBridge] = {}
        self._by_bot_id: dict[int, LiveBridge] = {}
        self._by_max_chat: dict[int, LiveBridge] = {}
        self._lock = asyncio.Lock()

    # --------------------------------------------------------------- membership

    async def add(
        self,
        bridge: ResolvedBridge,
        *,
        start: bool = True,
        expected: ExpectedBot | None = None,
    ) -> LiveBridge:
        """Bring a bridge up: prove the token names the right bot, then poll.

        `expected` is what the database says this bridge's bot is. Without it a
        token that Telegram accepts was enough — so a rotated, restored or
        mis-pasted token silently bound one contact's chat to another contact's
        bot, and nothing anywhere would have said so.
        """
        async with self._lock:
            if bridge.name in self._by_name:
                raise BridgeRegistryError(f"bridge '{bridge.name}' is already running")
            if bridge.max_chat_id in self._by_max_chat:
                other = self._by_max_chat[bridge.max_chat_id].name
                raise BridgeRegistryError(
                    f"bridge '{bridge.name}' and '{other}' both claim MAX chat "
                    f"{bridge.max_chat_id} — one bot maps to exactly one dialog"
                )

            bot = self._bot_factory(
                token=bridge.token.get_secret_value(),
                default=DefaultBotProperties(parse_mode=None),
            )
            # Here because this is where every contact bot is born, and a bot
            # that missed it would be the one chat that pings through an
            # import. Costs one dictionary lookup per request otherwise.
            with contextlib.suppress(AttributeError):
                bot.session.middleware(silence_history)

            try:
                me = await bot.get_me()
            except TelegramUnauthorizedError as error:
                await bot.session.close()
                raise BridgeRegistryError(
                    f"bridge '{bridge.name}': ${bridge.token_env} is not a valid bot token"
                ) from error
            except Exception as error:
                await bot.session.close()
                raise BridgeRegistryError(
                    f"bridge '{bridge.name}': cannot reach Telegram ({error})"
                ) from error

            identity = BotIdentity(bot_id=me.id, username=me.username)

            if expected is not None:
                problem = _mismatch(bridge.name, identity, expected, self._guardian_bot_id)
                if problem is not None:
                    await bot.session.close()
                    raise BotIdentityError(problem)
            elif self._guardian_bot_id is not None and identity.bot_id == self._guardian_bot_id:
                await bot.session.close()
                raise BotIdentityError(
                    f"bridge '{bridge.name}': ${bridge.token_env} is the guardian's own token"
                )

            if identity.bot_id in self._by_bot_id:
                # Two variables, one bot: both bridges would receive each other's
                # updates, and the owner would see one contact's messages in the
                # other's chat.
                other = self._by_bot_id[identity.bot_id].name
                await bot.session.close()
                raise BridgeRegistryError(
                    f"bridges '{bridge.name}' and '{other}' use the same Telegram bot "
                    f"(@{identity.username}) — give each contact its own bot"
                )

            runner = BotRunner(
                bridge_name=bridge.name,
                bot=bot,
                dispatcher=self._dispatcher,
                allowed_updates=self._allowed_updates,
                inbox=self._inbox,
                bot_id=identity.bot_id,
            )
            live = LiveBridge(
                name=bridge.name,
                max_chat_id=bridge.max_chat_id,
                identity=identity,
                runner=runner,
            )

            self._by_name[bridge.name] = live
            self._by_bot_id[identity.bot_id] = live
            self._by_max_chat[bridge.max_chat_id] = live

        # After the lock, before the log line: the menu is cosmetic and must not
        # be holding the registry lock while Telegram thinks about it.
        await publish_bridge_menu(bot)

        if start:
            runner.start()
        logger.info(
            "bridge %s is up: @%s (%s) <-> MAX chat %s",
            bridge.name,
            identity.username,
            identity.bot_id,
            bridge.max_chat_id,
        )
        return live

    async def remove(self, name: str) -> None:
        async with self._lock:
            live = self._by_name.pop(name, None)
            if live is None:
                return
            self._by_bot_id.pop(live.identity.bot_id, None)
            self._by_max_chat.pop(live.max_chat_id, None)

        await live.runner.stop()
        logger.info("bridge %s is down", name)

    async def close(self) -> None:
        for name in list(self._by_name):
            await self.remove(name)

    # ------------------------------------------------------------------ lookups

    def by_name(self, name: str) -> LiveBridge | None:
        return self._by_name.get(name)

    def by_bot_id(self, bot_id: int) -> LiveBridge | None:
        return self._by_bot_id.get(bot_id)

    def by_max_chat(self, max_chat_id: int) -> LiveBridge | None:
        return self._by_max_chat.get(max_chat_id)

    @property
    def live(self) -> tuple[LiveBridge, ...]:
        return tuple(self._by_name.values())


def _mismatch(
    name: str,
    actual: BotIdentity,
    expected: ExpectedBot,
    guardian_bot_id: int | None,
) -> str | None:
    """Why this token may not serve this bridge, or None when it may.

    Only ids and usernames appear in the message. The token is what is wrong and
    the token is exactly what must not be written down anywhere.
    """
    if guardian_bot_id is not None and actual.bot_id == guardian_bot_id:
        return f"bridge '{name}': ${name} would poll the guardian's own bot"
    if expected.bot_id is not None and int(expected.bot_id) != int(actual.bot_id):
        return (
            f"bridge '{name}': the stored token is bot {actual.bot_id}"
            f" (@{actual.username}), and this bridge is bot {expected.bot_id}"
        )
    if expected.username:
        # Telegram is case-insensitive about usernames and hands them back in
        # whatever case the owner typed; the deterministic name is lowercase.
        wanted = expected.username.lstrip("@").lower()
        got = (actual.username or "").lstrip("@").lower()
        if got != wanted:
            return (
                f"bridge '{name}': the stored token is @{actual.username or actual.bot_id},"
                f" and this bridge is @{wanted}"
            )
    return None
