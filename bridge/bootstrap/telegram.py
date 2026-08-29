"""The Telegram half of the console bootstrap.

Two things happen here and nothing else can do either. A *user* session, because
creating a bot has no Bot API method and @BotFather will not talk to one — and
the owner's own Telegram id, which falls out of that session and therefore never
needs to be asked for.

Every value the owner types lands in `.env` at 0600 immediately, so a run that
fails halfway is resumed rather than repeated. Nothing typed here is ever echoed
back: the api_hash and the 2FA password are hidden at the prompt and never
printed afterwards.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from typing import Any

from bridge.config.writer import set_env_value
from bridge.phone import normalize as normalize_phone
from bridge.provisioning import mtproto
from bridge.provisioning.naming import (
    GUARD_DISPLAY_NAME,
    guard_username,
    load_or_create_naming_secret,
)
from bridge.provisioning.provisioner import (
    ForeignUsernameError,
    MtprotoProvisioner,
    ProvisionerError,
    UsernameState,
    is_limit_error,
)
from bridge.telegram.user_session import (
    SYNC_ACCESS_WARNING,
    SYNC_HEADER,
    SYNC_NOT_ENABLED_YET,
    SYNC_SCAN_HINT,
    OwnerMismatchError,
    authorize_owner_session,
    qr_ascii,
    user_session_path,
)

from .intent import GuardianIntent
from .plan import Plan
from .ui import Ui, valid_api_id, valid_nonempty, valid_phone

logger = logging.getLogger(__name__)

GUARDIAN_NAME = GUARD_DISPLAY_NAME

API_HELP = "api_id и api_hash — на my.telegram.org → API development tools."


class BotFatherLimitError(mtproto.MtprotoError):
    """@BotFather will not create anything for a while. Hours, not minutes.

    Not a dead end: the username is deterministic, so the owner can make the bot
    by hand — @BotFather's own mini app is not rate-limited the same way — and
    hand the token back. That is the whole message, because a bare
    "BOT_CREATE_LIMIT_EXCEEDED" in a terminal is not an instruction.
    """

    def __init__(self, username: str) -> None:
        super().__init__(
            "@BotFather временно не создаёт ботов (лимит на несколько часов).\n\n"
            "Создайте бота руками с ТЕМ ЖЕ username:\n"
            f"  @{username}\n\n"
            "https://t.me/BotFather?profile → New Bot\n\n"
            "Потом отдайте токен:\n"
            "  .venv/bin/python scripts/adopt_guardian.py"
        )
        self.username = username


class GuardianUsernameTakenError(mtproto.MtprotoError):
    """The deterministic guardian name belongs to a different Telegram account.

    There is deliberately no fallback. A second username would work today and
    break every saved link and every re-run afterwards, and the bot it collided
    with is somebody else's — so setup stops and says so.
    """

    def __init__(self, username: str) -> None:
        super().__init__(
            "Детерминированное имя бота-стража уже занято другим Telegram-аккаунтом.\n"
            f"Username: @{username}"
        )
        self.username = username


@dataclass(frozen=True, slots=True)
class TelegramAccount:
    """A logged-in owner and the guardian bot that belongs to them."""

    session: mtproto.BotFatherSession
    owner_user_id: int
    guardian_username: str
    guardian_token: str


async def _remembered(
    plan: Plan,
    key: str,
    ui: Ui,
    question: str,
    *,
    hidden: bool = False,
    journal: list[str] | None = None,
) -> str:
    """Ask once, keep it in `.env`, never ask again.

    `journal` collects what this run wrote, so a cancellation can take it back
    out again — an api_hash left in a file after Ctrl+C is a change, and setup
    promises there were none.
    """
    existing = os.environ.get(key, "").strip()
    if existing:
        return existing

    validator = valid_nonempty
    if key == plan.api_id_env:
        validator = valid_api_id
    elif key == plan.phone_env:
        validator = valid_phone

    ask = ui.secret if hidden else ui.ask
    value = await ask(question, validate=validator)
    if key == plan.phone_env:
        # Store the form Telegram wants, not the form an address book shows.
        value = normalize_phone(value) or value
    set_env_value(plan.env_path, key, value)
    if journal is not None:
        journal.append(key)
    return value


async def connect_account(
    plan: Plan, ui: Ui, *, journal: list[str] | None = None
) -> mtproto.BotFatherSession:
    """Log the owner's Telegram account in, asking for a code if there is none."""
    ui.note(API_HELP)
    ui.plain()

    ask = partial(_remembered, plan, ui=ui, journal=journal)
    api_id = await ask(plan.api_id_env, question="API ID:")
    api_hash = await ask(plan.api_hash_env, question="API Hash:", hidden=True)
    phone = await ask(plan.phone_env, question="Номер телефона:")

    sent = False

    async def code(_: str) -> str:
        # Telethon calls this from inside `connect`; `mtproto._ask` awaits
        # whatever it returns, so an async prompt fits without a thread.
        nonlocal sent
        if not sent:
            ui.ok("Код отправлен")
            sent = True
        return await ui.ask("Код из Telegram:", validate=valid_nonempty)

    async def password(_: str) -> str:
        return await ui.secret("Пароль 2FA:", validate=valid_nonempty)

    with ui.working("Подключаюсь к Telegram…"):
        session = await mtproto.connect(
            api_id=int(api_id),
            api_hash=api_hash,
            phone=phone,
            secrets_dir=plan.secrets_dir,
            code_provider=code,
            password_provider=password,
        )

    ui.ok("Telegram подключён")
    ui.ok("Сессия сохранена (0600)")
    return session


