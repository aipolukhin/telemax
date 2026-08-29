"""The one thing Bot API cannot do: create a bot.

There is no method for it. @BotFather is itself a bot, and a bot cannot talk to
a bot, so the only way to automate creation is a *user* session over MTProto.
That is a second authentication contour with real weight — an `api_id`, an
`api_hash`, the owner's phone, and a session file that is exactly as sensitive
as the MAX one — which is why it is opt-in, used for this single step, and never
touched again while the bridge runs.

Telethon is imported inside the functions on purpose: with provisioning off, the
dependency does not need to exist at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .botfather import DELETE_CONFIRMATION, Reply, parse, redact

logger = logging.getLogger(__name__)

BOTFATHER = "BotFather"

#: How long to wait for each BotFather reply. It answers in under a second when
#: it is well; anything past this is a stuck dialogue, not a slow one.
REPLY_TIMEOUT_SECONDS = 30

SESSION_FILE = "telethon.session"

#: App-config keys Telegram publishes the bot creation limit under, most
#: specific first. Never a constant of ours: the number depends on the account,
#: on Premium, and on whatever the server decides next year.
BOT_LIMIT_KEYS_DEFAULT = ("bots_create_limit_default", "bots_create_limit")
BOT_LIMIT_KEYS_PREMIUM = ("bots_create_limit_premium", "double_limits__bots_create")


class MtprotoError(Exception):
    """The user session could not do what was asked."""


class BotFatherTooSoonError(MtprotoError):
    """@BotFather asked us to come back later, and said how much later.

    Its own type because the number is the whole content. Measured on this
    account: 62 seconds after a couple of walks, 58000 — over sixteen hours —
    after five in a row. No fixed cooldown is right for both, so the caller
    honours what was said instead of inventing a figure.
    """

    def __init__(self, seconds: int | None) -> None:
        self.seconds = seconds
        if seconds is None:
            super().__init__("@BotFather просит подождать (сколько — не сказал)")
        else:
            super().__init__(f"@BotFather просит подождать {seconds} с")


class WrongPeerError(MtprotoError):
    """The peer to be wiped is not the bot it was supposed to be.

    Its own type because it must never be handled as "try again". Deleting a
    history is irreversible for both sides, so anything short of proof that this
    peer is *this bridge's own bot* is a refusal.
    """


class UsernameStillTakenError(MtprotoError):
    """A deleted bot's username has not been released yet. Retry, do not rename."""


@dataclass(frozen=True, slots=True)
class CreatedBot:
    username: str
    token: str
    #: Whoever knows it fills it in — and on the @BotFather path that is the
    #: token itself. Its prose never names the id, but every token *is*
    #: `<bot_id>:<secret>`, so `bot_id_of` recovers it without asking anybody.
    #:
    #: Worth having rather than tidy: the id is what `/start` and the dialog
    #: wipe address the new bot by, and leaving it None sent them back to the
    #: username — which is the one thing the session's cache gets wrong about a
    #: bot rebuilt at a deterministic name.
    bot_id: int | None = None


@dataclass(frozen=True, slots=True)
class OwnedBot:
    """A bot the *current* Telegram account owns, as Telegram reports it.

    The local YAML is not evidence of ownership: a config copied between hosts,
    or a bot deleted from a phone, both leave it claiming bots that are not
    there. `bots.getAdminedBots` is the only answer that cannot be stale.
    """

    bot_id: int
    username: str | None
    name: str | None = None


def bot_id_of(token: str) -> int | None:
    """The bot's own id, read out of its token. None when the shape is unknown.

    A Telegram bot token is `<bot_id>:<secret>` and always has been. This is not
    a parse of prose — the id is structurally there, and it is the only place
    @BotFather ever tells us.
    """
    head, _, rest = (token or "").partition(":")
    if not rest or not head.isascii() or not head.isdigit():
        return None
    return int(head)


def session_path(secrets_dir: Path) -> Path:
    return secrets_dir / SESSION_FILE


def harden(path: Path) -> None:
    """A user session is a full account credential. Treat it like one."""
    if path.exists():
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


