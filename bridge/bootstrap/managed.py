"""`setup` without a Telegram account credential: a token, a tap, and done.

The old console had to log into the owner's *account*: an api_id, an api_hash, a
phone and a code, because only a user session could create the guardian bot and
only a session knew the owner's Telegram id. Managed Bots removed the first
reason. This module removes the second, which is the last one — and with it the
whole `telethon.session` from a production install.

Two things replace the session:

* **the guardian's own token, pasted in.** Its `getMe` says whether Bot Management
  Mode is on (`can_manage_bots`), which is what the account-ownership check was
  really for. A token that Telegram accepts but that cannot manage bots is
  refused with instructions rather than accepted and blamed later.
* **the owner's id, from the owner.** The console prints the bot's link, polls
  the guardian for a few minutes, and takes the id from whoever presses Start.
  A bot cannot be written to first, so this tap has to happen anyway — the same
  tap is now also the answer.

The chicken-and-egg is not solved and cannot be: creating managed bots needs a
manager, so the *first* bot is made by hand in @BotFather. That is one manual step
at install time, and it replaces a full account credential living on disk for ever.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any

from .plan import Plan
from .ui import SetupCancelled, Ui

logger = logging.getLogger(__name__)

#: How long the console waits for the owner to press Start before giving up. Long
#: enough to unlock a phone and find the chat, short enough not to hang a session.
START_TIMEOUT_SECONDS = 600.0

INSTRUCTIONS = """Создайте бота-стража руками — это единственный ручной шаг:

  1. @BotFather → /newbot → любое имя и username
  2. @BotFather → Mini App → выберите бота → Bot Management Mode → Enable
  3. вернитесь сюда и вставьте токен

Второй шаг обязателен: без него страж не сможет создавать ботов под контакты."""

NOT_A_MANAGER = """У этого бота выключен Bot Management Mode.

  @BotFather → Mini App → выберите бота → Bot Management Mode → Enable

Токен правильный, дело только в тумблере."""

TOKEN_REFUSED = "Telegram не принял этот токен."  # noqa: S105 - a message, not a secret

EXPECTED_INSTRUCTIONS = """Создайте бота-стража руками — это единственный ручной шаг.

Username обязан быть ровно таким:

  {username}

Он выведен из ваших Telegram и MAX аккаунтов, поэтому любая установка с теми же
аккаунтами вычислит то же имя — и найдёт этого бота без переноса каких-либо
файлов.

  1. @BotFather → /newbot → любое имя, username выше
  2. @BotFather → Mini App → выберите бота → Bot Management Mode → Enable
  3. вернитесь сюда и вставьте токен

