"""A message the bridge places as the owner knows what it is from the start.

The hole this closes, measured in production on 2026-08-07. A re-pull placed 176
of the owner's own MAX messages back into their Telegram chat through the owner's
own session — each one stamped `[29/07 13:47] ` because it was days old. That
session gets **no update for a message it sent itself**, so the handler that
writes a message's baseline never saw them. The first update that did arrive,
carrying no content change at all, landed on a message with nothing to subtract
from, was read as an edit, and asked MAX to write the stamp into the original.
Sixteen of those reached MAX, which refused all sixteen with
`error.edit.timeout` — the messages were past its edit window — and the retries
held one contact's queue for five hours.

Two independent guards, and this file holds both to their word:

* the placement writes the baseline itself, from the text it placed;
* an update on a message with no baseline proves nothing and carries nothing.

And a third thing that is neither: a Telegram copy of a MAX message is a
*rendering*, so an edit of one carries the bridge's own furniture. It comes off
before anything is written back into MAX.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, cast

import pytest

from bridge.formatting import strip_presentation
from bridge.routing.owner_voice import OwnerVoice
from bridge.service.runtime import place_owner_message
from bridge.storage import Database, MessageMapRepository, SourceMarker

ACCOUNT = 100000001
BOT = 9000000007
MAX_CHAT = 236856064
MAX_MESSAGE = 111411200131072002
PLACED = 1002959

#: What the re-pull actually placed: a nine-day-old MAX line, stamped.
STAMPED = "[29/07 13:47] Отл"


# ------------------------------------------------------- the placement's seed


class _Session:
    """Enough of the owner's session to place one line."""

    is_connected = True

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_own_message(
        self, peer_id: int, text: str, *, entities: Any = None
    ) -> int | None:
        self.sent.append((peer_id, text))
        return PLACED


class _Baseline:
    def __init__(self, *, fails: bool = False) -> None:
        self.rows: list[dict[str, Any]] = []
        self._fails = fails

    async def seed_baseline(self, **kwargs: Any) -> bool:
        if self._fails:
            raise RuntimeError("the database is away")
        self.rows.append(kwargs)
        return True


def _payload(text: str = STAMPED, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "peer_id": BOT,
        "bot_id": BOT,
        "owner_account_id": ACCOUNT,
        "max_chat_id": MAX_CHAT,
        "max_message_id": MAX_MESSAGE,
        "outgoing_text": text,
        "entities": None,
        "timestamp": 1_785_000_000_000,
        "is_outgoing": True,
    }
    payload.update(extra)
    return payload


async def _place(
    baseline: _Baseline | None, *, text: str = STAMPED, entities: Any = None
) -> _Session:
    session = _Session()
    await place_owner_message(
        _payload(text, entities=entities),
        sending=_nothing,
        voice=OwnerVoice(session=lambda: cast("Any", session)),
        media=cast("Any", None),
        messages=None,
        baseline=(lambda: cast("Any", baseline)) if baseline is not None else None,
    )
    return session


async def _nothing() -> None:
    return None


async def test_the_placement_writes_its_own_baseline() -> None:
    """The one moment the bridge knows exactly what the message is."""
    baseline = _Baseline()

    await _place(baseline)

    assert len(baseline.rows) == 1
    row = baseline.rows[0]
    assert row["account_id"] == ACCOUNT
    assert row["bot_id"] == BOT
    assert row["message_id"] == PLACED
    assert row["chosen_json"] == "[]", "nothing has reacted to a message just sent"


async def test_the_baseline_is_the_fingerprint_the_update_will_carry() -> None:
    """Written from the text placed, read from the message Telegram stores.

    The two have to agree or the baseline describes something else, and the very
    next update looks like a change nobody made. They agree because the
    placement sends the body verbatim — `parse_mode=None` — and both sides hash
    the same markdown rendering of it.
    """
    from telethon.tl.types import MessageEntityBold

    from bridge.telegram.owner_snapshot import content_fingerprint_of

    baseline = _Baseline()
    entities = [{"type": "bold", "offset": 0, "length": 3}]
    await _place(baseline, text="абв гд", entities=entities)

    class Stored:
        """The message as Telegram will report it back in an update."""

        id = PLACED
        message = "абв гд"
        entities: ClassVar[list[Any]] = [MessageEntityBold(offset=0, length=3)]
        reactions = None

    assert baseline.rows[0]["content_fingerprint"] == content_fingerprint_of(Stored())


async def test_a_baseline_that_cannot_be_written_does_not_fail_the_delivery() -> None:
    """The message is already in the chat. A retry would place it twice."""
    session = await _place(_Baseline(fails=True))

    assert session.sent == [(BOT, STAMPED)], "the placement itself stands"