class AccountBots:
    """The read-only questions about the *account's* bots, over any live client.

    Split out of `BotFatherSession` so the answers can be had without the things
    that session also does. Production runs `provisioning.mode: managed`, where
    bots are created through Telegram's own dialog and there is no @BotFather
    session at all — but the owner's intake session is already connected, and it
    can answer the two questions Bot API structurally cannot: how many bots this
    account owns, and how many it may own.

    Both matter for the same reason. The owner has bots that have nothing to do
    with Telemax, and they occupy the same slots. Counting only our own is a
    figure that reads as fact and lies right up until creation fails.

    Nothing here writes, and there is deliberately no `close`: the client is
    borrowed, and disconnecting somebody else's session is not this object's to
    do.
    """

    def __init__(self, client: Any | Callable[[], Any | None]) -> None:
        self._source = client

    def _client_or_none(self) -> Any:
        """The live client, or None. A provider so a reconnect is picked up."""
        return self._source() if callable(self._source) else self._source

    @property
    def _client(self) -> Any:
        return self._client_or_none()

    # ----------------------------------------------------------- what Telegram knows

    async def admined_bots(self) -> list[OwnedBot]:
        """Every bot this account owns, from `bots.getAdminedBots`.

        Includes bots Telemax knows nothing about: they occupy slots too, and a
        capacity figure that counts only our own is a figure that lies right up
        until creation fails.
        """
        from telethon.tl.functions.bots import GetAdminedBotsRequest  # type: ignore[import-untyped]

        try:
            result = await self._client(GetAdminedBotsRequest())
        except Exception as error:
            raise MtprotoError(f"could not read the list of bots: {error}") from error

        users = result if isinstance(result, list) else getattr(result, "users", [])
        bots: list[OwnedBot] = []
        for user in users or []:
            bot_id = getattr(user, "id", None)
            if bot_id is None:
                continue
            username = getattr(user, "username", None)
            if not username:
                # A bot can hold several usernames; the first is the one
                # @BotFather assigned and the one a deterministic name matches.
                extra = getattr(user, "usernames", None) or []
                username = next((getattr(item, "username", None) for item in extra), None)
            bots.append(
                OwnedBot(
                    bot_id=int(bot_id),
                    username=str(username).lower() if username else None,
                    name=getattr(user, "first_name", None),
                )
            )
        return bots

    async def is_premium(self) -> bool:
        """Premium doubles most account limits, so it decides which key to read."""
        try:
            me = await self._client.get_me()
        except Exception:
            logger.debug("could not read the Premium status", exc_info=True)
            return False
        return bool(getattr(me, "premium", False))

    async def app_config(self) -> dict[str, Any]:
        """Telegram's own client configuration, flattened to plain values."""
        from telethon.tl.functions.help import (  # type: ignore[import-untyped]
            GetAppConfigRequest,
        )

        try:
            result = await self._client(GetAppConfigRequest(hash=0))
        except Exception as error:
            raise MtprotoError(f"could not read the Telegram app config: {error}") from error

        config = getattr(result, "config", result)
        return _json_object(config)

    async def bot_creation_limit(self) -> int | None:
        """How many bots this account may own, or None when Telegram is silent.

        Deliberately not defaulted to 20. A wrong number here is not a cosmetic
        problem: it either hides slots the owner has, or invites a provisioning
        run that deletes working bots and then cannot recreate them.
        """
        config = await self.app_config()
        premium = await self.is_premium()
        keys = (
            (*BOT_LIMIT_KEYS_PREMIUM, *BOT_LIMIT_KEYS_DEFAULT)
            if premium
            else BOT_LIMIT_KEYS_DEFAULT
        )
        for key in keys:
            value = config.get(key)
            if isinstance(value, int | float) and int(value) > 0:
                return int(value)
        logger.info("Telegram did not publish a bot creation limit in its app config")
        return None

    async def count(self) -> int | None:
        """How many bots the account owns, or None when it cannot be asked."""
        if self._client_or_none() is None:
            return None
        try:
            return len(await self.admined_bots())
        except Exception:
            logger.debug("could not count the account's bots", exc_info=True)
            return None

    async def creation_limit(self) -> int | None:
        """The account's real cap, or None. Never a guess — see `bot_creation_limit`."""
        if self._client_or_none() is None:
            return None
        try:
            return await self.bot_creation_limit()
        except Exception:
            logger.debug("could not read the account's bot limit", exc_info=True)
            return None

    async def premium(self) -> bool:
        return False if self._client_or_none() is None else await self.is_premium()


class SessionUnavailableError(MtprotoError):
    """The borrowed client is not connected. A wait, never a verdict."""

    def __init__(self) -> None:
        super().__init__("сессия владельца сейчас не подключена")


