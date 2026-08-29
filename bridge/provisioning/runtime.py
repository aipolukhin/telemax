"""Running the guardian, and replaying what a new contact wrote.

The guardian is the one bot that carries no conversation, so it does not live in
the bridge registry: it gets its own `Bot` and its own polling task, feeding the
same dispatcher as everyone else.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bridge.telegram import DEFAULT_ALLOWED_UPDATES, BotRunner

logger = logging.getLogger(__name__)

GUARDIAN_NAME = "guardian"

#: What Telegram's own command menu offers. Deliberately four: everything else
#: the guardian can do is a button on the screen those four lead to, and a menu
#: of fifteen commands is a manual, not an interface.
#:
#: `/restart` used to be the fourth. It is an operator's tool that interrupts
#: delivery, and it sat in the same list as «Панель» — one tap away from a
#: mis-tap. It still works, and it is a button under Diagnostics; it is simply
#: not offered beside the three things an owner does every day.
#:
#: `/dialogs` keeps its command and gains an honest description: it is one of
#: three ways to add a contact, and the screen it opens is the least useful of
#: them for somebody whose relative has no MAX dialogs yet.
COMMANDS = (
    ("menu", "Панель"),
    ("bridges", "Мосты"),
    ("add", "Добавить контакт"),
    ("status", "Состояние Telemax"),
)


async def _describe(bot: Any) -> None:
    """Publish the command menu and point the ☰ button at it.

    Best effort and never fatal: a guardian that answers is worth more than one
    that refused to start because Telegram was busy for a second. The button is
    set explicitly rather than left to default — an install that once pointed it
    somewhere else would keep doing so for ever.
    """
    from aiogram.methods import SetChatMenuButton, SetMyCommands
    from aiogram.types import BotCommand, MenuButtonCommands

    try:
        await bot(
            SetMyCommands(
                commands=[
                    BotCommand(command=name, description=description)
                    for name, description in COMMANDS
                ]
            )
        )
        await bot(SetChatMenuButton(menu_button=MenuButtonCommands()))
    except Exception:
        # Cosmetic, and tried again on the next start.
        logger.debug("could not publish the guardian's command menu", exc_info=True)


async def _username_of(bot: Any) -> str:
    """The bot's own username, asked of Telegram rather than of the config.

    Best effort: a guardian that answers is worth more than one that refused to
    start because `getMe` was slow. The caller falls back to the environment,
    which is a cache and is treated as one.
    """
    try:
        me = await bot.get_me()
    except Exception:
        logger.debug("could not read the guardian's own username", exc_info=True)
        return ""
    return str(getattr(me, "username", "") or "")


class Guardian:
    """The guardian bot and the announcements it sends."""

    def __init__(
        self,
        bot: Bot,
        runner: BotRunner,
        owner_chat_id: int,
        username: str | None = None,
    ) -> None:
        self._bot = bot
        self._runner = runner
        self._owner_chat_id = owner_chat_id
        # From `getMe`, taken while the bot was being started. Everything that
        # builds a `t.me/newbot/<manager>/…` link needs it, and the environment
        # variable setup writes can be stale, empty, or another install's — an
        # empty one produced a link that opens nothing and said nothing.
        self._username = (username or "").lstrip("@")

    @property
    def bot(self) -> Bot:
        return self._bot

    @property
    def bot_id(self) -> int:
        """Which bot this is — the guardian's own updates are told apart by it."""
        return int(self._bot.id)

    @property
    def owner_chat_id(self) -> int:
        return self._owner_chat_id

    @property
    def username(self) -> str:
        """What Telegram says this bot is called. Empty only if `getMe` failed."""
        return self._username

    @classmethod
    async def start(
        cls,
        *,
        token: str,
        dispatcher: Dispatcher,
        owner_chat_id: int,
        bot_factory: Any = Bot,
        bot: Any = None,
    ) -> Guardian:
        # The caller may have built the Bot already — the runtime needs it to
        # draw the status message *before* the dispatcher that polls it exists.
        if bot is None:
            bot = bot_factory(
                token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
            )
        runner = BotRunner(
            bridge_name=GUARDIAN_NAME,
            bot=bot,
            dispatcher=dispatcher,
            allowed_updates=list(DEFAULT_ALLOWED_UPDATES),
        )
        runner.start()
        await _describe(bot)
        return cls(bot, runner, owner_chat_id, username=await _username_of(bot))

    async def announce(self, text: str, markup: Any) -> None:
        """One new contact, once, asking whether to build them a bridge.

        The only thing the guardian bot posts that is neither the anchor nor the
        single attention push. It is not an operational event — nothing is
        broken, nothing has a count, and it will never repeat itself — it is a
        person the owner has not decided about yet, and the decision needs the
        buttons this carries.

        There used to be an `invite` beside it with the same body and a
        different log line, for opening the conversation before the owner had
        pressed Start. `bootstrap.handoff` does that now, with its own send, and
        this one had no callers left.
        """
        try:
            await self._bot.send_message(
                chat_id=self._owner_chat_id, text=text, reply_markup=markup
            )
        except Exception:
            # A failed announcement must not disturb the bridges that do work.
            logger.exception("could not announce a new contact")

    async def stop(self) -> None:
        await self._runner.stop()


class BridgeReplayer:
    """Delivers a new contact's backlog through their freshly created bot."""

    def __init__(self, registry: Any, owner_chat_id: int) -> None:
        self._registry = registry
        self._owner_chat_id = owner_chat_id

    async def replay(self, bridge_name: str, messages: list[dict[str, Any]]) -> None:
        live = self._registry.by_name(bridge_name)
        if live is None:
            logger.warning("bridge %s vanished before its backlog was replayed", bridge_name)
            return

        for message in messages:
            text = str(message.get("text") or "")
            kinds = message.get("attachments") or []
            if kinds and not text:
                text = f"[{', '.join(str(kind) for kind in kinds)}]"
            elif kinds:
                text = f"{text}\n[{', '.join(str(kind) for kind in kinds)}]"
            if not text:
                continue
            try:
                await live.bot.send_message(chat_id=self._owner_chat_id, text=text)
            except Exception:
                # Losing one buffered line is better than aborting the replay
                # and leaving the rest stranded.
                logger.exception("could not replay a buffered message for %s", bridge_name)
