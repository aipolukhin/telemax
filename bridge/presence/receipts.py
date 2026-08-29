"""Read receipts: a watermark on one side, our own markup on the other.

Two facts shape everything here.

**MAX reports reading as a watermark.** `NOTIF_MARK` carries a timestamp, not a
message id: everything older than it counts as read. Per-message ticks would be
an invention, so the bridge tracks one mark per bridge and shows the tick on the
newest delivered message only.

**Bot API cannot draw ✓✓.** There are no read receipts for bots and no way to
change a message's status, so the tick is markup we add: a suffix on the last
outgoing message, or a reaction on it. Editing the whole history on every mark
would burn the rate limit to redraw ticks nobody is looking at.

A contact's reading does produce the `NOTIF_MARK` event:
arrives with the contact's own user id, so the tick is real and not decoration.

The `reaction` style has one slot. A bot may hold exactly one reaction per
message in a private chat — a second one is refused with `REACTIONS_TOO_MANY`
(private-chat reaction support) — and that slot is also used to mirror the
contact's MAX
reaction. So on a message the contact has reacted to, the two styles compete and
the last writer wins; `suffix`, the default, has no such conflict.
"""

from __future__ import annotations

import logging
from typing import Protocol

from bridge.config import PresenceConfig, ReadReceiptStyle
from bridge.storage import MessageMapRepository, ReadStateRepository

from .status_line import StatusLine

logger = logging.getLogger(__name__)

# Used when `read_receipt_style` is `reaction`: the bot marks its own message.
# Confirmed available in private chats (private-chat reaction support), one reaction at a time.
READ_REACTION = "👀"


class ReceiptRenderer(Protocol):
    """What the Telegram side must provide to show a tick."""

    async def append_suffix(
        self, bot_id: int, chat_id: int, message_id: int, suffix: str
    ) -> bool: ...

    async def set_reaction(
        self, bot_id: int, chat_id: int, message_id: int, emoji: str
    ) -> bool: ...


class ReadReceipts:
    def __init__(
        self,
        *,
        renderer: ReceiptRenderer,
        messages: MessageMapRepository,
        read_state: ReadStateRepository,
        config: PresenceConfig,
        status_line: StatusLine | None = None,
    ) -> None:
        self._renderer = renderer
        self._messages = messages
        self._read_state = read_state
        self._config = config
        self._status_line = status_line

    async def on_contact_read(self, bridge_name: str, *, mark: int) -> bool:
        """The contact read up to `mark`. Returns True when a tick was drawn."""
        if self._config.read_receipt_style is ReadReceiptStyle.OFF:
            return False

        # The contact read what *we* sent them, so the tick belongs on the
        # owner's own message — which a bot may react to in a private chat even
        # though it may never edit it. The cutoff keeps a message sent after the
        # mark from being ticked as read.
        last = await self._messages.last_sent_to_max(bridge_name, not_after_ms=mark)
        if last is None or last.telegram_message_id is None:
            # Nothing of ours has reached MAX yet: there is nothing to tick.
            return False

        # Repeats arrive on every reconnect and every chat sync. Without this
        # guard each one would trigger another edit of the same message.
        moved = await self._read_state.note_contact_read(
            bridge_name, mark=mark, ticked_message_id=last.telegram_message_id
        )
        if not moved:
            return False

        return await self._draw(
            bridge_name,
            last.telegram_bot_id,
            last.telegram_chat_id,
            last.telegram_message_id,
        )

    async def _draw(
        self, bridge_name: str, bot_id: int, chat_id: int, message_id: int
    ) -> bool:
        style = self._config.read_receipt_style

        if style is ReadReceiptStyle.LINE:
            # The owner's own message cannot be edited by a bot at all, so the
            # tick lives in a message of ours (see `status_line`).
            if self._status_line is None:
                return False
            await self._status_line.note_read(bridge_name, bot_id=bot_id, chat_id=chat_id)
            return True

        if style is ReadReceiptStyle.SUFFIX:
            if await self._renderer.append_suffix(
                bot_id, chat_id, message_id, self._config.read_receipt_suffix
            ):
                return True
            # A video note carries no caption, and an edit can fail for a dozen
            # other reasons. Fall through to the reaction rather than give up.
            logger.debug("suffix tick failed, falling back to a reaction")
            style = ReadReceiptStyle.REACTION

        if style is ReadReceiptStyle.REACTION:
            return await self._renderer.set_reaction(bot_id, chat_id, message_id, READ_REACTION)

        return False


class MaxReadMarker(Protocol):
    async def mark_read(self, chat_id: int, message_id: int) -> None: ...


