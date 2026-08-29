"""The one place a message is actually handed to the other side.

Routing decides *what* to send and renders it; this module decides *when it
counts as sent*, and it is the only code allowed to say so.

Three rules, and each one existed as a bug first.

**A job before a send.** The delivery job is written down together with the
dedup claim, in one transaction. Before this, a claim row was written and the
send went straight out: when the send failed, the row stayed, and the replay
that MAX or Telegram helpfully performed found the row and concluded the message
had already been delivered. The message was gone and the log said "already
delivered".

**A send that returns nothing is not a success.** The adapters return the remote
message id, and several of them return `None` when they swallowed an error.
`note_delivery()` used to be called regardless, so `/status` reported a delivery
that never happened. An adapter that cannot produce the id it was supposed to
produce now raises `UnconfirmedDeliveryError` and the job goes to AMBIGUOUS.

**Inline first, worker second.** The send is attempted immediately — latency
matters in a chat — and the job is settled straight after. The worker is not the
normal path; it is what picks the job up when the inline attempt failed, or when
the process died mid-attempt. Either way the job outlives the attempt.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from bridge.max_client import AttachmentKind, MaxAttachment
from bridge.storage import Direction, OutboxRepository

logger = logging.getLogger(__name__)


class UnstorablePayloadError(Exception):
    """The job could not be written down, so no job exists for this message.

    The caller has already claimed the dedup row, so it must fall back to
    sending directly rather than returning — a message with a claim and no job
    is one that a replay will skip for ever.
    """


class UnconfirmedDeliveryError(Exception):
    """The send went out and the remote id never came back.

    Distinct from an ordinary failure on purpose. A failure can be retried; this
    cannot be retried safely, because the message may well have arrived and a
    second attempt puts a duplicate in a real person's chat. See ADR 0002.
    """


class DeferDelivery(Exception):  # noqa: N818 - an outcome, not an error
    """Not now: this job is waiting on something that is not itself.

    An owner edit or delete whose original send is still in flight raises this.
    It is *not* a failure — it must not spend the retry budget, reach FAILED, or
    raise an incident. The pipe and the worker release the job back to PENDING
    with `next_attempt_at` pushed out and `attempts` untouched, so it re-checks
    the predecessor later. `delay_ms` is how long to wait before re-checking.
    """

    def __init__(self, delay_ms: int) -> None:
        super().__init__("deferred: waiting on the predecessor send")
        self.delay_ms = delay_ms


#: Kinds of delivery job. The kind decides which sender runs and what the
#: payload holds; both directions are listed here so the worker can serve one
#: queue with one function.
KIND_MAX_TO_TG_TEXT = "max_to_tg_text"
KIND_MAX_TO_TG_MEDIA = "max_to_tg_media"
#: A message the *owner* wrote in MAX, placed in Telegram as their own over their
#: own MTProto session. A third kind rather than a flag on the other two, because
#: what settles is different: the id that comes back is in the owner account's
#: numbering, so it fills `telegram_owner_message_id` and the aliases' owner-side
#: column, never the bot-side ones.
#:
#: It shares everything else on purpose — the same queue, the same `source_key`
#: namespace (one MAX message takes one branch, so one event is always one job),
#: the same worker, the same ordering guarantee, the same AMBIGUOUS. This used to
#: be a direct send from the router with the dedup row already written, which is
#: the exact shape `test_no_media_bypass.py` exists to forbid and which cost real
#: messages: a failure left a claim with nothing behind it and the next MAX
#: replay skipped the message for ever.
KIND_MAX_TO_TG_OWNER = "max_to_tg_owner"
#: What MAX did to a message it had already sent, carried into Telegram.
#:
#: Durable for the same reason everything else here is, and it was the last pair
#: of remote effects that was not: both used to be direct Bot API calls out of a
#: MAX ingress handler, through an adapter that answered `False` for every
#: failure. Neither *creates* anything — an edit replaces a body and a delete
#: removes one — so both stay out of `CREATING_KINDS` and a repeat costs nothing.
KIND_MAX_TO_TG_EDIT = "max_to_tg_edit"
KIND_MAX_TO_TG_DELETE = "max_to_tg_delete"
KIND_TG_TO_MAX_TEXT = "tg_to_max_text"
KIND_TG_TO_MAX_MEDIA = "tg_to_max_media"
#: A contact the owner shared. Not media — it carries no file to download — but
#: durable for the same reason text is: a MAX refusal or a timeout must leave a
#: job on the queue, not a message the owner watched vanish.
KIND_TG_TO_MAX_CONTACT = "tg_to_max_contact"
#: The owner's own edits and deletes, carried into MAX as durable jobs on the
#: same queue. Each depends on the original send job (by its source_key) and
#: resolves the race against it at execution — see `_resolve_owner_edit/delete`.
KIND_TG_TO_MAX_EDIT = "tg_to_max_edit"
KIND_TG_TO_MAX_DELETE = "tg_to_max_delete"

#: The owner's reaction, on the same queue as everything else they do.
#:
#: Setting one is idempotent, which is why it used to be called straight at MAX.
#: Idempotent is not the same as accounted: a process that dies between the
#: update and the call leaves nothing behind saying the reaction was meant, and
#: MTProto updates have no durable inbox to replay it from. The job is that
#: record. It carries the update's version, and the worker refuses to apply one
#: the state has already moved past.
KIND_TG_TO_MAX_REACTION = "tg_to_max_reaction"
#: Binding the owner's own id onto a message the bridge already delivered to
#: them. A job rather than a handler call because the head of the queue it binds
#: against may not have resolved yet, and an echo must survive that wait — and a
#: restart during it — without being remembered only in memory.
KIND_OWNER_ECHO_BIND = "owner_echo_bind"
#: Removing what is left of an album in Telegram after one of its parts was
#: deleted. MAX cannot drop a single attachment — the group goes as a whole or
#: not at all — so a part deleted in Telegram takes the MAX message with it, and
#: the parts still sitting in the Telegram chat would otherwise be a group whose
#: other half no longer exists anywhere. A durable job rather than a call beside
#: the delete, because it is a remote effect and must survive a restart.
KIND_TG_ALBUM_SWEEP = "tg_album_sweep"


#: How deep into a MAX payload the sanitiser will walk before giving up. MAX
#: nests a few levels; anything deeper is not something `resolve()` reads.
_MAX_RAW_DEPTH = 8


def json_safe(value: Any, depth: int = 0) -> Any:
    """Whatever survives a round trip through SQLite, and nothing else.

    A photo's `raw` carries a thumbnail as **bytes**, and `json.dumps` refuses
    it. Live verification found that the hard way: the claim row was already
    written when the serialisation blew up, so the message ended up with a
    mapping, no job, and no way to be replayed — the exact silent loss this
    whole path exists to prevent, reintroduced by a payload nobody had checked.

    Bytes are dropped rather than encoded. `MaxMediaSources.resolve()` reads ids
    and tokens; a preview blob is dead weight in the queue and, being image
    data, is content this project keeps out of the database on purpose.
    """
    if depth > _MAX_RAW_DEPTH:
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None
    if isinstance(value, dict):
        cleaned = {
            str(key): json_safe(item, depth + 1)
            for key, item in value.items()
            if not isinstance(item, (bytes, bytearray, memoryview))
        }
        return {key: item for key, item in cleaned.items() if item is not None}
    if isinstance(value, (list, tuple, set)):
        kept = [json_safe(item, depth + 1) for item in value]
        return [item for item in kept if item is not None]
    # An object of some other kind: keep its readable shape, not the object.
    payload = getattr(value, "__dict__", None)
    if payload:
        return json_safe(payload, depth + 1)
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - a sanitiser that raises is not a sanitiser
        # `__str__` is arbitrary code and may do anything, including fail. This
        # function exists so a payload can always be written down; letting it
        # raise would defeat that at the one moment it matters.
        return None


def attachment_to_payload(attachment: MaxAttachment) -> dict[str, Any]:
    """One MAX attachment, in a form that survives a restart.

    Bytes are never stored, and neither is a download URL: MAX hands out links
    that expire, so keeping one as the only source would make a retry fail for a
    reason nobody could see. What is kept is `raw` — the original payload with
    the `videoId` / `audioId` / `fileId` / `token` that
    `MaxMediaSources.resolve()` asks MAX with — plus the chat and message ids
    held alongside it on the job. From those three a **fresh** URL is resolved on
    every attempt, which is what makes retrying an upload meaningful at all.
    """
    return {
        "kind": attachment.kind.value,
        "raw": json_safe(attachment.raw),
        "file_name": attachment.file_name,
        "size": attachment.size,
        "duration_ms": attachment.duration_ms,
        "width": attachment.width,
        "height": attachment.height,
        "title": attachment.title,
        "performer": attachment.performer,
        "url": attachment.url,
        "missed": attachment.missed,
        "call_video": attachment.call_video,
        # A contact carries no file, so `raw` is not enough to rebuild it: these
        # are the whole of what a retry re-sends, and dropping them here made a
        # requeued contact arrive as an empty card.
        "contact_name": attachment.contact_name,
        "contact_phone": attachment.contact_phone,
        "contact_vcard": attachment.contact_vcard,
        "contact_user_id": attachment.contact_user_id,
        "contact_photo_url": attachment.contact_photo_url,
    }


def attachment_from_payload(stored: dict[str, Any]) -> MaxAttachment:
    """Rebuild the attachment a job was created for."""
    return MaxAttachment(
        kind=AttachmentKind(stored["kind"]),
        raw=stored.get("raw") or {},
        file_name=stored.get("file_name"),
        size=stored.get("size"),
        duration_ms=stored.get("duration_ms"),
        width=stored.get("width"),
        height=stored.get("height"),
        title=stored.get("title"),
        performer=stored.get("performer"),
        url=stored.get("url"),
        missed=bool(stored.get("missed")),
        call_video=bool(stored.get("call_video")),
        contact_name=stored.get("contact_name"),
        contact_phone=stored.get("contact_phone"),
        contact_vcard=stored.get("contact_vcard"),
        contact_user_id=stored.get("contact_user_id"),
        contact_photo_url=stored.get("contact_photo_url"),
    )


@dataclass(frozen=True, slots=True)
class Settled:
    """What happened to one attempt, in terms the queue understands.

    `exception` is carried rather than swallowed because the caller still has to
    classify it: a MAX refusal like `chat.control` is permanent and gets
    answered to the owner in words, while a timeout is worth another go. That
    decision needs the original error, not a string.
    """

    delivered: bool
    remote_message_id: int | None = None
    ambiguous: bool = False
    #: Left PENDING to re-check later, waiting on a predecessor. Not a failure:
    #: no error, no retry spent — the caller returns and the worker resumes it.
    deferred: bool = False
    error: str | None = None
    exception: BaseException | None = None


#: Called by the sender at the last possible moment before the remote request.
#: Everything before it — resolving a URL, downloading, validating — is still
#: safely retryable; everything after it may already have been accepted.
SendingHook = Callable[[], Awaitable[None]]

Sender = Callable[[str, Direction, dict[str, Any], SendingHook], Awaitable[int | None]]


class DeliveryPipe:
    """Creates a job, attempts it, and settles it. Used inline and by the worker.

    One implementation for both so the retry path cannot drift away from the
    live path — that drift is how a queue ends up delivering something subtly
    different from what the user saw fail.
    """

    def __init__(
        self,
        *,
        outbox: OutboxRepository,
        send: Sender,
        on_delivered: Callable[[str, str, int | None], Awaitable[None]] | None = None,
    ) -> None:
        self._outbox = outbox
        self._send = send
        self._on_delivered = on_delivered

    async def submit(
        self,
        *,
        bridge_name: str,
        direction: Direction,
        kind: str,
        payload: dict[str, Any],
        source_key: str,
    ) -> tuple[int, bool]:
        """Put the job on the queue **and take it**. Idempotent per source event.

        Returns `(job_id, ours)`. `ours=False` means the job is already done, or
        already in a worker's hands, and this caller must not send: doing so put
        two copies of the same message in the contact's chat, which live
        verification demonstrated before this was a claim.
        """
        storable = self._storable(payload, bridge_name=bridge_name, kind=kind)
        return await self._outbox.claim_for_attempt(
            bridge_name=bridge_name,
            direction=direction,
            kind=kind,
            payload=storable,
            source_key=source_key,
        )

    def _storable(self, payload: dict[str, Any], *, bridge_name: str, kind: str) -> dict[str, Any]:
        """The payload as it can be written down, sanitised only if it must be."""
        storable = payload
        try:
            json.dumps(storable, ensure_ascii=False)
        except (TypeError, ValueError):
            # Sanitising the whole payload rather than only the attachments.
            # `caption_entities` comes out of raw MAX elements too, and nothing
            # guaranteed it was any tamer than the thumbnail that broke this the
            # first time.
            storable = cast(dict[str, Any], json_safe(payload))
            try:
                json.dumps(storable, ensure_ascii=False)
            except (TypeError, ValueError) as error:
                # Nothing survives. The dedup row already exists, so there is no
                # returning quietly — that is the silent loss this queue was
                # built to prevent. A job is created anyway, holding only what
                # is certainly storable, and the caller fails it with a reason
                # the owner can read. Never a send that bypasses the queue.
                logger.error(
                    "bridge %s: %s payload cannot be stored (%s)",
                    bridge_name,
                    kind,
                    error,
                )
                raise UnstorablePayloadError(str(error)) from error
            logger.warning(
                "bridge %s: %s payload needed sanitising before it could be stored",
                bridge_name,
                kind,
            )

        return storable

    async def submit_in_order(
        self,
        *,
        bridge_name: str,
        direction: Direction,
        kind: str,
        payload: dict[str, Any],
        source_key: str,
    ) -> tuple[int, bool]:
        """As `submit`, but never ahead of something older in the same direction.

        The queue always promised one order per bridge; two senders quietly broke
        it. The worker takes the oldest job, but the inline path sent the moment a
        message arrived — so a message whose first attempt had failed and gone
        back to the queue was still waiting while the next one went straight out,
        and the contact read them the wrong way round.

        The job is written down first and only then claimed, so the decision is
        made against durable state: if anything older in this direction is still
        undelivered, this returns `ours=False` and the job stays PENDING for the
        worker, which will take it in turn. The common case — an empty queue —
        still sends inline, and the job exists either way.
        """
        job_id = await self._outbox.enqueue(
            bridge_name=bridge_name,
            direction=direction,
            kind=kind,
            payload=self._storable(payload, bridge_name=bridge_name, kind=kind),
            source_key=source_key,
        )
        if await self._outbox.older_undelivered(
            bridge_name, direction=direction, before_id=job_id
        ):
            logger.debug("bridge %s: %s waits its turn behind an older send", bridge_name, kind)
            return job_id, False
        return await self._outbox.claim_for_attempt(
            bridge_name=bridge_name,
            direction=direction,
            kind=kind,
            payload=payload,
            source_key=source_key,
        )

    async def submit_unstorable(
        self,
        *,
        bridge_name: str,
        direction: Direction,
        kind: str,
        source_key: str,
        reason: str,
        reference: dict[str, Any],
    ) -> int:
        """Record a job for a message whose payload could not be written down.

        It exists so the message is *visible* rather than lost: the dedup row is
        already there, and a claim with no job is a message the next replay
        skips for ever. The payload is a bare reference — ids only, nothing that
        can fail to serialise — and the job is failed immediately, because there
        is genuinely nothing to retry from.
        """
        job_id, _ = await self._outbox.claim_for_attempt(
            bridge_name=bridge_name,
            direction=direction,
            kind=kind,
            payload={"unstorable": True, "reason": reason[:300], **reference},
            source_key=source_key,
        )
        await self._outbox.mark_failed(job_id, error=f"payload could not be stored: {reason}")
        return job_id

    async def submit_noop(
        self,
        *,
        bridge_name: str,
        direction: Direction,
        kind: str,
        source_key: str,
        reason: str,
        reference: dict[str, Any],
    ) -> int:
        """Record that this event will never be delivered, and why.

        For a message there is genuinely nothing to send — a MAX event that
        renders to no text, carries no attachment and passes nothing on. The
        dedup row is already written by the time that is known, and returning in
        silence is what left thirty rows in the live database that no replay can
        ever get past: the claim says "delivered", the queue has never heard of
        it, and nothing anywhere says the message was dropped.

        So the event is accounted rather than forgotten. The job is created,
        failed with its reason and archived in one go: archived because there is
        nothing to retry and the owner has no decision to make, and a job on
        `/failed` that cannot be retried is noise that hides the ones that can.
        The row keeps the reason, so a run of these is visible as a pattern
        instead of as an absence.

        The payload is ids only — nothing that can fail to serialise, and nothing
        of what the message said.
        """
        job_id, _ = await self._outbox.claim_for_attempt(
            bridge_name=bridge_name,
            direction=direction,
            kind=kind,
            payload={"noop": True, "reason": reason[:300], **reference},
            source_key=source_key,
        )
        await self._outbox.mark_failed(job_id, error=f"nothing to deliver: {reason}")
        await self._outbox.archive(job_id, reason=reason)
        return job_id

    async def attempt(
        self,
        *,
        job_id: int,
        bridge_name: str,
        direction: Direction,
        kind: str,
        payload: dict[str, Any],
    ) -> Settled:
        """Send once and record the outcome. Never raises for a send failure.

        The caller gets a `Settled` rather than an exception because there is
        nothing useful for it to do about a failure: the job is on the queue and
        the worker owns it from here.
        """
        # Imported here rather than at module scope: `settlement` reads this
        # module's exception types and job kinds, so the dependency runs one way
        # and this is the one call that has to look back along it.
        from bridge.routing.settlement import Verdict, settle

        marked = False

        async def sending() -> None:
            """The sender calls this immediately before the remote request.

            It used to be stamped here, before `_send` was even entered — which
            meant a process that died while *downloading* a MAX attachment came
            back as AMBIGUOUS, asking the owner to adjudicate a message that had
            never left the machine. Preparation is retryable; only the request
            itself is not.

            `marked` is the same fact `send_started_at` records, kept in memory
            because this attempt is still running: the column is what a *later*
            process reads after a crash, and this is what *this* one reads now.
            """
            nonlocal marked
            marked = True
            await self._outbox.mark_sending(job_id)

        try:
            remote_id = await self._send(kind, direction, payload, sending)
        except (Exception, asyncio.CancelledError) as error:
            # Deliberately not `BaseException`. `KeyboardInterrupt` and
            # `SystemExit` are the process *being* killed, and the row has to stay
            # INFLIGHT so lease recovery reads `send_started_at` and judges it on
            # the next start. Catching them here would settle the job as a retry
            # and erase the one fact recovery needs.
            verdict = settle(kind, error, remote_marked=marked)
            if verdict.verdict is Verdict.AMBIGUOUS:
                logger.error(
                    "bridge %s: %s went out unconfirmed — left for the owner to decide (%s)",
                    bridge_name,
                    kind,
                    verdict.detail,
                )
                await self._outbox.mark_ambiguous(job_id, error=verdict.detail)
                if isinstance(error, asyncio.CancelledError):
                    # Recorded, then let the shutdown continue: the job is
                    # terminal, so nothing is waiting on this coroutine.
                    raise
                return Settled(delivered=False, ambiguous=True, error=verdict.detail)
            if verdict.verdict is Verdict.DEFER:
                # Waiting on a predecessor, not failing. Back to PENDING with the
                # clock pushed out; attempts untouched, no error, no incident.
                await self._outbox.defer(job_id, delay_ms=verdict.delay_ms)
                return Settled(delivered=False, deferred=True)
            if verdict.verdict is Verdict.PERMANENT:
                logger.error(
                    "bridge %s: %s cannot be delivered (%s)", bridge_name, kind, verdict.detail
                )
                await self._outbox.mark_failed(job_id, error=f"permanent: {verdict.detail}")
                if isinstance(error, Exception):
                    return Settled(delivered=False, error=verdict.detail, exception=error)
                raise
            logger.warning("bridge %s: %s failed inline (%s)", bridge_name, kind, verdict.detail)
            # Deliberately left PENDING rather than marked failed: the worker
            # applies the backoff policy and decides when to give up.
            #
            # The delay comes from the verdict rather than being zero. It was
            # zero for every failure, which for a rate limit meant the worker
            # picked the job up immediately and walked straight back into the
            # limit that had just refused it — one wasted request, and one of the
            # twelve tries spent on a server that had said "not yet".
            await self._outbox.mark_retry(
                job_id,
                delay_ms=verdict.delay_ms,
                error=verdict.detail,
                costs_attempt=verdict.costs_attempt,
            )
            if isinstance(error, Exception):
                return Settled(delivered=False, error=verdict.detail, exception=error)
            raise

        await self._outbox.mark_done(job_id, remote_message_id=remote_id)
        if self._on_delivered is not None:
            await self._on_delivered(bridge_name, kind, remote_id)
        return Settled(delivered=True, remote_message_id=remote_id)
