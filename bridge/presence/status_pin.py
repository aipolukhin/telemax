"""A pinned line at the top of the chat saying where the contact is.

MAX reports presence twice over: it pushes an event when a contact moves, and it
answers a query (`CONTACT_PRESENCE`) with the last-seen time of everybody in the
address book. Telegram, for its part, lets a bot pin its own message in a
private chat and edit it in place afterwards — both verified against the live
API — so the two fit together into one line that never scrolls away.

Three constraints shape the implementation.

**`status` first, `seen` only when it means anything.** The state lives in
`status`: 1 is online, 2 and 3 are the two buckets MAX shows when the exact
last-seen time is not ours to see, 0 or a missing field is offline. Whenever
`status` is set the server fills `seen` with its own clock — so an online
contact and a contact who hides the time both arrive as "seen a moment ago",
which is why reading freshness alone could never say "В сети".

**A bot may edit its own message for 48 hours only.** After that the edit is
refused, so the line is re-posted and re-pinned, and the old one removed.

**Presence is chatty.** Contacts move constantly and every edit costs rate
limit, so the line is only touched when the *rendered text* changes and never
more often than the refresh interval.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, tzinfo
from enum import IntEnum
from typing import Protocol

from bridge.config import PresenceConfig
from bridge.max_client import PresenceUpdate

logger = logging.getLogger(__name__)


class PresenceStatus(IntEnum):
    """The codes MAX puts in `presence.status`, named as its own client names them."""

    OFFLINE = 0
    ONLINE = 1
    WAS_RECENTLY = 2
    WAS_LONG_AGO = 3


#: The wording MAX itself uses, so the line reads like the app rather than like
#: a bridge. Every string and every threshold below is the web client's own.
ONLINE_TEXT = "В сети"
RECENTLY_TEXT = "Был(-а) недавно"
LONG_AGO_TEXT = "Был(-а) давно"
JUST_NOW_TEXT = "Только что"
MINUTES_TEXT = "{count} мин назад"
HOURS_TEXT = "{count} ч назад"
YESTERDAY_TEXT = "Был(-а) вчера в {time}"
DATE_TEXT = "Был(-а) {date}"

#: MAX counts `seen` in seconds. Anything inside this window reads as "just now".
JUST_NOW_SECONDS = 60
MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR
#: Past this the app stops saying "вчера" and gives the date instead.
YESTERDAY_SECONDS = 36 * HOUR

#: Russian abbreviated month names, the ones `Intl.DateTimeFormat("ru")` produces
#: for `{day: "numeric", month: "short"}` — the format the app asks for. Spelling
#: them out beats depending on a system locale being installed.
MONTHS_SHORT = (
    "янв.", "февр.", "мар.", "апр.", "мая", "июн.",
    "июл.", "авг.", "сент.", "окт.", "нояб.", "дек.",
)


class PinRenderer(Protocol):
    """What the Telegram side must provide for a pinned line."""

    async def send_status(self, bot_id: int, chat_id: int, text: str) -> int | None: ...

    async def edit_status(self, bot_id: int, chat_id: int, message_id: int, text: str) -> bool: ...

    async def delete_status(self, bot_id: int, chat_id: int, message_id: int) -> bool: ...

    async def pin_status(self, bot_id: int, chat_id: int, message_id: int) -> bool: ...


class PinStore(Protocol):
    """Where the id of the pinned message survives a restart."""

    async def pinned_status(self, bridge_name: str) -> tuple[int | None, str | None]: ...

    async def set_pinned_status(
        self, bridge_name: str, *, message_id: int | None, text: str | None
    ) -> None: ...


def render(
    presence: PresenceUpdate, *, now: float | None = None, tz: tzinfo | None = None
) -> str:
    """What MAX would write under the contact's name, in MAX's own words.

    `status` decides the state; only an offline contact has a `seen` worth
    reading, and then the ladder is the app's: "Только что", "5 мин назад",
    "3 ч назад", "Был(-а) вчера в 21:40", "Был(-а) 10 июл.".
    """
    status = presence.status
    if status == PresenceStatus.ONLINE:
        return ONLINE_TEXT
    if status == PresenceStatus.WAS_RECENTLY:
        return RECENTLY_TEXT
    if status == PresenceStatus.WAS_LONG_AGO:
        return LONG_AGO_TEXT
    if status not in (None, PresenceStatus.OFFLINE):
        # A code we have never seen. The app treats it as "недавно" rather than
        # guessing a time, and so does this.
        logger.debug("unknown presence status %r for user %s", status, presence.user_id)
        return RECENTLY_TEXT

    if not presence.seen:
        # No time and no state. MAX's own fallback for a contact it knows nothing
        # about is "Был(-а) давно"; inventing a duration would be worse.
        return LONG_AGO_TEXT

    moment = float(now if now is not None else time.time())
    elapsed = moment - presence.seen

    if elapsed < JUST_NOW_SECONDS:
        return JUST_NOW_TEXT
    if elapsed < HOUR:
        return MINUTES_TEXT.format(count=int(elapsed // MINUTE))

    try:
        seen_at = datetime.fromtimestamp(presence.seen, tz=tz)
        today = datetime.fromtimestamp(moment, tz=tz)
    except (OverflowError, OSError, ValueError):
        return LONG_AGO_TEXT

    # Hours only while it is still the same day for the owner: past midnight the
    # app switches to "вчера", which says more than "9 ч назад" ever could.
    if elapsed < DAY and seen_at.date() == today.date():
        return HOURS_TEXT.format(count=int(elapsed // HOUR))
    if elapsed < YESTERDAY_SECONDS:
        return YESTERDAY_TEXT.format(time=f"{seen_at:%H:%M}")

    date = f"{seen_at.day} {MONTHS_SHORT[seen_at.month - 1]}"
    if seen_at.year != today.year:
        date = f"{date} {seen_at.year}"
    return DATE_TEXT.format(date=date)


def _crosses_online(before: str | None, after: str) -> bool:
    """Is this the line arriving at or leaving «В сети»?

    The throttle exists for a ladder that ticks over on its own, and skipping one
    of those costs nothing — a minute later it says the same thing a minute
    later. Skipping a state change costs the truth: MAX pushes presence when a
    contact connects and when they disconnect and at no point in between, so a
    dropped edit leaves the line claiming «В сети» until they come back.
    """
    return (before == ONLINE_TEXT) != (after == ONLINE_TEXT)


class PinnedStatus:
    """Keeps one pinned presence line per bridge."""

    def __init__(
        self,
        *,
        renderer: PinRenderer,
        store: PinStore,
        config: PresenceConfig,
        timezone: tzinfo | None = None,
    ) -> None:
        self._renderer = renderer
        self._store = store
        self._config = config
        self._timezone = timezone
        self._last_write: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return self._config.pin_contact_status

    async def update(
        self,
        bridge_name: str,
        presence: PresenceUpdate,
        *,
        bot_id: int,
        chat_id: int,
        force: bool = False,
    ) -> bool:
        """Refresh the line. Returns True when Telegram was actually touched."""
        if not self.enabled:
            return False

        text = render(presence, tz=self._timezone)
        message_id, current = await self._store.pinned_status(bridge_name)

        if text == current and not force:
            return False

        now = time.time()
        last = self._last_write.get(bridge_name, 0.0)
        if not force and not _crosses_online(current, text):
            if now - last < self._config.pin_refresh_seconds:
                # A contact flicking between screens would otherwise cost an edit
                # a second. The next real change picks the new text up.
                return False

        if message_id is not None and await self._renderer.edit_status(
            bot_id, chat_id, message_id, text
        ):
            self._last_write[bridge_name] = now
            await self._store.set_pinned_status(bridge_name, message_id=message_id, text=text)
            return True

        # No line yet, or one too old to edit — a bot may only edit its own
        # message for 48 hours.
        return await self._repost(bridge_name, text, bot_id=bot_id, chat_id=chat_id,
                                  previous=message_id)

    async def _repost(
        self,
        bridge_name: str,
        text: str,
        *,
        bot_id: int,
        chat_id: int,
        previous: int | None,
    ) -> bool:
        new_id = await self._renderer.send_status(bot_id, chat_id, text)
        if new_id is None:
            return False

        await self._renderer.pin_status(bot_id, chat_id, new_id)
        if previous is not None:
            await self._renderer.delete_status(bot_id, chat_id, previous)

        self._last_write[bridge_name] = time.time()
        await self._store.set_pinned_status(bridge_name, message_id=new_id, text=text)
        return True