class BotFatherSession(AccountBots):
    """A logged-in user session, used only to drive @BotFather.

    Inherits the read-only account questions rather than repeating them: this
    session can answer them too, and the two answers must never differ.
    """

    def __init__(self, client: Any, secrets_dir: Path) -> None:
        super().__init__(client)
        self._secrets_dir = secrets_dir

    async def own_user_id(self) -> int | None:
        """Who this session belongs to — the `owner_user_id` nobody should type."""
        try:
            me = await self._client.get_me()
        except Exception:
            logger.debug("could not read the account id", exc_info=True)
            return None
        user_id = getattr(me, "id", None)
        return int(user_id) if user_id is not None else None

    async def send_to_saved(self, text: str) -> bool:
        """Write to the owner's own Saved Messages.

        This is the only way to put a link in front of somebody who has never
        opened the bot: a bot may not message first, and the owner always has a
        chat with themselves.
        """
        try:
            await self._client.send_message("me", text)
        except Exception:  # noqa: BLE001 - the caller falls back to the console
            logger.warning("could not write to Saved Messages")
            return False
        return True

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.disconnect()

    async def username_holder(self, username: str) -> int | None:
        """Which peer holds `@username`, or None when nobody does.

        This is what tells a username that is merely *ours and rebuildable*
        apart from one that belongs to a stranger — and after a deletion, it is
        how the release is observed rather than assumed.
        """
        from telethon import errors  # type: ignore[import-untyped]
        from telethon.tl.functions.contacts import (  # type: ignore[import-untyped]
            ResolveUsernameRequest,
        )

        try:
            result = await self._client(ResolveUsernameRequest(username=username))
        except errors.UsernameNotOccupiedError:
            return None
        except errors.UsernameInvalidError:
            return None
        except errors.FloodWaitError:
            raise
        except Exception as error:
            raise MtprotoError(f"could not resolve @{username}: {error}") from error

        for user in getattr(result, "users", []) or []:
            identifier = getattr(user, "id", None)
            if identifier is not None:
                return int(identifier)
        peer = getattr(result, "peer", None)
        for attribute in ("user_id", "channel_id", "chat_id"):
            identifier = getattr(peer, attribute, None)
            if identifier is not None:
                return int(identifier)
        return None

    async def delete_messages(self, bot_id: int, message_ids: list[int]) -> int:
        """Delete these messages in the chat with one of our bots, both sides.

        The reason this exists rather than the bot doing it: **a bot may not
        delete a message older than forty-eight hours, and the owner may.** The
        limit was lifted for user accounts years ago and never for bots, so a
        re-pull driven by the bot left everything older than two days in place —
        reported honestly as «останется», and still wrong.

        Targeted, not a dialog wipe. Only the ids the bridge itself placed are
        passed in, so the owner's own messages stay exactly where they are. That
        promise is on the confirmation screen and this is what keeps it.

        Returns how many Telegram says it removed. Telethon accepts the peer,
        and the peer is proved to be our bot first — the same gate as everything
        else addressed by id here.
        """
        if not message_ids:
            return 0
        client, entity = await self._own_bot(int(bot_id))
        try:
            await client.delete_messages(entity, message_ids, revoke=True)
        except Exception as error:
            raise MtprotoError(
                f"could not delete {len(message_ids)} message(s) with bot {bot_id}: {error}"
            ) from error
        return len(message_ids)

    async def send_start(self, username: str, bot_id: int | None = None) -> None:
        """`/start` from the owner's own account, which is the only way in.

        A bot cannot open a conversation, so the chat with a freshly created
        contact bot does not exist until somebody writes to it — and the person
        who must write is the owner. Idempotent by nature: a second `/start` is
        a second message, and the bridge treats it as the same event.

        By id whenever the caller knows it, and that is not a refinement. This
        used to send to the *username*, which the session resolves out of its
        own cache — so a bot rebuilt at a deterministic name got the dead peer,
        the send failed, the chat never opened, `my_chat_member` never arrived,
        and the greeting that would have repaired the cache was never sent.
        Compatibility tests confirmed: `could not send /start … MtprotoError`, twice, on
        exactly that path.

        Unlike the button, nothing forbids this: it is our own session, and
        `send_message` takes a resolved peer.
        """
        if bot_id is not None:
            _, entity = await self._own_bot(int(bot_id))
            try:
                await self._client.send_message(entity, "/start")
            except Exception as error:
                raise MtprotoError(f"could not send /start to bot {bot_id}: {error}") from error
            return
        try:
            await self._client.send_message(username, "/start")
        except Exception as error:
            raise MtprotoError(f"could not send /start to @{username}: {error}") from error

    async def _own_bot(self, bot_id: int) -> tuple[Any, Any]:
        """Resolve one of our bots by id, and prove it is that bot.

        The gate every id-addressed operation goes through, and the reason they
        are addressed by id at all. A username is answered from the client's own
        cache — a name outlives the bot that held it, and this account has one
        name that has covered four different bots — so a name is not an
        identity. An id is.

        Three proofs, because the callers do irreversible things: `wipe_dialog`
        destroys a conversation for both sides, and `send_start` writes to
        whoever it resolved. Anything short of "this is a bot and it is the one
        asked for" is a refusal, not a retry.

        The client is fetched before the guard on purpose: a session that is not
        connected is a wait, and folding it into `WrongPeerError` would turn a
        reconnect into a permanent refusal.
        """
        from telethon.tl import types  # type: ignore[import-untyped]

        client = self._client
        try:
            entity = await client.get_entity(types.PeerUser(user_id=bot_id))
        except Exception as error:
            raise WrongPeerError(f"could not resolve bot {bot_id}: {error}") from error

        if not isinstance(entity, types.User):
            raise WrongPeerError(f"peer {bot_id} is not a user-shaped peer")
        if not getattr(entity, "bot", False):
            raise WrongPeerError(f"peer {bot_id} is not a bot — refusing")
        if int(getattr(entity, "id", 0)) != bot_id:
            raise WrongPeerError(f"peer resolved to {entity.id}, not {bot_id}")
        return client, entity

    async def wipe_dialog(self, bot_id: int, *, keep_dialog: bool = False) -> int:
        """Delete the whole conversation with one of our bots. Returns its id.

        The owner's session can do what Bot API cannot: remove a private chat
        entirely — every message, both sides, no forty-eight-hour rule — and
        take the chat out of the dialog list with it. That is the difference
        between a bridge rebuilt into a chat still holding the last one's
        conversation and a bridge rebuilt into a clean one.

        **The identity gate below is the whole safety of this method.**
        `revoke=True` against the wrong peer destroys a real conversation for
        both sides and nothing brings it back. So the peer is resolved *by id,
        from the bridge row*, and three things are proved before anything is
        deleted: it is a user-shaped peer, it is a bot, and it is the bot whose
        id was asked for.

        Never by username. Usernames outlive the bots that held them — measured
        on this install, a deleted bot's name resolved to its replacement while
        clients still opened the old chat from cache. A name is not an identity.

        "Both sides" is the owner and the owner's own bot; there is no third
        party to lose anything.

        `keep_dialog` decides between the two things this call can do, and the
        difference is not cosmetic. Removing the dialog outright is how Telegram
        represents *stopping* a bot: the owner is no longer a started user, the
        bot may not write to them again until they press Start, and an import
        firing straight afterwards lands every message in a chat that refuses
        them. Measured on the first re-pull that used this — the bridge looked
        restarted and no history arrived.

        So a teardown removes the dialog (the bot is about to be deleted; the
        chat should not survive it) and a re-pull only empties it.
        """
        from telethon.tl.functions.messages import (  # type: ignore[import-untyped]
            DeleteHistoryRequest,
        )

        wanted = int(bot_id)
        client, entity = await self._own_bot(wanted)

        try:
            await client(
                DeleteHistoryRequest(
                    peer=entity, max_id=0, just_clear=keep_dialog, revoke=True
                )
            )
        except Exception as error:
            raise MtprotoError(f"could not wipe the dialog with bot {wanted}: {error}") from error
        logger.info(
            "%s the dialog with bot %s", "emptied" if keep_dialog else "removed", wanted
        )
        return wanted

    # ------------------------------------------------------------------ @BotFather

    async def create_bot(self, *, name: str, username: str) -> CreatedBot:
        """Walk `/newbot` to a token for exactly this username.

        There is no fallback candidate. The username is derived from an
        identifier that does not change, so "taken" is information — either the
        bot is already there or somebody else has the name — and inventing a
        near-miss would break every link the owner has saved.
        """
        await self._send("/newbot")
        reply = await self._await_reply()
        if reply.kind is Reply.TOO_SOON:
            raise BotFatherTooSoonError(reply.retry_after)
        if reply.kind is Reply.LIMIT:
            raise MtprotoError("BOT_CREATE_LIMIT_EXCEEDED")
        if reply.kind is not Reply.ASK_NAME:
            raise MtprotoError(f"unexpected answer to /newbot: {reply.kind}")

        await self._send(name)
        reply = await self._await_reply()
        if reply.kind is Reply.NAME_INVALID:
            raise MtprotoError(f"BotFather refused the name {name!r}")
        if reply.kind is not Reply.ASK_USERNAME:
            raise MtprotoError(f"unexpected answer to the name: {reply.kind}")

        await self._send(username)
        reply = await self._await_reply()
        if reply.kind is Reply.TOKEN and reply.token:
            return CreatedBot(
                username=username, token=reply.token, bot_id=bot_id_of(reply.token)
            )
        if reply.kind is Reply.USERNAME_TAKEN:
            raise UsernameStillTakenError(username)
        if reply.kind is Reply.TOO_SOON:
            raise BotFatherTooSoonError(reply.retry_after)
        if reply.kind is Reply.LIMIT:
            raise MtprotoError("BOT_CREATE_LIMIT_EXCEEDED")
        raise MtprotoError(f"unexpected answer to the username: {reply.kind}")

    async def token_of(self, username: str) -> str:
        """The existing token of a bot that already exists, over `/token`.

        The counterpart of `getManagedBotToken`, and the reason the session path
        does not have to delete a bot to recover a token it lost. `/token`, never
        `/revoke`: the second mints a new one and invalidates the old, which
        would stop a bridge that is running perfectly well on it.
        """
        target = username.lstrip("@")
        await self._send("/token")
        reply = await self._await_reply()
        if reply.kind is Reply.TOKEN and reply.token:
            # Some wordings hand it straight over when the account has one bot.
            return str(reply.token)
        if reply.kind is Reply.TOO_SOON:
            raise BotFatherTooSoonError(reply.retry_after)
        if reply.kind is not Reply.ASK_TOKEN_TARGET:
            raise MtprotoError(f"unexpected answer to /token: {reply.kind}")

        await self._send(f"@{target}")
        reply = await self._await_reply()
        if reply.kind is Reply.TOKEN and reply.token:
            return str(reply.token)
        if reply.kind is Reply.NO_SUCH_BOT:
            raise MtprotoError(f"@BotFather does not know a bot called @{target}")
        raise MtprotoError(f"@BotFather did not hand over the token: {reply.kind}")

    async def delete_bot(self, username: str) -> None:
        """Walk `/deletebot` for one bot, and confirm in BotFather's own words.

        The caller has already established that this bot belongs to the current
        account and carries the expected username — this function does not
        re-derive that, it only refuses to guess when BotFather says something
        it does not recognise.
        """
        await self._send("/deletebot")
        reply = await self._await_reply()
        if reply.kind is Reply.DELETED:
            # Some wordings answer the command itself when there is one bot.
            return
        if reply.kind is not Reply.ASK_DELETE_TARGET:
            raise MtprotoError(f"unexpected answer to /deletebot: {reply.kind}")

        await self._send(f"@{username.lstrip('@')}")
        reply = await self._await_reply()
        if reply.kind is Reply.NO_SUCH_BOT:
            raise MtprotoError(f"@BotFather does not know a bot called @{username}")
        if reply.kind is Reply.DELETED:
            return
        if reply.kind is not Reply.CONFIRM_DELETE:
            raise MtprotoError(f"unexpected answer to the bot username: {reply.kind}")

        await self._send(DELETE_CONFIRMATION)
        reply = await self._await_reply()
        if reply.kind is not Reply.DELETED:
            raise MtprotoError(f"@BotFather did not confirm the deletion: {reply.kind}")

    async def _send(self, text: str) -> None:
        await self._client.send_message(BOTFATHER, text)

    async def _await_reply(self) -> Any:
        """Wait for BotFather's next message and classify it.

        Polling the last message rather than subscribing keeps this independent
        of Telethon's event loop plumbing, and the dialogue is four steps long.
        """
        deadline = asyncio.get_running_loop().time() + REPLY_TIMEOUT_SECONDS
        seen: str | None = None

        while asyncio.get_running_loop().time() < deadline:
            messages = await self._client.get_messages(BOTFATHER, limit=1)
            if messages:
                message = messages[0]
                text = getattr(message, "message", "") or ""
                marker = f"{getattr(message, 'id', 0)}"
                if marker != seen and not getattr(message, "out", False):
                    seen = marker
                    logger.debug("BotFather said: %s", redact(text)[:120])
                    return parse(text)
            await asyncio.sleep(0.7)

        raise MtprotoError("BotFather did not answer in time")


