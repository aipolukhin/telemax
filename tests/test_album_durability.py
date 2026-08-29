"""An album must survive the gap between its own parts.

Telegram sends a media group as separate updates with nothing marking the last
one, so the group is only known to be complete once a moment has passed with
nothing new. The parts are acknowledged to Telegram as they arrive — which means
the process can die mid-album holding the only copy of two photos the owner
already watched leave their phone.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from bridge.media.upload import Album, AlbumCollector, PendingUpload, UploadKind
from bridge.storage import Database, MediaGroupRepository

BOT_ID = 555
GROUP = "album-1"


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


def photo(n: int) -> PendingUpload:
    return PendingUpload(UploadKind.PHOTO, f"file-{n}", f"photo{n}.jpg", size=1000)


async def test_parts_are_stored_as_they_arrive(database: Database) -> None:
    store = MediaGroupRepository(database)
    collector = AlbumCollector(flush=_never, window_seconds=99, store=store)

    await collector.add(
        GROUP, photo(1), caption="подпись", reply_to=None, message_id=1,
        bridge_name="dad", bot_id=BOT_ID,
    )
    await collector.add(
        GROUP, photo(2), caption="", reply_to=None, message_id=2,
        bridge_name="dad", bot_id=BOT_ID,
    )

    parts = await store.parts(GROUP)
    assert [part.telegram_message_id for part in parts] == [1, 2]


async def test_an_album_is_rebuilt_after_a_restart(tmp_path: Path) -> None:
    """The crash window: two photos accepted, the process dies, then a restart."""
    path = tmp_path / "bridge.db"

    first = await Database.connect(path)
    dying = AlbumCollector(
        flush=_never, window_seconds=99, store=MediaGroupRepository(first)
    )
    await dying.add(
        GROUP, photo(1), caption="подпись", reply_to=7, message_id=1,
        bridge_name="dad", bot_id=BOT_ID,
    )
    await dying.add(
        GROUP, photo(2), caption="", reply_to=None, message_id=2,
        bridge_name="dad", bot_id=BOT_ID,
    )
    await first.close()  # nothing was flushed

    second = await Database.connect(path)
    try:
        sent: list[tuple[str, Album]] = []

        async def flush(group_id: str, album: Album) -> None:
            sent.append((group_id, album))

        revived = AlbumCollector(
            flush=flush, window_seconds=99, store=MediaGroupRepository(second)
        )
        assert await revived.restore() == [GROUP]

        await revived.drain()
        assert len(sent) == 1
        group_id, album = sent[0]
        assert group_id == GROUP
        assert [item.file_id for item in album.items] == ["file-1", "file-2"]
    finally:
        await second.close()


async def test_the_restored_album_keeps_its_order_and_caption(tmp_path: Path) -> None:
    path = tmp_path / "bridge.db"
    first = await Database.connect(path)
    collector = AlbumCollector(
        flush=_never, window_seconds=99, store=MediaGroupRepository(first)
    )
    # Telegram puts the caption on whichever part the user typed it on.
    for n, caption in ((1, ""), (2, "вот это"), (3, "")):
        await collector.add(
            GROUP, photo(n), caption=caption, reply_to=None, message_id=n,
            bridge_name="dad", bot_id=BOT_ID,
        )
    await first.close()

    second = await Database.connect(path)
    try:
        sent: list[Album] = []

        async def flush(group_id: str, album: Album) -> None:
            sent.append(album)

        revived = AlbumCollector(
            flush=flush, window_seconds=99, store=MediaGroupRepository(second)
        )
        await revived.restore()
        await revived.drain()

        album = sent[0]
        assert [item.file_id for item in album.items] == ["file-1", "file-2", "file-3"]
        assert album.caption == "вот это"
    finally:
        await second.close()


async def test_the_caption_is_not_duplicated(database: Database) -> None:
    """Two parts carrying the caption must not produce it twice."""
    store = MediaGroupRepository(database)
    sent: list[Album] = []

    async def flush(group_id: str, album: Album) -> None:
        sent.append(album)

    collector = AlbumCollector(flush=flush, window_seconds=99, store=store)
    await collector.add(
        GROUP, photo(1), caption="одна подпись", reply_to=None, message_id=1,
        bridge_name="dad", bot_id=BOT_ID,
    )
    await collector.add(
        GROUP, photo(2), caption="одна подпись", reply_to=None, message_id=2,
        bridge_name="dad", bot_id=BOT_ID,
    )
    await collector.drain()

    assert sent[0].caption == "одна подпись"


async def test_a_replayed_part_is_not_added_twice(database: Database) -> None:
    """After a restart Telegram re-sends what was never acknowledged."""
    store = MediaGroupRepository(database)
    sent: list[Album] = []

    async def flush(group_id: str, album: Album) -> None:
        sent.append(album)

    collector = AlbumCollector(flush=flush, window_seconds=99, store=store)
    for _ in range(2):
        await collector.add(
            GROUP, photo(1), caption="", reply_to=None, message_id=1,
            bridge_name="dad", bot_id=BOT_ID,
        )
    await collector.drain()

    assert len(sent[0].items) == 1


async def test_a_flushed_album_leaves_nothing_behind(database: Database) -> None:
    """Otherwise the next restart would send it a second time."""
    store = MediaGroupRepository(database)

    async def flush(group_id: str, album: Album) -> None:
        return None

    collector = AlbumCollector(flush=flush, window_seconds=99, store=store)
    await collector.add(
        GROUP, photo(1), caption="", reply_to=None, message_id=1,
        bridge_name="dad", bot_id=BOT_ID,
    )
    await collector.drain()

    assert await store.parts(GROUP) == []
    assert await store.open_groups() == []


async def test_a_failed_flush_keeps_the_parts(database: Database) -> None:
    """If it did not go out, it must still be there to try again."""
    store = MediaGroupRepository(database)

    async def explode(group_id: str, album: Album) -> None:
        raise RuntimeError("MAX is down")

    collector = AlbumCollector(flush=explode, window_seconds=0.01, store=store)
    await collector.add(
        GROUP, photo(1), caption="", reply_to=None, message_id=1,
        bridge_name="dad", bot_id=BOT_ID,
    )

    import asyncio

    await asyncio.sleep(0.1)
    assert len(await store.parts(GROUP)) == 1


async def test_without_a_store_it_still_works(database: Database) -> None:
    """The collector must not require a database to be useful in tests."""
    sent: list[Album] = []

    async def flush(group_id: str, album: Album) -> None:
        sent.append(album)

    collector = AlbumCollector(flush=flush, window_seconds=99)
    await collector.add(GROUP, photo(1), caption="", reply_to=None, message_id=1)
    await collector.drain()

    assert len(sent) == 1
    assert await collector.restore() == []


async def _never(group_id: str, album: Album) -> None:
    raise AssertionError("this album was not supposed to be flushed")
