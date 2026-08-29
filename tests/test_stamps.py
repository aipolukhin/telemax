"""The timestamp on a delivered message, and the mark on an edited one.

Telegram's own timestamp is the delivery time. While the bridge is up the two
agree; after downtime the backfill lands in one burst and every recovered
message would claim to be from now. These tests pin the short form and the
"edited at" mark that MAX, unlike Telegram, actually reports.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest_asyncio

from bridge.config import TimestampStyle
from bridge.formatting import format_stamp
from bridge.max_client import normalize_message
from bridge.routing import BridgeRouter
from bridge.storage import BridgeStateRepository, Database, MessageMapRepository
from tests.test_routing import CONTACT, MOM, OWNER_CHAT, OWNER_MAX_ID, FakeLookup, FakeTelegram

NOW = datetime(2026, 7, 29, 14, 30)


def at(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def test_the_day_travels_with_the_message() -> None:
    stamp = format_stamp(at(datetime(2026, 7, 29, 9, 5)), TimestampStyle.COMPACT, now=NOW)
    assert stamp == "[29/07 09:05] "


def test_a_message_that_just_arrived_is_not_stamped() -> None:
    """Telegram already shows the right minute; a second one is noise."""
    assert format_stamp(at(NOW), TimestampStyle.COMPACT, now=NOW) == ""
    fresh = datetime(2026, 7, 29, 14, 29)
    assert format_stamp(at(fresh), TimestampStyle.COMPACT, now=NOW) == ""


def test_an_older_message_keeps_its_own_day() -> None:
    """The whole point: a message recovered after downtime must not look current.

    Telegram files everything under today's date header when it is delivered, so
    the day has to be part of the message.
    """
    stamp = format_stamp(at(datetime(2026, 7, 28, 23, 41)), TimestampStyle.COMPACT, now=NOW)
    assert stamp == "[28/07 23:41] "


def test_another_year_adds_the_year() -> None:
    stamp = format_stamp(at(datetime(2025, 12, 31, 23, 59)), TimestampStyle.COMPACT, now=NOW)
    assert stamp == "[31/12/2025 23:59] "


def test_full_style_stamps_everything_including_fresh_messages() -> None:
    stamp = format_stamp(at(NOW), TimestampStyle.FULL, now=NOW)
    assert stamp == "[29/07/2026 14:30] "

    stamp = format_stamp(at(datetime(2026, 7, 29, 9, 5)), TimestampStyle.FULL, now=NOW)
    assert stamp == "[29/07/2026 09:05] "


def test_off_and_missing_time_produce_nothing() -> None:
    assert format_stamp(at(NOW), TimestampStyle.OFF, now=NOW) == ""
    assert format_stamp(None, TimestampStyle.COMPACT, now=NOW) == ""
    assert format_stamp(0, TimestampStyle.COMPACT, now=NOW) == ""


def test_absurd_timestamp_does_not_break_delivery() -> None:
    """A clock this broken is not worth losing a message over."""
    assert format_stamp(10**18, TimestampStyle.COMPACT, now=NOW) == ""


def test_a_timezone_is_applied_when_configured() -> None:
    """A server runs on UTC; the owner reads Telegram in their own zone."""
    from zoneinfo import ZoneInfo

    # 06:05 UTC is 09:05 in Moscow.
    utc_moment = datetime(2026, 7, 29, 6, 5, tzinfo=ZoneInfo("UTC"))
    stamp = format_stamp(
        int(utc_moment.timestamp() * 1000),
        TimestampStyle.COMPACT,
        now=NOW,
        tz=ZoneInfo("Europe/Moscow"),
    )
    assert stamp == "[29/07 09:05] "


def test_edited_at_is_read_only_when_max_says_edited() -> None:
    """`updateTime` also rides along on messages nobody touched."""
    payload: dict[str, Any] = {
        "id": 1,
        "chatId": MOM.max_chat_id,
        "sender": CONTACT,
        "text": "текст",
        "time": at(NOW),
        "updateTime": at(NOW),
    }
    assert normalize_message(payload, own_user_id=OWNER_MAX_ID).edited_at is None

    edited = normalize_message(
        dict(payload, status="EDITED", updateTime=at(datetime(2026, 7, 29, 14, 40))),
        own_user_id=OWNER_MAX_ID,
    )
    assert edited.edited_at == at(datetime(2026, 7, 29, 14, 40))


@pytest_asyncio.fixture
async def stamped(tmp_path: Path) -> Any:
    database = await Database.connect(tmp_path / "bridge.db")
    telegram = FakeTelegram()
    router = BridgeRouter(
        lookup=FakeLookup(),
        telegram=telegram,
        max_sender=None,  # type: ignore[arg-type] - this test never sends to MAX
        messages=MessageMapRepository(database),
        state=BridgeStateRepository(database),
        owner_chat_id=OWNER_CHAT,
        timestamp_style=TimestampStyle.COMPACT,
    )
    try:
        yield router, telegram
    finally:
        await database.close()


async def test_delivered_message_is_stamped(stamped: Any) -> None:
    router, telegram = stamped
    payload = {
        "id": 1,
        "chatId": MOM.max_chat_id,
        "sender": CONTACT,
        "text": "привет",
        "time": at(datetime(2026, 7, 29, 9, 5)),
    }
    await router.on_max_message(normalize_message(payload, own_user_id=OWNER_MAX_ID))

    _, _, text = telegram.sent[0]
    assert text.startswith("[29/07 09:05] ")
    assert text.endswith("привет")


async def test_entities_move_with_the_stamp(stamped: Any) -> None:
    """A stamp in front of the text must not leave the bold range behind."""
    router, telegram = stamped
    payload = {
        "id": 2,
        "chatId": MOM.max_chat_id,
        "sender": CONTACT,
        "text": "жирный текст",
        "time": at(datetime(2026, 7, 29, 9, 5)),
        "elements": [{"type": "STRONG", "from": 0, "length": 6}],
    }
    await router.on_max_message(normalize_message(payload, own_user_id=OWNER_MAX_ID))

    _, _, text = telegram.sent[0]
    entities = telegram.entity_sets[0]
    assert entities is not None
    offset, length = entities[0]["offset"], entities[0]["length"]
    assert text[offset : offset + length] == "жирный"