async def offer_owner_session(
    plan: Plan, ui: Ui, *, owner_user_id: int, journal: list[str] | None = None
) -> bool:
    """Offer the owner-session QR link during setup, and run it if accepted.

    The same authorise/verify/2FA/reuse/recovery core as `telegram-sync`
    (`authorize_owner_session`) — there is one implementation, called with the
    console's own prompts here and with `print`/`getpass` in the command. It
    **never** touches `owner_mtproto_intake_enabled`: a linked session is not an
    enabled intake, and enabling it is a separate, probe-gated step. Returns True
    only when a session was actually linked.

    Skipped silently on a non-tty: this is an optional step, so it must not turn
    a piped setup into a cancellation the way a required prompt would.
    """
    if not ui.interactive:
        return False
    ui.plain()
    for line in SYNC_HEADER.splitlines():
        ui.plain(line)
    ui.plain()
    if not await ui.confirm("Подключить сейчас по QR?", default=False):
        ui.note("Пропущено. Позже: `python -m bridge telegram-sync`.")
        return False

    ui.note(API_HELP)
    ask = partial(_remembered, plan, ui=ui, journal=journal)
    api_id = await ask(plan.api_id_env, question="API ID:")
    api_hash = await ask(plan.api_hash_env, question="API Hash:", hidden=True)

    async def on_qr(url: str) -> None:
        ui.plain()
        ui.plain(qr_ascii(url))
        for line in SYNC_SCAN_HINT.splitlines():
            ui.plain(line)

    async def password(_: str) -> str:
        return await ui.secret("Пароль 2FA:", validate=valid_nonempty)

    try:
        owner = await authorize_owner_session(
            api_id=int(api_id),
            api_hash=api_hash,
            secrets_dir=plan.secrets_dir,
            owner_user_id=owner_user_id,
            on_qr=on_qr,
            password_provider=password,
        )
    except OwnerMismatchError as error:
        ui.fail(str(error))
        ui.note("Позже подключите аккаунт владельца: `python -m bridge telegram-sync`.")
        return False
    except (TimeoutError, mtproto.MtprotoError) as error:
        ui.fail(f"Не подключил: {error}")
        return False

    ui.ok(f"Аккаунт: {owner.name or 'без имени'} · ID {owner.account_id} (владелец)")
    ui.ok(f"Сессия: {user_session_path(plan.secrets_dir)} (0600)")
    ui.note(SYNC_ACCESS_WARNING)
    ui.note(SYNC_NOT_ENABLED_YET)
    return True


