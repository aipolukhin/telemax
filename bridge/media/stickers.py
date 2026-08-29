"""Telegram stickers, flattened into the one thing MAX will store: a PNG.

MAX takes a custom sticker as a still image and nothing else. Telegram, meanwhile,
sends stickers in three formats, none of which is PNG:

  `.webp`   static, and the common case
  `.tgs`    gzipped Lottie — Telegram's animated format, rendered by rlottie
  `.webm`   VP9 with an alpha channel — Telegram's video stickers

So every sticker is decoded here and re-encoded as PNG. The animated two lose
their motion, which is a real loss and not a shortcut: MAX has no format to put
it in on this path. What they must not lose is **transparency** — a sticker is
drawn straight onto the chat background, and a sticker flattened onto black
arrives as a black tile with a picture in it.

The frame taken from an animation is deliberately not the first one. Telegram
stickers almost always open on a scale-in, so frame zero is empty or nearly so;
a frame from partway through is the one a human would have picked as the
thumbnail.
"""

from __future__ import annotations

import gzip
import logging
from enum import StrEnum
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

#: How far into an animation to look for a representative frame. Past the
#: opening scale-in, before any closing one.
FRAME_POSITION = 0.4

#: What to render an animation at. MAX downscales to 170x170 on its side, so
#: this only has to be comfortably above that, not exact.
RENDER_SIZE = 512

#: Frame to settle for when a video says nothing about how long it is.
BLIND_FRAME_INDEX = 10


class StickerFormat(StrEnum):
    WEBP = "webp"
    TGS = "tgs"
    WEBM = "webm"
    STILL = "still"


class StickerConversionError(Exception):
    """The sticker could not be turned into a PNG."""


def detect_format(head: bytes) -> StickerFormat:
    """Classify by magic bytes rather than by file name.

    Telegram's own naming is not something to rely on: a `.webp` from the Bot
    API can be a video sticker when the set is one, and the bridge names its
    temp files itself anyway.
    """
    if head[:2] == b"\x1f\x8b":
        return StickerFormat.TGS
    if head[:4] == b"\x1a\x45\xdf\xa3":
        # EBML: Matroska, and every Telegram video sticker is a WebM in one.
        return StickerFormat.WEBM
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return StickerFormat.WEBP
    return StickerFormat.STILL


def sticker_to_png(source: Path, target: Path) -> StickerFormat:
    """Write `source` to `target` as a PNG MAX will accept. Returns the input format."""
    with source.open("rb") as handle:
        head = handle.read(16)

    kind = detect_format(head)
    try:
        if kind is StickerFormat.TGS:
            image = _from_lottie(source)
        elif kind is StickerFormat.WEBM:
            image = _from_video(source)
        else:
            image = _from_still(source)
    except StickerConversionError:
        raise
    except Exception as error:
        raise StickerConversionError(f"could not decode a {kind.value} sticker: {error}") from error

    # RGBA throughout: PNG keeps the alpha, and the alpha is most of what makes
    # a sticker look like one rather than like a small square photo.
    _fit_to_canvas(image.convert("RGBA")).save(target, "PNG", optimize=True)
    return kind


