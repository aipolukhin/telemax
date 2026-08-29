"""MAX event normalization and PyMax compatibility fixtures."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

import pytest

from bridge.max_client import (
    AttachmentKind,
    MaxClient,
    MaxClientError,
    TypingKind,
    normalize_attachment,
    normalize_message,
)
from bridge.max_client.interactive import PING
from bridge.max_client.opcodes import (
    Opcode,
    add_reaction_frame,
    remove_reaction_frame,
    typing_frame,
)
from bridge.max_client.session import ensure_session_dir, harden_session_files

OWNER = 100000002
CONTACT = 4242

VOICE = {"_type": "AUDIO", "audioId": 1, "duration": 1579, "wave": "...", "token": "t"}
VIDEO_NOTE = {"_type": "VIDEO", "videoType": 1, "width": 480, "height": 480, "duration": 2100}
VIDEO = {"_type": "VIDEO", "videoType": 0, "width": 720, "height": 1280, "duration": 4000}
MUSIC = {
    "_type": "FILE",
    "fileId": 9,
    "name": "track.mp3",
    "size": 4_000_000,
    # Seconds here — unlike every other duration in the protocol.
    "preview": {"_type": "MUSIC", "artistName": "Someone", "title": "Song", "duration": 275},
}
DOCUMENT = {"_type": "FILE", "fileId": 10, "name": "scan.pdf", "size": 1000, "token": "t"}
PHOTO = {"_type": "PHOTO", "photoId": 3, "baseUrl": "https://x/y", "width": 100, "height": 200}


def test_voice_is_audio_but_music_is_a_file() -> None:
    """The single most counter-intuitive fact about MAX attachments."""
    assert normalize_attachment(VOICE).kind is AttachmentKind.VOICE
    assert normalize_attachment(MUSIC).kind is AttachmentKind.MUSIC
    assert normalize_attachment(DOCUMENT).kind is AttachmentKind.FILE


def test_a_pymax_enum_is_read_as_its_value() -> None:
    """`class X(str, Enum)` stringifies to `X.PHOTO`, not `PHOTO`.

    Raw events carry plain strings, so this only bites where a value came back
    through a PyMax model — which is every attachment in a history backfill.
    Found on compatibility fixtures: photo, voice and circle all arrived as `unknown`.
    """
    from enum import Enum

    # Deliberately the shape PyMax uses, which is what the lint rule warns about
    # and exactly why this test exists.
    class AttachmentType(str, Enum):  # noqa: UP042
        PHOTO = "PHOTO"
        VIDEO = "VIDEO"
        AUDIO = "AUDIO"

    assert str(AttachmentType.PHOTO) == "AttachmentType.PHOTO"

    assert normalize_attachment({"_type": AttachmentType.PHOTO}).kind is AttachmentKind.PHOTO
    assert normalize_attachment({"_type": AttachmentType.AUDIO}).kind is AttachmentKind.VOICE
    assert (
        normalize_attachment({"_type": AttachmentType.VIDEO, "videoType": 1}).kind
        is AttachmentKind.VIDEO_NOTE
    )


def test_video_note_is_recognised_by_video_type() -> None:
    assert normalize_attachment(VIDEO_NOTE).kind is AttachmentKind.VIDEO_NOTE
    assert normalize_attachment(VIDEO).kind is AttachmentKind.VIDEO
    # Square geometry alone must not be enough — the flag is what counts.
    square_but_plain = dict(VIDEO, width=480, height=480)
    assert normalize_attachment(square_but_plain).kind is AttachmentKind.VIDEO


def test_music_duration_is_converted_from_seconds() -> None:
    music = normalize_attachment(MUSIC)
    assert music.duration_ms == 275_000
    assert music.title == "Song"
    assert music.performer == "Someone"

    # Everything else already arrives in milliseconds and must not be scaled.
    assert normalize_attachment(VOICE).duration_ms == 1579
    assert normalize_attachment(VIDEO_NOTE).duration_ms == 2100


def test_photo_dimensions_survive() -> None:
    photo = normalize_attachment(PHOTO)
    assert (photo.kind, photo.width, photo.height) == (AttachmentKind.PHOTO, 100, 200)


def test_unknown_attachment_does_not_explode() -> None:
    assert normalize_attachment({"_type": "SOMETHING_NEW"}).kind is AttachmentKind.UNKNOWN
    assert normalize_attachment(None).kind is AttachmentKind.UNKNOWN


def test_message_normalisation() -> None:
    payload = {
        "id": 111411200065536001,
        "chatId": 777,
        "sender": CONTACT,
        "text": "hi",
        "time": 1_700_000_000,
        "attaches": [PHOTO, VOICE],
    }

    message = normalize_message(payload, own_user_id=OWNER)

    assert message.message_id == 111411200065536001
    assert message.chat_id == 777
    assert message.sender_id == CONTACT
    assert message.is_outgoing is False
    assert [item.kind for item in message.attachments] == [
        AttachmentKind.PHOTO,
        AttachmentKind.VOICE,
    ]


def test_own_message_is_marked_outgoing() -> None:
    """Messages sent from the official app arrive as events too (WP11)."""
    payload = {"id": 1, "chatId": 777, "sender": OWNER, "text": "from my phone", "time": 1}
    assert normalize_message(payload, own_user_id=OWNER).is_outgoing is True


def test_reply_id_is_dug_out_of_the_raw_payload() -> None:
    """PyMax's typed model drops `link`, so WP10 has to read the raw payload."""
    payload = {
        "id": 2,
        "chatId": 777,
        "sender": CONTACT,
        "text": "answer",
        "time": 1,
        "link": {"type": "REPLY", "message": {"id": 111}},
    }
    assert normalize_message(payload, own_user_id=OWNER).reply_to_message_id == 111

    forwarded = dict(payload, link={"type": "FORWARD", "message": {"id": 111}})
    assert normalize_message(forwarded, own_user_id=OWNER).reply_to_message_id is None


