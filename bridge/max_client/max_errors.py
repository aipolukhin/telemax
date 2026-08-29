"""What a MAX error means, decided in one place.

Three mechanisms used to answer this and none of them saw the whole picture.
`refusals.classify` knew that a blocked chat refuses for ever — and was called
from exactly two places, both inline, so the worker retried the same refusal
twelve times behind them. `classify_native_error` knew which answers mean our
hand-built attach is wrong — and only for voice and circles. The worker's own
`classify` had no MAX branch at all, so every `ApiError` fell into "unknown,
retryable".

The classification is deliberately by **what the answer says**, never by whether
the server then closed the connection. Both facts are real and they do not line
up: `proto.payload` closes the socket and `error.message.like.unknown.like` does
not, but the second is just as final for the request that caused it. Reading the
socket instead of the code would make the same error mean different things
depending on how quickly we noticed.

Everything unrecognised stays retryable. A wrong "permanent" silently drops the
owner's message; a wrong "retryable" costs a queue slot and a log line.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MaxErrorClass(StrEnum):
    """What kind of "no" this is."""

    #: The dialog will not take our messages, and will not change its mind: a
    #: service account, a channel, somebody who blocked the owner.
    PERMISSION = "permission"
    #: The server refused the *shape* of what we sent — the frame or the attach.
    #: Retrying the identical thing gets the identical answer.
    SCHEMA = "schema"
    #: A field we sent carries a value the server does not know. The request is
    #: refused; the request *shape* is fine, so a different value may work.
    VALUE = "value"
    #: Nobody has seen this one. Bounded retries, no conclusions.
    UNKNOWN = "unknown"


#: `(marker, class, what the owner is told)`. Markers are matched inside the
#: error's own text rather than against it whole: MAX's wording is not a contract
#: and has changed spelling before, while these fragments have not.
#:
#: Order matters — the first match wins, and the schema markers are more specific
#: than the permission ones.
_MARKERS: tuple[tuple[str, MaxErrorClass, str | None], ...] = (
    # --- schema / attachment: the shape of what we sent -----------------------
    # Compatibility tests: five different field combinations of an AUDIO attach all
    # answered this, so it is about the attach and not about a missing key.
    ("errors.process.attachment.video.not.supported", MaxErrorClass.SCHEMA, None),
    # Compatibility tests: a contact attach with no vCard.
    ("missing info for contact attachment", MaxErrorClass.SCHEMA, None),
    # The AUDIO validator's answer for a waveform it will not take. No code of
    # its own that we have seen, so the message is what identifies it.
    ("invalid media wave", MaxErrorClass.SCHEMA, None),
    # The frame does not match the schema at all. Live: this one closes the
    # socket on the next frame, which is why native media has a breaker.
    ("proto.payload", MaxErrorClass.SCHEMA, None),
    # --- known value rejections ----------------------------------------------
    # Compatibility tests: an emoji outside the server's set. The socket stays up,
    # and a different emoji works — so this is about the value, not the frame.
    ("error.message.like.unknown.like", MaxErrorClass.VALUE, None),
    # --- permission / chat ----------------------------------------------------
    ("restriction to input", MaxErrorClass.PERMISSION,
     "В этот диалог MAX писать нельзя — это служебный аккаунт."),
    ("chat.control", MaxErrorClass.PERMISSION, "MAX не разрешает писать в этот диалог."),
    ("not allowed", MaxErrorClass.PERMISSION, "MAX не разрешает писать в этот диалог."),
    ("blocked", MaxErrorClass.PERMISSION, "Контакт закрыл переписку в MAX."),
    ("no access", MaxErrorClass.PERMISSION, "Нет доступа к этому диалогу в MAX."),
)

#: What the owner is told when the bridge will try again.
RETRYABLE_MESSAGE = "MAX не принял сообщение. Попробую ещё раз."

#: …and when nobody should keep trying, but there is no better wording.
GENERIC_PERMANENT_MESSAGE = "MAX отказался принять это сообщение."


@dataclass(frozen=True, slots=True)
class MaxErrorVerdict:
    """One MAX refusal, read."""

    kind: MaxErrorClass
    #: The error code, or the marker that identified it. Never the full message:
    #: a refusal's text can carry a chat title or a person's name, and this
    #: travels into logs, `/status` and incidents.
    reason: str
    #: What to tell the owner, when there is something worth telling them.
    owner_message: str | None = None

    @property
    def permanent(self) -> bool:
        """Whether trying the identical thing again can only fail identically."""
        return self.kind in (MaxErrorClass.PERMISSION, MaxErrorClass.SCHEMA)

    @property
    def schema_rejection(self) -> bool:
        """The server refused the shape of what we built, not the request."""
        return self.kind is MaxErrorClass.SCHEMA


def classify_max_error(error: BaseException) -> MaxErrorVerdict:
    """Read one MAX error. Unrecognised wording is retryable, deliberately."""
    code = str(getattr(error, "error", "") or "")
    parts = [
        str(getattr(error, attribute, "") or "")
        for attribute in ("message", "localized_message", "title")
    ]
    haystack = " ".join([code, *parts, str(error)]).lower()

    for marker, kind, owner_message in _MARKERS:
        if marker in haystack:
            return MaxErrorVerdict(
                kind=kind,
                reason=code or marker,
                owner_message=owner_message
                or (GENERIC_PERMANENT_MESSAGE if kind is not MaxErrorClass.UNKNOWN else None),
            )
    return MaxErrorVerdict(
        kind=MaxErrorClass.UNKNOWN,
        reason=code or "unclassified MAX error",
        owner_message=RETRYABLE_MESSAGE,
    )


__all__ = [
    "GENERIC_PERMANENT_MESSAGE",
    "RETRYABLE_MESSAGE",
    "MaxErrorClass",
    "MaxErrorVerdict",
    "classify_max_error",
]
