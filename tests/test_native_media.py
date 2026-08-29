"""R7 — a voice message and a video note sent natively into MAX.

The protocol was represented by compatibility fixtures: opcode 82 hands
out an upload URL keyed by `type` (2 = voice → au.oneme.ru, 1 = circle →
vu.oneme.ru), the file is POSTed there as octet-stream, and a hand-built AUDIO /
VIDEO(videoType:1) attach goes out on opcode 64. These tests pin the frame
shapes and the three-step flow so a wrong field — the kind that drops the MAX
connection rather than degrading — cannot creep back in.
"""

from __future__ import annotations

import array
import asyncio
from pathlib import Path
from typing import Any

import pytest

from bridge.max_client.client import MaxClient, MaxMediaError
from bridge.max_client.opcodes import (
    MediaUploadType,
    Opcode,
    circle_attach,
    media_upload_frame,
    native_media_frame,
    voice_attach,
)
from bridge.media import native_max

# --------------------------------------------------------------- frame builders


def test_media_upload_request_is_typed_by_kind() -> None:
    assert media_upload_frame(MediaUploadType.AUDIO) == {
        "uploaderType": 1,
        "type": 2,
        "count": 1,
    }
    assert media_upload_frame(MediaUploadType.VIDEO) == {
        "uploaderType": 1,
        "type": 1,
        "count": 1,
    }


def test_voice_attach_shape() -> None:
    attach = voice_attach(duration_ms=5060, wave=b"\x00\x7f\xff", token="tok")
    assert attach == {
        "_type": "AUDIO",
        "duration": 5060,
        "wave": b"\x00\x7f\xff",
        "token": "tok",
    }
    # wave stays bytes so PyMax's codec packs it as msgpack `bin`, not a string.
    assert isinstance(attach["wave"], bytes)


def test_circle_attach_carries_the_videotype_marker() -> None:
    attach = circle_attach(duration_ms=3000, wave=b"\x10", token="tok")
    assert attach["_type"] == "VIDEO"
    assert attach["videoType"] == 1
    assert attach["token"] == "tok"


def test_native_media_frame_envelope() -> None:
    frame = native_media_frame(42, voice_attach(duration_ms=1, wave=b"", token="t"))
    assert frame["chatId"] == 42
    assert frame["notify"] is True
    message = frame["message"]
    assert isinstance(message, dict)
    assert message["cid"] < 0  # negative millisecond clock the server echoes
    assert message["attaches"][0]["_type"] == "AUDIO"
    assert "link" not in message


def test_native_media_frame_reply_link() -> None:
    frame = native_media_frame(1, circle_attach(duration_ms=1, wave=b"", token="t"), reply_to=99)
    assert frame["message"]["link"] == {"type": "REPLY", "messageId": 99}


# ------------------------------------------------------------------- send flow


class _Recorder:
    """A MaxClient wired so `_send_native_media` runs against fakes.

    `fake_post` takes exactly what `MaxClient._post_media` takes. It used to
    accept a keyword `content_type` that production has never had — left over
    from `c9e71da`, whose change did not survive — and collect the values into a
    list nothing asserted on. A stand that accepts a signature the real function
    does not is a stand that will keep passing after the real one changes.
    """

    def __init__(self, tmp_path: Path, *, slot: dict[str, Any]) -> None:
        self.client = MaxClient(phone="1", session_dir=tmp_path, session_name="s")
        self.invokes: list[tuple[Opcode, dict[str, Any]]] = []
        self.posts: list[tuple[str, bytes, str]] = []
        self._slot = slot

        async def fake_invoke(opcode: Opcode, payload: dict[str, Any], **_: Any) -> Any:
            self.invokes.append((opcode, payload))
            if opcode is Opcode.MEDIA_UPLOAD:
                return self._slot
            return {"message": {"id": 777}}

        async def fake_post(url: str, data: bytes, filename: str) -> None:
            self.posts.append((url, data, filename))

        self.client._invoke = fake_invoke  # type: ignore[method-assign]
        self.client._post_media = fake_post  # type: ignore[method-assign]
        # `send_media` calls `_require()` first; make it look connected so the
        # routing tests reach the branch under test (the fakes stand in for the
        # real socket from there on).
        self.client._client = object()
        self.client._ready.set()


_SLOT = {"info": {"url": "https://au.oneme.ru/uploadAudio?signatureToken=x"}, "token": "THE_TOKEN"}


