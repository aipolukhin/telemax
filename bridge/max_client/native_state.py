"""What the native voice/circle path knows about itself, for this process.

Two questions live here, and they are the same question asked twice.

**Was the message created?** A native send has three remote steps and only the
last one creates anything. op82 hands out a slot, the POST puts bytes on a CDN
neither side calls a message, and op64 is the one frame that makes a bubble
appear in somebody's chat. So a failure is only ever ambiguous *after* op64 was
written, and everything before it is an ordinary retry. `classify_native_error`
is the other half: when the server *answers* op64 with a refusal, the message
was provably not created, and that is a different outcome again — not a retry,
not a question for the owner, but a reason to stop trying this way.

**Should we keep trying this way?** A refusal of a native attach may also drop
the MAX connection. Retrying it
twelve times per message, across four bridges, costs a session reconnect each
time — the damage is the churn, not the message. So a *confirmed* protocol
rejection trips a breaker for that kind, and every later voice or circle goes
out as a plain attachment without touching op82 at all, until the process is
restarted and somebody has looked.

The breaker is deliberately narrow. Only explicit schema refusals trip it; a
chat that refuses a message, a reply target that no longer exists, or any
error nobody has seen before keeps the behaviour it already had. A breaker that
opens on an unknown error would turn one bad chat into a dead feature.

`voice` and `circle` are tracked apart because the server validates them apart:
the AUDIO validator is the strict one, and a wave it refuses got a circle through
(`bridge/media/native_max.py` module docstring). One being broken says nothing
about the other.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .max_errors import classify_max_error

logger = logging.getLogger(__name__)

#: The two kinds that take the second media pipeline. Named here so a typo is a
#: KeyError at the seam rather than a counter nobody ever increments.
NATIVE_KINDS: tuple[str, ...] = ("voice", "circle")

#: The exact hosts op82 may hand back, per kind. An allowlist rather than a
#: pattern: the URL is chosen by the server, so a substring test
#: (`"oneme.ru" in host`) would let
#: `au.oneme.ru.somewhere-else` take a delivery of private correspondence. The
#: path is pinned too — the same host serves more than this one endpoint.
UPLOAD_ENDPOINTS: dict[str, tuple[str, str]] = {
    "voice": ("au.oneme.ru", "/uploadAudio"),
    "circle": ("vu.oneme.ru", "/uploadVideo"),
}


@dataclass(frozen=True, slots=True)
class NativeErrorVerdict:
    """What one MAX error answered to op64 means for the native path."""

    #: The server answered, so the message was provably not created. True for
    #: every `ApiError`: an error frame is an answer.
    answered: bool
    #: …and the answer was about the shape of what we sent, not about the chat.
    #: Only this trips the breaker.
    protocol_rejection: bool
    reason: str


def classify_native_error(error: BaseException) -> NativeErrorVerdict:
    """Read a PyMax `ApiError` raised by op64, in native-media terms.

    A thin reading of `classify_max_error`: the shared classifier decides what
    kind of "no" this is, and this decides what the native path does about it.
    A schema rejection is the one that also drops the connection, so it is the
    one that trips the breaker. A blocked chat is just as permanent and must not
    trip it — the attach is fine, the conversation is not, and turning voice
    messages off for every contact over one of them would be the wrong repair.

    Anything that is not an `ApiError` is not an answer at all — a transport
    failure — and the caller must treat it as unconfirmed rather than ask this.
    """
    verdict = classify_max_error(error)
    return NativeErrorVerdict(
        answered=True,
        protocol_rejection=verdict.schema_rejection,
        reason=verdict.reason,
    )


class UploaderUrlError(Exception):
    """The URL op82 handed back is not one this kind may upload to.

    Carries no URL and no query: the reason is logged and stored, and the query
    is where the `signatureToken` lives.
    """


def check_upload_url(kind: str, url: str) -> str:
    """Return the host, or raise. Called before a single byte is read or sent.

    The upload URL is chosen by the server, so it is input, not configuration.
    Everything about it is pinned to the supported transport contract: the scheme
    (a plaintext POST would expose private correspondence), the
    exact host, the default port, the endpoint path, and the absence of userinfo
    — `https://au.oneme.ru@elsewhere/` reads as the right host to a human and is
    not one.

    Nothing here is a guess about what MAX *might* also accept. If the server
    ever moves the endpoint, update this allowlist explicitly instead of
    loosening validation.
    """
    from urllib.parse import urlsplit

    expected = UPLOAD_ENDPOINTS.get(kind)
    if expected is None:
        raise UploaderUrlError(f"{kind} has no native upload endpoint")
    host, path = expected

    try:
        parts = urlsplit(url)
    except ValueError as error:
        raise UploaderUrlError(f"{kind}: unparseable upload URL") from error

    if parts.scheme != "https":
        raise UploaderUrlError(f"{kind}: upload URL is not https")
    if parts.username or parts.password:
        raise UploaderUrlError(f"{kind}: upload URL carries userinfo")
    if parts.hostname != host:
        raise UploaderUrlError(f"{kind}: upload host is {parts.hostname!r}, not {host!r}")
    try:
        port = parts.port
    except ValueError as error:  # a port that is not a number at all
        raise UploaderUrlError(f"{kind}: upload URL has an unreadable port") from error
    if port not in (None, 443):
        raise UploaderUrlError(f"{kind}: upload URL uses port {port}")
    if parts.path != path:
        raise UploaderUrlError(f"{kind}: upload path is {parts.path!r}, not {path!r}")
    return host


@dataclass(frozen=True, slots=True)
class KindStatus:
    """One kind's counters and breaker, as `/status` and health read them."""

    kind: str
    #: What the operator asked for. Distinct from the breaker on purpose: "off
    #: because I said so" and "off because the protocol moved" are different
    #: answers to the same question.
    enabled: bool
    attempts: int = 0
    successes: int = 0
    ordinary_fallbacks: int = 0
    protocol_rejections: int = 0
    uploader_drift: int = 0
    unconfirmed: int = 0
    breaker_open: bool = False
    breaker_reason: str | None = None
    breaker_opened_at: int | None = None

    @property
    def state(self) -> str:
        """One word for the whole kind, in the order that matters to a reader."""
        if not self.enabled:
            return "disabled"
        if self.breaker_open:
            return "breaker-open"
        if self.uploader_drift:
            return "uploader-drift"
        if self.successes:
            return "healthy"
        if self.ordinary_fallbacks:
            return "degraded"
        return "idle"