def _fit_to_canvas(image: Image.Image) -> Image.Image:
    """Centre the sticker on a transparent RENDER_SIZE square.

    MAX is strict about this, and says so only after the upload has succeeded —
    `op193` answers `sticker.invalid.size`. Compatibility tests confirmed:

        512x512  accepted      446x512  refused (not square)
        500x500  accepted      256x256  refused (square but too small)
                               170x170  refused
        600x600  refused by the upload itself, as `invalid.format`

    Telegram stickers are 512 on the long side and *anything* on the short one,
    so a plain pass-through is refused about as often as not. Padding rather
    than stretching keeps the drawing's proportions; the added margin is
    transparent, and MAX scales the result to 170x170 on its side regardless.
    """
    if image.size == (RENDER_SIZE, RENDER_SIZE):
        return image

    fitted = image.copy()
    fitted.thumbnail((RENDER_SIZE, RENDER_SIZE), Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", (RENDER_SIZE, RENDER_SIZE), (0, 0, 0, 0))
    canvas.paste(
        fitted,
        ((RENDER_SIZE - fitted.width) // 2, (RENDER_SIZE - fitted.height) // 2),
    )
    return canvas


def to_telegram_sticker(path: Path, *, animated: bool) -> str:
    """Make a downloaded MAX sticker into what `sendSticker` actually accepts.

    Both halves of this are things Telegram does not complain about, which is
    what makes them worth pinning down (confirmed in compatibility tests):

    * **An animated sticker must be gzipped.** MAX serves the Lottie with
      `Content-Encoding: gzip`, and any decent HTTP client — ours included —
      unwraps that transparently, leaving bare JSON. Telegram answers `ok:true`
      to such a file and quietly files it as a *document* of type
      `application/x-bad-tgsticker`. No error to catch, no fallback triggered,
      and an empty square in the chat. So the bytes are re-gzipped here if they
      arrived bare.
    * **A static sticker must be WebP.** MAX's `getSmile` serves PNG, and
      `sendSticker` takes only `.WEBP` for stills, so it would be refused and
      quietly demoted to a photo.

    Returns the file name Telegram should be given, extension included.
    """
    payload = path.read_bytes()

    if animated:
        if payload[:2] != b"\x1f\x8b":
            path.write_bytes(gzip.compress(payload, 9))
        return "sticker.tgs"

    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "sticker.webp"

    try:
        with Image.open(path) as image:
            image.load()
            converted = image.convert("RGBA")
        converted.save(path, "WEBP", lossless=True)
    except Exception:
        # Re-encoding is an improvement, not a precondition. If the bytes will
        # not open, the send still gets its chance — and the sender falls back
        # to a photo and then a document. Losing the message over a cosmetic
        # conversion would be much the worse trade.
        logger.debug("could not re-encode a sticker to WebP; sending it as it came",
                     exc_info=True)
        path.write_bytes(payload)
        return "sticker.png"

    return "sticker.webp"


def _from_still(source: Path) -> Image.Image:
    """WebP, PNG, anything Pillow opens. An animated WebP yields its first frame."""
    with Image.open(source) as image:
        image.load()
        return image.convert("RGBA")


def _from_lottie(source: Path) -> Image.Image:
    """`.tgs` — gzipped Lottie JSON, rendered by rlottie."""
    try:
        from rlottie_python import LottieAnimation
    except ImportError as error:  # pragma: no cover - dependency is declared
        raise StickerConversionError("rlottie-python is not installed") from error

    # `from_tgs` wants a path and does its own gunzip. Reading it here first
    # turns a corrupt file into a clear error instead of a bare rlottie crash.
    try:
        with gzip.open(source, "rb") as handle:
            handle.read(1)
    except OSError as error:
        raise StickerConversionError(f"not a valid .tgs: {error}") from error

    with LottieAnimation.from_tgs(str(source)) as animation:
        total = animation.lottie_animation_get_totalframe()
        frame = int(total * FRAME_POSITION) if total else 0
        image = animation.render_pillow_frame(
            frame_num=frame, width=RENDER_SIZE, height=RENDER_SIZE
        )

    if image is None:
        raise StickerConversionError("rlottie rendered nothing")
    return image


def _from_video(source: Path) -> Image.Image:
    """`.webm` — VP9 with alpha. PyAV carries its own ffmpeg, so no system one."""
    try:
        import av
    except ImportError as error:  # pragma: no cover - dependency is declared
        raise StickerConversionError("av is not installed") from error

    with av.open(str(source)) as container:
        if not container.streams.video:
            raise StickerConversionError("the sticker has no video stream")
        stream = container.streams.video[0]

        # `frames` is 0 for a stream the container did not count, which is the
        # normal case for a Telegram sticker.
        counted = stream.frames or 0
        wanted = int(counted * FRAME_POSITION) if counted else BLIND_FRAME_INDEX

        chosen = None
        for index, frame in enumerate(container.decode(video=0)):
            chosen = frame
            if index >= wanted:
                break

    if chosen is None:
        raise StickerConversionError("the sticker decoded to no frames at all")
    return _frame_to_image(chosen)


def _frame_to_image(frame: object) -> Image.Image:
    """RGBA out of a PyAV frame, without numpy.

    `frame.to_image()` is documented to give RGB, and dropping the alpha is
    exactly the thing this module exists to avoid. The packed RGBA plane is read
    directly instead — with its stride, which is not always `width * 4`.
    """
    rgba = frame.reformat(format="rgba")  # type: ignore[attr-defined]
    plane = rgba.planes[0]
    return Image.frombytes(
        "RGBA", (rgba.width, rgba.height), bytes(plane), "raw", "RGBA", plane.line_size, 1
    )
