"""Turning a MAX avatar into something Telegram will accept as a bot's face.

MAX serves avatars as **WebP** at whatever aspect ratio the person uploaded —
1440×1920 for a phone photo. Telegram's `setMyProfilePhoto` takes a *static*
photo that must be JPEG, and it answers a WebP with `PHOTO_CROP_SIZE_SMALL`,
which reads like a size complaint and is really a format one. That error cost a
round of guessing at query parameters, so it is written down here.

The crop is deliberate too. Telegram cuts a square itself, and a centre cut of a
3:4 portrait takes the chin: faces sit above the middle. So the square is taken
one third down instead of one half.
"""

from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)

#: Telegram refuses a profile photo whose square side is under this.
MIN_SIDE = 160

#: Enough for any avatar; larger only costs upload time.
MAX_SIDE = 1024

JPEG_QUALITY = 90

#: How far down the source the square starts, as a fraction of the slack. A
#: portrait cropped at the centre loses the face.
VERTICAL_BIAS = 3


class AvatarUnusableError(Exception):
    """The image cannot become a profile photo."""


def to_profile_jpeg(data: bytes) -> bytes:
    """Square, JPEG, and big enough — or a clear refusal."""
    try:
        from PIL import Image
    except ImportError as error:  # pragma: no cover - dependency is declared
        raise AvatarUnusableError("Pillow is not installed") from error

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as error:
        raise AvatarUnusableError(f"not a readable image: {error}") from error

    side = min(image.width, image.height)
    if side < MIN_SIDE:
        raise AvatarUnusableError(f"{image.width}x{image.height} is below Telegram's minimum")

    left = (image.width - side) // 2
    top = (image.height - side) // VERTICAL_BIAS
    square = image.convert("RGB").crop((left, top, left + side, top + side))

    if side > MAX_SIDE:
        from PIL import Image as _Image

        square = square.resize((MAX_SIDE, MAX_SIDE), _Image.Resampling.LANCZOS)

    buffer = io.BytesIO()
    square.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()