def test_typing_frame_shape() -> None:
    assert typing_frame(777) == {"chatId": 777, "type": "TEXT"}
    assert typing_frame(777, TypingKind.VIDEO)["type"] == "VIDEO"


def test_reaction_frames_use_a_numeric_message_id() -> None:
    """A string id makes the server answer proto.payload and hang up."""
    frame = add_reaction_frame(777, 111411200065536001, "👍")

    assert frame["messageId"] == 111411200065536001
    assert isinstance(frame["messageId"], int)
    assert frame["reaction"] == {"reactionType": "EMOJI", "id": "👍"}

    assert remove_reaction_frame(777, 5) == {"chatId": 777, "messageId": 5}


# --------------------------------------------------------------- fake PyMax


class FakeConnection:
    """The object PyMax actually delivers inbound frames through."""

    def __init__(self) -> None:
        #: Frames the bridge passed through instead of intercepting.
        self.frames: list[Any] = []
        self.on_event: Any = None

    async def deliver(self, frame: Any) -> None:
        """What `_dispatch_event` does: call whatever `on_event` is *now*."""
        await self.on_event(frame)


class FakeApp:
    def __init__(self) -> None:
        self.calls: list[tuple[int, dict[str, Any]]] = []
        self.fail_with: Exception | None = None
        self.connection = FakeConnection()
        # The line that made the first fix a no-op: PyMax copies the bound
        # method onto the connection once, at construction. Patching
        # `app.on_event` afterwards changes an attribute nothing reads.
        self.connection.on_event = self.on_event

    @property
    def frames(self) -> list[Any]:
        return self.connection.frames

    async def on_event(self, frame: Any) -> None:
        self.connection.frames.append(frame)

    async def invoke(self, opcode: int, payload: dict[str, Any]) -> Any:
        self.calls.append((opcode, payload))
        if self.fail_with is not None:
            raise self.fail_with
        return {"ok": True}


class FakeProfile:
    def __init__(self, user_id: int) -> None:
        self.contact = type("Contact", (), {"id": user_id})()


class FakePyMaxClient:
    """Enough of PyMax to exercise the wrapper, with its awkward parts kept.

    In particular `start()` never returns, and handlers are registered through
    decorators before it is called.
    """

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.me = FakeProfile(OWNER)
        self._app = self._build_app()
        self.handlers: dict[str, Any] = {}
        self.sent: list[dict[str, Any]] = []
        self.read: list[tuple[int, int]] = []
        #: Numbers MAX knows about, and what it answers with.
        self.directory: dict[str, Any] = {}
        self.searched: list[str] = []
        self.imported: list[list[Any]] = []
        self.started = asyncio.Event()
        self.stopped = False
        #: When set, `start()` raises instead of coming up — the way a bad
        #: session or an unreachable server fails the connection attempt.
        self.fail_start: BaseException | None = None

    def _build_app(self) -> Any:
        """PyMax rebuilds the app on every reconnect; the fake must be able to."""
        return FakeApp()

    def _register(self, name: str) -> Any:
        def decorator() -> Any:
            def wrapper(handler: Any) -> Any:
                self.handlers[name] = handler
                return handler

            return wrapper

        return decorator

    def __getattr__(self, name: str) -> Any:
        if name.startswith("on_"):
            return self._register(name)
        raise AttributeError(name)

    async def emit(self, name: str, event: Any = None) -> Any:
        """Deliver an event the way PyMax 2.3.1 really does it.

        Its dispatcher calls every handler as `callback(event, client)` — there
        is no arity adaptation anywhere in the library. A one-argument handler
        raises TypeError, and an exception escaping a handler tears the
        connection down, so the calling convention is part of the contract and
        this fake has to reproduce it.
        """
        handler = self.handlers[name]
        if name == "on_start":
            return await handler(self)
        return await handler(event, self)

    async def start(self) -> None:
        if self.fail_start is not None:
            raise self.fail_start
        if "on_start" in self.handlers:
            await self.emit("on_start")
        self.started.set()
        # start() is a reconnect loop: it blocks until cancelled.
        await asyncio.Event().wait()

    async def stop(self) -> None:
        self.stopped = True

    async def send_message(self, chat_id: int, text: str, reply_to: int | None = None) -> Any:
        self.sent.append({"chat_id": chat_id, "text": text, "reply_to": reply_to})
        return type("Message", (), {"id": 555})()

    async def read_message(self, message_id: int, chat_id: int) -> None:
        self.read.append((chat_id, message_id))

    async def search_by_phone(self, phone: str) -> Any:
        """PyMax raises when the answer carries no contact — "nobody" is an error."""
        self.searched.append(phone)
        found = self.directory.get(phone)
        if found is None:
            raise RuntimeError("contact missing in payload")
        return found

    async def import_contacts(self, contacts: list[Any]) -> list[Any]:
        self.imported.append(list(contacts))
        return [self.directory[item.phone] for item in contacts if item.phone in self.directory]


