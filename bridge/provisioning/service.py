"""Turning a stranger in MAX into a bridge, without a restart.

Bot API cannot create bots — only @BotFather can, and only for a user account.
So the default flow is a conversation: a new contact writes, the guardian bot
asks the owner, the owner creates a bot in BotFather and pastes the token back,
and the bridge comes up on the running process.

Everything the contact wrote meanwhile is held in `pending_inbox` and replayed
in arrival order once the bot exists, so the conversation is not missing its
beginning. The buffer has a cap and a TTL: an unknown contact must not be able
to fill the disk while the owner is asleep.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import SecretStr

from bridge.config import AppConfig, BridgeSource, ResolvedBridge, UnknownChatPolicy
from bridge.max_client import IncomingMaxMessage
from bridge.storage import (
    BridgeRecord,
    BridgeRepository,
    BridgeState,
    PendingContactRepository,
    PendingContactState,
)

from .byphone import is_personal_chat
from .secrets import ContactBotSecretStore

logger = logging.getLogger(__name__)

# `123456789:AA...` — checked before the token is ever sent anywhere, and the
# same pattern redacts it from logs (log redaction).
TOKEN_PATTERN = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$")


def bridge_name_for_username(username: str) -> str:
    """`k4md26v3g7sh2qpn_max_bot` -> `k4md26v3g7sh2qpn`.

    The bridge name has to be as stable as the username it belongs to, or a
    rebuilt bot would arrive under a new name and its history, its read marks
    and its pinned status line would all be filed under the old one.
    """
    stem = username.lower().removesuffix("_max_bot").removesuffix("_bot")
    return stem or username.lower()


def token_env_for(username: str) -> str:
    """The variable name that will hold this bot's token. Never the token."""
    return f"TELEMAX_BOT_{bridge_name_for_username(username).upper()}"




class BridgeActivator(Protocol):
    """Brings a bridge up on the live process."""

    async def add(self, bridge: ResolvedBridge, *, start: bool = True) -> object: ...


#: Pulls the existing MAX conversation into a freshly made bot. Returns how many
#: messages were delivered.
Backfiller = Callable[[int, int], Awaitable[int]]


class MessageReplayer(Protocol):
    async def replay(self, bridge_name: str, messages: list[dict[str, object]]) -> None: ...


@dataclass(frozen=True, slots=True)
class PendingAnnouncement:
    """What the guardian should ask the owner."""

    max_chat_id: int
    display_name: str
    buffered: int
    #: The nonce this question's buttons carry, so the answer can be spent once.
    revision: int = 0


class ProvisioningError(Exception):
    """The owner did something the bridge cannot act on. The text is for them."""


