"""WP9 — Telegram attachments on their way into MAX.

A voice message stays a voice message and a video note stays a circle: the
second pipeline that carries them was implemented by the compatibility layer (R7,
2026-08-04), so `plan_upload` routes them to their own kinds rather than
degrading. The rest is about not losing things: an album is one MAX message, an
oversized file is refused before it is fetched, and a failure is named.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from bridge.media.upload import (
    BOT_API_DOWNLOAD_LIMIT,
    Album,
    AlbumCollector,
    UploadKind,
    plan_upload,
    too_large,
)


def test_the_upload_router_claims_media_and_nothing_else() -> None:
    """aiogram stops at the first handler that runs.

    The upload router is registered before the text one, so a handler without a
    filter swallows every plain message and the text never reaches MAX at all —
    which is exactly what happened on the live bridge.
    """
    from aiogram import Dispatcher

    from bridge.routing.upload_router import MediaUploader, build_upload_router

    router = build_upload_router(
        MediaUploader(bridge_router=None, pipeline=None)  # type: ignore[arg-type]
    )
    handler = router.message.handlers[0]
    assert handler.filters, "an unfiltered handler here silently eats text messages"

    # And the dispatcher must accept the router as built.
    Dispatcher().include_router(router)


def test_a_photo_stays_a_photo() -> None:
    item = plan_upload(photo=True, file_id="a", file_name="photo.jpg")
    assert item.kind is UploadKind.PHOTO
    assert item.degraded_notice is None


def test_a_voice_message_stays_a_voice() -> None:
    """The second pipeline is reachable now (R7): no degradation, no notice."""
    item = plan_upload(voice=True, file_id="a", file_name="voice.ogg")
    assert item.kind is UploadKind.VOICE
    assert item.degraded_notice is None


def test_a_circle_stays_a_circle() -> None:
    item = plan_upload(video_note=True, file_id="a", file_name="circle.mp4")
    assert item.kind is UploadKind.CIRCLE
    assert item.degraded_notice is None


def test_music_and_documents_are_files() -> None:
    assert plan_upload(audio=True, file_id="a", file_name="t.mp3").kind is UploadKind.FILE
    assert plan_upload(document=True, file_id="a", file_name="d.pdf").kind is UploadKind.FILE


def test_the_bot_api_limit_is_checked_before_fetching() -> None:
    """Telegram will not serve a bot anything bigger; downloading is pointless."""
    big = plan_upload(
        document=True, file_id="a", file_name="d.bin", size=BOT_API_DOWNLOAD_LIMIT + 1
    )
    small = plan_upload(document=True, file_id="a", file_name="d.bin", size=1024)

    assert too_large(big)
    assert not too_large(small)
    assert not too_large(plan_upload(document=True, file_id="a", file_name="d.bin"))


@dataclass
class Flushes:
    calls: list[tuple[str, Album]] = field(default_factory=list)

    async def __call__(self, group_id: str, album: Album) -> None:
        self.calls.append((group_id, album))


async def test_an_album_is_collected_into_one_message() -> None:
    flushes = Flushes()
    collector = AlbumCollector(flush=flushes, window_seconds=0.05)

    for index in range(3):
        await collector.add(
            "group-1",
            plan_upload(photo=True, file_id=f"f{index}", file_name="photo.jpg"),
            caption="три фото" if index == 1 else "",
            reply_to=None,
            message_id=100 + index,
            context=("bot", 42),
        )

    await asyncio.sleep(0.2)

    assert len(flushes.calls) == 1, "five photos must not become five messages"
    _, album = flushes.calls[0]
    assert len(album.items) == 3
    assert album.caption == "три фото", "Telegram puts it on whichever part was typed on"
    assert album.first_message_id == 100
    assert album.context == ("bot", 42)


async def test_a_late_part_restarts_the_window() -> None:
    """The last part of an album is not marked; only silence ends the group."""
    flushes = Flushes()
    collector = AlbumCollector(flush=flushes, window_seconds=0.1)

    await collector.add(
        "group-2",
        plan_upload(photo=True, file_id="a", file_name="a.jpg"),
        caption="",
        reply_to=None,
        message_id=1,
    )
    await asyncio.sleep(0.06)
    await collector.add(
        "group-2",
        plan_upload(photo=True, file_id="b", file_name="b.jpg"),
        caption="",
        reply_to=None,
        message_id=2,
    )
    await asyncio.sleep(0.06)
    assert flushes.calls == [], "the group was still arriving"

    await asyncio.sleep(0.12)
    assert len(flushes.calls) == 1
    assert len(flushes.calls[0][1].items) == 2


async def test_two_albums_do_not_mix() -> None:
    flushes = Flushes()
    collector = AlbumCollector(flush=flushes, window_seconds=0.05)

    await collector.add(
        "group-a",
        plan_upload(photo=True, file_id="a", file_name="a.jpg"),
        caption="",
        reply_to=None,
        message_id=1,
    )
    await collector.add(
        "group-b",
        plan_upload(video=True, file_id="b", file_name="b.mp4"),
        caption="",
        reply_to=None,
        message_id=2,
    )
    await asyncio.sleep(0.2)

    assert {group for group, _ in flushes.calls} == {"group-a", "group-b"}


async def test_drain_flushes_what_is_still_waiting() -> None:
    """A shutdown mid-album must not eat the photos."""
    flushes = Flushes()
    collector = AlbumCollector(flush=flushes, window_seconds=30)

    await collector.add(
        "group-3",
        plan_upload(photo=True, file_id="a", file_name="a.jpg"),
        caption="",
        reply_to=None,
        message_id=1,
    )
    await collector.drain()

    assert len(flushes.calls) == 1
