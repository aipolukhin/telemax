"""The shared dispatcher: one for the whole process, N bots feeding it.

Handler order is load-bearing. Commands are registered here, before the router
that forwards messages to MAX (text forwarding), so `/status` answers the owner instead of
being typed at their mother.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import (
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .forward_authors import LearnForwardAuthors
from .owner import OwnerOnlyMiddleware

logger = logging.getLogger(__name__)

# Given a bot id, produce the text of `/status`. The dispatcher must not know
# how health is assembled, and the health module must not know about aiogram.
StatusProvider = Callable[[int], Awaitable[str]]

START_TEXT = (
    "Мост работает. Пишите сюда — сообщение уйдёт вашему контакту, "
    "а его ответы придут в этот же чат.\n\n/status — состояние моста."
)

#: The way back. Creating a bot drops the owner into the new bot's chat and
#: leaves them there, so building a second bridge meant finding the guardian by
#: hand every time. A bot cannot navigate the client, but it can offer a link.
BACK_TO_GUARDIAN = "← К стражу"

GUARD_TEXT = "Панель управления мостами."

#: No guardian to link to — a deployment with provisioning off, or a `getMe`
#: that has not answered yet. Said out loud rather than answered with a bare
#: sentence and no button, which reads as the command being broken.
NO_GUARDIAN = "Страж не настроен в этой установке."

#: Published on every bridge bot, so the chat says what can be done in it. The
#: contact bots had `/status` and no menu at all: the command existed and
#: nothing anywhere mentioned it.
BRIDGE_COMMANDS = (
    ("guard", "Открыть стража"),
    ("status", "Состояние моста"),
)

# Given nothing, the guardian's @username, or None where there is no guardian to
# go back to. A callable because the dispatcher is built before the guardian's
# `getMe` has answered, and a value captured then would be the stale one.
GuardianUsername = Callable[[], str | None]


def _back_to_guardian(username: str | None) -> InlineKeyboardMarkup | None:
    name = (username or "").strip().lstrip("@")
    if not name:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BACK_TO_GUARDIAN, url=f"https://t.me/{name}")]
        ]
    )


def _guardian_markup(resolve: GuardianUsername | None) -> InlineKeyboardMarkup | None:
    """The button, or None. Never raises: this is drawn on the owner's way in."""
    if resolve is None:
        return None
    try:
        return _back_to_guardian(resolve())
    except Exception:
        logger.debug("could not resolve the guardian username", exc_info=True)
        return None


async def publish_bridge_menu(bot: Any) -> None:
    """Put `/guard` and `/status` in the bot's ☰ menu. Best effort, never fatal.

    A bridge that carries messages and has no menu is working; one that refused
    to come up because Telegram was busy for a second is not.
    """
    from aiogram.methods import SetChatMenuButton, SetMyCommands
    from aiogram.types import BotCommand, MenuButtonCommands

    try:
        await bot(
            SetMyCommands(
                commands=[
                    BotCommand(command=name, description=description)
                    for name, description in BRIDGE_COMMANDS
                ]
            )
        )
        await bot(SetChatMenuButton(menu_button=MenuButtonCommands()))
    except Exception:
        logger.debug("could not publish the bridge command menu", exc_info=True)


def build_commands_router(
    status_provider: StatusProvider | None,
    guardian_username: GuardianUsername | None = None,
) -> Router:
    router = Router(name="commands")

    @router.message(Command("start"))
    async def _start(message: Message) -> None:
        # Kept, and not relied on: Telegram's managed-creation flow opens the
        # chat without ever sending this. `/guard` and the About line are what
        # actually carry the way back.
        await message.answer(START_TEXT, reply_markup=_guardian_markup(guardian_username))

    @router.message(Command("guard"))
    async def _guard(message: Message) -> None:
        """The reliable way back.

        `/start` is not one: in the managed-creation flow Telegram opens the
        chat with the new bot itself, and — measured across four bridges, zero
        `/start` updates ever recorded — the owner's tap on Start never reaches
        the bot. A command they can type, and that sits in the ☰ menu, does.
        """
        markup = _guardian_markup(guardian_username)
        await message.answer(GUARD_TEXT if markup else NO_GUARDIAN, reply_markup=markup)

    @router.my_chat_member()
    async def _opened(event: ChatMemberUpdated, bot: Bot) -> None:
        """Greet the owner the moment they open the chat, not one `/start` later.

        The first tap on Start in a freshly created bot sends no message:
        Telegram treats the bot as already started by the creation flow, so
        `/start` never arrives and the greeting — with the only link back to the
        guardian — waited for a second, manual one. Measured across five bots:
        not one `/start` update on the first open.

        What does arrive is this. Private chats report the owner going from
        `left` to `member`, which is the same event a human would call "they
        opened the chat".
        """
        if event.chat.type != "private":
            return
        if event.new_chat_member.status not in {"member", "administrator", "creator"}:
            # Blocked, or left. Nothing to greet, and writing to somebody who
            # just blocked the bot is not going to work anyway.
            return
        if event.old_chat_member.status == event.new_chat_member.status:
            return
        try:
            await bot.send_message(
                chat_id=event.chat.id,
                text=START_TEXT,
                reply_markup=_guardian_markup(guardian_username),
            )
        except Exception:
            logger.info("could not greet the owner on opening the chat", exc_info=True)

    @router.message(Command("status"))
    async def _status(message: Message, bot: Bot) -> None:
        if status_provider is None:
            await message.answer("Состояние недоступно.")
            return
        await message.answer(await status_provider(bot.id))

    return router


def build_dispatcher(
    *,
    owner_user_id: int,
    status_provider: StatusProvider | None = None,
    forward_authors: Any = None,
    guardian_username: GuardianUsername | None = None,
) -> Dispatcher:
    dispatcher = Dispatcher()

    # Outer middleware: runs before filters, on every update type, so a stranger
    # never reaches a handler. Registered after aiogram's own user-context
    # middleware, which is what fills in `event_from_user`.
    dispatcher.update.outer_middleware(OwnerOnlyMiddleware(owner_user_id))

    if forward_authors is not None:
        # After the owner check and before everything else, because it must see
        # forwards whatever the intake gate decides: the gate closes the Bot API
        # *delivery* path, and this is the one thing only that path can see —
        # the author of a forward as their own profile has them, which the
        # owner's session is structurally unable to report. It observes and
        # never consumes.
        dispatcher.update.outer_middleware(LearnForwardAuthors(forward_authors))

    dispatcher.include_router(build_commands_router(status_provider, guardian_username))
    return dispatcher
