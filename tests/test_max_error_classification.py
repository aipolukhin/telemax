"""AU-3 G5 — one reading of a MAX error, applied everywhere.

Three mechanisms answered this and none of them saw the whole picture.
`refusals.classify` knew a blocked chat refuses for ever, and was called from
exactly two places, both inline — so the worker retried the same refusal twelve
times behind an owner who had already been told about it.
`classify_native_error` knew which answers mean our hand-built attach is wrong,
and only for voice and circles. The worker's own `classify` had no MAX branch at
all.

Classification is by **what the answer says**, never by whether the server then
closed the connection. Both facts are real and they do not line up: `proto.payload`
closes the socket and `error.message.like.unknown.like` does not, and the second
is just as final for the request that caused it. Reading the socket would make
the same error mean different things depending on how fast we noticed.
"""

from __future__ import annotations

import pytest
from pymax.exceptions import ApiError

from bridge.max_client.max_errors import MaxErrorClass, classify_max_error
from bridge.max_client.native_state import classify_native_error
from bridge.routing.delivery import KIND_TG_TO_MAX_CONTACT, KIND_TG_TO_MAX_TEXT
from bridge.routing.refusals import classify as classify_refusal
from bridge.routing.settlement import Verdict, settle


def api(code: str = "", message: str = "") -> ApiError:
    return ApiError(opcode=64, error=code or None, message=message)


# ------------------------------------------------------------- the four classes


@pytest.mark.parametrize(
    "error",
    [
        api("chat.control", "no"),
        api("", "restriction to input"),
        api("", "user is blocked"),
        api("", "no access to this chat"),
        api("", "not allowed"),
    ],
)
def test_a_chat_that_will_not_take_us_is_permanent(error: ApiError) -> None:
    verdict = classify_max_error(error)
    assert verdict.kind is MaxErrorClass.PERMISSION
    assert verdict.permanent
    assert not verdict.schema_rejection  # the attach is fine; the chat is not
    assert verdict.owner_message  # and the owner is told, in their own terms


@pytest.mark.parametrize(
    "error",
    [
        api("proto.payload", "Expected number at 20"),
        api("errors.process.attachment.video.not.supported", "nope"),
        api("", "Invalid media wave"),
        api("", "Missing info for contact attachment [proto.payload]"),
    ],
)
def test_a_refusal_of_what_we_built_is_permanent_and_a_schema_rejection(
    error: ApiError,
) -> None:
    verdict = classify_max_error(error)
    assert verdict.kind is MaxErrorClass.SCHEMA
    assert verdict.permanent
    assert verdict.schema_rejection


def test_an_unknown_value_is_refused_without_being_a_bad_frame() -> None:
    """Live 2026-07-29: an emoji outside the server's set. The socket stays up and
    a different value works, so this is about the value and not the shape."""
    verdict = classify_max_error(api("error.message.like.unknown.like", "unknown like"))
    assert verdict.kind is MaxErrorClass.VALUE
    assert not verdict.schema_rejection


def test_anything_unrecognised_stays_retryable() -> None:
    """A wrong "permanent" silently drops the owner's message; a wrong "retryable"
    costs a queue slot and a log line."""
    verdict = classify_max_error(api("something.nobody.has.seen", "?"))
    assert verdict.kind is MaxErrorClass.UNKNOWN
    assert not verdict.permanent


def test_the_reason_never_carries_the_message_text() -> None:
    """A refusal's text can hold a chat title or a person's name, and the reason
    travels into logs, `/status` and incidents."""
    verdict = classify_max_error(api("chat.control", "Аня closed this chat"))
    assert verdict.reason == "chat.control"
    assert "Аня" not in verdict.reason


def test_classification_does_not_read_whether_the_socket_closed() -> None:
    """Two refusals that differ in socket behaviour and not in finality."""
    closes_socket = classify_max_error(api("proto.payload", "bad"))
    keeps_socket = classify_max_error(api("chat.control", "no"))
    assert closes_socket.permanent and keeps_socket.permanent


# --------------------------------------------------------- one reading, one path


@pytest.mark.parametrize("kind", [KIND_TG_TO_MAX_TEXT, KIND_TG_TO_MAX_CONTACT])
def test_the_settlement_policy_uses_it(kind: str) -> None:
    """Inline and worker both go through `settle`, so both stop retrying."""
    assert settle(kind, api("chat.control", "no"), remote_marked=True).verdict is (
        Verdict.PERMANENT
    )
    assert settle(kind, api("who.knows", "?"), remote_marked=True).verdict is Verdict.RETRY


def test_the_owner_facing_wording_uses_it() -> None:
    blocked = classify_refusal(api("", "user is blocked"))
    assert blocked.permanent
    assert "MAX" in blocked.message or "Контакт" in blocked.message

    unknown = classify_refusal(api("nobody.knows", "?"))
    assert not unknown.permanent


def test_native_media_reads_the_same_verdict() -> None:
    """The breaker opens on a refusal of the attach — and only on that."""
    assert classify_native_error(api("proto.payload", "bad")).protocol_rejection
    assert classify_native_error(api("", "Invalid media wave")).protocol_rejection
    # Permanent, but about the conversation. Turning voice off for every contact
    # over one blocked chat would be the wrong repair.
    assert not classify_native_error(api("chat.control", "no")).protocol_rejection
    assert not classify_native_error(api("who.knows", "?")).protocol_rejection


def test_a_permanent_chat_refusal_does_not_trip_the_native_breaker() -> None:
    """Named separately because it is the mistake a shared classifier invites:
    "permanent" and "our frame is wrong" are different questions."""
    verdict = classify_max_error(api("chat.control", "no"))
    assert verdict.permanent
    assert not verdict.schema_rejection


def test_only_one_module_classifies_max_errors() -> None:
    """The structural half: the marker tables live in one file."""
    import ast
    from pathlib import Path

    markers = ("chat.control", "restriction to input", "proto.payload", "invalid media wave")
    offenders: list[str] = []
    for path in sorted(Path("bridge").rglob("*.py")):
        if path.name == "max_errors.py":
            continue
        text = path.read_text(encoding="utf-8")
        for marker in markers:
            if marker in text and "docs/" not in text[: text.index(marker)][-80:]:
                # Mentioned in prose is fine; a string literal used for matching
                # is what this is looking for.
                tree = ast.parse(text)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Constant) and node.value == marker:
                        offenders.append(f"{path}:{node.lineno} {marker}")
    assert offenders == [], f"MAX error markers outside max_errors.py: {offenders}"