async def test_a_placement_with_no_baseline_writer_still_delivers() -> None:
    """The owner session can be up before the dispatch behind it is built."""
    session = await _place(None)

    assert session.sent == [(BOT, STAMPED)]


# ------------------------------------ the rendering comes off before it goes back


def test_the_stamp_comes_off() -> None:
    assert strip_presentation("[29/07 13:47] Отл") == "Отл"
    assert strip_presentation("[29/07/2025 13:47] Отл") == "Отл"


def test_the_forward_header_and_the_edit_mark_come_off() -> None:
    assert strip_presentation("↪ Переслано от Аня\nпривет") == "привет"
    assert strip_presentation("↪ Пересланное сообщение\nпривет") == "привет"
    assert strip_presentation("привет (изм. 12:40)") == "привет"
    assert strip_presentation("привет (изм. 29/07 12:40)") == "привет"
    assert strip_presentation("[29/07 13:47] ↪ Переслано от Аня\nпривет (изм. 12:40)") == (
        "привет"
    )


def test_somebody_elses_brackets_are_left_alone() -> None:
    """The patterns are the renderers' own shapes, not "anything in brackets"."""
    assert strip_presentation("[дома] буду поздно") == "[дома] буду поздно"
    assert strip_presentation("[29/07] Отл") == "[29/07] Отл"
    assert strip_presentation("см. (изм. проекта)") == "см. (изм. проекта)"


# ---------------------------------------------- and the router applies that rule


class _Pipe:
    def __init__(self) -> None:
        self.jobs: list[dict[str, Any]] = []

    async def submit(self, **kwargs: Any) -> tuple[int, bool]:
        self.jobs.append(kwargs)
        return len(self.jobs), True

    async def attempt(self, *_: Any, **__: Any) -> None:
        return None


async def _router(database: Database, *, marker: SourceMarker) -> tuple[Any, _Pipe, int]:
    from bridge.routing.router import BridgeRouter
    from bridge.storage import BridgeStateRepository

    messages = MessageMapRepository(database)
    if marker is SourceMarker.FROM_MAX:
        claimed = await messages.claim_from_max(
            bridge_name="timur",
            max_chat_id=MAX_CHAT,
            max_message_id=MAX_MESSAGE,
            telegram_bot_id=BOT,
            telegram_chat_id=ACCOUNT,
        )
        assert claimed is not None
        link_id = claimed
    else:
        link_id = await messages.record_from_telegram(
            bridge_name="timur",
            max_chat_id=MAX_CHAT,
            telegram_bot_id=BOT,
            telegram_chat_id=ACCOUNT,
            telegram_message_id=None,
            max_message_id=MAX_MESSAGE,
        )
    await messages.attach_owner_message(
        link_id, PLACED, telegram_owner_account_id=ACCOUNT
    )
    pipe = _Pipe()
    router = BridgeRouter(
        lookup=lambda *_: None,
        telegram=cast("Any", None),
        max_sender=cast("Any", None),
        messages=messages,
        state=BridgeStateRepository(database),
        owner_chat_id=ACCOUNT,
        pipe=cast("Any", pipe),
    )
    return router, pipe, link_id


@pytest.mark.parametrize(
    ("marker", "carried"),
    [
        # A rendering of a MAX message: the stamp is the bridge's, not the
        # owner's, and MAX must never be told it is part of the message.
        (SourceMarker.FROM_MAX, "Отл"),
        # The owner's own words, which they may legitimately have begun with
        # something stamp-shaped. Nothing is taken off those.
        (SourceMarker.FROM_TG, STAMPED),
    ],
)
async def test_the_edit_carried_to_max_is_the_body_not_the_rendering(
    tmp_path: Path, marker: SourceMarker, carried: str
) -> None:
    database = await Database.connect(tmp_path / "bridge.db")
    router, pipe, _ = await _router(database, marker=marker)

    await router.on_owner_edit(
        owner_account_id=ACCOUNT, owner_message_id=PLACED, text=STAMPED, edit_pts=2004898
    )

    assert len(pipe.jobs) == 1
    assert pipe.jobs[0]["payload"]["text"] == carried
    await database.close()


async def test_an_edit_that_is_only_decoration_is_not_carried(tmp_path: Path) -> None:
    """A stamp on its own is not a message, and MAX has nothing to be told."""
    database = await Database.connect(tmp_path / "bridge.db")
    router, pipe, _ = await _router(database, marker=SourceMarker.FROM_MAX)

    await router.on_owner_edit(
        owner_account_id=ACCOUNT,
        owner_message_id=PLACED,
        text="[29/07 13:47] ",
        edit_pts=2004898,
    )

    assert pipe.jobs == []
    await database.close()
