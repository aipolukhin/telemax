"""The cover Telegram shows for a video, drawn the right way up.

A phone films in landscape and writes a rotation into the container: the frames
are stored 1280×720 and a display matrix says "turn this 90° to play it". Every
player applies that matrix, which is why the video itself looks correct
everywhere. A *thumbnailer* that decodes one frame and stops does not — it gets
the stored frame, and the cover comes out lying on its side.

Telegram generates its own cover when a bot sends a video without one, and that
is exactly the cover it generates. Measured on the live bridge: a 43-second clip
declared 720×1280, `thumbs=[]` on the sent message, display matrix 90°, and a
sideways still in the chat above a video that plays upright.

So the bridge draws the cover itself, with the matrix applied. Nothing else about
the delivery changes: the file is still uploaded untouched, and the dimensions
sent alongside it were already the post-rotation ones.
"""

from __future__ import annotations

import io
import logging
import math
import struct
from pathlib import Path

logger = logging.getLogger(__name__)

#: Telegram's limits for a thumbnail: JPEG, no side over 320px, under 200 kB.
#: Exceeding any of them is not a soft failure — the API refuses the send.
THUMB_MAX_SIDE = 320
THUMB_MAX_BYTES = 200 * 1024


def _rotation_of(frame: object) -> int:
    """Degrees the stored frame must be turned by to be shown upright.

    The display matrix arrives as `int32_t[9]` in 16.16 fixed point, native byte
    order — the same shape `av_display_rotation_get` reads. Only the first two
    entries matter for a rotation, and only right angles are produced by
    cameras, so the answer is snapped to a quarter turn rather than trusted to
    the last degree.
    """
    side_data = getattr(frame, "side_data", None)
    if not side_data:
        return 0
    # PyAV's container yields the entries themselves rather than mapping a type
    # to a value — `container[entry]` raises — so each entry is both the key and
    # the bytes, and is read as such.
    for entry in side_data:
        if "DISPLAYMATRIX" not in str(getattr(entry, "type", "")).upper():
            continue
        raw = bytes(entry)
        if len(raw) < 36:
            return 0
        matrix = struct.unpack("=9i", raw[:36])
        degrees = math.degrees(math.atan2(matrix[1] / 65536.0, matrix[0] / 65536.0))
        return int(round(degrees / 90.0) * 90) % 360
    return 0


def video_thumbnail(path: Path) -> bytes | None:
    """One JPEG cover for a video file, upright, within Telegram's limits.

    None whenever anything is not straightforward — an unreadable container, a
    codec that will not decode, no frames at all. The caller then sends the video
    without a cover, which is what it did before this existed: a missing cover is
    a smaller problem than a wrong one, and neither is worth failing a delivery.
    """
    try:
        import av
    except Exception:
        logger.debug("no decoder available for a video cover", exc_info=True)
        return None

    try:
        with av.open(str(path)) as container:
            streams = container.streams.video
            if not streams:
                return None
            frame = next(container.decode(streams[0]), None)
            if frame is None:
                return None
            rotation = _rotation_of(frame)
            # Same shape `stickers.py` uses: PyAV ships no annotation for it.
            image = frame.to_image()  # type: ignore[no-untyped-call]
    except Exception:
        logger.debug("could not decode a cover for %s", path.name, exc_info=True)
        return None

    if rotation:
        # Pillow rotates counter-clockwise; the matrix says how far the stored
        # frame is *from* upright, so the correction runs the other way.
        image = image.rotate(-rotation, expand=True)
    image.thumbnail((THUMB_MAX_SIDE, THUMB_MAX_SIDE))

    for quality in (82, 70, 55, 40):
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, "JPEG", quality=quality, optimize=True)
        body = buffer.getvalue()
        if len(body) <= THUMB_MAX_BYTES:
            return body
    logger.debug("cover for %s stayed over the size limit", path.name)
    return None