async def _client(tmp_path: Path) -> tuple[MaxClient, FakePyMaxClient]:
    made: list[FakePyMaxClient] = []

    def factory(**kwargs: Any) -> FakePyMaxClient:
        fake = FakePyMaxClient(**kwargs)
        made.append(fake)
        return fake

    client = MaxClient(
        phone="+70000000000",
        session_dir=tmp_path / "max-session",
        session_name="s.db",
        client_factory=factory,
    )
    await client.start(timeout=5)
    return client, made[0]


async def test_start_stop_and_identity(tmp_path: Path) -> None:
    client, fake = await _client(tmp_path)
    try:
        assert client.is_ready
        assert client.own_user_id == OWNER
    finally:
        await client.stop()

    assert fake.stopped is True
    assert not client.is_ready


async def test_session_directory_is_private(tmp_path: Path) -> None:
    session_dir = tmp_path / "max-session"
    ensure_session_dir(session_dir)
    (session_dir / "s.db").write_text("token", encoding="utf-8")
    harden_session_files(session_dir)

    assert (session_dir.stat().st_mode & 0o777) == 0o700
    assert ((session_dir / "s.db").stat().st_mode & 0o777) == 0o600


async def test_handler_exception_does_not_reach_pymax(tmp_path: Path) -> None:
    """The rule that keeps the account online: never let a handler raise."""
    client, fake = await _client(tmp_path)
    seen: list[Any] = []

    async def explode(event: Any) -> None:
        seen.append(event)
        raise RuntimeError("delivery is broken")

    client.on_message(explode)

    payload = {"id": 1, "chatId": 777, "sender": CONTACT, "text": "hi", "time": 1}
    await fake.emit("on_message", payload)  # must not raise

    assert len(seen) == 1
    await client.stop()


async def test_every_handler_takes_the_client_argument(tmp_path: Path) -> None:
    """PyMax passes the client alongside the event to every handler.

    Its dispatcher does `callback(event, client)` with no arity check, so a
    handler written with one parameter raises TypeError — and a raising handler
    costs the connection. Found the hard way on compatibility fixtures; this pins it for
    all handlers at once, including the ones no other test exercises.
    """
    client, fake = await _client(tmp_path)
    try:
        assert set(fake.handlers) >= {
            "on_start",
            "on_message",
            "on_message_edit",
            "on_message_delete",
            "on_typing",
            "on_message_read",
        }
        # `on_reaction_update` is deliberately absent: reactions are read off
        # the raw frame, because PyMax's model raises before any handler runs.
        assert "on_reaction_update" not in fake.handlers
        for name, handler in fake.handlers.items():
            event = fake if name == "on_start" else object()
            # Binding is what PyMax does before the call; a one-parameter
            # handler fails right here.
            inspect.signature(handler).bind(event, fake)
            # And nothing may escape, whatever the event turns out to be.
            await fake.emit(name, event)
    finally:
        await client.stop()


async def test_unparseable_event_is_skipped(tmp_path: Path) -> None:
    client, fake = await _client(tmp_path)
    delivered: list[Any] = []
    client.on_typing(lambda event: _collect(delivered, event))

    await fake.emit("on_typing", object())  # no chat_id at all

    assert delivered == []
    await client.stop()


async def _collect(sink: list[Any], event: Any) -> None:
    sink.append(event)


async def test_read_mark_knows_whose_it_is(tmp_path: Path) -> None:
    client, fake = await _client(tmp_path)
    marks: list[Any] = []
    client.on_read(lambda event: _collect(marks, event))

    for user_id in (OWNER, CONTACT):
        await fake.emit(
            "on_message_read",
            type(
                "Event",
                (),
                {"chat_id": 777, "user_id": user_id, "mark": 100, "set_as_unread": False},
            )(),
        )

    assert [mark.is_own for mark in marks] == [True, False]
    await client.stop()


async def test_reaction_counters_are_flattened(tmp_path: Path) -> None:
    """Delivered from the raw frame: PyMax's typed path raises before us."""
    client, fake = await _client(tmp_path)
    updates: list[Any] = []
    client.on_reaction(lambda event: _collect(updates, event))

    await fake._app.connection.deliver(
        type(
            "Frame",
            (),
            {
                "opcode": 155,
                "payload": {
                    "chatId": 777,
                    "messageId": 111411200196608003,
                    "reactionInfo": {
                        "counters": [{"reaction": "👍", "count": 2}],
                        "totalCount": 2,
                    },
                },
            },
        )()
    )

    assert updates[0].counters == {"👍": 2}
    assert updates[0].message_id == 111411200196608003
    await client.stop()


