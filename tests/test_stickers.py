"""W5 — Telegram stickers reaching MAX as the one thing it stores: a PNG.

Two rules are load-bearing and neither is obvious from the code alone:

* **PNG, not WebP.** MAX's sticker upload answers WebP with a bare
  `server.error` (measured 2026-08-02). Everything here exists to guarantee the
  bytes handed to MAX are a PNG whatever Telegram sent.
* **Transparency survives.** A sticker is drawn onto the chat background. One
  flattened onto black arrives as a black tile with a picture inside it, which
  is worse than not sending it.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from bridge.max_client import AttachmentKind, MaxAttachment
from bridge.media.sources import sticker_is_animated, sticker_url
from bridge.media.stickers import (
    StickerConversionError,
    StickerFormat,
    detect_format,
    sticker_to_png,
    to_telegram_sticker,
)
from bridge.media.upload import UploadKind, plan_upload


def _rgba_square(path: Path, fmt: str) -> None:
    """A square with a genuinely transparent corner, so alpha loss is visible."""
    image = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
    for x in range(40, 120):
        for y in range(40, 120):
            image.putpixel((x, y), (200, 40, 40, 255))
    image.save(path, fmt)


def _lottie_tgs(path: Path) -> None:
    animation = {
        "v": "5.5.7", "fr": 60, "ip": 0, "op": 60, "w": 512, "h": 512,
        "nm": "t", "ddd": 0, "assets": [],
        "layers": [{
            "ddd": 0, "ind": 1, "ty": 4, "nm": "c", "sr": 1,
            "ks": {
                "o": {"a": 0, "k": 100}, "r": {"a": 0, "k": 0},
                "p": {"a": 0, "k": [256, 256, 0]}, "a": {"a": 0, "k": [0, 0, 0]},
                "s": {"a": 0, "k": [100, 100, 100]},
            },
            "ao": 0,
            "shapes": [
                {"ty": "el", "p": {"a": 0, "k": [0, 0]}, "s": {"a": 0, "k": [400, 400]}, "nm": "e"},
                {"ty": "fl", "c": {"a": 0, "k": [0.9, 0.2, 0.2, 1]},
                 "o": {"a": 0, "k": 100}, "nm": "f"},
            ],
            "ip": 0, "op": 60, "st": 0, "bm": 0,
        }],
    }
    path.write_bytes(gzip.compress(json.dumps(animation).encode()))


def _webm(path: Path) -> None:
    av = pytest.importorskip("av")
    with av.open(str(path), "w") as container:
        stream = container.add_stream("libvpx-vp9", rate=30)
        stream.width, stream.height, stream.pix_fmt = 120, 120, "yuv420p"
        for index in range(12):
            frame = Image.new("RGB", (120, 120), (10 + index * 4, 20, 30))
            container.mux(stream.encode(av.VideoFrame.from_image(frame)))
        container.mux(stream.encode())


# ------------------------------------------------------------------- detection


def test_format_is_read_from_the_bytes_not_the_name(tmp_path: Path) -> None:
    """Telegram's naming is not a contract; the magic bytes are."""
    webp = tmp_path / "anything.bin"
    _rgba_square(webp, "WEBP")
    tgs = tmp_path / "also.bin"
    _lottie_tgs(tgs)

    assert detect_format(webp.read_bytes()[:16]) is StickerFormat.WEBP
    assert detect_format(tgs.read_bytes()[:16]) is StickerFormat.TGS
    assert detect_format(b"\x1a\x45\xdf\xa3rest") is StickerFormat.WEBM
    assert detect_format(b"\x89PNG\r\n\x1a\n") is StickerFormat.STILL


# ------------------------------------------------------------------ conversion


def test_a_webp_sticker_becomes_a_png(tmp_path: Path) -> None:
    source, target = tmp_path / "s.webp", tmp_path / "out.png"
    _rgba_square(source, "WEBP")

    assert sticker_to_png(source, target) is StickerFormat.WEBP
    with Image.open(target) as image:
        assert image.format == "PNG"
        assert image.mode == "RGBA"


def test_transparency_survives_the_conversion(tmp_path: Path) -> None:
    """The whole point: MAX draws a sticker straight onto the chat background."""
    source, target = tmp_path / "s.webp", tmp_path / "out.png"
    # Already the accepted size, so nothing is padded and the coordinates below
    # mean what they say.
    image = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    image.paste(Image.new("RGBA", (312, 312), (200, 40, 40, 255)), (200, 200))
    image.save(source, "WEBP")

    sticker_to_png(source, target)
    with Image.open(target) as result:
        rgba = result.convert("RGBA")
        assert rgba.getpixel((5, 5))[3] == 0, "the clear corner went opaque"
        assert rgba.getpixel((400, 400))[3] == 255