async def connect(
    *,
    api_id: int,
    api_hash: str,
    phone: str,
    secrets_dir: Path,
    code_provider: Any = None,
    password_provider: Any = None,
) -> BotFatherSession:
    """Log the owner's Telegram account in, or reuse a stored session.

    The session lands in the secrets directory at 0600: whoever holds it holds
    the account, not merely a bot.
    """
    try:
        from telethon import TelegramClient
    except ImportError as error:  # pragma: no cover - optional dependency
        raise MtprotoError(
            "Telethon is not installed. This project is not on PyPI: install it from the "
            "checkout with `.venv/bin/pip install -e '.[mtproto]'`, or create bots by "
            "hand in @BotFather"
        ) from error

    # One directory creation at start-up; not worth an async filesystem layer.
    secrets_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    os.chmod(secrets_dir, stat.S_IRWXU)
    path = session_path(secrets_dir)

    client = TelegramClient(str(path.with_suffix("")), api_id, api_hash)
    await client.connect()

    if not await client.is_user_authorized():
        if code_provider is None:
            # A background service has no terminal to answer a login code in.
            raise MtprotoError(
                "нет сохранённой сессии Telegram — выполните "
                "`python -m bridge telegram-login` один раз"
            )
        await client.send_code_request(phone)
        code = await _ask(code_provider, "Код из Telegram: ")
        try:
            await client.sign_in(phone, code)
        except Exception as error:
            # A 2FA password is a normal outcome, not a failure.
            if "password" not in str(error).lower():
                raise MtprotoError(f"sign-in failed: {error}") from error
            password = await _ask(password_provider, "Пароль двухфакторной аутентификации: ")
            await client.sign_in(password=password)

    harden(path)
    return BotFatherSession(client, secrets_dir)