async def test_a_dialog_reaction_arrives_as_a_chat_update(tmp_path: Path) -> None:
    """Opcode 155 is not what fires in a private dialog — 135 is."""
    client, fake = await _client(tmp_path)
    updates: list[Any] = []
    client.on_chat_reaction(lambda event: _collect(updates, event))

    frame = type(
        "Frame",
        (),
        {
            "opcode": 135,
            "payload": {
                "chat": {
                    "id": 777,
                    "type": "DIALOG",
                    "lastReactedMessageId": 111411200262144004,
                    "lastReaction": "👍",
                }
            },
        },
    )()
    await fake._app.connection.deliver(frame)

    assert updates[0].message_id == 111411200262144004
    assert updates[0].emoji == "👍"
    assert fake._app.frames == [frame], "a chat update is still PyMax's business too"
    await client.stop()


async def test_other_frames_pass_straight_through(tmp_path: Path) -> None:
    """Only opcode 155 is taken away from PyMax; the rest is its business.

    A message frame is *watched* rather than intercepted (see the rescue tests
    below), and one carrying no message at all is not even that: nothing to
    carry, nothing to warn about.
    """
    client, fake = await _client(tmp_path)
    delivered: list[Any] = []
    client.on_message(lambda event: _collect(delivered, event))

    passed = type("Frame", (), {"opcode": 128, "payload": {}})()
    await fake._app.connection.deliver(passed)

    assert fake._app.frames == [passed]
    assert delivered == []
    await client.stop()


# --------------------------------------- messages PyMax refuses to type at all


#: The envelope a message arrives in, represented by this fixture (opcode 128, 2026-08-10).
#: The attach is a video note: `videoType: 1`, and every field PyMax's
#: `VideoAttachment` declares required.
CIRCLE_ID = 111411201048576016
CIRCLE_ATTACH = {
    "_type": "VIDEO",
    "videoType": 1,
    "videoId": 2500000000001,
    "duration": 2100,
    "width": 480,
    "height": 480,
    "previewData": "<bytes 302>",
    "thumbnail": "https://v.oneme.ru/videoMsg?cid=x",
    "token": "fixture-media-token",
}


def _circle_frame(
    *, without: str | None = None, extra: dict[str, Any] | None = None, **message: Any
) -> dict[str, Any]:
    """The frame, optionally with a field of the attach missing or added.

    `without` is MAX dropping a field, which after the sweep in `pymax_compat`
    types cleanly — that is the whole point of the sweep. `extra` is MAX sending
    something new, which is how the shapes that still defeat PyMax are built.
    """
    attach = {key: value for key, value in CIRCLE_ATTACH.items() if key != without}
    attach.update(extra or {})
    return {
        "chatId": 300000004,
        "message": {
            "id": CIRCLE_ID,
            "time": 1700000004000,
            "type": "USER",
            "sender": CONTACT,
            "text": "",
            "attaches": [attach],
            **message,
        },
        "prevMessageId": 111411200983040015,
        "unread": 1,
        "mark": 1700000004000,
    }


def _frame(opcode: int, payload: dict[str, Any]) -> Any:
    return type("Frame", (), {"opcode": opcode, "payload": payload})()


def test_the_preview_field_max_stopped_sending_is_relaxed() -> None:
    """Caught on the wire 2026-08-10: `previewData` is gone, `thumbhash` is there.

    PyMax declares `VideoAttachment.previewData` required and the photo's
    equivalent optional, which is exactly why photos kept arriving and circles
    stopped. Relaxed in `pymax_compat` rather than only rescued in the client,
    because the login answer carries each chat's last message — and a contact's
    circle sitting there would fail the login itself, where no frame hook runs.
    """
    from bridge.max_client.pymax_compat import typed_message_error

    live_shape = _circle_frame(without="previewData")
    live_shape["message"]["attaches"][0]["thumbhash"] = "cQcSHYSJl4iZh4d3d4dv"

    assert typed_message_error(live_shape) is None


#: A shape PyMax genuinely cannot type, now that presence alone no longer
#: defeats it: a field that is there and is the wrong kind of thing. Kept for
#: the rescue's own tests — the rescue is not about missing fields, it is about
#: everything the model can still refuse.
UNTYPEABLE = {"videoId": {"not": "a number"}}


def test_no_attachment_field_is_required_except_the_discriminator() -> None:
    """The policy, asserted rather than described.

    The MAX app requires nothing — its reader skips unknown keys and its builder
    validates none of them (`wire_vocabulary`). Every required field in a typed
    model is therefore a claim about MAX that MAX never made, and three of them
    have already been disproved in production, each at a cost. The discriminator
    is the exception: it is what tells the models apart at all.
    """
    import typing

    from pymax.types.domain.message import KnownAttachment

    from bridge.max_client.pymax_compat import DISCRIMINATOR

    members = typing.get_args(typing.get_args(KnownAttachment)[0])
    assert members, "PyMax's attachment union changed shape; the sweep reads it"
    for model in members:
        required = [name for name, f in model.model_fields.items() if f.is_required()]
        assert required == [DISCRIMINATOR], f"{model.__name__} still requires {required}"


def test_a_field_max_drops_no_longer_costs_the_message() -> None:
    """Any of them, not the one we happened to be bitten by.

    `previewData` is the field that was actually withdrawn; the others are the
    same bet with a different name on it.
    """
    from bridge.max_client.pymax_compat import typed_message_error

    for field in ("previewData", "token", "thumbnail", "videoId", "width"):
        assert typed_message_error(_circle_frame(without=field)) is None, field