def test_voice_send_runs_op82_then_post_then_op64(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "bridge.media.native_max.probe_media", lambda p, kind="voice": (5060, b"\x01\x02")
    )
    source = tmp_path / "voice.ogg"
    source.write_bytes(b"OggS-bytes")
    rec = _Recorder(tmp_path, slot=_SLOT)

    message_id = asyncio.run(rec.client._send_native_media(42, "voice", source, "voice.ogg"))
    assert message_id == 777

    # 1) op82 asks for an AUDIO slot
    assert rec.invokes[0][0] is Opcode.MEDIA_UPLOAD
    assert rec.invokes[0][1] == {"uploaderType": 1, "type": 2, "count": 1}
    # 2) the bytes are POSTed to the handed-out URL
    assert rec.posts == [
        ("https://au.oneme.ru/uploadAudio?signatureToken=x", b"OggS-bytes", "voice.ogg")
    ]
    # 3) op64 sends an AUDIO attach carrying the slot's token, the probed
    #    duration and the probed wave
    op, payload = rec.invokes[1]
    assert op is Opcode.MSG_SEND
    attach = payload["message"]["attaches"][0]
    assert attach == {"_type": "AUDIO", "duration": 5060, "wave": b"\x01\x02", "token": "THE_TOKEN"}


def test_a_non_oneme_uploader_aborts_before_op64(tmp_path: Path, monkeypatch: Any) -> None:
    """If op82 hands back the OK CDN (the wrong client identity), bail before the
    POST and op64. Not because the CDN cannot carry a voice — it can, and the note
    saying otherwise was our own bug (`3553fcc`) — but because it transcodes and
    replaces `duration`/`wave` with its own, so that path is not handled by Telemax and
    ours. The parser still reads the list shape."""
    monkeypatch.setattr("bridge.media.native_max.probe_media", lambda p, kind="voice": (1, b""))
    source = tmp_path / "voice.ogg"
    source.write_bytes(b"x")
    slot = {"info": [{"url": "https://omu.okcdn.ru/upload.do?x", "videoId": 5, "token": "OKTOK"}]}
    rec = _Recorder(tmp_path, slot=slot)

    with pytest.raises(MaxMediaError):
        asyncio.run(rec.client._send_native_media(1, "voice", source, "voice.ogg"))
    assert rec.posts == []  # never uploaded
    assert len(rec.invokes) == 1  # only op82, never op64


def test_circle_send_asks_for_a_video_slot(tmp_path: Path, monkeypatch: Any) -> None:
    seen: dict[str, Any] = {}

    def _probe(path: Path, kind: str = "voice") -> tuple[int, bytes]:
        seen["kind"] = kind
        return 3000, b"\x09"

    monkeypatch.setattr("bridge.media.native_max.probe_media", _probe)
    source = tmp_path / "circle.mp4"
    source.write_bytes(b"video-bytes")
    slot = {"info": {"url": "https://vu.oneme.ru/uploadVideo?x"}, "token": "T2"}
    rec = _Recorder(tmp_path, slot=slot)

    asyncio.run(rec.client._send_native_media(1, "circle", source, "circle.mp4"))
    assert rec.invokes[0][1] == {"uploaderType": 1, "type": 1, "count": 1}
    attach = rec.invokes[1][1]["message"]["attaches"][0]
    assert attach["_type"] == "VIDEO" and attach["videoType"] == 1
    # a circle's amplitude is CameraX's peak, not AudioRecord's RMS
    assert seen["kind"] == "circle"


