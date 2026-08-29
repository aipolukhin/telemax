"""AU-3 G1/G7 — every message-creating MAX send answers the same way.

Five operations put a bubble in somebody's chat: text, contact, sticker, the
ordinary media fallback, and native media. Before this they had five separate
implementations of "call, read the id, decide what a failure meant", and the
copies had drifted far enough to disagree about the same server answer — an
unreadable id came back as `None` from a contact (which the delivery layer reads
as "unknown, ask the owner") and as `ValueError` from a sticker (which it reads
as "retry", and a retried sticker is a second sticker).

The table below is the point of the file: one row per fault, one column per
operation, and the assertion is that the *whole row* agrees. A future divergence
fails here rather than in somebody's chat.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pymax.exceptions import ApiError

from bridge.max_client.client import (
    MaxClient,
    MaxClientError,
    MaxNativeRejectedError,
    MaxUnconfirmedSendError,
)
from bridge.max_client.native_state import NativeMediaState

AUDIO_SLOT = {"info": {"url": "https://au.oneme.ru/uploadAudio?signatureToken=x"}, "token": "T"}


class Rig:
    """A `MaxClient` with the socket replaced and nothing else.

    `send_text`, `send_contact`, `send_sticker`, `send_media` and
    `_send_native_media` are the production ones — the point is to drive the real
    decision tree, not a copy of it.
    """

    def __init__(
        self,
        tmp_path: Path,
        *,
        fault: BaseException | None = None,
        answer: Any = None,
        state: NativeMediaState | None = None,
        pymax_fault: BaseException | str | None = "same",
    ) -> None:
        self.state = state or NativeMediaState()
        self.client = MaxClient(
            phone="1", session_dir=tmp_path, session_name="s", native_media=self.state
        )
        self.calls: list[str] = []
        self._fault = fault
        # The plain-attachment path is a *different* send, and MAX can refuse the
        # hand-built attach while accepting an ordinary file — that is the whole
        # reason the fallback exists. Defaults to the same fault so the shared
        # rows above stay honest; the rejection test overrides it.
        self._pymax_fault = fault if pymax_fault == "same" else pymax_fault
        self._answer = answer if answer is not None else {"message": {"id": 777}}

        async def fake_invoke(opcode: Any, payload: dict[str, Any], **_: Any) -> Any:
            self.calls.append(f"op{int(opcode)}")
            if int(opcode) == 82:  # the upload slot: not a creating boundary
                return AUDIO_SLOT
            if self._fault is not None:
                raise self._fault
            return self._answer

        async def fake_post(url: str, data: bytes, filename: str) -> None:
            self.calls.append("upload")

        outer = self

        class FakePyMax:
            async def send_message(self, **kw: Any) -> Any:
                outer.calls.append("pymax.send_message")
                if outer._pymax_fault is not None:
                    raise outer._pymax_fault
                answer = outer._answer
                message = answer.get("message") if isinstance(answer, dict) else None
                identifier = message.get("id") if isinstance(message, dict) else None
                return type("M", (), {"id": identifier})()

        self.client._invoke = fake_invoke  # type: ignore[method-assign]
        self.client._post_media = fake_post  # type: ignore[method-assign]
        self.client._client = FakePyMax()
        self.client._ready.set()

    def source(self, name: str) -> Path:
        path = self.client._session_dir / name
        path.write_bytes(b"payload")
        return path


def _text(rig: Rig) -> Any:
    return rig.client.send_text(1, "hi")


def _contact(rig: Rig) -> Any:
    return rig.client.send_contact(1, vcard="BEGIN:VCARD\r\nEND:VCARD")


def _sticker(rig: Rig) -> Any:
    return rig.client.send_sticker(1, 42)


def _media(rig: Rig) -> Any:
    return rig.client.send_media(1, [("photo", rig.source("p.jpg"), "p.jpg")])


def _native(rig: Rig) -> Any:
    return rig.client._send_native_media(1, "voice", rig.source("v.ogg"), "v.ogg")


CREATING: dict[str, Callable[[Rig], Any]] = {
    "text": _text,
    "contact": _contact,
    "sticker": _sticker,
    "media": _media,
    "native": _native,
}


@pytest.fixture(autouse=True)
def _readable_media(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "bridge.media.native_max.probe_media", lambda path, kind="voice": (1000, bytes(80))
    )


def outcome(tmp_path: Path, operation: str, **rig_kwargs: Any) -> str:
    """What one operation does with one fault, as a comparable word."""
    rig = Rig(tmp_path, **rig_kwargs)
    try:
        result = asyncio.run(CREATING[operation](rig))
    except MaxNativeRejectedError:
        return "native-rejected"
    except MaxUnconfirmedSendError:
        return "unconfirmed"
    except MaxClientError:
        return "retryable:MaxClientError"
    except ApiError:
        return "raised:ApiError"
    except BaseException as error:  # noqa: BLE001 - that is the measurement
        return f"raised:{type(error).__name__}"
    return f"sent:{result}"


OPERATIONS = list(CREATING)


# --------------------------------------------------------------- the whole row


@pytest.mark.parametrize("operation", OPERATIONS)
def test_success_names_the_message(tmp_path: Path, operation: str) -> None:
    assert outcome(tmp_path, operation) == "sent:777"


@pytest.mark.parametrize("operation", OPERATIONS)
def test_an_answer_without_an_id_is_unconfirmed(tmp_path: Path, operation: str) -> None:
    """The frame was accepted and we cannot name what it created — exactly as
    unknown as silence, and the case the old code called success."""
    assert outcome(tmp_path, operation, answer={"ok": True}) == "unconfirmed"


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("bad", ["not-a-number", 0, -1, None, {"nested": 1}])
def test_a_malformed_id_is_unconfirmed(tmp_path: Path, operation: str, bad: Any) -> None:
    """Contact returned None here and sticker raised `ValueError` — one server
    answer, two different job states, one of them a duplicate."""
    assert outcome(tmp_path, operation, answer={"message": {"id": bad}}) == "unconfirmed"


@pytest.mark.parametrize("operation", OPERATIONS)
def test_a_timeout_is_unconfirmed(tmp_path: Path, operation: str) -> None:
    """Our own `wait_for` can fire while `transport.send` is still running, so a
    timeout does not prove the frame stayed home."""
    assert outcome(tmp_path, operation, fault=TimeoutError("op")) == "unconfirmed"


@pytest.mark.parametrize("operation", OPERATIONS)
def test_a_connection_error_is_unconfirmed(tmp_path: Path, operation: str) -> None:
    assert outcome(tmp_path, operation, fault=ConnectionError("closed")) == "unconfirmed"


@pytest.mark.parametrize("operation", OPERATIONS)
def test_an_os_error_is_unconfirmed(tmp_path: Path, operation: str) -> None:
    assert outcome(tmp_path, operation, fault=OSError("broken pipe")) == "unconfirmed"


@pytest.mark.parametrize("operation", OPERATIONS)
def test_a_session_that_is_already_down_stays_retryable(tmp_path: Path, operation: str) -> None:
    """`_require()` runs before the call is built, so this is the one failure at
    the boundary that is provably a not-sent."""
    assert outcome(
        tmp_path, operation, fault=MaxClientError("not connected")
    ) == "retryable:MaxClientError"


@pytest.mark.parametrize("operation", OPERATIONS)
def test_an_unknown_api_error_keeps_its_own_meaning(tmp_path: Path, operation: str) -> None:
    """A blocked chat is an *answer*: the message was not created. It travels on
    for the delivery layer to classify, and never becomes a question."""
    fault = ApiError(opcode=64, error="chat.blocked", message="no")
    assert outcome(tmp_path, operation, fault=fault) == "raised:ApiError"


@pytest.mark.parametrize("operation", OPERATIONS)
def test_a_confirmed_schema_rejection_is_an_answer_too(tmp_path: Path, operation: str) -> None:
    """Native is the only one that acts on it — the breaker. Everything else
    passes it through unchanged, and nothing turns it into "unknown"."""
    fault = ApiError(opcode=64, error="proto.payload", message="Expected number")
    expected = "native-rejected" if operation == "native" else "raised:ApiError"
    assert outcome(tmp_path, operation, fault=fault) == expected


# -------------------------------------------------- the row must agree with itself


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("timeout", {"fault": TimeoutError("x")}),
        ("connection", {"fault": ConnectionError("x")}),
        ("oserror", {"fault": OSError("x")}),
        ("no id", {"answer": {"ok": True}}),
        ("malformed id", {"answer": {"message": {"id": "nope"}}}),
        ("session down", {"fault": MaxClientError("x")}),
        ("unknown ApiError", {"fault": ApiError(opcode=64, error="chat.blocked", message="n")}),
    ],
)
def test_every_creating_operation_agrees(tmp_path: Path, label: str, kwargs: Any) -> None:
    """The file's whole reason: one answer, one outcome, whichever send made it."""
    verdicts = {op: outcome(tmp_path, op, **kwargs) for op in OPERATIONS}
    assert len(set(verdicts.values())) == 1, f"{label} split the operations: {verdicts}"