class Provisioner:
    def __init__(
        self,
        *,
        config: AppConfig,
        bridges: BridgeRepository,
        pending: PendingContactRepository,
        activator: BridgeActivator,
        replayer: MessageReplayer,
        backfiller: Backfiller | None = None,
        secrets: ContactBotSecretStore | None = None,
    ) -> None:
        self._config = config
        self._bridges = bridges
        self._pending = pending
        self._activator = activator
        self._replayer = replayer
        self._backfiller = backfiller
        # The one writer of `bots.env`. Passed in rather than built here: the
        # lock inside it is only worth having while one object holds it for the
        # life of the process.
        self._secrets = secrets or ContactBotSecretStore(config.secrets_file)
        # One lock per process: two messages from the same new contact arriving
        # together must not produce two questions or two bots.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- incoming side

    async def on_unbridged_message(
        self, message: IncomingMaxMessage, *, display_name: str | None
    ) -> PendingAnnouncement | None:
        """Record a message from a contact with no bridge yet.

        Returns what to ask the owner, or None when nothing should be asked.
        """
        policy = self._config.provisioning.unknown_chat_policy
        if policy is UnknownChatPolicy.IGNORE:
            return None

        if not is_personal_chat(message.chat_id):
            # One bot is one person. A group has no contact to name a bot after,
            # the picker filters groups out, and the «Создать» button under such
            # an announcement could only ever end in «личных диалогов не нашлось».
            logger.debug("MAX chat %s is not a personal dialog; not asking", message.chat_id)
            return None

        async with self._lock:
            contact = await self._pending.get(message.chat_id)
            if contact is not None and contact.state in {
                PendingContactState.IGNORED,
                PendingContactState.BLOCKED,
            }:
                return None

            contact = await self._pending.note_seen(
                max_chat_id=message.chat_id,
                max_user_id=message.sender_id,
                display_name=display_name or (contact.display_name if contact else None),
            )

            stored = await self._pending.buffer(
                message.chat_id,
                {
                    "message_id": message.message_id,
                    "text": message.text,
                    "timestamp": message.timestamp,
                    "attachments": [item.kind.value for item in message.attachments],
                },
                cap=self._config.provisioning.pending_max_messages,
            )
            if not stored:
                logger.warning("buffer for MAX chat %s is full", message.chat_id)

            already_asked = contact.state is PendingContactState.ASKED
            if already_asked:
                return None

            revision = await self._pending.note_asked(message.chat_id)

        return PendingAnnouncement(
            max_chat_id=message.chat_id,
            display_name=display_name or contact.display_name or "неизвестный контакт",
            buffered=contact.buffered + 1,
            revision=revision,
        )

    async def awaiting_decision(self) -> int:
        """Contacts the guardian asked about and nobody answered for.

        Invisible until now: the question is asked once, so a missed
        announcement meant a person quietly buffering for ever with nothing on
        any screen saying so. Production had four.
        """
        return await self._pending.count_in_state(PendingContactState.ASKED)

    async def expire_buffered(self) -> int:
        """Apply `pending_ttl_days`. Nothing remote, nothing created, nothing bridged.

        The config key existed and the sweep had no caller, so the docstring at
        the top of this file promising a TTL was not true: production was holding
        messages older than the seven days it claims.
        """
        days = self._config.provisioning.pending_ttl_days
        return await self._pending.expire(older_than_ms=days * 86_400_000)

    async def consume_announcement(self, max_chat_id: int, revision: int) -> bool:
        """Spend the one answer this question has. False when it is already spent."""
        return await self._pending.consume_announcement(max_chat_id, revision)

    async def ignore(self, max_chat_id: int, *, block: bool = False) -> None:
        """Stop asking about this contact. `block` also stops buffering."""
        await self._pending.set_state(
            max_chat_id,
            PendingContactState.BLOCKED if block else PendingContactState.IGNORED,
        )
        if block:
            await self._pending.drain(max_chat_id)

    # ------------------------------------------------------------- picking dialogs

    async def bridged_chat_ids(self) -> set[int]:
        """MAX chats that already have a bot, so the picker does not offer them."""
        return {record.max_chat_id for record in await self._bridges.active()}

    def attach_backfiller(self, backfiller: Backfiller) -> None:
        """Wired after construction: it needs the router, which needs this."""
        self._backfiller = backfiller

    async def backfill(self, max_chat_id: int, limit: int) -> int:
        """Pull the conversation that existed before this bridge did.

        Deliberately not automatic. A dialog can be years long, the owner may
        not want it copied into Telegram at all, and the only person who knows
        is the owner — so the guardian asks, and this runs only if they say yes.
        """
        if self._backfiller is None:
            raise ProvisioningError("Загрузка истории недоступна: нет связи с MAX.")
        return await self._backfiller(max_chat_id, limit)

    # --------------------------------------------------------------- token intake

    async def activate(self, *, max_chat_id: int, token: str, name: str | None = None) -> str:
        """Store the token, bring the bridge up, replay the backlog.

        Returns the bridge name. Raises `ProvisioningError` with a message meant
        for the owner when the token or the mapping is unusable.
        """
        token = token.strip()
        if not TOKEN_PATTERN.match(token):
            raise ProvisioningError(
                "Это не похоже на токен бота. Он выглядит как 123456789:AA... от @BotFather."
            )

        existing = await self._bridges.by_max_chat(max_chat_id)
        if existing is not None and existing.state is BridgeState.ACTIVE:
            raise ProvisioningError(f"Для этого контакта уже есть мост «{existing.bridge_name}».")

        # An existing row keeps its name. Inserting a second row for one MAX chat
        # is a `UNIQUE(max_chat_id)` violation, and it used to happen after the
        # token had already been written down.
        bridge_name = (
            existing.bridge_name
            if existing is not None
            else (name or await self._unique_name(max_chat_id))
        )
        token_env = f"TELEMAX_BOT_{bridge_name.upper()}"
        await self._secrets.save(token_env, token)

        bridge = ResolvedBridge(
            name=bridge_name,
            max_chat_id=max_chat_id,
            token_env=token_env,
            token=SecretStr(token),
            source=BridgeSource.GUARDIAN,
        )

        # The durable row before the transport, and in a state that does not
        # claim to be serving. Starting the poller first left a bot carrying
        # somebody's messages with nothing in the register naming it: it worked
        # until the next restart and then vanished.
        await self._bridges.upsert(
            BridgeRecord(
                bridge_name=bridge_name,
                max_chat_id=max_chat_id,
                token_env=token_env,
                source=BridgeSource.GUARDIAN.value,
                state=BridgeState.PROVISIONING,
            )
        )

        try:
            live = await self._activator.add(bridge)
        except Exception as error:
            raise ProvisioningError(f"Бот не поднялся: {error}") from error

        identity = getattr(live, "identity", None)
        await self._bridges.upsert(
            BridgeRecord(
                bridge_name=bridge_name,
                max_chat_id=max_chat_id,
                token_env=token_env,
                telegram_bot_id=getattr(identity, "bot_id", None),
                source=BridgeSource.GUARDIAN.value,
                state=BridgeState.ACTIVE,
            )
        )
        await self._pending.set_state(max_chat_id, PendingContactState.PROVISIONED)

        backlog = await self._pending.drain(max_chat_id)
        if backlog:
            await self._replayer.replay(bridge_name, backlog)

        return bridge_name

    async def _unique_name(self, max_chat_id: int) -> str:
        base = f"chat{abs(max_chat_id) % 1_000_000}"
        candidate = base
        suffix = 1
        while await self._bridges.get(candidate) is not None:
            suffix += 1
            candidate = f"{base}_{suffix}"
        return candidate

    @property
    def secrets(self) -> ContactBotSecretStore:
        return self._secrets

    @staticmethod
    def secrets_path(config: AppConfig) -> Path:
        return config.secrets_file