def test_a_missing_slot_is_an_error_not_a_broken_send(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr("bridge.media.native_max.probe_media", lambda p, kind="voice": (0, b""))
    source = tmp_path / "voice.ogg"
    source.write_bytes(b"x")
    rec = _Recorder(tmp_path, slot={"info": {}})  # no url, no token

    with pytest.raises(MaxMediaError):
        asyncio.run(rec.client._send_native_media(1, "voice", source, "voice.ogg"))
    assert rec.posts == []  # nothing uploaded when there is nowhere to upload


def test_send_media_routes_voice_to_the_native_path(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr("bridge.max_client.client.NATIVE_MEDIA_ENABLED", True)
    monkeypatch.setattr("bridge.max_client.client.NATIVE_VOICE_ENABLED", True)
    monkeypatch.setattr(
        "bridge.media.native_max.probe_media", lambda p, kind="voice": (1, bytes(80))
    )
    source = tmp_path / "voice.ogg"
    source.write_bytes(b"x")
    rec = _Recorder(tmp_path, slot=_SLOT)

    asyncio.run(rec.client.send_media(5, [("voice", source, "voice.ogg")]))
    assert rec.invokes[0][0] is Opcode.MEDIA_UPLOAD


def test_a_voice_cannot_ride_with_other_media(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr("bridge.max_client.client.NATIVE_MEDIA_ENABLED", True)
    monkeypatch.setattr("bridge.max_client.client.NATIVE_VOICE_ENABLED", True)
    rec = _Recorder(tmp_path, slot=_SLOT)
    a = tmp_path / "voice.ogg"
    a.write_bytes(b"x")
    b = tmp_path / "p.jpg"
    b.write_bytes(b"y")
    with pytest.raises(MaxMediaError):
        asyncio.run(rec.client.send_media(1, [("voice", a, "voice.ogg"), ("photo", b, "p.jpg")]))


def test_native_media_off_degrades_voice_to_a_file(tmp_path: Path, monkeypatch: Any) -> None:
    """With the flag off (prod), a voice goes through PyMax as a file — no native
    opcode, no dropped connection."""
    monkeypatch.setattr("bridge.max_client.client.NATIVE_MEDIA_ENABLED", False)
    sent: dict[str, Any] = {}

    class _FakePyMax:
        async def send_message(self, **kwargs: Any) -> Any:
            sent.update(kwargs)
            return type("M", (), {"id": 5})()

    rec = _Recorder(tmp_path, slot=_SLOT)
    rec.client._client = _FakePyMax()
    rec.client._ready.set()
    source = tmp_path / "voice.ogg"
    source.write_bytes(b"x")

    asyncio.run(rec.client.send_media(1, [("voice", source, "voice.ogg")]))
    assert not rec.invokes  # never reached the native op82
    # went out as a PyMax File attachment
    assert type(sent["attachments"][0]).__name__ == "File"


# ------------------------------------------------------------------- waveform


def test_the_waveform_is_seven_bit() -> None:
    """The AUDIO validator refuses any bar above 127 — that was the whole of
    `Invalid media wave`. Full-scale input must land exactly on the ceiling."""
    loud = [native_max.FULL_SCALE] * 40
    raw = native_max._raw_wave([int(v) for v in loud])
    assert max(raw) == native_max.WAVE_MAX
    assert max(native_max._resample(native_max.WAVE_BARS, raw)) <= native_max.WAVE_MAX


def test_quiet_audio_is_not_stretched_to_full_scale() -> None:
    """The app caps the scale-up at 2x, so a quiet clip stays quiet instead of
    being normalised to the top."""
    quiet = [500] * 40
    raw = native_max._raw_wave(quiet)
    assert max(raw) == min(native_max.WAVE_MAX, int(500 * 2.0 / 256))


def test_a_circle_reads_peaks_and_a_voice_reads_rms() -> None:
    """One spike in an otherwise silent chunk: the peak reading sees it, the RMS
    reading averages it away."""
    window = array.array("h", [0] * 63 + [30000])
    peak = native_max._amplitude(window, 0, len(window), True)
    rms = native_max._amplitude(window, 0, len(window), False)
    assert peak == 30000
    assert rms < peak
    assert native_max._tick_value(peak) > native_max._tick_value(rms)


def test_a_soundless_circle_gets_a_full_run_of_zero_bars() -> None:
    """What the app sends when CameraX reports AUDIO_STATE_DISABLED: every bar 0,
    but `WAVE_BARS` of them — an empty wave is refused, an all-zero one is not."""
    wave = native_max._silent_wave()
    assert len(wave) == native_max.WAVE_BARS
    assert set(wave) == {0}


def test_resample_keeps_the_ends_and_zeroes_the_tail_pair() -> None:
    """`qb0.c` bounds-checks against len-1, so bars landing on the final source
    pair come out 0. Reproduced rather than fixed — real waveforms carry it."""
    src = bytes([10, 20, 30, 40])
    out = native_max._resample(8, src)
    assert out[0] == src[0]
    assert out[-1] == src[-1]
    assert out[-2] == 0


def test_a_note_without_duration_or_waveform_never_reaches_op64(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The server refuses a token-based attach missing either field with
    `attachment.video.not.supported`, and that rejection drops the MAX
    connection. Bail before the upload instead."""
    monkeypatch.setattr(
        "bridge.media.native_max.probe_media", lambda p, kind="voice": (0, bytes(80))
    )
    source = tmp_path / "voice.ogg"
    source.write_bytes(b"x")
    rec = _Recorder(tmp_path, slot=_SLOT)

    with pytest.raises(MaxMediaError):
        asyncio.run(rec.client._send_native_media(1, "voice", source, "voice.ogg"))
    assert rec.posts == []
    assert len(rec.invokes) == 1  # only op82


def test_a_tick_takes_the_loudest_read_not_the_last() -> None:
    """The recorder merges each read into its counter with `max` and the poll
    clears it, so a burst early in a 75 ms window still shows up on that tick."""
    quiet = array.array("h", [0] * 320)
    loud = array.array("h", [8000] * 320)
    assert native_max._amplitude(loud, 0, 320, False) > native_max._amplitude(quiet, 0, 320, False)
    assert native_max._tick_value(0) == 0
    assert native_max._tick_value(32768) == pytest.approx(32768, abs=1)


def test_a_non_oneme_uploader_degrades_instead_of_losing_the_message(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """If the server ever stops handing us the ONE_ME uploader, a voice must still
    arrive — as a file. Safe because the upload creates no message: an object on a
    CDN that no op64 referenced does not exist as far as the chat is concerned.
    (Not because every `MaxMediaError` predates the upload — an HTTP refusal
    arrives after the whole body was sent. See `test_native_media_faults`.)"""
    monkeypatch.setattr("bridge.max_client.client.NATIVE_MEDIA_ENABLED", True)
    monkeypatch.setattr("bridge.max_client.client.NATIVE_VOICE_ENABLED", True)
    monkeypatch.setattr(
        "bridge.media.native_max.probe_media", lambda p, kind="voice": (1000, bytes(80))
    )
    sent: dict[str, Any] = {}

    class _FakePyMax:
        async def send_message(self, **kwargs: Any) -> Any:
            sent.update(kwargs)
            return type("M", (), {"id": 9})()

    # op82 answers with the OK CDN — a path the bridge has no supported flow for.
    slot = {"info": {"url": "https://omu.okcdn.ru/upload.do?x"}, "token": "T"}
    rec = _Recorder(tmp_path, slot=slot)
    rec.client._client = _FakePyMax()
    rec.client._ready.set()
    source = tmp_path / "voice.ogg"
    source.write_bytes(b"x")

    assert asyncio.run(rec.client.send_media(1, [("voice", source, "voice.ogg")])) == 9
    assert rec.posts == []  # never uploaded to the wrong host
    assert len(rec.invokes) == 1  # op82 only, never op64
    assert type(sent["attachments"][0]).__name__ == "File"


def test_a_wrong_uploader_says_so_once_and_says_what_to_do(
    tmp_path: Path, monkeypatch: Any, caplog: Any
) -> None:
    """A compatibility failure names the host, version, and safe next action."""
    import logging

    monkeypatch.setattr("bridge.max_client.client.NATIVE_MEDIA_ENABLED", True)
    monkeypatch.setattr("bridge.max_client.client.NATIVE_VOICE_ENABLED", True)
    monkeypatch.setattr(
        "bridge.media.native_max.probe_media", lambda p, kind="voice": (1000, bytes(80))
    )

    class _FakePyMax:
        async def send_message(self, **kwargs: Any) -> Any:
            return type("M", (), {"id": 9})()

    slot = {"info": {"url": "https://omu.okcdn.ru/upload.do?x"}, "token": "T"}
    rec = _Recorder(tmp_path, slot=slot)
    rec.client._client = _FakePyMax()
    rec.client._ready.set()
    source = tmp_path / "voice.ogg"
    source.write_bytes(b"x")

    with caplog.at_level(logging.WARNING, logger="bridge.max_client.client"):
        asyncio.run(rec.client.send_media(1, [("voice", source, "voice.ogg")]))
        asyncio.run(rec.client.send_media(1, [("voice", source, "voice.ogg")]))

    loud = [r for r in caplog.records if "omu.okcdn.ru" in r.getMessage()]
    assert len(loud) == 1  # once per process, not once per voice
    message = loud[0].getMessage()
    assert "Upgrade Telemax" in message
    assert "26.16" in message