def test_the_union_still_cannot_degrade_which_is_why_the_rescue_stays() -> None:
    """The canary. `UnknownAttachment` refuses a known `_type`.

    So an attach PyMax half-recognises cannot fall back to unknown — it fails
    the whole message. If this ever stops raising, PyMax fixed the union, and
    the frame rescue in `MaxClient` can go.
    """
    import pytest as _pytest
    from pydantic import ValidationError
    from pymax.types.domain.attachments import UnknownAttachment

    with _pytest.raises(ValidationError):
        UnknownAttachment.model_validate({"_type": "VIDEO"})


def test_the_diagnostic_names_fields_and_never_values() -> None:
    """A payload carries CDN tokens; a log line about it must not."""
    from bridge.max_client.pymax_compat import typed_message_error

    reason = typed_message_error(_circle_frame(extra=UNTYPEABLE))

    assert reason is not None
    assert CIRCLE_ATTACH["token"] not in reason


def test_a_key_even_the_max_app_cannot_read_is_named() -> None:
    """The rarer event: MAX ahead of every parser at once, ours and the phone's.

    `thumbhash` is in the app's vocabulary — it was reading it before the server
    started sending it — so the arrival of that field says nothing. A key that is
    in neither vocabulary does.
    """
    from bridge.max_client.wire_vocabulary import keys_the_app_cannot_read

    assert keys_the_app_cannot_read(CIRCLE_ATTACH) == []
    assert keys_the_app_cannot_read({**CIRCLE_ATTACH, "thumbhash": "x"}) == []
    assert keys_the_app_cannot_read({**CIRCLE_ATTACH, "quantumPreview": 1}) == ["quantumPreview"]


async def test_a_message_pymax_cannot_type_is_carried_anyway(tmp_path: Path) -> None:
    """The live loss this exists for: a video note that never arrived.

    PyMax answers a `ValidationError` with a debug line and a `None`, so the
    frame is gone before any handler, and nothing below — the claim, the queue,
    `/failed` — is ever told a message existed. Measured 2026-08-10.
    """
    client, fake = await _client(tmp_path)
    delivered: list[Any] = []
    client.on_message(lambda event: _collect(delivered, event))

    frame = _frame(128, _circle_frame(extra=UNTYPEABLE))
    await fake._app.connection.deliver(frame)

    assert [item.message_id for item in delivered] == [CIRCLE_ID]
    assert delivered[0].chat_id == 300000004, "the chat is in the envelope, not the message"
    assert delivered[0].attachments[0].kind is AttachmentKind.VIDEO_NOTE
    assert fake._app.frames == [frame], "PyMax still gets the frame"
    await client.stop()


async def test_a_message_pymax_can_type_is_left_to_pymax(tmp_path: Path) -> None:
    """No second copy. The rescue covers the branch that ended in nothing."""
    client, fake = await _client(tmp_path)
    delivered: list[Any] = []
    client.on_message(lambda event: _collect(delivered, event))

    frame = _frame(128, _circle_frame())
    await fake._app.connection.deliver(frame)

    assert delivered == [], "PyMax accepted it; delivering it here would duplicate it"
    assert fake._app.frames == [frame]
    await client.stop()


async def test_an_untypeable_edit_arrives_as_an_edit(tmp_path: Path) -> None:
    """Opcode 67 carries the same model, so it fails and is rescued the same."""
    client, fake = await _client(tmp_path)
    edits: list[Any] = []
    client.on_message_edit(lambda event: _collect(edits, event))

    payload = _circle_frame(extra=UNTYPEABLE, status="EDITED")
    await fake._app.connection.deliver(_frame(67, payload))

    assert [item.message_id for item in edits] == [CIRCLE_ID]
    await client.stop()


async def test_an_untypeable_removal_arrives_as_a_deletion(tmp_path: Path) -> None:
    """A message whose status says it is gone must not be delivered as new."""
    client, fake = await _client(tmp_path)
    deletions: list[Any] = []
    delivered: list[Any] = []
    client.on_message_delete(lambda event: _collect(deletions, event))
    client.on_message(lambda event: _collect(delivered, event))

    payload = _circle_frame(extra=UNTYPEABLE, status="REMOVED")
    await fake._app.connection.deliver(_frame(128, payload))

    assert deletions[0].message_ids == (CIRCLE_ID,)
    assert deletions[0].chat_id == 300000004
    assert delivered == []
    await client.stop()


async def test_a_handler_that_fails_on_a_rescued_message_stays_contained(
    tmp_path: Path,
) -> None:
    """The rescue runs inside PyMax's dispatch: raising there drops the socket."""
    client, fake = await _client(tmp_path)

    async def explode(_: Any) -> None:
        raise RuntimeError("delivery is broken")

    client.on_message(explode)

    frame = _frame(128, _circle_frame(extra=UNTYPEABLE))
    await fake._app.connection.deliver(frame)

    assert fake._app.frames == [frame]
    await client.stop()


