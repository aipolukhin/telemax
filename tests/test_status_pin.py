"""W9 — the pinned line that says where the contact is.

MAX reports last-seen, Telegram lets a bot pin and edit its own message in a
private chat, and the two meet in one line at the top of the dialog. What is
worth pinning down here: the wording, the fact that a chatty contact does not
cost an edit per twitch, and the re-post path for a message too old to edit —
a bot may only edit its own message for 48 hours.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest_asyncio

from bridge.config import PresenceConfig
from bridge.max_client import PresenceUpdate
from bridge.presence.status_pin import (
    JUST_NOW_TEXT,
    LONG_AGO_TEXT,
    ONLINE_TEXT,
    RECENTLY_TEXT,
    PinnedStatus,
    render,
)
from bridge.storage import BridgeRecord, BridgeRepository, Database

BRIDGE = "second-account"
BOT = 4242
CHAT = 100000001
CONTACT = 200000002


@dataclass
class FakeRenderer:
    sent: list[str] = field(default_factory=list)
    edits: list[tuple[int, str]] = field(default_factory=list)
    pinned: list[int] = field(default_factory=list)
    deleted: list[int] = field(default_factory=list)
    next_id: int = 700
    edit_ok: bool = True

    async def send_status(self, bot_id: int, chat_id: int, text: str) -> int | None:
        self.sent.append(text)
        self.next_id += 1
        return self.next_id

    async def edit_status(self, bot_id: int, chat_id: int, message_id: int, text: str) -> bool:
        self.edits.append((message_id, text))
        return self.edit_ok

    async def delete_status(self, bot_id: int, chat_id: int, message_id: int) -> bool:
        self.deleted.append(message_id)
        return True

    async def pin_status(self, bot_id: int, chat_id: int, message_id: int) -> bool:
        self.pinned.append(message_id)
        return True


@pytest_asyncio.fixture
async def pinned(tmp_path: Path) -> Any:
    database = await Database.connect(tmp_path / "bridge.db")
    bridges = BridgeRepository(database)
    await bridges.upsert(
        BridgeRecord(
            bridge_name=BRIDGE,
            max_chat_id=1,
            token_env="TOKEN",
            max_user_id=CONTACT,
            telegram_bot_id=BOT,
        )
    )
    renderer = FakeRenderer()
    status = PinnedStatus(
        renderer=renderer,
        store=bridges,
        config=PresenceConfig(pin_refresh_seconds=5),
    )
    try:
        yield status, renderer, bridges
    finally:
        await database.close()


def at(seconds_ago: int, *, now: float) -> str:
    return render(PresenceUpdate(user_id=1, seen=int(now) - seconds_ago), now=now)


def test_the_ladder_is_the_one_max_shows() -> None:
    """The line has to read like the app, not like a bridge.

    Fixed to an afternoon so "3 ч назад" cannot fall over midnight into
    "вчера" — which is exactly what the app would say, and what the day
    boundary below checks on purpose.
    """
    now = time.mktime(time.strptime("2026-07-29 14:30", "%Y-%m-%d %H:%M"))

    assert at(5, now=now) == JUST_NOW_TEXT
    assert at(61, now=now) == "1 мин назад"
    assert at(300, now=now) == "5 мин назад"
    assert at(3 * 3600, now=now) == "3 ч назад"


def test_past_midnight_the_app_says_yesterday_not_hours() -> None:
    """MAX counts hours only inside one calendar day; 09:00 the morning after
    a 21:40 visit reads "вчера", not "11 ч назад"."""
    now = time.mktime(time.strptime("2026-07-29 09:00", "%Y-%m-%d %H:%M"))
    seen = int(time.mktime(time.strptime("2026-07-28 21:40", "%Y-%m-%d %H:%M")))

    assert render(PresenceUpdate(user_id=1, seen=seen), now=now) == "Был(-а) вчера в 21:40"


def test_past_a_day_and_a_half_the_date_takes_over() -> None:
    now = time.mktime(time.strptime("2026-07-29 14:30", "%Y-%m-%d %H:%M"))
    this_year = int(time.mktime(time.strptime("2026-07-10 09:05", "%Y-%m-%d %H:%M")))
    last_year = int(time.mktime(time.strptime("2025-07-10 09:05", "%Y-%m-%d %H:%M")))

    assert render(PresenceUpdate(user_id=1, seen=this_year), now=now) == "Был(-а) 10 июл."
    assert render(PresenceUpdate(user_id=1, seen=last_year), now=now) == "Был(-а) 10 июл. 2025"


def test_online_is_read_from_the_status_not_from_the_clock() -> None:
    """The regression that hid «В сети»: MAX sets `seen` to its own now for
    every contact that carries a status, so freshness says "Только что" where
    the app says "В сети"."""
    now = time.time()

    assert render(PresenceUpdate(user_id=1, seen=int(now), status=1), now=now) == ONLINE_TEXT
    assert render(PresenceUpdate(user_id=1, seen=int(now), status=2), now=now) == RECENTLY_TEXT
    assert render(PresenceUpdate(user_id=1, seen=int(now), status=3), now=now) == LONG_AGO_TEXT
    # An offline contact keeps the ladder, whether the status says 0 or nothing.
    assert render(PresenceUpdate(user_id=1, seen=int(now), status=0), now=now) == JUST_NOW_TEXT


def test_an_unknown_status_is_not_turned_into_a_time() -> None:
    """MAX's own client falls back to "недавно" for a code it does not know."""
    now = time.time()

    assert render(PresenceUpdate(user_id=1, seen=int(now) - 5, status=9), now=now) == RECENTLY_TEXT