@dataclass
class _Counters:
    attempts: int = 0
    successes: int = 0
    ordinary_fallbacks: int = 0
    protocol_rejections: int = 0
    uploader_drift: int = 0
    unconfirmed: int = 0
    breaker_reason: str | None = None
    breaker_opened_at: int | None = None


class NativeMediaState:
    """Process-local: what native media has done, and whether it may keep going.

    One of these exists per running bridge, built by the runtime and shared by
    the MAX client that writes to it and the health service that reads it. In
    RAM on purpose — the breaker is a statement about *this* process's session,
    and a restart is exactly the event that should clear it.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        voice_enabled: bool = True,
        circle_enabled: bool = True,
    ) -> None:
        self._enabled = enabled
        self._per_kind_enabled = {"voice": voice_enabled, "circle": circle_enabled}
        self._counters: dict[str, _Counters] = {kind: _Counters() for kind in NATIVE_KINDS}
        # The wrong-uploader warning is loud and actionable, so it is said once
        # per process rather than once per voice.
        self._drift_announced = False

    # ------------------------------------------------------------------ gates

    def is_enabled(self, kind: str) -> bool:
        """What the operator configured, ignoring anything that went wrong."""
        return self._enabled and self._per_kind_enabled.get(kind, False)

    def is_open(self, kind: str) -> bool:
        """Whether the breaker has tripped for this kind."""
        counters = self._counters.get(kind)
        return counters is not None and counters.breaker_opened_at is not None

    def allows(self, kind: str) -> bool:
        """May a native send be attempted at all? False means not even op82."""
        return self.is_enabled(kind) and not self.is_open(kind)

    # --------------------------------------------------------------- counting

    def note_attempt(self, kind: str) -> None:
        self._bump(kind, "attempts")

    def note_success(self, kind: str) -> None:
        self._bump(kind, "successes")

    def note_ordinary_fallback(self, kind: str) -> None:
        """One message that went out as a plain attachment instead."""
        self._bump(kind, "ordinary_fallbacks")

    def note_unconfirmed(self, kind: str) -> None:
        """op64 was written and the outcome is unknown. Never retried."""
        self._bump(kind, "unconfirmed")

    def note_uploader_drift(self, kind: str) -> bool:
        """The server handed back an uploader we have no path for.

        Returns True the first time in this process, so the caller can say the
        long version once and the short version afterwards.
        """
        self._bump(kind, "uploader_drift")
        first = not self._drift_announced
        self._drift_announced = True
        return first

    # --------------------------------------------------------------- breaker

    def trip(self, kind: str, reason: str) -> bool:
        """A confirmed protocol rejection. Returns True if this opened it."""
        counters = self._counters[kind]
        counters.protocol_rejections += 1
        if counters.breaker_opened_at is not None:
            return False
        counters.breaker_opened_at = int(time.time() * 1000)
        counters.breaker_reason = reason[:200]
        logger.error(
            "MAX refused a native %s attach (%s). Native %s is off for this process: every "
            "%s now goes out as a plain attachment, and no further op82 is sent. A refused "
            "attach also drops the MAX connection, which is what this stops repeating. "
            "Upgrade Telemax or disable this native media kind before restarting.",
            kind,
            reason[:200],
            kind,
            kind,
        )
        return True

    # -------------------------------------------------------------- reporting

    def status(self, kind: str) -> KindStatus:
        counters = self._counters[kind]
        return KindStatus(
            kind=kind,
            enabled=self.is_enabled(kind),
            attempts=counters.attempts,
            successes=counters.successes,
            ordinary_fallbacks=counters.ordinary_fallbacks,
            protocol_rejections=counters.protocol_rejections,
            uploader_drift=counters.uploader_drift,
            unconfirmed=counters.unconfirmed,
            breaker_open=counters.breaker_opened_at is not None,
            breaker_reason=counters.breaker_reason,
            breaker_opened_at=counters.breaker_opened_at,
        )

    def snapshot(self) -> tuple[KindStatus, ...]:
        return tuple(self.status(kind) for kind in NATIVE_KINDS)

    # ------------------------------------------------------------------ inner

    def _bump(self, kind: str, field_name: str) -> None:
        counters = self._counters.get(kind)
        if counters is None:  # a kind that never takes this path
            return
        setattr(counters, field_name, getattr(counters, field_name) + 1)


#: Used where a client is built without one (onboarding, tests). Never shared
#: between two `NativeMediaState` owners: the runtime builds its own.
def default_state() -> NativeMediaState:
    return NativeMediaState()


__all__ = [
    "NATIVE_KINDS",
    "KindStatus",
    "NativeErrorVerdict",
    "NativeMediaState",
    "classify_native_error",
    "default_state",
]