async def identify_bot(token: str) -> tuple[int, str] | None:
    """Who a bot token belongs to, or None when Telegram will not say.

    The only way to find out: Bot API has no "which bot is this" beyond `getMe`,
    and guessing from the numeric prefix would put a working link in front of
    the owner that opens somebody else's bot.
    """
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=None))
    try:
        me = await bot.get_me()
    except Exception:  # noqa: BLE001 - revoked, malformed or simply not a token
        logger.info("a guardian token was not accepted by Telegram")
        return None
    else:
        return int(me.id), str(me.username or "").lower()
    finally:
        with suppress(Exception):
            await bot.session.close()


async def _adopt_existing(plan: Plan, ui: Ui, username: str) -> str | None:
    """Reuse the token already in `.env`, if it really is this bot's.

    Three things have to line up: a token exists, Telegram accepts it, and the
    bot it names carries exactly the deterministic username. Any mismatch and
    this returns None, which sends the caller down the rebuild path — adopting
    a token for the wrong bot would be worse than making a new one.
    """
    token = os.environ.get(plan.guardian_token_env, "").strip()
    if not token:
        return None

    with ui.working("Проверяю сохранённый токен…"):
        identity = await identify_bot(token)
    if identity is None:
        ui.warn("Сохранённый токен не принят Telegram — пересоздам бота.")
        return None

    _, actual = identity
    if actual != username:
        ui.warn(f"Сохранённый токен принадлежит @{actual}, а нужен @{username}.")
        return None

    set_env_value(plan.env_path, f"{plan.guardian_token_env}_USERNAME", username)
    ui.ok(f"Бот-страж уже на месте: @{username}")
    return token


async def _adopt_after_failure(
    ui: Ui, provisioner: MtprotoProvisioner, username: str
) -> bool:
    """After a BotFather answer nobody understood: did the bot get created?

    A timeout is not evidence that nothing happened, and `/newbot` is the one
    command here whose effect is irreversible. `getAdminedBots` settles it in one
    read — and if the bot is there, the token can still be had, because a token
    for a bot that exists is what `adopt_guardian.py` was written for.

    False means "genuinely not created": the caller re-raises, and the intent on
    disk keeps the username so a re-run asks for the same one.
    """
    with ui.working("Проверяю, не создался ли бот всё-таки…"):
        try:
            state = await provisioner.check_username(username)
        except Exception:
            logger.debug("could not check @%s after a failed create", username, exc_info=True)
            return False
    if state is not UsernameState.OWNED:
        return False

    ui.warn(f"@{username} всё-таки создан — BotFather не ответил, но бот есть.")
    ui.note(
        "Токен придётся забрать вручную: @BotFather → /mybots → выбрать бота → API Token,\n"
        "затем `.venv/bin/python scripts/adopt_guardian.py`."
    )
    return True