def test_an_animated_tgs_is_rendered_to_a_frame(tmp_path: Path) -> None:
    pytest.importorskip("rlottie_python")
    source, target = tmp_path / "s.tgs", tmp_path / "out.png"
    _lottie_tgs(source)

    assert sticker_to_png(source, target) is StickerFormat.TGS
    with Image.open(target) as image:
        assert image.format == "PNG"
        # Rendered, not blank: the middle of the canvas holds the circle.
        assert image.convert("RGBA").getpixel((256, 256))[3] > 0


def test_a_video_sticker_is_decoded_to_a_frame(tmp_path: Path) -> None:
    source, target = tmp_path / "s.webm", tmp_path / "out.png"
    _webm(source)

    assert sticker_to_png(source, target) is StickerFormat.WEBM
    with Image.open(target) as image:
        assert image.format == "PNG"
        # Padded up to what MAX will accept, like every other sticker.
        assert image.size == (512, 512)


def test_a_non_square_sticker_is_padded_to_the_size_max_accepts(tmp_path: Path) -> None:
    """MAX refuses anything but a large square, and only says so after upload.

    Measured 2026-08-02 against op193: 512x512 and 500x500 accepted; 446x512
    refused as `sticker.invalid.size` for not being square; 256x256 and 170x170
    refused for being small; 600x600 refused by the upload itself. Telegram
    stickers are 512 on the long side and anything on the short one — 446x512 is
    an ordinary real one — so without this most stickers would never arrive.
    """
    source, target = tmp_path / "s.webp", tmp_path / "out.png"
    image = Image.new("RGBA", (446, 512), (0, 0, 0, 0))
    image.paste(Image.new("RGBA", (200, 200), (200, 40, 40, 255)), (100, 100))
    image.save(source, "WEBP")

    sticker_to_png(source, target)

    with Image.open(target) as result:
        assert result.size == (512, 512)


def test_padding_does_not_stretch_the_drawing(tmp_path: Path) -> None:
    """A squashed sticker would be worse than a padded one, and just as wrong."""
    source, target = tmp_path / "s.webp", tmp_path / "out.png"
    # Wide and short: half the canvas height, so the padding is unmistakable.
    image = Image.new("RGBA", (512, 256), (0, 0, 0, 0))
    image.paste(Image.new("RGBA", (512, 256), (200, 40, 40, 255)), (0, 0))
    image.save(source, "WEBP")

    sticker_to_png(source, target)

    with Image.open(target) as result:
        rgba = result.convert("RGBA")
        assert rgba.size == (512, 512)
        # The drawing keeps its 2:1 shape: opaque across the middle band,
        # transparent above and below it.
        assert rgba.getpixel((256, 256))[3] == 255
        assert rgba.getpixel((256, 10))[3] == 0
        assert rgba.getpixel((256, 502))[3] == 0


def test_a_corrupt_sticker_is_an_error_not_a_crash(tmp_path: Path) -> None:
    """The caller degrades to a notice; it needs one exception type to catch."""
    source, target = tmp_path / "s.tgs", tmp_path / "out.png"
    source.write_bytes(b"\x1f\x8b" + b"not actually gzip")

    with pytest.raises(StickerConversionError):
        sticker_to_png(source, target)


# ------------------------------------------------------------ MAX -> Telegram


def _max_sticker(**extra: object) -> MaxAttachment:
    raw = {
        "_type": "STICKER", "stickerId": 23000000001, "width": 170, "height": 170,
        "url": "https://i.oneme.ru/getSmile?smileId=59383e44e&smileType=4",
        **extra,
    }
    return MaxAttachment(kind=AttachmentKind.STICKER, raw=raw)


def test_an_animated_max_sticker_is_fetched_as_its_lottie() -> None:
    """MAX's own packs are Lottie, and Lottie gzipped *is* Telegram's `.tgs`.

    Measured 2026-08-02: a real `lottieUrl` served 512x512, 60 fps, exactly
    3.00 s, 11.5 KB gzipped — the `.tgs` specification line for line. Telegram
    took the bytes unmodified and answered `is_animated: true`. So preferring
    this field is what carries the animation across; the still `url` would
    silently flatten every MAX sticker there is.
    """
    attachment = _max_sticker(
        stickerType="LOTTIE",
        setId=221006,
        lottieUrl="https://fd.oneme.ru/getfile?rq=AF3O5oQh",
    )

    assert sticker_is_animated(attachment) is True
    assert sticker_url(attachment) == "https://fd.oneme.ru/getfile?rq=AF3O5oQh"


