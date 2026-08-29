"""A video's cover, drawn upright rather than left to Telegram.

Reported from the live bridge: the last video in one of the chats played the
right way up and had a cover lying on its side. The file explains it — a phone
capture, frames stored 1280×720, a display matrix saying "turn this 90°", and
`thumbs=[]` on the message the bot sent. Players apply the matrix; a thumbnailer
that decodes one frame does not, so Telegram's own cover came out sideways.

The dimensions were never wrong: 720×1280 was already the post-rotation size, so
the box in the chat was the right shape and only the picture inside it was
turned. That is why this fixes the cover and touches nothing else about the send.
"""

from __future__ import annotations

import io
import struct
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from bridge.max_client import AttachmentKind, MaxAttachment
from bridge.media.delivery import OutgoingMedia
from bridge.media.thumbs import THUMB_MAX_BYTES, THUMB_MAX_SIDE, _rotation_of, video_thumbnail


def _matrix(a: int, b: int) -> bytes:
    """A display matrix side-data blob, the way ffmpeg lays it out."""
    values = [a * 65536, b * 65536, 0, -b * 65536, a * 65536, 0, 0, 0, 1 << 30]
    return struct.pack("=9i", *values)


class _Entry(bytes):
    """PyAV hands back the side-data entry itself, not a value behind a key."""

    type = "Type.DISPLAYMATRIX"


class _Frame:
    def __init__(self, entries: list[Any]) -> None:
        self.side_data = entries


def test_a_quarter_turn_is_read_off_the_matrix() -> None:
    """The exact numbers measured on the reported file: a=0, b=1 → 90°."""
    frame = _Frame([_Entry(_matrix(0, 1))])
    assert _rotation_of(frame) == 90


def test_an_upright_video_reports_no_turn() -> None:
    assert _rotation_of(_Frame([_Entry(_matrix(1, 0))])) == 0


def test_a_half_turn_is_read_too() -> None:
    assert _rotation_of(_Frame([_Entry(_matrix(-1, 0))])) == 180


def test_a_frame_with_no_side_data_reports_no_turn() -> None:
    assert _rotation_of(_Frame([])) == 0
    assert _rotation_of(object()) == 0


def test_a_truncated_matrix_is_not_guessed_at() -> None:
    class Short(bytes):
        type = "Type.DISPLAYMATRIX"

    assert _rotation_of(_Frame([Short(b"\x00" * 8)])) == 0


def test_other_side_data_is_ignored() -> None:
    class Other(bytes):
        type = "Type.MOTION_VECTORS"

    assert _rotation_of(_Frame([Other(_matrix(0, 1))])) == 0


# --------------------------------------------------------------- the real file


def _rotated_sample(tmp_path: Path, *, rotation: int) -> Path:
    """A tiny landscape clip that declares itself rotated, like a phone's."""
    av = pytest.importorskip("av")
    path = tmp_path / f"clip{rotation}.mp4"
    container = av.open(str(path), mode="w")
    stream = container.add_stream("mpeg4", rate=5)
    stream.width, stream.height = 160, 90
    stream.pix_fmt = "yuv420p"
    if rotation:
        stream.set_display_rotation(rotation)
    for index in range(3):
        image = Image.new("RGB", (160, 90), (20 * index, 90, 160))
        frame = av.VideoFrame.from_image(image)
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    return path


def test_a_rotated_clip_gets_an_upright_cover(tmp_path: Path) -> None:
    """The whole point: stored landscape, declared rotated, cover comes back tall."""
    pytest.importorskip("av")
    body = video_thumbnail(_rotated_sample(tmp_path, rotation=90))
    assert body is not None
    cover = Image.open(io.BytesIO(body))
    assert cover.height > cover.width, "a rotated clip must not keep a landscape cover"


def test_an_unrotated_clip_keeps_its_shape(tmp_path: Path) -> None:
    pytest.importorskip("av")
    body = video_thumbnail(_rotated_sample(tmp_path, rotation=0))
    assert body is not None
    cover = Image.open(io.BytesIO(body))
    assert cover.width > cover.height


def test_the_cover_stays_inside_telegrams_limits(tmp_path: Path) -> None:
    """Not style: the API refuses a thumbnail over 320px or 200 kB."""
    pytest.importorskip("av")
    body = video_thumbnail(_rotated_sample(tmp_path, rotation=90))
    assert body is not None
    cover = Image.open(io.BytesIO(body))
    assert max(cover.size) <= THUMB_MAX_SIDE
    assert len(body) <= THUMB_MAX_BYTES
    assert cover.format == "JPEG"


def test_a_file_that_is_not_a_video_gives_no_cover(tmp_path: Path) -> None:
    """A missing cover is a smaller problem than a wrong one, and never an error."""
    broken = tmp_path / "not-a-video.mp4"
    broken.write_bytes(b"certainly not an mp4")
    assert video_thumbnail(broken) is None
    assert video_thumbnail(tmp_path / "absent.mp4") is None