class AutoRead:
    """Marks the MAX chat read on the owner's behalf — the contact's second tick.

    MAX takes `CHAT_MARK` (op50) as `{type: "READ_MESSAGE", chatId, messageId,
    mark}` and forwards it to the other side as `NOTIF_MARK`, which is what
    draws ✓✓ in their app. Compatibility tests confirm that the mark a session
    sends reaches the contact.

    What is hard is knowing *when*. A bot cannot know that the owner read
    anything — Bot API has no such signal — so:

    * `on_read` (default) listens to the owner's own Telegram session, which is
      told which messages the owner has actually opened. Answering is proof of
      reading too, so a reply marks under this mode as well. Without the owner
      session only that second half is live.
    * `on_reply` marks only when the owner answers.
    * `on_delivery` marks the moment a message reaches Telegram, looked at or
      not — it changes what the contact observes, so it stays opt-in.
    """

    def __init__(
        self,
        *,
        marker: MaxReadMarker,
        read_state: ReadStateRepository,
        config: PresenceConfig,
        messages: MessageMapRepository | None = None,
    ) -> None:
        self._marker = marker
        self._read_state = read_state
        self._config = config
        # Only `on_read` needs the map: Telegram names what was read by *its*
        # message id, and MAX has to be told about the MAX one behind it.
        self._messages = messages
        self._latest_incoming: dict[str, tuple[int, int]] = {}

    def note_incoming(self, bridge_name: str, *, max_chat_id: int, max_message_id: int) -> None:
        """Remember what would be marked read if the owner answers."""
        self._latest_incoming[bridge_name] = (max_chat_id, max_message_id)

    async def on_delivered_to_telegram(self, bridge_name: str) -> bool:
        from bridge.config import AutoRead as AutoReadMode

        if self._config.auto_read is not AutoReadMode.ON_DELIVERY:
            return False
        return await self._mark(bridge_name)

    async def on_owner_replied(self, bridge_name: str) -> bool:
        from bridge.config import AutoRead as AutoReadMode

        if self._config.auto_read not in (AutoReadMode.ON_REPLY, AutoReadMode.ON_READ):
            return False
        return await self._mark(bridge_name)

    async def on_owner_read(
        self, bridge_name: str, *, owner_account_id: int, owner_message_id: int
    ) -> bool:
        """The owner opened the bot chat in Telegram, up to `owner_message_id`.

        The watermark is in the **owner's** Telegram numbering — the ids their own
        client uses, which are not the ids the contact bot got back for the same
        messages. `last_read_by_owner` is what knows that; the only job here is to
        hand it the account as well as the id, because an id means nothing outside
        the account that issued it.

        The mark goes on the newest MAX message the owner has actually reached —
        never on one further down the chat, and never on a message whose
        owner-side identity is not yet known.
        """
        from bridge.config import AutoRead as AutoReadMode

        if self._config.auto_read is not AutoReadMode.ON_READ:
            return False
        if self._messages is None:
            return False

        link = await self._messages.last_read_by_owner(
            bridge_name,
            owner_account_id=owner_account_id,
            not_after_owner_id=owner_message_id,
        )
        if link is None or link.max_message_id is None:
            return False
        return await self._mark_message(
            bridge_name, max_chat_id=link.max_chat_id, max_message_id=link.max_message_id
        )

    async def mark_now(self, bridge_name: str) -> bool:
        """The `/read` command: mark it regardless of the configured trigger."""
        return await self._mark(bridge_name)

    async def _mark(self, bridge_name: str) -> bool:
        latest = self._latest_incoming.get(bridge_name)
        if latest is None:
            return False
        max_chat_id, max_message_id = latest
        return await self._mark_message(
            bridge_name, max_chat_id=max_chat_id, max_message_id=max_message_id
        )

    async def _mark_message(
        self, bridge_name: str, *, max_chat_id: int, max_message_id: int
    ) -> bool:
        """Tell MAX, then remember. Never the other way round.

        The stored watermark stops us from re-marking the same place after a
        reconnect replays the same messages, and from walking a mark backwards
        when Telegram repeats an older read event. So it is read first — a repeat
        costs nothing and asks MAX nothing — and *written* only once MAX has
        accepted the mark.

        The order matters because it used to be the other way round: the
        watermark moved, then the frame failed, and every later repeat of the
        same read event was refused as "already marked". The tick was owed and
        would never be drawn. Now a failure leaves the watermark where it was, so
        the next read event — the reconnect replay, the owner opening the chat
        again — carries it through.
        """
        marks = await self._read_state.get(bridge_name)
        if max_message_id <= marks.own_read_mark:
            return False

        try:
            await self._marker.mark_read(max_chat_id, max_message_id)
        except Exception:
            # Structure only: a chat id of ours, never a word of the message.
            # `info` rather than an alert — an unmarked message is a missing
            # tick, not a missing message, and the next read event retries it.
            logger.info(
                "could not mark MAX chat %s read; the watermark stays put",
                max_chat_id,
                exc_info=True,
            )
            return False

        await self._read_state.note_own_read(bridge_name, mark=max_message_id)
        return True