def test_a_static_max_sticker_still_uses_its_picture() -> None:
    attachment = _max_sticker(stickerType="STATIC")

    assert sticker_is_animated(attachment) is False
    assert sticker_url(attachment) == "https://i.oneme.ru/getSmile?smileId=59383e44e&smileType=4"


def test_a_lottie_url_alone_is_enough_to_count_as_animated() -> None:
    """`stickerType` is not always there; the field beside it says the same."""
    attachment = _max_sticker(lottieUrl="https://fd.oneme.ru/getfile?rq=x")

    assert sticker_is_animated(attachment) is True


async def test_a_lottie_sticker_is_downloaded_as_tgs_and_not_rejected(tmp_path: Path) -> None:
    """The two things the pipeline would otherwise get wrong on a `.tgs`.

    Gzip is not an image, so the sticker kind's content check would throw the
    animation away; and `sendSticker` reads the extension, so a file not named
    `.tgs` is refused even when the bytes are right.
    """
    from test_media import FakeProtocol, FakeSession, fetcher_for

    from bridge.media import MaxMediaSources, MediaPipeline, TempFiles

    body = gzip.compress(b'{"v":"5.5.2","w":512,"h":512,"fr":60,"op":180}')
    pipeline = MediaPipeline(
        sources=MaxMediaSources(FakeProtocol()),
        temp_files=TempFiles(tmp_path / "tmp"),
        fetcher=fetcher_for(FakeSession(body=body, headers={"Content-Type": "application/gzip"})),
        max_file_size_mb=1,
    )
    attachment = _max_sticker(
        stickerType="LOTTIE", lottieUrl="https://fd.oneme.ru/getfile?rq=AF3O5oQh"
    )

    async with pipeline.fetch_from_max(attachment, chat_id=1, message_id=2) as local:
        assert local.path.suffix == ".tgs"
        assert local.path.read_bytes() == body


# ------------------------------------------- what Bot API silently will not take


def test_a_bare_lottie_is_gzipped_before_it_reaches_telegram(tmp_path: Path) -> None:
    """The bug that produced empty stickers in a real chat.

    MAX serves the Lottie with `Content-Encoding: gzip`, so any HTTP client
    unwraps it and leaves bare JSON. Telegram then answers `ok: true` and files
    it as a *document* of type `application/x-bad-tgsticker` — no exception, no
    fallback, an empty square. Re-gzipping is what makes it a sticker again.
    """
    body = b'{"tgs":1,"v":"5.5.2","w":512,"h":512,"fr":60,"op":180}'
    path = tmp_path / "sticker.tgs"
    path.write_bytes(body)

    name = to_telegram_sticker(path, animated=True)

    assert name == "sticker.tgs"
    assert path.read_bytes()[:2] == b"\x1f\x8b"
    assert gzip.decompress(path.read_bytes()) == body


def test_an_already_gzipped_lottie_is_left_alone(tmp_path: Path) -> None:
    """Double-gzipping would break it exactly as thoroughly as not gzipping."""
    body = gzip.compress(b'{"tgs":1}')
    path = tmp_path / "sticker.tgs"
    path.write_bytes(body)

    to_telegram_sticker(path, animated=True)

    assert path.read_bytes() == body


def test_a_png_still_becomes_webp(tmp_path: Path) -> None:
    """MAX's `getSmile` serves PNG; `sendSticker` takes only WebP for stills."""
    path = tmp_path / "sticker.webp"
    _rgba_square(path, "PNG")

    name = to_telegram_sticker(path, animated=False)

    assert name == "sticker.webp"
    payload = path.read_bytes()
    assert payload[:4] == b"RIFF" and payload[8:12] == b"WEBP"


def test_transparency_survives_the_webp_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "sticker.webp"
    _rgba_square(path, "PNG")

    to_telegram_sticker(path, animated=False)

    with Image.open(path) as image:
        assert image.convert("RGBA").getpixel((5, 5))[3] == 0


# ------------------------------------------------------------------ round trip