async def test_actions_go_through_the_right_opcodes(tmp_path: Path) -> None:
    client, fake = await _client(tmp_path)

    await client.send_typing(777)
    await client.add_reaction(777, 111411200065536001, "👍")
    await client.remove_reaction(777, 111411200065536001)

    # The keepalive is not an action: op1 belongs to the presence flag, which
    # took PyMax's ping loop over on `on_start` (see `max_client/interactive.py`).
    actions = [(opcode, payload) for opcode, payload in fake._app.calls if opcode != PING]
    assert [opcode for opcode, _ in actions] == [
        Opcode.MSG_TYPING,
        Opcode.MSG_REACTION,
        Opcode.MSG_CANCEL_REACTION,
    ]
    assert isinstance(actions[1][1]["messageId"], int)

    assert await client.send_text(777, "hello") == 555

    # A read mark is our own frame, not PyMax's: the mark has to be the read
    # message's own time, and PyMax's typed method always sends `now`.
    message_id = 111411200786432012  # sent at 1700000012000 ms
    await client.mark_read(777, message_id)
    opcode, payload = fake._app.calls[-1]
    assert opcode == Opcode.CHAT_MARK
    assert payload == {
        "type": "READ_MESSAGE",
        "chatId": 777,
        "messageId": message_id,
        "mark": 1700000012000,
    }
    assert fake.read == [], "PyMax's read_message would over-mark the chat"

    await client.stop()


async def test_a_mark_refuses_an_id_that_carries_no_time(tmp_path: Path) -> None:
    """No falling back to `now`: that would mark the whole chat read.

    The mark is a watermark over time. An id whose top bits are not a plausible
    millisecond is one this cannot place, and "now" places it past every message
    in the conversation — the precise thing `on_read` exists not to do. A missing
    tick is a missing tick; a wrong one is a lie about what the owner has seen.
    """
    client, fake = await _client(tmp_path)
    before = len(fake._app.calls)

    with pytest.raises(MaxClientError):
        await client.mark_read(777, 42)

    assert len(fake._app.calls) == before, "nothing may reach the wire"
    await client.stop()


async def test_typing_failure_is_swallowed(tmp_path: Path) -> None:
    """An indicator must never break delivery or the connection."""
    client, fake = await _client(tmp_path)
    fake._app.fail_with = RuntimeError("server said no")

    await client.send_typing(777)  # must not raise

    await client.stop()


async def test_actions_require_a_connection(tmp_path: Path) -> None:
    client = MaxClient(
        phone="+70000000000",
        session_dir=tmp_path / "max-session",
        session_name="s.db",
        client_factory=FakePyMaxClient,
    )

    with pytest.raises(MaxClientError):
        await client.send_text(777, "hello")


async def test_history_is_normalised_oldest_first(tmp_path: Path) -> None:
    """The backfill after downtime depends on this order and on dedup upstream."""
    client, fake = await _client(tmp_path)

    async def history(chat_id: int) -> list[Any]:
        return [
            {"id": 3, "chatId": chat_id, "sender": CONTACT, "text": "third", "time": 3},
            {"id": 1, "chatId": chat_id, "sender": CONTACT, "text": "first", "time": 1},
        ]

    fake.fetch_history = history  # type: ignore[attr-defined]

    messages = await client.fetch_history(777)

    assert [item.message_id for item in messages] == [1, 3]
    assert messages[0].chat_id == 777
    await client.stop()


async def test_reconnect_hook_fires_only_after_a_reconnect(tmp_path: Path) -> None:
    client, fake = await _client(tmp_path)
    calls: list[int] = []

    async def on_reconnect() -> None:
        calls.append(1)

    client.on_reconnect(on_reconnect)

    # The first on_start already happened during start(); a second one is a
    # reconnect, and that is when the backfill belongs.
    await fake.emit("on_start")

    assert calls == [1]
    await client.stop()


async def test_a_failing_reconnect_backfill_does_not_take_the_session_down(
    tmp_path: Path,
) -> None:
    """The backfill runs on the reconnect path; it must not cost the connection.

    A resync that raises used to escape the `on_start` handler, and an exception
    out of a PyMax handler tears the socket down — so a hiccup during catch-up
    would drop the very session it was trying to catch up. The failure is
    swallowed, logged, and the session stays ready.
    """
    client, fake = await _client(tmp_path)

    async def on_reconnect() -> None:
        raise RuntimeError("history fetch fell over")

    client.on_reconnect(on_reconnect)

    await fake.emit("on_start")  # the reconnect — must not raise

    assert client.is_ready
    await client.stop()


async def test_a_failed_start_surfaces_as_text_without_the_phone(tmp_path: Path) -> None:
    """A connection that never comes up is reported to a person, not dumped.

    The wrapper keeps `type(error).__name__: error`, deliberately not the object
    or its chain: PyMax raises with the dial attempt — phone number and all — in
    the exception context, and letting that reach the surface would print the
    owner's number. Here the number lives in `__context__`; the surfaced message
    must not.
    """

    def factory(**kwargs: Any) -> FakePyMaxClient:
        fake = FakePyMaxClient(**kwargs)
        # The dial attempt — phone and all — sits in the exception context, the
        # way PyMax raises it. The surfaced text must not carry it through.
        error = RuntimeError("handshake refused")
        error.__cause__ = ConnectionError("dialing +70000000000")
        fake.fail_start = error
        return fake

    client = MaxClient(
        phone="+70000000000",
        session_dir=tmp_path / "max-session",
        session_name="s.db",
        client_factory=factory,
    )

    with pytest.raises(MaxClientError) as caught:
        await client.start(timeout=5)

    message = str(caught.value)
    assert message == "RuntimeError: handshake refused"
    assert "+70000000000" not in message
    assert not client.is_ready