def _json_object(node: Any) -> dict[str, Any]:
    """Telegram's `JsonObject` as a plain dict, one level of nesting resolved."""
    entries = getattr(node, "value", None)
    if not isinstance(entries, list):
        return {}
    out: dict[str, Any] = {}
    for entry in entries:
        key = getattr(entry, "key", None)
        if key is None:
            continue
        out[str(key)] = _json_value(getattr(entry, "value", None))
    return out


def _json_value(node: Any) -> Any:
    if node is None:
        return None
    value = getattr(node, "value", None)
    if isinstance(value, list):
        # Either a nested object (a list of key/value entries) or an array.
        if value and hasattr(value[0], "key"):
            return _json_object(node)
        return [_json_value(item) for item in value]
    return value


async def _ask(provider: Any, prompt: str) -> str:
    if provider is None:
        from getpass import getpass

        return getpass(prompt).strip()
    value = provider(prompt)
    if asyncio.iscoroutine(value):
        value = await value
    return str(value).strip()


class BorrowedBotFatherSession(BotFatherSession):
    """@BotFather over a client somebody else owns and keeps alive.

    Production already holds one authorised Telethon connection: the owner's
    intake session. A second one on the same session file is how an account
    gets its keys revoked, so provisioning borrows that connection rather than
    opening its own.

    Safe to borrow because the conversation is *polled*, not subscribed —
    `_await_reply` reads the last message in the @BotFather chat instead of
    installing a handler, so it cannot compete with intake's own. And the
    `/newbot` this sends is an owner message in a chat that is not a bridge,
    which intake already ignores by name.

    Two overrides, both about not owning what you borrowed:

    * `close` does nothing. Disconnecting this client would take the owner's
      whole intake down with it.
    * an absent client is a sentence rather than an `AttributeError` three
      frames further in — intake reconnects, and a walk that starts during the
      gap must fail as "not now", not as "cannot".
    """

    def __init__(self, client: Callable[[], Any | None], *, secrets_dir: Path) -> None:
        super().__init__(client, secrets_dir)

    @property
    def _client(self) -> Any:
        live = self._client_or_none()
        if live is None:
            raise SessionUnavailableError
        return live

    @property
    def connected(self) -> bool:
        return self._client_or_none() is not None

    async def close(self) -> None:
        """Nothing. The client belongs to intake and outlives every walk."""
        logger.debug("borrowed session left open; it is not ours to close")