def test_contact_and_sticker_agree_on_a_malformed_id(tmp_path: Path) -> None:
    """Named on its own because this exact pair diverged in production code."""
    answer = {"message": {"id": "not-a-number"}}
    assert outcome(tmp_path, "contact", answer=answer) == outcome(
        tmp_path, "sticker", answer=answer
    )


# ---------------------------------------------------- no retry, no second copy


@pytest.mark.parametrize("operation", OPERATIONS)
def test_an_unconfirmed_send_is_attempted_exactly_once(tmp_path: Path, operation: str) -> None:
    rig = Rig(tmp_path, fault=TimeoutError("op"))
    with pytest.raises(MaxUnconfirmedSendError):
        asyncio.run(CREATING[operation](rig))

    creating = [c for c in rig.calls if c in ("op64", "pymax.send_message")]
    assert len(creating) == 1


def test_an_unconfirmed_native_send_never_falls_back_to_a_plain_attachment(
    tmp_path: Path,
) -> None:
    """`send_media` degrades on a refusal and must not on a question: a plain copy
    behind a message that may already be there is the duplicate by another route."""
    rig = Rig(tmp_path, fault=TimeoutError("op"))
    with pytest.raises(MaxUnconfirmedSendError):
        asyncio.run(rig.client.send_media(1, [("voice", rig.source("v.ogg"), "v.ogg")]))

    assert "pymax.send_message" not in rig.calls


def test_an_unconfirmed_send_becomes_one_ambiguous_job(tmp_path: Path) -> None:
    """The delivery half of the contract, for the kinds the worker carries."""
    from bridge.routing.delivery import UnconfirmedDeliveryError
    from bridge.service.runtime import _creating_send

    rig = Rig(tmp_path, fault=TimeoutError("op"))
    with pytest.raises(UnconfirmedDeliveryError):
        asyncio.run(_creating_send(lambda: rig.client.send_text(1, "hi")))


def test_a_confirmed_native_rejection_still_degrades_once(tmp_path: Path) -> None:
    """An `ApiError` proves the message was not created, so the fallback is safe
    and stays — this is the one place a fallback is still right."""
    fault = ApiError(opcode=64, error="proto.payload", message="Invalid media wave")
    rig = Rig(tmp_path, fault=fault, pymax_fault=None)

    assert asyncio.run(rig.client.send_media(1, [("voice", rig.source("v.ogg"), "v.ogg")])) == 777
    assert rig.calls.count("pymax.send_message") == 1
    assert rig.state.is_open("voice")