async def test_a_start_that_never_readies_times_out_cleanly(tmp_path: Path) -> None:
    """No `on_start` ever arrives: the wait ends as an error, not a hang."""

    class Mute(FakePyMaxClient):
        async def start(self) -> None:
            self.started.set()
            await asyncio.Event().wait()  # blocks without ever emitting on_start

    client = MaxClient(
        phone="+70000000000",
        session_dir=tmp_path / "max-session",
        session_name="s.db",
        client_factory=Mute,
    )

    with pytest.raises(MaxClientError, match="did not come up"):
        await client.start(timeout=0.2)

    assert not client.is_ready


# ------------------------------------------------------ reactions off the wire

REACTION_FRAME = {
    "chatId": 236856064,
    # The field PyMax types as `str`. The server sends a number, every time.
    "messageId": 111411200196608003,
    "reactionInfo": {
        "counters": [{"reaction": "👍", "count": 2}, {"reaction": "❤️", "count": 1}],
        "totalCount": 3,
    },
}


def test_a_reaction_frame_is_read_without_pymax() -> None:
    """PyMax's own model raises on this payload — measured, not supposed.

    `ReactionUpdateEvent.messageId` is declared `str`; the server sends an int;
    `model_validate` raises inside PyMax's dispatcher and becomes
    `RuntimeError: Failed to dispatch inbound frame`. The typed handler is never
    reached, so a reaction in MAX simply never appeared in Telegram.
    """
    from bridge.max_client import reaction_from

    update = reaction_from(REACTION_FRAME)

    assert update.chat_id == 236856064
    assert update.message_id == 111411200196608003
    assert update.counters == {"👍": 2, "❤️": 1}
    assert update.total == 3


def test_pymax_still_cannot_parse_it() -> None:
    """A canary: if this ever passes, PyMax fixed the field and the intercept
    can go. Until then removing it silently loses every reaction."""
    import pytest as _pytest
    from pydantic import ValidationError
    from pymax.types.events.reaction import ReactionUpdateEvent

    with _pytest.raises(ValidationError):
        ReactionUpdateEvent.model_validate(
            {
                "messageId": REACTION_FRAME["messageId"],
                "chatId": REACTION_FRAME["chatId"],
                "counters": [],
                "totalCount": 0,
            }
        )


def test_counters_at_the_top_level_are_accepted_too() -> None:
    """Both shapes have been seen; guessing wrong loses the reaction silently."""
    from bridge.max_client import reaction_from

    update = reaction_from(
        {
            "chatId": 1,
            "messageId": 2,
            "counters": [{"reaction": "🔥", "count": 5}],
            "totalCount": 5,
        }
    )

    assert update.counters == {"🔥": 5}
    assert update.total == 5


def test_a_cleared_reaction_reads_as_empty_not_as_a_failure() -> None:
    from bridge.max_client import reaction_from

    update = reaction_from({"chatId": 1, "messageId": 2, "reactionInfo": {}})

    assert update.counters == {}
    assert update.total == 0


async def test_the_reaction_hook_survives_a_reconnect(tmp_path: Path) -> None:
    """PyMax rebuilds its `App` on every reconnect.

    `start()` is a reconnect loop and `_reset_runtime()` builds a fresh `App`
    on the way round, so a hook installed only on the app that exists at
    start-up works until the first blip and then silently stops. That failure
    is indistinguishable from "reactions are not supported".
    """
    client, fake = await _client(tmp_path)
    updates: list[Any] = []
    client.on_reaction(lambda event: _collect(updates, event))

    # What PyMax does internally when the connection drops.
    fake._app = fake._build_app()

    await fake._app.connection.deliver(
        type(
            "Frame",
            (),
            {
                "opcode": 155,
                "payload": {"chatId": 1, "messageId": 2, "counters": [], "totalCount": 0},
            },
        )()
    )

    assert updates, "the rebuilt app lost the interception"
    await client.stop()


# ------------------------------------------------------- looking up a number


def _max_user(user_id: int, first: str, last: str, avatar: str) -> Any:
    """What `CONTACT_INFO_BY_PHONE` answers with, in the live shape (2026-08-04)."""
    # The pinned model itself, not a stand-in: `normalize_contact` reads the
    # payload through `model_dump`, so an object that merely has the attributes
    # would pass a test the real answer fails.
    from pymax.types import User

    return User(
        id=user_id,
        names=[{"firstName": first, "lastName": last, "type": "ONEME"}],
        baseUrl=avatar,
    )


async def test_a_number_resolves_to_a_name_and_an_avatar(tmp_path: Path) -> None:
    client, fake = await _client(tmp_path)
    fake.directory["+79001234567"] = _max_user(
        200000003, "Анна", "Смирнова", "https://i.oneme.ru/i?r=abc"
    )
    try:
        contact = await client.search_by_phone("+79001234567")
    finally:
        await client.stop()

    assert contact is not None
    assert contact.user_id == 200000003
    assert contact.display_name == "Анна Смирнова"
    assert contact.avatar_url == "https://i.oneme.ru/i?r=abc"


async def test_a_number_nobody_has_is_not_an_error(tmp_path: Path) -> None:
    """PyMax raises when the payload has no contact; "not found" is not a crash."""
    client, fake = await _client(tmp_path)
    try:
        assert await client.search_by_phone("+79009999999") is None
    finally:
        await client.stop()
    assert fake.searched == ["+79009999999"], "one number, one lookup"