# ------------------------------------------------------------ into the delivery


async def test_only_videos_are_given_a_cover(tmp_path: Path) -> None:
    """A photo is its own thumbnail and a document does not get one, so neither
    is worth decoding for."""
    from bridge.media.delivery import MaxMediaDelivery
    from bridge.media.store import LocalFile

    delivery = MaxMediaDelivery(pipeline=None, sender=None)  # type: ignore[arg-type]
    sample = _rotated_sample(tmp_path, rotation=90)
    local = LocalFile(path=sample, display_name="video.mp4", size=sample.stat().st_size)

    video = await delivery._cover(MaxAttachment(kind=AttachmentKind.VIDEO, raw={}), local)
    photo = await delivery._cover(MaxAttachment(kind=AttachmentKind.PHOTO, raw={}), local)
    document = await delivery._cover(MaxAttachment(kind=AttachmentKind.FILE, raw={}), local)

    assert video is not None
    assert photo is None and document is None


def test_the_adapter_wraps_the_cover_for_telegram() -> None:
    from bridge.routing.media_adapter import RegistryMediaSender

    with_cover = OutgoingMedia(
        kind=AttachmentKind.VIDEO,
        path=Path("/dev/null"),
        file_name="video.mp4",
        thumbnail=b"\xff\xd8jpeg",
    )
    without = OutgoingMedia(
        kind=AttachmentKind.VIDEO, path=Path("/dev/null"), file_name="video.mp4"
    )

    wrapped = RegistryMediaSender._cover(with_cover)
    assert wrapped is not None and wrapped.data == b"\xff\xd8jpeg"
    assert RegistryMediaSender._cover(without) is None, "no cover means the old behaviour"


# ------------------------------------------------- the owner's own placements


async def test_a_video_placed_as_the_owner_carries_the_cover_too() -> None:
    """The same sideways cover, on the other transport.

    A video the owner sent in MAX is placed by their own session rather than by
    the bot, and Telegram draws its own cover there for exactly the same reason —
    so the fix has to travel with the media, not with the transport.
    """
    from bridge.routing.owner_voice import MtprotoOwnerSender

    placed: list[dict[str, Any]] = []

    class Session:
        is_connected = True

        async def send_own_message(self, *a: Any, **k: Any) -> int | None:
            return 1

        async def send_own_file(self, peer_id: int, path: str, **kwargs: Any) -> int | None:
            placed.append(kwargs)
            return 900

    media = OutgoingMedia(
        kind=AttachmentKind.VIDEO,
        path=Path("/dev/null"),
        file_name="video.mp4",
        thumbnail=b"\xff\xd8jpeg",
    )
    sender = MtprotoOwnerSender(session=lambda: Session())
    await sender.send_video(0, 4242, media, reply_to=None)

    assert placed[0]["thumbnail"] == b"\xff\xd8jpeg"
    assert placed[0]["kind"] == "video"


async def test_a_placement_without_a_cover_still_goes() -> None:
    """No cover is the old behaviour, not a refusal."""
    from bridge.routing.owner_voice import MtprotoOwnerSender

    placed: list[dict[str, Any]] = []

    class Session:
        is_connected = True

        async def send_own_message(self, *a: Any, **k: Any) -> int | None:
            return 1

        async def send_own_file(self, peer_id: int, path: str, **kwargs: Any) -> int | None:
            placed.append(kwargs)
            return 900

    media = OutgoingMedia(
        kind=AttachmentKind.FILE, path=Path("/dev/null"), file_name="doc.pdf"
    )
    sender = MtprotoOwnerSender(session=lambda: Session())
    assert await sender.send_document(0, 4242, media, reply_to=None) == 900
    assert placed[0]["thumbnail"] is None


async def test_the_session_hands_the_cover_to_telethon() -> None:
    """As bytes, and with no `file_size` alongside it — Telethon would otherwise
    be told the video's size and upload the cover as if it were that big."""
    from types import SimpleNamespace

    from bridge.telegram.user_session import TelegramUserSession

    calls: list[dict[str, Any]] = []

    class FakeClient:
        async def send_file(self, peer: int, path: str, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return SimpleNamespace(id=901)

    async def connect() -> Any:
        raise AssertionError("unused")

    session = TelegramUserSession(
        connect=connect,
        owner_user_id=1,
        health=SimpleNamespace(),
        allowed_bot_ids=lambda: None,  # type: ignore[arg-type]
    )
    session._client = FakeClient()  # the client under test, reached directly

    await session.send_own_file(4242, "/tmp/clip.mp4", kind="video", thumbnail=b"\xff\xd8jpeg")  # noqa: S108

    assert calls[0]["thumb"] == b"\xff\xd8jpeg"
    assert "file_size" not in calls[0]
