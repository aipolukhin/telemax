"""History is paged, and a bot is never answered as a stranger.

Two things the owner found in one sitting.

A re-pull asked for a thousand messages and delivered forty. MAX takes
`backward` — how many to load back from a point in time — and it was never
passed, so every caller got one default page however much they asked for. A
re-pull that says a thousand and brings forty is not a smaller re-pull; it is a
chat the owner believes is complete.

And «Этот бот недоступен.» kept arriving in the owner's own chats. That line is
the brush-off for a person who found a contact bot and pressed Start. Telegram
attributes some service updates to the bot itself, and answering one had the
bridge telling its owner it did not exist.
"""

from __future__ import annotations

from typing import Any

import pytest

from bridge.max_client.client import _page_history

pytestmark = pytest.mark.asyncio


class Pymax:
    """A MAX client that pages backwards, like the real one."""

    def __init__(self, total: int, *, page_cap: int = 200) -> None:
        # Oldest first; ids and times both ascend.
        self.all = [{"id": n, "time": 1_000 + n} for n in range(1, total + 1)]
        self.page_cap = page_cap
        self.calls: list[tuple[int | None, int | None]] = []

    async def fetch_history(
        self, *, chat_id: int, backward: int | None = None, from_time: int | None = None
    ) -> list[dict[str, int]]:
        self.calls.append((backward, from_time))
        older = [m for m in self.all if from_time is None or m["time"] < from_time]
        want = min(backward or 40, self.page_cap)
        return older[-want:]


async def test_a_short_history_comes_back_in_one_page() -> None:
    pymax = Pymax(12)

    got = await _page_history(pymax, 1, 1000)

    assert len(got) == 12
    assert len(pymax.calls) == 2, "one page, then one that brings nothing new"


async def test_more_than_one_page_is_walked_back() -> None:
    """The defect in one assertion: forty arrived where a thousand were asked."""
    pymax = Pymax(500, page_cap=200)

    got = await _page_history(pymax, 1, 1000)

    assert len(got) == 500
    assert len(pymax.calls) > 1, "a single request could never have returned these"
    assert all(call[0] is not None for call in pymax.calls), "backward is passed"


async def test_the_asked_for_number_is_not_exceeded_by_much() -> None:
    pymax = Pymax(1000, page_cap=200)

    got = await _page_history(pymax, 1, 250)

    assert 250 <= len(got) < 250 + 200, "stops at the first page past the ask"


async def test_paging_stops_when_nothing_new_comes_back() -> None:
    """A server repeating its tail would otherwise loop for ever."""

    class Stuck(Pymax):
        async def fetch_history(self, **kwargs: Any) -> list[dict[str, int]]:
            self.calls.append((kwargs.get("backward"), kwargs.get("from_time")))
            return self.all[-5:]

    pymax = Stuck(50)
    got = await _page_history(pymax, 1, 1000)

    assert len(got) == 5
    assert len(pymax.calls) == 2, "the second page brought nothing new and it stopped"


async def test_an_older_pymax_without_paging_still_works() -> None:
    class Old:
        def __init__(self) -> None:
            self.calls = 0

        async def fetch_history(self, *, chat_id: int) -> list[dict[str, int]]:
            self.calls += 1
            return [{"id": 1, "time": 1}]

    pymax = Old()
    got = await _page_history(pymax, 1, 1000)

    assert len(got) == 1
    assert pymax.calls == 1


# ------------------------------------------------ the brush-off is for people


async def test_a_bot_is_never_told_the_bot_is_unavailable() -> None:
    """Telegram attributes some service updates to the bot itself. Answering one
    put «Этот бот недоступен.» into the owner's chat with their own bridge."""
    from bridge.telegram.owner import OwnerOnlyMiddleware

    sent: list[str] = []

    class Bot:
        async def send_message(self, chat_id: int, text: str) -> None:
            sent.append(text)

    middleware = OwnerOnlyMiddleware(owner_user_id=7)
    user = type("U", (), {"id": 555, "is_bot": True})()
    chat = type("C", (), {"id": 555, "type": "private"})()
    event = type("E", (), {"message": type("M", (), {"chat": chat})()})()

    async def handler(event: Any, data: dict[str, Any]) -> None:
        raise AssertionError("a non-owner update must not reach a handler")

    result = await middleware(
        handler, event, {"event_from_user": user, "event_chat": chat, "bot": Bot()}
    )

    assert result is None
    assert sent == [], "silence, not a line saying the bridge does not exist"


async def test_a_person_who_found_the_bot_still_gets_one_line() -> None:
    from bridge.telegram.owner import STRANGER_REPLY, OwnerOnlyMiddleware

    sent: list[str] = []

    class Bot:
        async def send_message(self, chat_id: int, text: str) -> None:
            sent.append(text)

    middleware = OwnerOnlyMiddleware(owner_user_id=7)
    user = type("U", (), {"id": 555, "is_bot": False})()
    chat = type("C", (), {"id": 555, "type": "private"})()
    event = type("E", (), {"message": type("M", (), {"chat": chat})()})()

    async def handler(event: Any, data: dict[str, Any]) -> None:
        raise AssertionError("a stranger must not reach a handler either")

    await middleware(
        handler, event, {"event_from_user": user, "event_chat": chat, "bot": Bot()}
    )

    assert sent == [STRANGER_REPLY]


async def test_pages_arrive_newest_first_and_are_delivered_oldest_first() -> None:
    """Paging walks *backwards*; a conversation reads *forwards*.

    The first page fetched is the newest block, the last page fetched is the
    oldest. Delivered in that order the chat would read end-to-start in blocks —
    the newest forty at the top, then the forty before them underneath. So the
    fetched order is the reverse of the delivered order, and the sort in
    `fetch_history` is what turns one into the other. It is not tidying up.
    """
    pymax = Pymax(500, page_cap=200)

    fetched = [m["id"] for m in await _page_history(pymax, 1, None)]

    assert fetched != sorted(fetched), "pages really do come newest-first"
    assert fetched[0] > fetched[-1], "the first page is newer than the last"
    assert sorted(fetched) == list(range(1, 501)), "and nothing is lost between them"


async def test_the_client_hands_them_over_in_conversation_order() -> None:
    """The property the sort exists for, asserted through the real method."""
    import asyncio

    from bridge.max_client import client as module

    pymax = Pymax(300, page_cap=100)
    seen: list[int] = []

    def fake_normalize(item: Any, *, own_user_id: Any, chat_id: Any) -> Any:
        seen.append(item["id"])
        return type("M", (), {"message_id": item["id"]})()

    original_normalize = module.normalize_message
    original_replace = module.replace
    module.normalize_message = fake_normalize  # type: ignore[assignment]
    module.replace = lambda item, **_: item  # type: ignore[assignment]
    try:
        instance = module.MaxClient.__new__(module.MaxClient)
        ready = asyncio.Event()
        ready.set()
        object.__setattr__(instance, "_client", pymax)
        object.__setattr__(instance, "_ready", ready)
        object.__setattr__(instance, "_own_user_id", 99)
        messages = await module.MaxClient.fetch_history(instance, 1, limit=None)
    finally:
        module.normalize_message = original_normalize  # type: ignore[assignment]
        module.replace = original_replace  # type: ignore[assignment]

    ids = [m.message_id for m in messages]
    assert ids == sorted(ids), "oldest first, whatever order the pages came in"
    assert ids == list(range(1, 301))
