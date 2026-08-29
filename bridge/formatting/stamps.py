"""The time a message was written in MAX, rendered short.

Telegram stamps a message with the moment the bot delivered it. While the bridge
is up those two are within a second of each other, so the stamp is redundant —
but after downtime the backfill arrives in one burst and every recovered message
claims to be from now. The same happens to anything the owner wrote from the MAX
app while the bridge was down.

So the stamp is the MAX time, and it is kept short on purpose: `HH:MM` for
today, `DD.MM HH:MM` for anything older. A full date on every line of a live
conversation is noise, and the point here is the exception, not the rule.
"""

from __future__ import annotations

from datetime import datetime, tzinfo

from bridge.config import TimestampStyle

#: MAX sends timestamps in milliseconds, like the rest of the protocol.
MS_PER_SECOND = 1000


#: Shown after the body of a message MAX reports as edited.


def format_clock(
    timestamp_ms: int | None,
    *,
    now: datetime | None = None,
    tz: tzinfo | None = None,
) -> str:
    """`14:12`, or `28/07 23:41` when it was not today.

    Used where the date would be noise — an edit that just happened, a tick on
    a message from a minute ago — unlike the stamp on a message itself, which
    always carries its day because a backfill lands under today's header.
    """
    if not timestamp_ms:
        return ""
    try:
        moment = datetime.fromtimestamp(timestamp_ms / MS_PER_SECOND, tz=tz)
    except (OverflowError, OSError, ValueError):
        return ""

    reference = now or datetime.now(tz=tz)
    if reference.tzinfo is None and moment.tzinfo is not None:
        reference = reference.replace(tzinfo=moment.tzinfo)

    if moment.date() == reference.date():
        return f"{moment:%H:%M}"
    if moment.year == reference.year:
        return f"{moment:%d/%m %H:%M}"
    return f"{moment:%d/%m/%Y %H:%M}"


#: A message younger than this was delivered as it was written, so Telegram's
#: own timestamp is already correct and a stamp is pure noise.
FRESH_WINDOW_SECONDS = 120


EDIT_MARK = "изм."


def format_edit_mark(
    edited_at_ms: int | None,
    style: TimestampStyle,
    *,
    now: datetime | None = None,
    tz: tzinfo | None = None,
) -> str:
    """` (изм. 12:40)` — when the text changed, not when it was written.

    Telegram shows its own
    "edited" label off the `edit_date` it sets, and the bridge has no business
    repeating it — except that Telegram also sets `edit_hide` on a *bot's* edit
    of a plain-text message, which tells every client to draw the message as
    though it had never changed. Bot text edits may therefore need an explicit
    marker while captions and owner-side edits do not.

    So a contact's edit of a text message is the one case where Telegram
    deliberately says nothing, and the one case where this still does.

    It also carries the *time*, which Telegram's label never does, and it is the
    only signal at all on a first delivery of a message MAX had already edited —
    there is no Telegram edit there to label.
    """
    if style is TimestampStyle.OFF or not edited_at_ms:
        return ""
    stamp = format_clock(edited_at_ms, now=now, tz=tz)
    return f" ({EDIT_MARK} {stamp})" if stamp else ""


def format_stamp(
    timestamp_ms: int | None,
    style: TimestampStyle,
    *,
    now: datetime | None = None,
    tz: tzinfo | None = None,
    fresh_within: float = FRESH_WINDOW_SECONDS,
) -> str:
    """`[29/07 12:34] ` — or an empty string when there is nothing to show.

    Nothing to show covers the common case: a message that arrived while the
    bridge was up. Telegram already stamps it with the right minute, so a second
    time next to it says nothing. The stamp exists for the other case — a
    backfill after downtime, which Telegram files under *today* no matter when
    the conversation actually happened — and there the date has to travel with
    the message itself.
    """
    if style is TimestampStyle.OFF or not timestamp_ms:
        return ""

    try:
        moment = datetime.fromtimestamp(timestamp_ms / MS_PER_SECOND, tz=tz)
    except (OverflowError, OSError, ValueError):
        # A clock that far off is not worth a broken delivery.
        return ""

    reference = now or datetime.now(tz=tz)
    if reference.tzinfo is None and moment.tzinfo is not None:
        reference = reference.replace(tzinfo=moment.tzinfo)

    if style is not TimestampStyle.FULL and fresh_within > 0:
        age = (reference - moment).total_seconds()
        if -fresh_within <= age <= fresh_within:
            return ""

    if style is TimestampStyle.FULL or moment.year != reference.year:
        return f"[{moment:%d/%m/%Y %H:%M}] "
    return f"[{moment:%d/%m %H:%M}] "