async def ensure_guardian(
    plan: Plan,
    ui: Ui,
    session: mtproto.BotFatherSession,
    *,
    stop_runtime: Callable[[], object] | None = None,
    sleep: Any = None,
    expected_username: str | None = None,
) -> TelegramAccount:
    """Put the guardian bot at its deterministic username, whatever is there now.

    The username is a function of the owner's Telegram id, so a re-run finds the
    same name every time. What it finds *at* that name decides the rest:

    * nobody — create it;
    * a bot this account owns — it is a previous bootstrap's guardian: stop the
      runtime holding its token, delete it, wait for Telegram to release the
      name, and create it again so the new token is the only live one;
    * somebody else's bot — stop. Nothing is deleted and nothing is renamed.
    """
    with ui.working("Определяю ваш Telegram id…"):
        owner_user_id = await session.own_user_id()
    if not owner_user_id:
        raise mtproto.MtprotoError(
            "Не удалось прочитать id аккаунта — попробуйте запустить setup ещё раз."
        )
    ui.ok(f"Ваш Telegram id: {owner_user_id}")

    # V2 when the caller could establish both owner accounts, V1 otherwise. V1
    # is kept for exactly one case: an installation that already has a guardian
    # under a `naming-secret` name, where recomputing it is the only way to
    # recognise the bot that exists. Nothing new is minted under V1.
    if expected_username:
        username = expected_username
    else:
        ui.warn("Имя стража считается по старому контракту (naming-secret).")
        secret = load_or_create_naming_secret(plan.secrets_dir)
        username = guard_username(secret, owner_user_id)
    provisioner = MtprotoProvisioner(session, sleep=sleep or asyncio.sleep)

    with ui.working("Проверяю имя бота-стража…"):
        state = await provisioner.check_username(username)

    if state is UsernameState.FOREIGN:
        raise GuardianUsernameTakenError(username)

    if state is UsernameState.OWNED:
        # A working token for exactly this bot means there is nothing to
        # rebuild. Deleting and recreating it would produce an identical bot,
        # a new token, and — as @BotFather rate limits creation by the hour —
        # a real chance of ending up with no guardian at all.
        adopted = await _adopt_existing(plan, ui, username)
        if adopted is not None:
            return TelegramAccount(
                session=session,
                owner_user_id=owner_user_id,
                guardian_username=username,
                guardian_token=adopted,
            )

    if state is UsernameState.OWNED:
        # Never two guardians at once: the old token keeps long-polling until
        # the process holding it is gone.
        if stop_runtime is not None:
            with ui.working("Останавливаю прежний Telemax…"):
                stop_runtime()
        with ui.working(f"Пересоздаю бота-стража @{username}…"):
            try:
                await provisioner.delete_owned_bot(username)
            except ForeignUsernameError as error:
                raise GuardianUsernameTakenError(username) from error
            if not await provisioner.wait_for_username_release(username):
                raise mtproto.MtprotoError(
                    f"Telegram ещё не освободил @{username}. Попробуйте через минуту."
                )
        # The old token cannot come back: it belonged to a bot that is gone.
        set_env_value(plan.env_path, plan.guardian_token_env, "")

    # Written before `/newbot`, because a timeout after that command is
    # indistinguishable from one before it. The intent says which username was
    # about to be asked for; the postcondition below is what turns "I do not
    # know" into an answer. No token goes in it — nothing about this file is
    # secret, and it must stay that way.
    intent = GuardianIntent.for_data_dir(plan.data_dir)
    intent.begin(username=username, owner_user_id=owner_user_id)

    try:
        with ui.working("Создаю бота-стража через @BotFather…"):
            created = await provisioner.create_bot(name=GUARDIAN_NAME, username=username)
    except ProvisionerError as error:
        if is_limit_error(error):
            intent.note("limit")
            raise BotFatherLimitError(username) from error
        # A timeout, or prose nobody recognised. Neither says whether the bot
        # exists — so the account is asked, rather than the owner being told
        # something the console does not know.
        intent.note("unknown")
        await _adopt_after_failure(ui, provisioner, username)
        raise

    set_env_value(plan.env_path, plan.guardian_token_env, created.token)
    intent.finish(username=created.username)
    # The username is not a secret, but it lives with the token so that a second
    # run recognises the bot it already made.
    set_env_value(plan.env_path, f"{plan.guardian_token_env}_USERNAME", created.username)
    ui.ok(f"Бот-страж: @{created.username}")

    return TelegramAccount(
        session=session,
        owner_user_id=owner_user_id,
        guardian_username=created.username,
        guardian_token=created.token,
    )


