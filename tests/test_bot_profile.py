"""W8 — a bot that looks like the contact, and an identifier that names nobody.

Three separate concerns meet here:

* the name and the avatar are pulled from MAX and pushed through Bot API;
* MAX serves WebP while Telegram takes JPEG only, and says `PHOTO_CROP_SIZE_SMALL`
  when it gets anything else, so the conversion is not optional;
* the identifier derived from a phone number must not be reversible, which a
  plain hash of a ten-digit space would be.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from test_media import FakeSession, fetcher_for

from bridge.max_client import MaxContact
from bridge.media import MediaPipeline, TempFiles
from bridge.provisioning import BotProfileSync
from bridge.provisioning.avatar import AvatarUnusableError, to_profile_jpeg
from bridge.provisioning.profile import NAME_SUFFIX, build_bot_name, signature_of
from bridge.provisioning.secrets import ContactBotSecretStore


def make_image(width: int, height: int, fmt: str = "WEBP") -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (30, 90, 160)).save(buffer, format=fmt)
    return buffer.getvalue()


# ------------------------------------------------------------------ avatar


def test_webp_becomes_a_square_jpeg() -> None:
    jpeg = to_profile_jpeg(make_image(1440, 1920))

    from PIL import Image

    converted = Image.open(io.BytesIO(jpeg))
    assert converted.format == "JPEG"
    assert converted.width == converted.height


def test_a_tall_photo_is_cropped_above_the_middle() -> None:
    """A centre crop of a portrait takes the chin instead of the face."""
    from PIL import Image

    source = Image.new("RGB", (600, 1200), (0, 0, 0))
    for y in range(0, 400):  # a bright band where a face would be
        for x in range(0, 600, 4):
            source.putpixel((x, y), (255, 255, 255))
    buffer = io.BytesIO()
    source.save(buffer, format="WEBP")

    result = Image.open(io.BytesIO(to_profile_jpeg(buffer.getvalue())))
    top_row = [result.getpixel((x, 5)) for x in range(0, 600, 50)]
    assert any(pixel[0] > 100 for pixel in top_row), "the crop started below the face"


def test_a_tiny_avatar_is_refused_rather_than_upscaled() -> None:
    with pytest.raises(AvatarUnusableError):
        to_profile_jpeg(make_image(64, 64))


def test_garbage_is_not_an_avatar() -> None:
    with pytest.raises(AvatarUnusableError):
        to_profile_jpeg(b"<html>not an image</html>")


# ------------------------------------------------------------------ profile sync


def test_the_bot_name_says_it_is_a_max_dialog() -> None:
    assert build_bot_name("Иван Петров") == f"Иван Петров{NAME_SUFFIX}"
    assert build_bot_name("  ") == ""

    long_name = "и" * 200
    built = build_bot_name(long_name)
    assert len(built) <= 64
    assert built.endswith(NAME_SUFFIX), "the suffix is never what gets cut"


@dataclass
class FakeWriter:
    names: list[str] = field(default_factory=list)
    descriptions: list[str] = field(default_factory=list)
    photos: list[bytes] = field(default_factory=list)
    name_ok: bool = True

    async def set_name(self, bot_id: int, name: str) -> bool:
        self.names.append(name)
        return self.name_ok

    async def set_short_description(self, bot_id: int, text: str) -> bool:
        self.descriptions.append(text)
        return True

    async def set_profile_photo(self, bot_id: int, photo: bytes, file_name: str) -> bool:
        self.photos.append(photo)
        return True


@dataclass
class FakeContacts:
    contact: MaxContact | None

    async def contact_profile(self, user_id: int) -> MaxContact | None:
        return self.contact


def sync_for(tmp_path: Path, contact: MaxContact | None, image: bytes) -> Any:
    writer = FakeWriter()
    session = FakeSession(body=image, headers={"Content-Type": "image/webp"})
    pipeline = MediaPipeline(
        sources=None,  # type: ignore[arg-type] - the avatar path never resolves MAX ids
        temp_files=TempFiles(tmp_path / "tmp"),
        fetcher=fetcher_for(session),
        max_file_size_mb=5,
    )
    return BotProfileSync(writer=writer, contacts=FakeContacts(contact), pipeline=pipeline), writer


async def test_name_and_avatar_are_pulled_from_max(tmp_path: Path) -> None:
    contact = MaxContact(
        user_id=200000002,
        display_name="Иван Петров",
        avatar_url="https://i.oneme.ru/i?r=abc",
    )
    sync, writer = sync_for(tmp_path, contact, make_image(800, 1000))

    result = await sync.apply(bot_id=4242, max_user_id=contact.user_id)

    assert result == contact
    assert writer.names == [f"Иван Петров{NAME_SUFFIX}"]
    assert writer.photos, "the avatar never reached Telegram"
    assert writer.photos[0][:3] == b"\xff\xd8\xff", "Telegram takes JPEG only"


async def test_an_unchanged_profile_is_not_re_applied(tmp_path: Path) -> None:
    """`setMyName` is rate-limited hard, and the bridge restarts often."""
    contact = MaxContact(user_id=1, display_name="Тот же", avatar_url="https://i.oneme.ru/i?r=x")
    sync, writer = sync_for(tmp_path, contact, make_image(400, 400))

    await sync.apply(bot_id=4242, max_user_id=1, known_signature=signature_of(contact))

    assert writer.names == []
    assert writer.photos == []


async def test_a_changed_name_is_applied(tmp_path: Path) -> None:
    contact = MaxContact(user_id=1, display_name="Новое имя")
    sync, writer = sync_for(tmp_path, contact, b"")

    await sync.apply(bot_id=4242, max_user_id=1, known_signature="something-else")

    assert writer.names == [f"Новое имя{NAME_SUFFIX}"]


async def test_a_contact_without_an_avatar_still_gets_a_name(tmp_path: Path) -> None:
    contact = MaxContact(user_id=1, display_name="Без фото")
    sync, writer = sync_for(tmp_path, contact, b"")

    await sync.apply(bot_id=4242, max_user_id=1)

    assert writer.names == [f"Без фото{NAME_SUFFIX}"]
    assert writer.photos == []


async def test_an_unknown_contact_leaves_the_bot_alone(tmp_path: Path) -> None:
    sync, writer = sync_for(tmp_path, None, b"")

    assert await sync.apply(bot_id=4242, max_user_id=1) is None
    assert writer.names == []
    assert writer.descriptions == []


async def test_a_broken_avatar_does_not_break_the_bridge(tmp_path: Path) -> None:
    """A bridge with the wrong picture works; one that refuses to start does not."""
    contact = MaxContact(user_id=1, display_name="Кто-то", avatar_url="https://i.oneme.ru/i?r=x")
    sync, writer = sync_for(tmp_path, contact, b"<html>expired</html>")

    await sync.apply(bot_id=4242, max_user_id=1)

    assert writer.names, "the name still went through"
    assert writer.photos == []


# ------------------------------------------------- bridges made at run time


async def test_a_bridge_made_at_run_time_is_dressed_too(tmp_path: Path) -> None:
    """The gap that shipped a working bridge with a blank avatar.

    `BridgeService.start` dresses every bridge it brings up, but a bot the
    guardian provisions arrives through the gateway instead — a second, thinner
    path that skipped the step entirely. Both now call the same function.
    """
    from bridge.service.runtime import LiveBridgeGateway

    dressed: list[tuple[str, int, int]] = []

    async def dress(name: str, bot_id: int, contact_id: int) -> None:
        dressed.append((name, bot_id, contact_id))

    async def contact_of_chat(max_chat_id: int) -> int | None:
        return 200000002

    class Registry:
        def by_name(self, name: str) -> None:
            return None

        async def add(self, bridge: Any, *, expected: Any = None) -> Any:
            identity = type("Identity", (), {"bot_id": 9000000001, "username": "x"})()
            return type("Live", (), {"identity": identity})()

    class Bridges:
        def __init__(self) -> None:
            self.rows: list[Any] = []

        async def upsert(self, record: Any) -> None:
            self.rows.append(record)

        async def by_max_chat(self, max_chat_id: int) -> Any:
            return None

        async def get(self, bridge_name: str) -> Any:
            return None

        async def set_state(self, bridge_name: str, state: Any) -> None:
            return None

    import os

    os.environ["TELEMAX_BOT_EXAMPLEBRIDGE01"] = "9000000001:token"
    gateway = LiveBridgeGateway(
        registry=Registry(),  # type: ignore[arg-type]
        bridges=Bridges(),  # type: ignore[arg-type]
        secrets=ContactBotSecretStore(tmp_path / "bots.env"),
        contact_of_chat=contact_of_chat,
        dress_bot=dress,
    )

    name = await gateway.start_worker(
        max_chat_id=236856064,
        username="examplebridge01_max_bot",
        token_env="TELEMAX_BOT_EXAMPLEBRIDGE01",
        title="Иван Петров",
    )

    assert name == "examplebridge01"
    assert dressed == [("examplebridge01", 9000000001, 200000002)]
    os.environ.pop("TELEMAX_BOT_EXAMPLEBRIDGE01", None)
