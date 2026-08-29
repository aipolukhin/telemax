"""What one failed attempt means — the same answer for both senders.

A job can be attempted in two places. The router sends inline, straight after
claiming the job, so the owner's message goes out now rather than after a poll;
the worker sends everything the inline path did not finish. They call the same
`send_job`, and until this module they disagreed about what its failures meant.

The disagreement was not cosmetic. Crash recovery — `reclaim_expired_leases` —
reads `send_started_at` and correctly turns "died after the remote call started"
into AMBIGUOUS. The *live* failure paths never read it: any exception became
`mark_retry`, and the worker then sent the message again. So a process that
**died** was handled more carefully than one that got a timeout, and a planned
`systemctl stop` was handled worse than `kill -9` — the hard kill has no chance
to overwrite the mark, and the clean one does.

The rule is not "anything after the mark is ambiguous". That would turn a chat
that refused us into a question for the owner, and there is nothing to ask: the
server answered. Proof wins over the mark, and the mark is what decides only
where there is no proof.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from aiogram.exceptions import TelegramAPIError

from bridge.max_client import MaxClientError, MaxUnconfirmedSendError
from bridge.max_client.max_errors import classify_max_error
from bridge.routing.delivery import (
    KIND_MAX_TO_TG_MEDIA,
    KIND_MAX_TO_TG_OWNER,
    KIND_MAX_TO_TG_TEXT,
    KIND_TG_TO_MAX_CONTACT,
    KIND_TG_TO_MAX_MEDIA,
    KIND_TG_TO_MAX_TEXT,
    DeferDelivery,
    UnconfirmedDeliveryError,
)
from bridge.storage import MessageMapRepository
from bridge.telegram.errors import (
    TelegramOutcome,
    TelegramTransportUnavailableError,
    classify_telegram,
)

logger = logging.getLogger(__name__)

#: Job kinds whose remote call *creates* a message somebody receives. Named once,
#: here, so the question "would a retry duplicate this?" has one answer per kind
#: rather than a string comparison in each place that needs it.
#:
#: **Both directions belong here, and only one of them used to.** The MAX→TG
#: kinds were left out on the grounds that Telegram "has its own semantics", and
#: what that produced was a system stricter about a process that *died* than
#: about one that got a timeout: `reclaim_expired_leases` reads `send_started_at`
#: and answers AMBIGUOUS for any kind, while a live 60-second aiohttp timeout —
#: which covers the body upload, so a 40 MB album accepted on the 61st second
#: looks identical to one that never left — answered RETRY and the worker sent
#: the message again. One MAX event, two messages in the owner's chat, proven on
#: a temp database before this line changed.
#:
#: Editing, deleting and binding an echo are absent on purpose and stay absent:
#: they replace or record state, so a second attempt costs nothing and asking the
#: owner about one would be noise.
CREATING_KINDS: frozenset[str] = frozenset(
    {
        KIND_TG_TO_MAX_TEXT,
        KIND_TG_TO_MAX_CONTACT,
        KIND_TG_TO_MAX_MEDIA,
        KIND_MAX_TO_TG_TEXT,
        KIND_MAX_TO_TG_MEDIA,
        KIND_MAX_TO_TG_OWNER,
    }
)


class Verdict(StrEnum):
    """What to do with the job, decided once for both senders."""

    #: The remote call may have landed. Terminal until the owner says otherwise.
    AMBIGUOUS = "ambiguous"
    #: Proven not to have landed and proven not to be worth trying again.
    PERMANENT = "permanent"
    #: Not a failure — waiting on something older.
    DEFER = "defer"
    #: Try again. The caller applies its own backoff and attempt budget.
    RETRY = "retry"


#: The shortest a rate-limited job may wait, whatever the server names. A
#: `retry_after` of zero would otherwise put the job straight back in front of the
#: limit that produced it, which is a tight loop by another name.
MIN_RATE_LIMIT_MS = 1_000


@dataclass(frozen=True, slots=True)
class Settlement:
    verdict: Verdict
    detail: str
    #: How long before the next attempt. Zero means "the caller's own backoff",
    #: which is every case except a server that named its own wait.
    delay_ms: int = 0
    #: Whether this failure spends one of the job's twelve tries.
    #:
    #: False for a rate limit, and only for a rate limit. `retry_after` is the
    #: server saying "not yet", not "no": counting it as a failure meant a busy
    #: minute could walk a perfectly deliverable message all the way to FAILED,
    #: and the owner would be asked to decide about a message Telegram had never
    #: refused.
    costs_attempt: bool = True

    @property
    def ambiguous(self) -> bool:
        return self.verdict is Verdict.AMBIGUOUS


def is_creating(kind: str) -> bool:
    return kind in CREATING_KINDS


def settle(kind: str, error: BaseException, *, remote_marked: bool) -> Settlement:
    """Read one failure. `remote_marked` is whether `sending()` had fired.

    Precedence is by strength of evidence, not by exception hierarchy:

    1. **Someone already decided it is unknown.** `UnconfirmedDeliveryError` and
       `MaxUnconfirmedSendError` are the send layer saying it wrote the frame and
       got nothing usable back. Nothing below can improve on that.
    2. **Proven undeliverable.** `PermanentDeliveryError` — the payload cannot be
       delivered by anyone, ever.
    3. **Not a failure at all.** `DeferDelivery` waits on a predecessor and costs
       no attempt.
    4. **Proven never sent.** `MaxClientError` comes from the session check that
       runs before the frame is built, so it is a not-sent even for a creating
       kind that had already marked its boundary. This clause is why the mark is
       not consulted first: the mark says "we were about to", and this says "and
       then we did not".
    5. **Cancelled.** A shutdown cannot prove where it landed. For a creating
       kind past the mark that is a question; for an idempotent mutation, or
       before the mark, it is the retry it always was.
    6. **Anything else.** Past the mark on a creating kind, unknown means the
       message may exist, so it is a question. Otherwise it keeps the retry
       classification it had.

    An `ApiError` is handled between 4 and 5: it is an *answer*, so the message
    was not created and it must never become AMBIGUOUS however the mark reads.
    Whether to keep trying is then `classify_max_error`'s call — a blocked chat
    and an unparseable frame both refuse identically for ever, and retrying
    either twelve times is a queue that never drains.

    Telegram's own failures are read the same way and by the same rule, through
    `classify_telegram`: an answer means nothing was created, silence past the
    mark on a creating kind means it may have been. The two sides of the bridge
    now settle on one contract rather than two.
    """
    from bridge.retry.worker import PermanentDeliveryError
    from bridge.routing.owner_voice import (
        OwnerTransportUnavailableError,
        PartialOwnerAlbumError,
    )

    if isinstance(error, UnconfirmedDeliveryError | MaxUnconfirmedSendError):
        return Settlement(Verdict.AMBIGUOUS, str(error))
    if isinstance(error, PartialOwnerAlbumError):
        # Part of a group is in somebody's chat and part is not, and nothing here
        # can read the chat back to find out which. A retry would place what did
        # arrive a second time.
        return Settlement(Verdict.AMBIGUOUS, f"the album was only half placed: {error}")
    if isinstance(error, PermanentDeliveryError):
        return Settlement(Verdict.PERMANENT, str(error))
    if isinstance(error, DeferDelivery):
        return Settlement(Verdict.DEFER, "waiting on a predecessor", error.delay_ms)
    if isinstance(error, MaxClientError):
        return Settlement(Verdict.RETRY, f"{type(error).__name__}: {error}")
    if isinstance(error, OwnerTransportUnavailableError | TelegramTransportUnavailableError):
        # There was no client to send through, so nothing was built and nothing
        # left. Proven not sent even past the mark, which the media hook fires
        # before the transport is resolved — without this clause an owner session
        # that had simply gone away became a question about a message that never
        # existed.
        return Settlement(Verdict.RETRY, f"transport away: {error}")

    from telethon.errors import FloodWaitError  # type: ignore[import-untyped]

    if isinstance(error, TelegramAPIError | FloodWaitError):
        # Both transports' own vocabularies, read by one classifier. A flood wait
        # from the owner's session means exactly what a `retry_after` from the Bot
        # API means, and answering them differently is how one of them ends up
        # spending a retry budget the other does not.
        return _settle_telegram(kind, error, remote_marked=remote_marked)

    from pymax.exceptions import ApiError

    if isinstance(error, ApiError):
        # The server *answered*. Whatever it said, it did not silently create the
        # message, so the mark is irrelevant and a question for the owner would be
        # a question about nothing.
        #
        # Whether to keep trying is a different question, and the one place that
        # reads a MAX error answers it. A chat that will not take our messages,
        # and a frame the server will not parse, both refuse identically for ever
        # — retrying either twelve times is a queue that never drains, which is
        # exactly what happened while this classification was inline-only.
        verdict = classify_max_error(error)
        if verdict.permanent:
            return Settlement(Verdict.PERMANENT, f"MAX refused it: {verdict.reason}")
        return Settlement(Verdict.RETRY, f"MAX refused it: {verdict.reason}")

    unknown = f"{type(error).__name__}: {error}"
    if isinstance(error, asyncio.CancelledError):
        if is_creating(kind) and remote_marked:
            return Settlement(
                Verdict.AMBIGUOUS,
                "cancelled after the remote call began; the message may have been sent",
            )
        return Settlement(Verdict.RETRY, "cancelled")

    if is_creating(kind) and remote_marked:
        return Settlement(
            Verdict.AMBIGUOUS,
            f"the remote call had begun and failed in a way that proves nothing ({unknown})",
        )
    return Settlement(Verdict.RETRY, unknown)


def _settle_telegram(kind: str, error: BaseException, *, remote_marked: bool) -> Settlement:
    """One Telegram failure, in the queue's terms.

    The whole of the difference is whether Telegram said anything. It answered:
    this request created nothing, so a question for the owner would be a question
    about a message that does not exist, and the only thing left to decide is
    whether trying again could ever help. It said nothing: past the mark on a
    creating kind the message may be sitting in somebody's chat, and a retry is
    how they read it twice.
    """
    verdict = classify_telegram(error)
    if verdict.outcome is TelegramOutcome.RATE_LIMITED:
        # An answer, and one that names its own wait. Nothing was created — a 429
        # is issued *instead of* doing the work — so the only questions are how
        # long to wait and whether waiting counts against the job. It does not.
        return Settlement(
            Verdict.RETRY,
            verdict.detail,
            max(verdict.retry_after_ms, MIN_RATE_LIMIT_MS),
            costs_attempt=False,
        )
    if verdict.outcome is TelegramOutcome.NOT_SENT:
        return Settlement(Verdict.RETRY, verdict.detail)
    if verdict.outcome is TelegramOutcome.NO_ANSWER:
        if is_creating(kind) and remote_marked:
            return Settlement(
                Verdict.AMBIGUOUS,
                f"the remote call had begun and Telegram never answered ({verdict.detail})",
            )
        return Settlement(Verdict.RETRY, verdict.detail)
    # Every remaining outcome is Telegram having refused: a representation it
    # will not take, a group it will not build, a target that is already where it
    # was asked to be, or a flat no. By the time one reaches this function the
    # layer that could have done something else about it already has, so there is
    # nothing left to try and nothing to ask the owner about.
    return Settlement(Verdict.PERMANENT, verdict.detail)


async def settle_max_delivery_mapping(
    messages: MessageMapRepository | None,
    payload: dict[str, Any],
    max_message_id: int,
) -> int:
    """Give the mapping row the id MAX assigned, then hand the id on.

    Called from `send_job`, which is the one function both senders share — the
    attach used to live in the router, so only messages the *inline* path
    delivered ever got their MAX id. Anything the worker carried (a retry, a
    message written while MAX was briefly down) left `max_message_id` NULL: 33 of
    202 rows in production, at least eight of them with a DONE job holding a real
    remote id.

    That is not a lost message — the delivery happened — but it is a lost half of
    its identity. A reply to such a message resolves to nothing and arrives in MAX
    without its quote, and edit and delete survive only through a defensive
    fallback to `outbox.remote_message_id`.

    An album needs nothing extra here: every part already aliases this one
    canonical row, so filling it in settles all of them at once.
    """
    if messages is None:
        return max_message_id
    link_id = payload.get("link_id")
    if link_id is None:
        return max_message_id
    if not await messages.attach_max_message(int(link_id), max_message_id):
        # Two remote messages claiming one mapping row. Loud rather than
        # overwritten: whichever id loses becomes a message no reply, edit or
        # delete can resolve against again.
        logger.error(
            "mapping row %s already names a different MAX message than %s; "
            "the mapping was left as it was and needs a look",
            link_id,
            max_message_id,
        )
    return max_message_id


__all__ = [
    "CREATING_KINDS",
    "Settlement",
    "Verdict",
    "is_creating",
    "settle",
    "settle_max_delivery_mapping",
]
