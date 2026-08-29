"""AU-2 F1/F2 — what a native send does when op64 goes wrong.

The native path has three remote steps and only the last one creates a message.
Everything here is about telling the three outcomes of that last step apart,
because collapsing them is how a person receives a voice message twice:

* **op64 answered with a refusal** — the message was not created. Degrade this
  one to a plain attachment, and stop trying this way: the same refusal also
  drops the MAX connection, and twelve retries across four bridges is a
  reconnect storm rather than a delivery problem. That is the whole of
  `a5112a3`/`973bb9e`, which turned the feature off by hand, twice.
* **op64 was written and nothing came back** — the message may be in the chat.
  Never retried, never followed by a plain copy: the owner decides (ADR 0002).
* **anything before op64** — op82, the upload, a file we could not read. The
  message does not exist yet, so these stay ordinary retries.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pymax.exceptions import ApiError

from bridge.max_client.client import (
    MaxClient,
    MaxClientError,
    MaxUnconfirmedSendError,
)
from bridge.max_client.native_state import NativeMediaState, classify_native_error
from bridge.max_client.opcodes import Opcode

AUDIO_SLOT = {"info": {"url": "https://au.oneme.ru/uploadAudio?signatureToken=x"}, "token": "T"}
VIDEO_SLOT = {"info": {"url": "https://vu.oneme.ru/uploadVideo?signatureToken=x"}, "token": "T"}


class Rig:
    """A `MaxClient` whose socket and upload POST are replaced, nothing else.

    `send_media`, `_send_native_media` and `_invoke_native_send` are the real
    ones — the point is to drive the production decision tree, not a copy of it.
    """

    def __init__(
        self,
        tmp_path: Path,
        *,
        slot: Any = AUDIO_SLOT,
        op82_error: BaseException | None = None,
        post_error: BaseException | None = None,
        op64_error: BaseException | None = None,
        op64_answer: Any | None = None,
        fallback_error: BaseException | None = None,
        state: NativeMediaState | None = None,
    ) -> None:
        self.state = state or NativeMediaState()
        self.client = MaxClient(
            phone="1", session_dir=tmp_path, session_name="s", native_media=self.state
        )
        self.invokes: list[int] = []
        self.posts: list[str] = []
        self.fallbacks: list[dict[str, Any]] = []
        self._slot = slot
        self._op82_error = op82_error
        self._post_error = post_error
        self._op64_error = op64_error
        self._op64_answer = op64_answer if op64_answer is not None else {"message": {"id": 777}}
        self._fallback_error = fallback_error

        async def fake_invoke(opcode: Any, payload: dict[str, Any], **_: Any) -> Any:
            self.invokes.append(int(opcode))
            if int(opcode) == int(Opcode.MEDIA_UPLOAD):
                if self._op82_error is not None:
                    raise self._op82_error
                return self._slot
            if self._op64_error is not None:
                raise self._op64_error
            return self._op64_answer

        async def fake_post(url: str, data: bytes, filename: str) -> None:
            self.posts.append(url)
            if self._post_error is not None:
                raise self._post_error

        outer = self

        class FakePyMax:
            async def send_message(self, **kwargs: Any) -> Any:
                outer.fallbacks.append(kwargs)
                if outer._fallback_error is not None:
                    raise outer._fallback_error
                return type("M", (), {"id": 9})()

        self.client._invoke = fake_invoke  # type: ignore[method-assign]
        self.client._post_media = fake_post  # type: ignore[method-assign]
        self.client._client = FakePyMax()
        self.client._ready.set()

    def send(self, kind: str = "voice", *, name: str | None = None) -> int | None:
        source = self.client._session_dir / (name or f"{kind}.bin")
        source.write_bytes(b"payload")
        return asyncio.run(self.client.send_media(1, [(kind, source, source.name)]))

    @property
    def op82_calls(self) -> int:
        return self.invokes.count(int(Opcode.MEDIA_UPLOAD))

    @property
    def op64_calls(self) -> int:
        return self.invokes.count(int(Opcode.MSG_SEND))


@pytest.fixture(autouse=True)
def _readable_media(monkeypatch: Any) -> None:
    """A duration and a waveform, so every test here fails for its own reason."""
    monkeypatch.setattr(
        "bridge.media.native_max.probe_media", lambda path, kind="voice": (1000, bytes(80))
    )


def _rejection(message: str = "Invalid media wave", code: str | None = None) -> ApiError:
    return ApiError(opcode=64, error=code, message=message)


# ------------------------------------------------------------------ unconfirmed


def test_an_op64_timeout_is_unconfirmed_and_never_falls_back(tmp_path: Path) -> None:
    """The window F1 is about: the server accepted the frame and answered slower
    than our 20 s. Retrying it — or sending a plain copy alongside — is how the
    contact gets the voice twice."""
    rig = Rig(tmp_path, op64_error=TimeoutError("op64"))

    with pytest.raises(MaxUnconfirmedSendError):
        rig.send()

    assert rig.op64_calls == 1  # tried once, never again
    assert rig.fallbacks == []  # and no plain copy behind it
    assert rig.state.status("voice").unconfirmed == 1


def test_a_disconnect_after_the_frame_may_have_been_written_is_unconfirmed(
    tmp_path: Path,
) -> None:
    """A socket that dies *during* op64 cannot prove the frame did not land."""
    rig = Rig(tmp_path, op64_error=ConnectionResetError("peer went away"))

    with pytest.raises(MaxUnconfirmedSendError):
        rig.send()
    assert rig.fallbacks == []


def test_an_answer_without_a_message_id_is_unconfirmed(tmp_path: Path) -> None:
    """A well-formed answer carrying no id says nothing about whether the message
    was created, so it is the same question as silence."""
    rig = Rig(tmp_path, op64_answer={"ok": True})

    with pytest.raises(MaxUnconfirmedSendError):
        rig.send()
    assert rig.fallbacks == []


def test_an_unreadable_message_id_is_unconfirmed(tmp_path: Path) -> None:
    rig = Rig(tmp_path, op64_answer={"message": {"id": "not-a-number"}})

    with pytest.raises(MaxUnconfirmedSendError):
        rig.send()


def test_a_session_that_is_already_gone_is_a_retry_not_a_question(tmp_path: Path) -> None:
    """`_invoke` checks the session before it writes anything, so this one is a
    *provable* not-sent — the only failure at the op64 boundary that is."""
    rig = Rig(tmp_path, op64_error=MaxClientError("MAX client is not connected"))

    with pytest.raises(MaxClientError):
        rig.send()
    assert rig.fallbacks == []  # still no plain copy: the job retries whole


def test_an_unconfirmed_send_becomes_ambiguous_in_the_delivery_layer(tmp_path: Path) -> None:
    """The other half of the contract: `send_media` raising this is what puts the
    job in AMBIGUOUS instead of on the retry queue."""
    from bridge.routing.delivery import UnconfirmedDeliveryError
    from bridge.service.runtime import _carry_media_into_max

    rig = Rig(tmp_path, op64_error=TimeoutError("op64"))
    source = tmp_path / "voice.ogg"
    source.write_bytes(b"payload")

    with pytest.raises(UnconfirmedDeliveryError):
        asyncio.run(
            _carry_media_into_max(
                rig.client, {"max_chat_id": 1}, [("voice", source, "voice.ogg")]
            )
        )


# --------------------------------------------------------------------- breaker


def test_a_known_audio_rejection_opens_the_voice_breaker_and_degrades_once(
    tmp_path: Path,
) -> None:
    """`Invalid media wave` is an *answer*: the message was not created, so this
    one goes out plainly. It also drops the connection, so it is the last one
    that tries."""
    rig = Rig(tmp_path, op64_error=_rejection("Invalid media wave"))

    assert rig.send() == 9  # degraded, delivered
    assert len(rig.fallbacks) == 1

    status = rig.state.status("voice")
    assert status.breaker_open
    assert status.protocol_rejections == 1
    assert status.breaker_opened_at is not None
    assert status.state == "breaker-open"


def test_a_known_video_rejection_opens_the_circle_breaker(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        slot=VIDEO_SLOT,
        op64_error=_rejection(
            "attachment not supported", code="errors.process.attachment.video.not.supported"
        ),
    )

    assert rig.send("circle") == 9
    assert rig.state.status("circle").breaker_open


def test_a_frame_the_server_will_not_parse_opens_the_breaker(tmp_path: Path) -> None:
    """`proto.payload` means the frame does not match the schema, and that is the
    refusal that takes the socket down (the compatibility contract)."""
    rig = Rig(tmp_path, op64_error=_rejection("Expected number at 20", code="proto.payload"))

    assert rig.send() == 9
    assert rig.state.status("voice").breaker_open


def test_a_broken_voice_does_not_disable_circles(tmp_path: Path) -> None:
    """The AUDIO validator is the strict one — a wave it refuses got a circle
    through. One being broken says nothing about the other."""
    state = NativeMediaState()
    Rig(tmp_path, op64_error=_rejection("Invalid media wave"), state=state).send()

    assert state.is_open("voice")
    assert not state.is_open("circle")
    assert state.allows("circle")


def test_an_unknown_api_error_does_not_open_the_breaker(tmp_path: Path) -> None:
    """A chat that refuses, a reply target that is gone, a code nobody has seen:
    the worker's own classification decides, and the feature stays on. A breaker
    that opens on anything would turn one difficult chat into a dead feature."""
    rig = Rig(tmp_path, op64_error=ApiError(opcode=64, error="chat.blocked", message="no"))

    with pytest.raises(ApiError):
        rig.send()

    assert not rig.state.is_open("voice")
    assert rig.state.status("voice").protocol_rejections == 0
    assert rig.fallbacks == []  # unchanged behaviour: the job retries whole


def test_a_fallback_that_fails_on_the_dropped_socket_stays_retryable(tmp_path: Path) -> None:
    """The rejection took the connection with it, so the plain send fails too.
    The job is owed, not lost — and the breaker means the next attempt does not
    repeat op82/POST/op64 to learn the same thing again."""
    rig = Rig(
        tmp_path,
        op64_error=_rejection("Invalid media wave"),
        fallback_error=MaxClientError("MAX client is not connected"),
    )

    with pytest.raises(MaxClientError):
        rig.send()
    assert rig.state.is_open("voice")


def test_a_retry_with_the_breaker_open_never_reaches_op82(tmp_path: Path) -> None:
    """What the breaker is for: the second message costs no reconnect at all."""
    state = NativeMediaState()
    first = Rig(tmp_path, op64_error=_rejection("Invalid media wave"), state=state)
    first.send()

    second = Rig(tmp_path, state=state)
    assert second.send() == 9
    assert second.invokes == []  # not even the upload slot was asked for
    assert len(second.fallbacks) == 1
    assert type(second.fallbacks[0]["attachments"][0]).__name__ == "File"


def test_a_circle_still_takes_the_native_path_while_voice_is_broken(tmp_path: Path) -> None:
    state = NativeMediaState()
    Rig(tmp_path, op64_error=_rejection("Invalid media wave"), state=state).send()

    circle = Rig(tmp_path, slot=VIDEO_SLOT, state=state)
    assert circle.send("circle") == 777
    assert circle.op82_calls == 1


def test_the_operator_switch_reads_differently_from_a_tripped_breaker(tmp_path: Path) -> None:
    """Two ways for a kind to be off, and `/status` must not blur them."""
    off = NativeMediaState(voice_enabled=False)
    assert off.status("voice").state == "disabled"
    assert not off.allows("voice")

    tripped = NativeMediaState()
    tripped.trip("voice", "Invalid media wave")
    assert tripped.status("voice").state == "breaker-open"
    assert tripped.is_enabled("voice")  # the operator never asked for this


def test_a_disabled_kind_never_reaches_op82(tmp_path: Path) -> None:
    rig = Rig(tmp_path, state=NativeMediaState(voice_enabled=False))

    assert rig.send() == 9
    assert rig.invokes == []


# ------------------------------------------------------- everything before op64


def test_an_op82_timeout_stays_retryable(tmp_path: Path) -> None:
    """No slot, no upload, no message. Nothing to be ambiguous about."""
    rig = Rig(tmp_path, op82_error=TimeoutError("op82"))

    with pytest.raises(TimeoutError):
        rig.send()
    assert rig.posts == []
    assert rig.fallbacks == []
    assert not rig.state.is_open("voice")


def test_an_upload_timeout_stays_retryable(tmp_path: Path) -> None:
    """The bytes may be on the CDN, but a CDN object nothing references is not a
    message. This retries the whole chain, which is correct and costs an upload."""
    rig = Rig(tmp_path, post_error=TimeoutError("upload"))

    with pytest.raises(TimeoutError):
        rig.send()
    assert rig.op64_calls == 0
    assert rig.fallbacks == []
    assert not rig.state.is_open("voice")


def test_an_upload_the_cdn_refuses_degrades_without_touching_the_breaker(
    tmp_path: Path,
) -> None:
    """A 415 arrives after the whole body was sent — so the docstring's old claim
    that every `MaxMediaError` predates op64 was wrong. The fallback is safe all
    the same, and for the reason that actually holds: without op64 no message was
    created."""
    from bridge.max_client.client import MaxMediaError

    rig = Rig(tmp_path, post_error=MaxMediaError("media upload refused (415): x"))

    assert rig.send() == 9
    assert rig.posts == ["https://au.oneme.ru/uploadAudio?signatureToken=x"]
    assert rig.op64_calls == 0
    assert not rig.state.is_open("voice")
    assert rig.state.status("voice").ordinary_fallbacks == 1


# ------------------------------------------------------------------ classifier


def test_the_classifier_names_only_measured_refusals() -> None:
    assert classify_native_error(_rejection("Invalid media wave")).protocol_rejection
    assert classify_native_error(
        _rejection("nope", code="errors.process.attachment.video.not.supported")
    ).protocol_rejection
    assert classify_native_error(_rejection("bad", code="proto.payload")).protocol_rejection
    assert not classify_native_error(
        ApiError(opcode=64, error="chat.blocked", message="blocked")
    ).protocol_rejection
    assert not classify_native_error(
        ApiError(opcode=64, error="error.message.like.unknown.like", message="unknown like")
    ).protocol_rejection


def test_the_classifier_reason_carries_no_message_text() -> None:
    """A refusal's message can hold a chat title or a person's name; the reason is
    stored in health and logged, so it is the code alone."""
    verdict = classify_native_error(
        ApiError(opcode=64, error="chat.blocked", message="Аня blocked this bot")
    )
    assert verdict.reason == "chat.blocked"


# ---------------------------------------------------------------- happy path


def test_a_confirmed_native_send_counts_as_a_success(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    assert rig.send() == 777
    status = rig.state.status("voice")
    assert (status.attempts, status.successes, status.ordinary_fallbacks) == (1, 1, 0)
    assert status.state == "healthy"
