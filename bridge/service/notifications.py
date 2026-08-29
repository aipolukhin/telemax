"""The guardian is quiet, and exactly one message may interrupt the owner.

Telemax used to narrate. A bad afternoon produced, as separate messages in the
guardian chat:

    🟡 16 сообщений не отправлено
    🟡 2 моста не созданы
    ⚠️ Очередь выросла: 27 сообщений ждут отправки
    ⚠️ Сообщение ждёт отправки 16 мин
    ✅ Доставка восстановлена

Every line was true. Together they were why the owner stopped reading the chat,
and — worse — why the guardian's *anchor*, the one message that carries the
buttons, kept being pushed up out of reach by things nobody had asked about.

The previous increment collapsed that cascade into one message per *problem*,
edited in place. Right shape, wrong volume: eighty-two ambiguous sends were
still a message, two stuck provisioning attempts were still a message, and a
recovery still drew a green line for a problem the owner had never been told
about. A quiet guardian is not one that says fewer things about everything. It
is one that says nothing at all unless the owner has to stop what they are
doing.

So the rule is written down once, in `critical()`:

    the guardian may interrupt the owner only while Telemax cannot write down
    what it is told, or MAX has been gone long enough that this is an outage
    rather than a reconnect.

Everything else — failed sends, ambiguous sends, provisioning that stalled, a
queue that grew, a queue that stopped moving, a bridge that did not come up, a
reconnect, a recovery, a six-hourly reminder — is *state*. State lives on
HOME → Проблемы, which is read when the owner opens the bot rather than pushed
at them at three in the morning. Detection does not move: every incident is
still raised, still deduplicated, still recorded durably, and still counted into
the home badge. What changed is that raising one is no longer the same act as
saying it out loud.

When the guardian does interrupt, it interrupts once. One message, one button,
edited while the condition lasts and edited again — never re-sent — when it is
over. There is no second push for a second category, because a second push is
how the cascade came back last time.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .health import MAX_OFFLINE_ALERT_SECONDS

logger = logging.getLogger(__name__)

__all__ = [
    "ATTENTION_TEXT",
    "PROBLEMS_CALLBACK",
    "PUSH_KEY",
    "RESOLVED_TEXT",
    "RETIRED_TEXT",
    "SUSTAINED_OUTAGE_MS",
    "NotificationCentre",
    "critical",
]


#: How long MAX has to be gone before its absence is an outage rather than a
#: reconnect. The same number the `max-offline` incident is raised on — one
#: threshold read twice, not a second threshold — and it is what keeps the
#: ordinary reconnects, which last seconds and happen most days, from ever
#: reaching the chat at all.
SUSTAINED_OUTAGE_MS = MAX_OFFLINE_ALERT_SECONDS * 1000

#: The one thing the guardian ever says on its own initiative.
#:
#: Deliberately without a count, a category or a component name. A number in a
#: push is a number that goes stale between the send and the reading, and it
#: invites the very thing this replaces: a second push when the number moves. It
#: says that something wants a decision and offers the one place where every
#: such decision is actually made.
ATTENTION_TEXT = "⚠️ Telemax требует внимания\n\nЕсть проблемы, требующие вашего решения."

#: And what that same message becomes when it is over. Compact, green, and with
#: no button, because there is nothing left to open from here.
RESOLVED_TEXT = "✅ Telemax снова работает"

#: What replaces a notification left over from the version that kept one message
#: per problem. Edited, never sent. Without it an upgrade strands «🟡 16
#: сообщений не отправлено» in the chat for good: nothing will ever edit it
#: again, so it would sit there claiming a number that stopped being true.
RETIRED_TEXT = "ℹ️ Это сообщение больше не обновляется.\n\nСостояние Telemax — в меню бота."

#: Where the push's one button goes. Named by value rather than imported: this
#: module is about delivery, and the screens import in the other direction.
PROBLEMS_CALLBACK = "onb:problems"

#: What the push's button says.
PROBLEMS_BUTTON = "Открыть проблемы"

#: The single entry the push occupies in the guardian's state file. A name
#: rather than an incident key, because there is exactly one push and it is not
#: about any particular incident.
PUSH_KEY = "attention"


def critical(facts: Any) -> bool:
    """May the guardian interrupt the owner right now? Never raises.

    Two conditions, and the list is meant to be hard to grow.

    **Storage.** A bridge that cannot commit a write is not degraded, it is
    losing what it is told, and the owner's own actions in Telegram are
    suspended until it can. Nothing shown on HOME is trustworthy while this is
    true, which is what makes it the catastrophic case rather than a bad one:
    the owner cannot find this out by looking, because looking is what stopped
    working.

    **A sustained MAX outage.** Not a reconnect. The incident behind it is only
    raised after ten unbroken minutes, and the same threshold is applied again
    here against the facts as they are at send time — so an alert that sat in a
    retrying queue right through the recovery cannot push after the fact.

    Everything else is deliberately absent. A failed send, an ambiguous send, a
    provisioning attempt that stalled, a bridge that did not start: each is a
    decision the owner makes from HOME → Проблемы whenever they next look, and
    eighty-two of them are still one look.
    """
    if facts is None:
        return False
    if not bool(getattr(facts, "database_healthy", True)):
        return True
    offline = int(getattr(facts, "max_offline_ms", None) or 0)
    return (
        not bool(getattr(facts, "max_connected", True)) and offline >= SUSTAINED_OUTAGE_MS
    )


class NotificationCentre:
    """Drains the alert queue and, almost always, says nothing.

    Sits exactly where it did: handed one claimed alert at a time by
    `AlertDispatcher`, raising on a transport failure so the queue's claiming,
    retrying and giving-up behaviour is untouched. What it decides is no longer
    "which message in the chat does this text belong in" but the much smaller
    question "does this justify interrupting anybody".

    Built once per process and kept, so the one-time retirement of the previous
    version's messages happens once rather than on every alert.
    """

    def __init__(
        self,
        *,
        send: Callable[[str, Any], Awaitable[int | None]],
        edit: Callable[[int, str, Any], Awaitable[bool]],
        read: Callable[[], dict[str, int]],
        write: Callable[[dict[str, int]], None],
        facts: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self._send = send
        self._edit = edit
        self._read = read
        self._write = write
        #: None where there is nothing to read the state from. Such a centre is
        #: permanently silent, which is the safe direction: a push decided from
        #: no facts is a guess.
        self._facts = facts
        self._retired = False

    async def publish(self, alert: dict[str, Any]) -> None:
        """One queued alert. Raises on a transport failure, as the queue expects.

        The alert's own key and severity are not read, and that is the point.
        Which incident happened to be drained first says nothing useful — two of
        them routinely describe the same second — so the decision is made from
        the state as it is now. A recovery row that arrives while the outage is
        still on cannot close anything, and a raise that arrives after it ended
        cannot open anything.
        """
        await self._retire_legacy()

        facts = await self._facts_now()
        if facts is None:
            # The state could not be read. Pushing on a guess is bad; closing a
            # push on a guess is worse. The incident is already recorded and the
            # next pass reads the facts again a minute later.
            return

        message_id = self._read().get(PUSH_KEY)

        if critical(facts):
            markup = self._problems_markup()
            if message_id is not None and await self._edit(message_id, ATTENTION_TEXT, markup):
                # Worse, different, or simply still true: the push already says
                # the only thing it will ever say. Saying it again is the
                # cascade, so the edit is the whole of the update.
                return
            # No push yet, or one Telegram will not let us edit any more — over
            # forty-eight hours old, or deleted by hand. An outage that outlives
            # the edit window is exactly the moment silence is worst.
            sent = await self._send(ATTENTION_TEXT, markup)
            if sent is not None:
                self._remember(PUSH_KEY, sent)
                logger.info("the guardian pushed one attention message")
            return

        if message_id is None:
            # The ordinary case, and the whole of this increment: nothing was
            # pushed, so there is nothing to take back. Whatever this alert was
            # about is on HOME → Проблемы when the owner next opens the bot.
            return

        # There was a push and the condition behind it has passed. Its own
        # message goes green in place; a *new* green message is never sent,
        # which is what «✅ Доставка восстановлена» arriving at 3am used to be.
        await self._edit(message_id, RESOLVED_TEXT, None)
        self._forget(PUSH_KEY)

    async def _retire_legacy(self) -> None:
        """Edit away the per-problem messages the previous version left behind.

        Only ever edits, never sends, and only ever touches ids this centre
        itself wrote down. Once per process: the map is emptied of everything
        that is not the push, so a second pass has nothing to find.
        """
        if self._retired:
            return
        self._retired = True
        remembered = dict(self._read())
        legacy = [key for key in remembered if key != PUSH_KEY]
        if not legacy:
            return
        for key in legacy:
            message_id = remembered.pop(key)
            # A refused edit is a message that is too old or already gone, and
            # either way there is nothing further to do about it.
            await self._edit(message_id, RETIRED_TEXT, None)
        self._write(remembered)
        logger.info("retired %s notification message(s) from the previous design", len(legacy))

    def _problems_markup(self) -> Any:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=PROBLEMS_BUTTON, callback_data=PROBLEMS_CALLBACK)]
            ]
        )

    async def _facts_now(self) -> Any:
        """The state as it is at send time, or None when it cannot be read."""
        if self._facts is None:
            return None
        try:
            return await self._facts()
        except Exception:  # a notification is not worth a crash
            logger.debug("could not read the facts for a notification", exc_info=True)
            return None

    def _remember(self, key: str, message_id: int) -> None:
        remembered = dict(self._read())
        remembered[key] = message_id
        self._write(remembered)

    def _forget(self, key: str) -> None:
        remembered = dict(self._read())
        if remembered.pop(key, None) is not None:
            self._write(remembered)