async def test_a_max_sticker_sent_back_returns_as_itself(tmp_path: Path) -> None:
    """The one way animation survives Telegram -> MAX.

    A custom sticker cannot be animated — MAX re-encodes any upload to a single
    frame. But `MSG_SEND` accepts a `stickerId` the account does not own, so a
    sticker the bridge carried *out* of MAX can go back as the original. It is
    recognised by the bytes: Telegram hands the same file back byte for byte
    (verified 2026-08-02), so no id has to travel through the delivery queue.
    """
    from bridge.storage import Database, StickerOriginRepository

    db = await Database.connect(tmp_path / "b.db")
    try:
        origins = StickerOriginRepository(db)
        source = tmp_path / "sticker.tgs"
        source.write_bytes(gzip.compress(b'{"tgs":1,"v":"5.5.2"}'))
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        await origins.put(digest, 23000000001)

        assert await origins.get(digest) == 23000000001
        # A sticker we never carried is not claimed as one of MAX's.
        assert await origins.get("0" * 64) is None
    finally:
        await db.close()


async def test_the_round_trip_key_is_the_bytes_before_conversion(tmp_path: Path) -> None:
    """Hashing the converted PNG instead would never match.

    Conversion is exactly what destroys the animation, so the lookup has to
    happen on the file as Telegram sent it, not on what we would have made of it.
    """
    source, converted = tmp_path / "s.tgs", tmp_path / "s.png"
    source.write_bytes(gzip.compress(b'{"tgs":1,"v":"5.5.2","w":512,"h":512,"fr":60,"op":180}'))

    before = hashlib.sha256(source.read_bytes()).hexdigest()
    sticker_to_png(source, converted)
    after = hashlib.sha256(converted.read_bytes()).hexdigest()

    assert before != after


# ---------------------------------------------------------------------- planning


def test_a_sticker_is_planned_as_a_sticker_not_a_photo() -> None:
    item = plan_upload(sticker=True, file_id="A", file_name="sticker.webp", size=9)

    assert item.kind is UploadKind.STICKER
    # A static sticker crosses intact, so there is nothing to apologise for.
    assert item.degraded_notice is None


def test_a_sticker_message_is_planned_as_one(tmp_path: Path) -> None:
    """`describe` has to see the sticker; nothing else in the chain runs otherwise."""
    from bridge.routing.upload_router import describe

    class _Sticker:
        file_id = "AAA"
        file_unique_id = "U1"
        file_size = 4096
        is_animated = False
        is_video = False

    class _Message:
        photo = None
        video = None
        video_note = None
        voice = None
        audio = None
        document = None
        animation = None
        sticker = _Sticker()

    item = describe(_Message())  # type: ignore[arg-type]
    assert item is not None
    assert item.kind is UploadKind.STICKER
    assert item.file_name.endswith(".webp")


def test_an_animated_sticker_is_named_tgs_so_the_converter_can_tell(tmp_path: Path) -> None:
    from bridge.routing.upload_router import describe

    class _Sticker:
        file_id = "AAA"
        file_unique_id = "U1"
        file_size = 4096
        is_animated = True
        is_video = False

    class _Message:
        photo = None
        video = None
        video_note = None
        voice = None
        audio = None
        document = None
        animation = None
        sticker = _Sticker()

    item = describe(_Message())  # type: ignore[arg-type]
    assert item is not None
    assert item.file_name.endswith(".tgs")


def test_pymax_parses_a_setless_custom_sticker() -> None:
    """The bug that stops the account logging in, pinned.

    A sticker created by a user belongs to no set, so it arrives without
    `setId` — and PyMax declares that field required. Because the login answer
    is parsed through these models, one such sticker in the chat list made
    `LoginResponse` raise and the whole session fail to open. Measured against
    a real frame on 2026-08-02.
    """
    from pymax.types.domain.message import Message

    import bridge.max_client  # noqa: F401 - importing applies the repair

    frame = {
        "authorType": "USER", "_type": "STICKER", "width": 170, "height": 170,
        "time": 1700000002000, "stickerType": "STATIC", "audio": False,
        "url": "https://i.oneme.ru/getSmile?smileId=6f2b82c6e&smileType=4",
        "stickerId": 29841960046,
    }

    message = Message.model_validate(
        {"id": 1, "time": 1, "sender": 1, "type": "USER", "text": "", "attaches": [frame]}
    )
    assert type(message.attaches[0]).__name__ == "StickerAttachment"


def test_a_sticker_never_carries_a_degraded_notice() -> None:
    """No "ушёл кадром": an animated sticker that came from MAX returns as
    itself via the round trip, so the blanket warning was false in exactly the
    case that matters, and a genuine conversion still looks like a sticker."""
    animated = plan_upload(sticker=True, file_id="A", file_name="sticker.tgs", size=9)
    static = plan_upload(sticker=True, file_id="B", file_name="sticker.webp", size=9)

    assert animated.kind is UploadKind.STICKER
    assert animated.degraded_notice is None
    assert static.degraded_notice is None
