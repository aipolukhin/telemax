"""Adapters between the routing core and the two client libraries.

Routing speaks in bridge names and ids; aiogram speaks in `Bot` objects and
PyMax in its own client. These three small classes are the seam, and they are
the only place where both worlds are visible at once.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import tzinfo
from pathlib import Path
from typing import Any

from aiogram import F, Router
from aiogram.types import Contact, Message

from bridge.config import TimestampStyle
from bridge.formatting import forward_prefix
from bridge.max_client import MaxClient
from bridge.presence import AutoRead, remember_text
from bridge.telegram import (
    BotRegistry,
    LiveBridge,
    TelegramTransportUnavailableError,
    bot_api_forward_author,
    bot_api_forward_date,
    is_bot_api_forward,
)
from bridge.telegram.commands import is_bot_command

from .echo import OwnEchoes
from .router import (
    UNSUPPORTED_NOTICE,
    BridgeRouter,
    BridgeTarget,
)

logger = logging.getLogger(__name__)

#: Called when the gate closes the Bot API path for one owner message. Optional
#: so a router built without it behaves exactly as before; wired in the running
#: service so the hand-off is countable rather than invisible.
OwnerMessageSeen = Callable[[int, int], Awaitable[None]]
OwnerIntakeSuppressed = Callable[[], Awaitable[None]]


async def note_owner_intake_suppressed(note: OwnerIntakeSuppressed | None, bot_id: int) -> None:
    """Say that one owner message was left to the MTProto session.

    The gate closes this path on purpose and keeps it closed across a
    disconnect — a lost message is the accepted trade against a duplicated one.
    What was not acceptable is that the trade was silent: a session that stops
    receiving owner updates looked exactly like a session carrying them, and the
    only trace was a `debug` line nobody has enabled. Structure only: the bot id
    is ours, and nothing about the message itself is read, logged or counted.
    """
    logger.info("owner message left to the MTProto session for bot %s", bot_id)
    if note is not None:
        with contextlib.suppress(Exception):
            # Health is a report, never a gate: a counter that cannot be written
            # must not stop the routing decision that was already made.
            await note()


def bridge_router_lookup(bridge_router: BridgeRouter, bot_id: int) -> BridgeTarget | None:
    return bridge_router.target_for_bot(bot_id)


def forward_line_of(
    message: Message,
    *,
    timestamp_style: TimestampStyle = TimestampStyle.COMPACT,
    timezone: tzinfo | None = None,
) -> str:
    """The forward line for a message the owner passed on, or `""`.

    MAX's own forward is a link to a message id *inside MAX*, so a message that
    came out of Telegram has nothing to point at: the origin has to travel as
    ordinary text. The line is plain rather than markdown on purpose — PyMax
    parses markdown out of everything it sends and has no escape syntax, so a
    contact called `*` would otherwise turn the rest of the message bold.

    It carries the original's own time, the same as a forward travelling the
    other way: MAX stamps the message with the moment it arrived, which for
    anything forwarded is not when it was written.
    """
    if not is_bot_api_forward(message):
        return ""
    author = bot_api_forward_author(message)
    return forward_prefix(
        author.name,
        username=author.username,
        at_ms=bot_api_forward_date(message),
        style=timestamp_style,
        tz=timezone,
    )


def mark_forwarded(
    text: str,
    message: Message,
    *,
    timestamp_style: TimestampStyle = TimestampStyle.COMPACT,
    timezone: tzinfo | None = None,
) -> str:
    """`forward_line_of`, already applied to the text it belongs in front of."""
    line = forward_line_of(message, timestamp_style=timestamp_style, timezone=timezone)
    return f"{line}{text}"


def _vcard_escape(value: str) -> str:
    """Escape the four characters vCard 3.0 gives meaning to, in that order.

    Backslash first, or it would double-escape the escapes added after it. A
    name or phone is otherwise passed through — a stray `;` in a name would
    start a spurious field, and a newline would end the line early.
    """
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def vcard_of(contact: Contact) -> str:
    """A vCard for a Telegram contact, built from its fields.

    Telegram's own `contact.vcard` is usually empty and, when present, need not
    carry a phone at all — so the card is built from the structured fields
    instead, which are the two MAX actually parses out (`FN` and `TEL`). The
    phone is mandatory on an aiogram `Contact`, so there is always something to
    dial; the name is `first last`, trimmed.
    """
    name = " ".join(
        part for part in (contact.first_name or "", contact.last_name or "") if part.strip()
    ).strip() or "Контакт"
    phone = str(contact.phone_number or "").strip()
    return (
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        f"FN:{_vcard_escape(name)}\r\n"
        f"TEL;TYPE=CELL:{_vcard_escape(phone)}\r\n"
        "END:VCARD"
    )


class RegistryLookup:
    """`BridgeLookup` backed by the live registry, so hot-added bridges work."""

    def __init__(self, registry: BotRegistry) -> None:
        self._registry = registry

    def bridge_for_max_chat(self, max_chat_id: int) -> BridgeTarget | None:
        live = self._registry.by_max_chat(max_chat_id)
        return self._target(live)

    def bridge_for_bot(self, bot_id: int) -> BridgeTarget | None:
        live = self._registry.by_bot_id(bot_id)
        return self._target(live)

    @staticmethod
    def _target(live: LiveBridge | None) -> BridgeTarget | None:
        if live is None:
            return None
        return BridgeTarget(
            name=live.name,
            max_chat_id=live.max_chat_id,
            bot_id=live.identity.bot_id,
        )


class RegistrySender:
    """`TelegramSender` that finds the right `Bot` by id."""

    def __init__(self, registry: BotRegistry) -> None:
        self._registry = registry

    async def send_text(
        self,
        bot_id: int,
        chat_id: int,
        text: str,
        *,
        reply_to: int | None = None,
        entities: list[dict[str, Any]] | None = None,
    ) -> int | None:
        live = self._registry.by_bot_id(bot_id)
        if live is None:
            # Proven not sent, and said so rather than answered as `None`: the
            # caller turned `None` into "Telegram returned no message id", which
            # is the wording for a message that may be in the chat. A bot that is
            # no longer registered is the opposite — no request was built.
            raise TelegramTransportUnavailableError(f"bot {bot_id} is no longer registered")
        sent = await live.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_to_message_id=reply_to,
            entities=entities,  # type: ignore[arg-type]
        )
        message_id = getattr(sent, "message_id", None)
        if message_id is None:
            return None
        # Bot API cannot read a message back, so a later read tick has no way to
        # know what to re-send unless we remember it here.
        remember_text(bot_id, chat_id, int(message_id), text)
        return int(message_id)


class RegistryMutations:
    """`BotMutations`: editing and deleting the bot's own messages.

    Every method used to live on `RegistrySender` and answer `False` for any
    failure — so "Telegram is unreachable", "this message has no text to edit"
    and "a bot may not delete this" were one outcome, and the caller had nowhere
    to record any of them. Nothing is swallowed here: the durable job behind the
    call is what decides whether a failure is a no-op, a wait or a refusal, and
    it can only decide with the exception in hand.
    """

    def __init__(self, registry: BotRegistry) -> None:
        self._registry = registry

    def _bot(self, bot_id: int) -> Any:
        live = self._registry.by_bot_id(bot_id)
        if live is None:
            raise TelegramTransportUnavailableError(f"bot {bot_id} is no longer registered")
        return live.bot

    @staticmethod
    def _entities(items: list[dict[str, Any]] | None) -> list[Any] | None:
        if not items:
            return None
        from aiogram.types import MessageEntity

        return [MessageEntity.model_validate(item) for item in items]

    async def delete(self, bot_id: int, chat_id: int, message_id: int) -> None:
        await self._bot(bot_id).delete_message(chat_id=chat_id, message_id=message_id)

    async def edit_text(
        self,
        bot_id: int,
        chat_id: int,
        message_id: int,
        text: str,
        entities: list[dict[str, Any]] | None = None,
    ) -> None:
        await self._bot(bot_id).edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            entities=self._entities(entities),
        )
        # Bot API cannot read a message back, so the read tick has no way to know
        # what to re-send unless the new body is remembered here too.
        remember_text(bot_id, chat_id, message_id, text)

    async def edit_caption(
        self,
        bot_id: int,
        chat_id: int,
        message_id: int,
        caption: str,
        entities: list[dict[str, Any]] | None = None,
    ) -> None:
        """The method that did not exist, and whose absence lost every caption edit."""
        await self._bot(bot_id).edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=caption,
            caption_entities=self._entities(entities),
        )


class MaxTextSender:
    """`MaxSender` over the MAX client wrapper."""

    def __init__(self, client: MaxClient) -> None:
        self._client = client

    async def send_text(self, chat_id: int, text: str, *, reply_to: int | None = None) -> int:
        return await self._client.send_text(chat_id, text, reply_to=reply_to)

    async def send_media(
        self,
        chat_id: int,
        items: list[tuple[str, Path, str]],
        *,
        text: str = "",
        reply_to: int | None = None,
    ) -> int:
        return await self._client.send_media(chat_id, items, text=text, reply_to=reply_to)

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> None:
        await self._client.edit_text(chat_id, message_id, text)

    async def delete_messages(
        self, chat_id: int, message_ids: list[int], *, for_everyone: bool = True
    ) -> None:
        await self._client.delete_messages(chat_id, message_ids, for_everyone=for_everyone)

    async def send_contact(
        self,
        chat_id: int,
        *,
        vcard: str,
        contact_user_id: int | None = None,
        reply_to: int | None = None,
    ) -> int:
        return await self._client.send_contact(
            chat_id, vcard=vcard, contact_user_id=contact_user_id, reply_to=reply_to
        )


class GuardianBridgeLink:
    """`BridgeLink` that points a shared MAX contact at the guardian.

    A deep link rather than any logic in the contact bot: the button opens the
    guardian on «поднять мост» with the person's MAX user id in the start
    payload, and the whole provisioning flow stays where it already lives. None
    when there is no guardian username to link to (a deployment with
    provisioning off), so the card is drawn without a button.
    """

    def __init__(self, guardian_username: str | None) -> None:
        self._username = (guardian_username or "").strip() or None

    def for_contact(self, max_user_id: int) -> str | None:
        if self._username is None:
            return None
        from bridge.onboarding.tokens import deep_link
        from bridge.provisioning.byphone import bridge_deep_link_payload

        return deep_link(self._username, bridge_deep_link_payload(max_user_id))


class MaxDisplayNames:
    """`DisplayNames` over the MAX client, resolved late.

    Through a provider rather than by value: the router is built before the
    session is up, and the client object it would have captured then is None.
    Asking each time means the first forwarded message is not doomed to be the
    unattributed one.

    `own_name`, never `display_name`: the latter is the owner's own address-book
    label, which is right on the bot that carries the contact and wrong on a
    line that names a third party to somebody else.
    """

    def __init__(self, client_provider: Callable[[], MaxClient | None]) -> None:
        self._provider = client_provider

    async def forward_author(self, user_id: int) -> tuple[str | None, str | None]:
        client = self._provider()
        if client is None:
            return None, None
        contact = await client.contact_profile(user_id)
        if contact is None:
            return None, None
        return contact.own_name, contact.profile_link

    async def contact_avatar(self, user_id: int) -> str | None:
        """The full-resolution avatar for a shared contact's card.

        `avatar_url` is the profile's `baseUrl` — 1440px, the same the guardian
        shows — rather than the 190px thumbnail a CONTACT attach carries.
        """
        client = self._provider()
        if client is None:
            return None
        contact = await client.contact_profile(user_id)
        return contact.avatar_url if contact is not None else None


def build_forwarding_router(
    bridge_router: BridgeRouter,
    *,
    on_owner_message: AutoRead | None = None,
    own_echoes: OwnEchoes | None = None,
    on_owner_intake_suppressed: OwnerIntakeSuppressed | None = None,
    on_owner_message_seen: OwnerMessageSeen | None = None,
    timestamp_style: TimestampStyle = TimestampStyle.COMPACT,
    timezone: tzinfo | None = None,
) -> Router:
    """What the contact bot sees the owner do — and deliberately does not carry.

    The owner's Telegram is a puppet session, and it is the only authority on
    what the owner did. Everything they write, edit, delete, share or react to
    reaches MAX through that session. This router still exists, and still sees
    every one of those messages, because the bot is the chat they are typing
    into — but it carries none of them.

    It used to. `owner_mtproto_intake` was a gate: MTProto authoritative when
    true, Bot API forwarding when false. That is a fallback, and a fallback for
    the owner's own events is a second source of truth for one user action —
    with different ids, different keys and different coverage. It is gone; there
    is no configuration under which Bot API becomes the owner ingress again.

    What is left here is real and has to stay:

    * **echo suppression.** A message the bridge itself placed on the owner's
      behalf comes back through this bot, and recognising it is what stops the
      owner seeing their own line twice — and what gives a reply to it a
      bot-side id to resolve through.
    * **counting the hand-off.** Every owner message this path stands aside for
      is counted, so "the session is carrying them" can be told apart from "they
      are going nowhere".

    Included *after* the command router, and it still checks for a leading
    slash: relying on router order alone would make an accidental reordering
    type `/status` at somebody's mother.
    """
    router = Router(name="forwarding")

    @router.edited_message()
    async def _edited(message: Message) -> None:
        """The owner edited a message. The session carries it; this counts it.

        Registered rather than absent so the hand-off is countable: an edit that
        reaches neither transport should show up as a number that climbs while
        nothing changes in MAX, not as silence.
        """
        text = message.text or message.caption or ""
        if not text or text.startswith("/"):
            return
        await note_owner_intake_suppressed(
            on_owner_intake_suppressed, message.bot.id if message.bot else 0
        )

    @router.message(F.contact)
    async def _contact(message: Message) -> None:
        """A contact the owner shared, carried into MAX as a native contact."""
        contact = message.contact
        if contact is None:
            return
        # The owner's session carries the card itself; this path only counts
        # that it stood aside.
        await note_owner_intake_suppressed(
            on_owner_intake_suppressed, message.bot.id if message.bot else 0
        )

    @router.message()
    async def _forward(message: Message) -> None:
        text = message.text or message.caption or ""
        if is_bot_command(text):
            # Shared with the MTProto intake on purpose: the two paths read the
            # same chat and used to disagree about this, which is how «/start»
            # was answered by the bot *and* delivered to the contact.
            return

        if not text:
            # Attachments are handled by the upload router, which runs first;
            # anything reaching here with no text is something neither of us
            # knows what to do with.
            await message.answer(UNSUPPORTED_NOTICE)
            return

        bot_id = message.bot.id if message.bot else 0
        claim = own_echoes.claim(bot_id, text) if own_echoes is not None else None
        if claim is not None:
            # A message the bridge itself placed on the owner's behalf (see
            # `routing/echo.py`). It is already in MAX — the owner wrote it there
            # — and sending it back would show them their own line twice. The copy
            # is also where the bot's own id for it appears, which is what makes a
            # reply to it resolvable.
            if claim.placed is not None:
                await bridge_router.note_own_placement(
                    bot_id=bot_id,
                    telegram_message_id=message.message_id,
                    placed=claim.placed,
                )
            return
        # Not an echo, so it is the owner writing: their session carries it.
        # Dropping *after* the echo check is what keeps own-voice suppression
        # intact while leaving the message itself to the one transport that owns
        # it.
        #
        # One thing is taken from it and only one: the bot's own id for the
        # message, which nothing else has and which a bot needs to put a
        # reaction on it. An observation, not an intake — see `owner_binding`.
        if on_owner_message_seen is not None:
            await on_owner_message_seen(bot_id, message.message_id)
        await note_owner_intake_suppressed(on_owner_intake_suppressed, bot_id)

    return router