Второй шаг обязателен: без него страж не сможет создавать ботов под контакты."""


@dataclass(frozen=True, slots=True)
class ManagedGuardian:
    """A guardian that exists, can manage bots, and belongs to a known owner."""

    token: str
    bot_id: int
    username: str
    owner_user_id: int


class BotCheck:
    """`getMe` for a pasted token, with the one flag that matters."""

    async def identify(self, token: str) -> tuple[int, str, bool] | None:
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties

        bot = Bot(token=token, default=DefaultBotProperties(parse_mode=None))
        try:
            me = await bot.get_me()
        except Exception:  # noqa: BLE001 - revoked, malformed, or not a token
            logger.info("a pasted guardian token was not accepted by Telegram")
            return None
        else:
            return int(me.id), str(me.username or "").lower(), bool(me.can_manage_bots)
        finally:
            with contextlib.suppress(Exception):
                await bot.session.close()

    async def await_start(
        self,
        token: str,
        *,
        timeout: float,  # noqa: ASYNC109 - a person pressing a button, not a cancel scope
    ) -> int | None:
        """The id of whoever presses Start, or None if nobody does in time.

        A short-lived poller of its own: the service is not installed yet, so
        nothing else is holding this token. It stops the moment the id is known,
        because two pollers on one token fight over `getUpdates`.
        """
        from aiogram import Bot, Dispatcher
        from aiogram.client.default import DefaultBotProperties
        from aiogram.types import Message

        bot = Bot(token=token, default=DefaultBotProperties(parse_mode=None))
        dispatcher = Dispatcher()
        found: asyncio.Future[int] = asyncio.get_running_loop().create_future()

        @dispatcher.message()
        async def _any(message: Message) -> None:
            if message.from_user is None or found.done():
                return
            found.set_result(int(message.from_user.id))

        polling = asyncio.create_task(
            dispatcher.start_polling(bot, handle_signals=False, allowed_updates=["message"])
        )
        try:
            return await asyncio.wait_for(asyncio.shield(found), timeout=timeout)
        except TimeoutError:
            return None
        finally:
            with contextlib.suppress(Exception):
                await dispatcher.stop_polling()
            polling.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await polling
            with contextlib.suppress(Exception):
                await bot.session.close()


WRONG_USERNAME = (
    "Этот бот называется @{actual}, а нужен @{expected}.\n\n"
    "Имя выводится из ваших Telegram и MAX аккаунтов и другим быть не может:\n"
    "любая другая установка с теми же аккаунтами вычислит именно это имя.\n"
    "Создайте бота с этим username и пришлите его токен."
)


async def adopt_guardian(
    plan: Plan,
    ui: Ui,
    *,
    checker: Any = None,
    journal: list[str] | None = None,
    timeout: float = START_TIMEOUT_SECONDS,  # noqa: ASYNC109 - a person, not a cancel scope
    expected_username: str | None = None,
    owner_user_id: int | None = None,
) -> ManagedGuardian:
    """Take a token, prove it can manage bots, and learn who the owner is.

    `expected_username` is the V2 name computed from the two owner accounts.
    When it is given the token must belong to exactly that bot: a guardian at
    any other name is one no other installation of these two accounts would ever
    look for. When it is absent this is the *adopt* path — the owner already has
    a guardian, whatever it is called, and what `getMe` says is the truth.

    `owner_user_id` skips the Start round trip when the identity is already
    proved. Pressing Start was how the Telegram id used to be learned, and it
    cannot be that any more: the guardian's own name now depends on that id.
    """
    from bridge.config.writer import set_env_value

    check = checker or BotCheck()

    if expected_username:
        ui.plain(EXPECTED_INSTRUCTIONS.format(username=expected_username))
    else:
        ui.plain(INSTRUCTIONS)
    ui.plain()

    while True:
        token = (await ui.secret("Токен бота-стража")).strip()
        if not token:
            raise SetupCancelled
        identity = await check.identify(token)
        if identity is None:
            ui.fail(TOKEN_REFUSED)
            continue
        bot_id, username, can_manage = identity
        if expected_username and username != expected_username.lower():
            # Never adopted under another name and never renamed around: the
            # name is derived, and a different one is a different bot.
            ui.fail(WRONG_USERNAME.format(actual=username, expected=expected_username))
            continue
        if not can_manage:
            ui.fail(NOT_A_MANAGER)
            if not await ui.confirm("Включили? Проверить ещё раз", default=True):
                raise SetupCancelled
            continue
        break

    ui.ok(f"Бот-страж: @{username}")
    set_env_value(plan.env_path, plan.guardian_token_env, token)
    set_env_value(plan.env_path, f"{plan.guardian_token_env}_USERNAME", username)
    if journal is not None:
        journal.append(plan.guardian_token_env)

    if owner_user_id is None:
        ui.plain()
        ui.plain("Откройте бота и нажмите «Start» — так я узнаю ваш Telegram-аккаунт:")
        ui.plain(f"https://t.me/{username}")
        ui.plain()

        with ui.working("Жду нажатия Start…"):
            owner_user_id = await check.await_start(token, timeout=timeout)

        if owner_user_id is None:
            raise SetupCancelled
        ui.ok("Аккаунт определён")

    return ManagedGuardian(
        token=token, bot_id=bot_id, username=username, owner_user_id=owner_user_id
    )