async def test_an_import_carries_exactly_one_contact(tmp_path: Path) -> None:
    """The address book is a list; this call must never be handed one."""
    client, fake = await _client(tmp_path)
    fake.directory["+79001234567"] = _max_user(7, "Папа", "", "https://i.oneme.ru/i?r=x")
    try:
        contact = await client.import_contact("+79001234567", "Папа")
    finally:
        await client.stop()

    assert contact is not None and contact.user_id == 7
    assert len(fake.imported) == 1
    assert len(fake.imported[0]) == 1, "one contact per import, never the phone book"
    assert fake.imported[0][0].phone == "+79001234567"
    assert fake.imported[0][0].first_name == "Папа"


# ------------------------------------------------------------ session identity


def test_an_identity_is_drawn_once_and_then_read(tmp_path: Path) -> None:
    """PyMax re-rolls its whole user agent in `Client.__init__` and never saves
    it, so a restart comes back as a different phone on the same token. Ours is
    drawn once and lives next to the session database."""
    from bridge.max_client.identity import DEVICES, load_or_create

    first = load_or_create(tmp_path, timezone="Asia/Novosibirsk")
    assert (first.device_name, first.os_version, first.screen, first.arch) in DEVICES
    assert first.timezone == "Asia/Novosibirsk"  # the owner's own, not a default
    assert load_or_create(tmp_path) == first  # never re-drawn
    assert (tmp_path / "identity.json").exists()


def test_an_existing_session_keeps_the_identity_it_was_created_with(tmp_path: Path) -> None:
    """Upgrading to per-install identities must not turn a live account into a
    different phone — that reads as a stolen session, not as a user."""
    from bridge.max_client.identity import LEGACY_IDENTITY, load_or_create

    identity = load_or_create(tmp_path, session_exists=True)
    for field, value in LEGACY_IDENTITY.items():
        assert getattr(identity, field) == value


def test_two_installs_do_not_share_one_fingerprint(tmp_path: Path) -> None:
    """The whole point of moving the device out of the source: several people
    running the bridge must not all report a byte-identical phone."""
    from bridge.max_client.identity import load_or_create

    identities = []
    for n in range(8):
        directory = tmp_path / str(n)
        directory.mkdir()
        identities.append(load_or_create(directory, timezone="Europe/Moscow"))

    assert len({i.device_id for i in identities}) == 8  # every install its own
    assert len({i.device_name for i in identities}) > 1  # and not all one phone


def test_the_upload_header_describes_the_same_phone() -> None:
    """The control socket and the upload POST must not disagree about the device."""
    from bridge.max_client.client import NATIVE_MEDIA_APP_VERSION
    from bridge.max_client.identity import reference_identity

    identity = reference_identity()
    header = identity.upload_user_agent(app_version=NATIVE_MEDIA_APP_VERSION)
    assert NATIVE_MEDIA_APP_VERSION in header
    assert identity.os_version in header
    assert identity.device_name in header
    assert identity.screen in header


def test_the_client_session_id_counts_up_and_survives(tmp_path: Path) -> None:
    """The app keeps a launch counter (`app.stats.session.id`), so ours does too:
    monotonic and persisted. PyMax's `randint(1, 70)` would walk backwards."""
    from bridge.max_client.client import next_client_session_id

    assert next_client_session_id(tmp_path) == 1
    assert next_client_session_id(tmp_path) == 2
    assert (tmp_path / "client_session_id").read_text().strip() == "2"


def test_a_broken_counter_file_does_not_break_the_connect(tmp_path: Path) -> None:
    """A file with nothing recoverable behind it starts over — honestly.

    This used to be the *whole* contract, and it contradicted the reason the
    counter exists: a torn write sent a long-lived install from 43 back to 1,
    which is exactly the walking-backwards signal PyMax's `randint` was replaced
    to avoid. Now that only happens when there is genuinely no history to read;
    a torn write recovers from the copy (`test_identity_state.py`).
    """
    (tmp_path / "client_session_id").write_text("not a number")
    from bridge.max_client.client import next_client_session_id

    assert next_client_session_id(tmp_path) == 1


def test_the_device_id_looks_like_an_android_id(tmp_path: Path) -> None:
    """The app sends `Settings.Secure.ANDROID_ID` — 16 lowercase hex characters.
    PyMax sends `uuid4()`, which no Android install would."""
    from bridge.max_client.identity import load_or_create

    device_id = load_or_create(tmp_path).device_id
    assert len(device_id) == 16
    assert all(c in "0123456789abcdef" for c in device_id)
    assert load_or_create(tmp_path).device_id == device_id  # stable across restarts


def test_the_app_version_stays_above_the_native_media_gate() -> None:
    """The one identity field the uploader keys on. Measured on the live server:
    26.15.0 gets a voice on ONE_ME but a circle on the OK CDN, 26.16.0 gets both.
    Lower this and circles silently stop being circles."""
    from bridge.max_client.client import (
        MIN_NATIVE_MEDIA_APP_VERSION,
        NATIVE_MEDIA_APP_VERSION,
    )

    parts = tuple(int(p) for p in NATIVE_MEDIA_APP_VERSION.split(".")[:2])
    assert parts >= MIN_NATIVE_MEDIA_APP_VERSION