def test_a_contact_with_neither_time_nor_state() -> None:
    assert render(PresenceUpdate(user_id=1, seen=None)) == LONG_AGO_TEXT


async def test_the_first_update_posts_and_pins(pinned: Any) -> None:
    status, renderer, bridges = pinned

    assert await status.update(
        BRIDGE, PresenceUpdate(user_id=CONTACT, seen=int(time.time())),
        bot_id=BOT, chat_id=CHAT,
    )

    assert renderer.sent == [JUST_NOW_TEXT]
    assert renderer.pinned == [701], "a line nobody sees is not a status"
    message_id, text = await bridges.pinned_status(BRIDGE)
    assert (message_id, text) == (701, JUST_NOW_TEXT)


async def test_a_change_edits_the_same_message(pinned: Any) -> None:
    status, renderer, _ = pinned
    now = time.time()
    await status.update(
        BRIDGE, PresenceUpdate(user_id=CONTACT, seen=int(now)), bot_id=BOT, chat_id=CHAT
    )

    assert await status.update(
        BRIDGE,
        PresenceUpdate(user_id=CONTACT, seen=int(now) - 4000),
        bot_id=BOT,
        chat_id=CHAT,
        force=True,
    )

    assert len(renderer.sent) == 1, "the line must not multiply"
    assert renderer.edits and renderer.edits[-1][0] == 701


async def test_an_unchanged_status_costs_nothing(pinned: Any) -> None:
    """Presence repeats constantly; every edit spends rate limit."""
    status, renderer, _ = pinned
    presence = PresenceUpdate(user_id=CONTACT, seen=int(time.time()))

    await status.update(BRIDGE, presence, bot_id=BOT, chat_id=CHAT)
    assert await status.update(BRIDGE, presence, bot_id=BOT, chat_id=CHAT) is False

    assert renderer.edits == []
    assert len(renderer.sent) == 1


async def test_a_flapping_contact_is_throttled(pinned: Any) -> None:
    status, renderer, _ = pinned
    now = time.time()
    await status.update(
        BRIDGE, PresenceUpdate(user_id=CONTACT, seen=int(now)), bot_id=BOT, chat_id=CHAT
    )

    # A different text, but too soon after the last write.
    assert await status.update(
        BRIDGE, PresenceUpdate(user_id=CONTACT, seen=int(now) - 5000), bot_id=BOT, chat_id=CHAT
    ) is False
    assert renderer.edits == []


async def test_going_offline_is_never_throttled_away(pinned: Any) -> None:
    """MAX pushes presence on connect and on disconnect and never in between, so
    a dropped edit would leave the line saying «В сети» until the contact is
    back."""
    status, renderer, _ = pinned
    now = time.time()
    online = PresenceUpdate(user_id=CONTACT, seen=int(now), status=1)
    await status.update(BRIDGE, online, bot_id=BOT, chat_id=CHAT)

    # Well inside pin_refresh_seconds — a ladder tick here would be skipped.
    assert await status.update(
        BRIDGE,
        PresenceUpdate(user_id=CONTACT, seen=int(now)),
        bot_id=BOT,
        chat_id=CHAT,
    )
    assert renderer.edits[-1][1] == JUST_NOW_TEXT

    # And back the other way.
    assert await status.update(BRIDGE, online, bot_id=BOT, chat_id=CHAT)
    assert renderer.edits[-1][1] == ONLINE_TEXT


async def test_a_line_too_old_to_edit_is_reposted_and_repinned(pinned: Any) -> None:
    """A bot may edit its own message for 48 hours; after that it must repost."""
    status, renderer, bridges = pinned
    now = time.time()
    await status.update(
        BRIDGE, PresenceUpdate(user_id=CONTACT, seen=int(now)), bot_id=BOT, chat_id=CHAT
    )
    renderer.edit_ok = False

    assert await status.update(
        BRIDGE,
        PresenceUpdate(user_id=CONTACT, seen=int(now) - 9000),
        bot_id=BOT,
        chat_id=CHAT,
        force=True,
    )

    assert len(renderer.sent) == 2
    assert renderer.pinned == [701, 702]
    assert renderer.deleted == [701], "two pinned status lines would be worse than none"
    message_id, _ = await bridges.pinned_status(BRIDGE)
    assert message_id == 702


async def test_the_feature_can_be_switched_off(tmp_path: Path) -> None:
    database = await Database.connect(tmp_path / "off.db")
    bridges = BridgeRepository(database)
    renderer = FakeRenderer()
    status = PinnedStatus(
        renderer=renderer,
        store=bridges,
        config=PresenceConfig(pin_contact_status=False),
    )
    try:
        assert await status.update(
            BRIDGE, PresenceUpdate(user_id=CONTACT, seen=int(time.time())),
            bot_id=BOT, chat_id=CHAT,
        ) is False
        assert renderer.sent == []
    finally:
        await database.close()
