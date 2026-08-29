"""What one Telegram failure means — decided once, for every caller.

Three questions get asked about the same exception in three different places,
and before this module each place answered on its own:

* **the adapter** asks "may I send this same content a different way?";
* **the settlement policy** asks "could the message already exist?";
* **the worker** asks "is it worth trying again, and when?".

Answering the first one with a bare `except Exception` is what put a second
album in a chat: a timeout after Telegram had accepted the group was read as
"the group was refused" and the parts went out again, one by one. So the
distinction this module draws is not between kinds of error but between kinds of
*evidence*:

* **the server answered** — whatever it said, this request created nothing, and
  another representation of the same content is a fresh, honest attempt;
* **the server did not answer** — the request may have been carried out, and
  nothing may be sent again in any form.

`TelegramNetworkError` is where the second case hides. aiogram raises it for a
`TimeoutError` *and* for any `ClientError`, and its timeout is the whole request
including the body upload — so a 40 MB album accepted on the 61st second comes
back looking exactly like an album that never left.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)

MS_PER_SECOND = 1000


class TelegramTransportUnavailableError(Exception):
    """There was no client to send through, so nothing was sent.

    Raised instead of answering `None`: a bot that has been removed from the
    registry is a transport problem, and reporting it as "Telegram returned no
    message id" turned a message that never left the machine into a question for
    the owner about whether it had arrived.
    """


class TelegramOutcome(StrEnum):
    """What the evidence supports, in the terms the three callers need."""

    #: No answer came back. The request may have been carried out. Nothing may be
    #: re-sent, in this form or any other.
    NO_ANSWER = "no-answer"
    #: The server answered with its own wait. Nothing was created.
    RATE_LIMITED = "rate-limited"
    #: The server refused *this representation* of the content — the file is not
    #: acceptable as a photo, a sticker, a video note. The same bytes may still
    #: be acceptable as something else, and nothing was created.
    REFUSED_REPRESENTATION = "refused-representation"
    #: The server refused the *group*, not its members. Nothing was created, and
    #: the parts may be sent as individual messages.
    REFUSED_GROUP = "refused-group"
    #: The server says the target of a mutation is not there, or is already in
    #: the state asked for. For an edit or a delete that is success, not failure.
    NO_OP = "no-op"
    #: The message has no text to edit, or no caption to edit — the other half of
    #: it is where the body lives. An edit creates nothing, so switching method is
    #: free; it is gated on this answer so a timeout can never do it.
    REFUSED_EDIT_FORM = "refused-edit-form"
    #: The server refused, and no other representation would help.
    REFUSED = "refused"
    #: Proven not sent, before the request: no client, no session.
    NOT_SENT = "not-sent"


@dataclass(frozen=True, slots=True)
class TelegramVerdict:
    outcome: TelegramOutcome
    detail: str
    #: Only for `RATE_LIMITED`: what the server said to wait, in milliseconds.
    retry_after_ms: int = 0

    @property
    def answered(self) -> bool:
        """Did the server say something? Then this request created nothing."""
        return self.outcome not in (TelegramOutcome.NO_ANSWER,)


#: Descriptions that mean "not acceptable *as this*". Measured against the Bot
#: API's own wording and its MTProto error names, which it passes through
#: verbatim for the media errors. Matching is on a lowercased description, so a
#: marker is a substring and never a regular expression: a pattern here would be
#: read by nobody and would quietly widen over time.
#:
#: Deliberately narrow. Anything not in this list is permanent, because the cost
#: of guessing wrong in this direction is a second message in somebody's chat
#: and the cost of guessing wrong the other way is one job on `/failed` with a
#: readable reason.
_REPRESENTATION_MARKERS: tuple[str, ...] = (
    # Photo: dimensions, extension, or the server's own processing of it.
    "photo_invalid_dimensions",
    "photo_ext_invalid",
    "photo_content_type_invalid",
    "photo_save_file_invalid",
    "photo_crop_size_small",
    "image_process_failed",
    "photo dimensions",
    # Sticker: Telegram validates the container, not just the name.
    "sticker_png_dimensions",
    "sticker_png_nopng",
    "sticker_tgs_notgzip",
    "sticker_video_nowebm",
    "sticker_file_invalid",
    "sticker_emoji_invalid",
    "wrong sticker file",
    "sticker must be",
    # Video / video note.
    "video_file_invalid",
    "video_content_type_invalid",
    "videonote_size_invalid",
    # Generic "this is not that".
    "wrong type of file",
    "wrong file type",
    "unsupported file type",
    "type of file mismatch",
    "wrong padding",
    # The avatar card, which Telegram fetches from a URL itself.
    "failed to get http url content",
    "wrong file identifier/http url specified",
    "wrong remote file identifier",
    "webpage_curl_failed",
    "webpage_media_empty",
)

#: Descriptions that mean "the group is wrong, its members are not". The parts
#: are still sendable one at a time — see `RegistryMediaSender.send_album` for
#: the product policy that decides whether they are.
_GROUP_MARKERS: tuple[str, ...] = (
    "media_group_invalid",
    "group send failed",
    "in the same media group",
    "media group",
)

#: Descriptions that say the body of this message lives in its other half.
_EDIT_FORM_MARKERS: tuple[str, ...] = (
    "there is no text in the message to edit",
    "there is no caption in the message to edit",
    "message_no_edit_time",
)

#: Descriptions that make a mutation a no-op rather than a failure. An edit that
#: changes nothing and a delete of something already gone have both arrived at
#: the state they were asked for.
_NO_OP_MARKERS: tuple[str, ...] = (
    "message is not modified",
    "message to edit not found",
    "message to delete not found",
    "message_id_invalid",
    "message identifier is not specified",
)


def _matches(description: str, markers: tuple[str, ...]) -> str | None:
    for marker in markers:
        if marker in description:
            return marker
    return None


def classify_telegram(error: BaseException) -> TelegramVerdict:
    """Read one Telegram failure. The only place that reads one.

    Order is by strength of evidence and not by exception hierarchy:

    1. **We never called.** `TelegramTransportUnavailableError` — no client.
    2. **The server named a wait.** `TelegramRetryAfter` carries `retry_after`,
       and a 429 is issued *instead of* doing the work.
    3. **The server refused.** `TelegramBadRequest` and the permission errors are
       answers: whatever they say, this request created nothing. Which kind of
       refusal it is decides whether another representation may be tried.
    4. **Everything else is silence.** Timeouts, resets, 5xx, cancellation and
       anything unrecognised. A 5xx is deliberately here rather than under (3):
       Telegram answering "internal error" does not say the request was not
       carried out, and this project has one rule for "may have landed".
    """
    from telethon.errors import FloodWaitError  # type: ignore[import-untyped]

    if isinstance(error, TelegramTransportUnavailableError):
        return TelegramVerdict(TelegramOutcome.NOT_SENT, str(error))

    if isinstance(error, TelegramRetryAfter):
        return TelegramVerdict(
            TelegramOutcome.RATE_LIMITED,
            f"rate limited for {error.retry_after}s",
            retry_after_ms=int(error.retry_after) * MS_PER_SECOND,
        )

    if isinstance(error, FloodWaitError):
        # The owner's session runs into the same wall, and says so in its own
        # words. Telethon sleeps through a short one itself
        # (`flood_sleep_threshold`, 60s by default) and raises anything longer —
        # so what reaches here is by definition a wait worth writing down rather
        # than holding a worker on. The number is kept exactly: an hour-long wait
        # rounded down to a guess is how a queue walks into the limit again.
        return TelegramVerdict(
            TelegramOutcome.RATE_LIMITED,
            f"the owner's session is rate limited for {error.seconds}s",
            retry_after_ms=int(error.seconds) * MS_PER_SECOND,
        )

    if isinstance(error, TelegramBadRequest | TelegramNotFound):
        description = str(error).lower()
        marker = _matches(description, _NO_OP_MARKERS)
        if marker is not None:
            return TelegramVerdict(TelegramOutcome.NO_OP, f"target is already there: {marker}")
        marker = _matches(description, _EDIT_FORM_MARKERS)
        if marker is not None:
            return TelegramVerdict(
                TelegramOutcome.REFUSED_EDIT_FORM, f"the body is in the other half: {marker}"
            )
        marker = _matches(description, _GROUP_MARKERS)
        if marker is not None:
            return TelegramVerdict(TelegramOutcome.REFUSED_GROUP, f"group refused: {marker}")
        marker = _matches(description, _REPRESENTATION_MARKERS)
        if marker is not None:
            return TelegramVerdict(
                TelegramOutcome.REFUSED_REPRESENTATION, f"representation refused: {marker}"
            )
        # An unrecognised bad request is permanent and gets no fallback. A chat
        # that cannot be written into, an entity that does not parse and a reply
        # to a message that is gone all live here, and all of them would refuse
        # a second representation in exactly the same way.
        return TelegramVerdict(TelegramOutcome.REFUSED, f"Telegram refused it: {error}")

    if isinstance(
        error,
        TelegramForbiddenError | TelegramUnauthorizedError | TelegramConflictError
        | TelegramEntityTooLarge,
    ):
        return TelegramVerdict(TelegramOutcome.REFUSED, f"Telegram refused it: {error}")

    if isinstance(error, TelegramNetworkError | TelegramServerError):
        return TelegramVerdict(TelegramOutcome.NO_ANSWER, f"{type(error).__name__}: {error}")

    return TelegramVerdict(TelegramOutcome.NO_ANSWER, f"{type(error).__name__}: {error}")


def refused_representation(error: BaseException) -> bool:
    """May the same content be offered to Telegram in a different form?

    True only when the server answered and its answer was about the *form*. The
    one question `RegistryMediaSender` asks before every fallback it makes, and
    the reason there is no `except Exception` left in it.
    """
    return classify_telegram(error).outcome is TelegramOutcome.REFUSED_REPRESENTATION


def refused_group(error: BaseException) -> bool:
    """Did Telegram refuse the album itself, leaving its parts sendable?"""
    return classify_telegram(error).outcome is TelegramOutcome.REFUSED_GROUP


def is_no_op(error: BaseException) -> bool:
    """Is this mutation already in the state it asked for?"""
    return classify_telegram(error).outcome is TelegramOutcome.NO_OP


__all__ = [
    "TelegramOutcome",
    "TelegramTransportUnavailableError",
    "TelegramVerdict",
    "classify_telegram",
    "is_no_op",
    "refused_group",
    "refused_representation",
]
