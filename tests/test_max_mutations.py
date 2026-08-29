"""MAX→Telegram edit and delete, carried by the queue and addressed correctly.

Both used to be direct Bot API calls out of a MAX ingress handler, through a
port that answered `False` for every failure. Three consequences, and each is
asserted here as its opposite: an album lost its head and kept the rest, a
caption edit could not be expressed at all, and a message the owner had placed
was addressed with the bot's view of somebody else's id.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError

from bridge.retry.worker import PermanentDeliveryError
from bridge.routing.delivery import (
    KIND_MAX_TO_TG_MEDIA,
    KIND_MAX_TO_TG_OWNER,
    KIND_MAX_TO_TG_TEXT,
    DeferDelivery,
)
from bridge.routing.max_mutation import (
    DELETED_NOTICE,
    Authorship,
    delete_source_key,
    edit_source_key,
    resolve_max_delete,
    resolve_max_edit,
    resolve_target,
)
from bridge.storage import (
    Database,
    Direction,
    MediaGroupRepository,
    MessageMapRepository,
    OutboxRepository,
)

BRIDGE = "mum"
BOT = 111
OWNER_CHAT = 999
OWNER_ACCOUNT = 100000001
MAX_CHAT = 7


def bad_request(description: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=None, message=description)  # type: ignore[arg-type]


class FakeBot:
    """`BotMutations` that records and can be told to refuse."""

    def __init__(self, fail: dict[str, BaseException] | None = None) -> None:
        self.calls: list[tuple[str, int, Any]] = []
        self._fail = dict(fail or {})

    async def delete(self, bot_id: int, chat_id: int, message_id: int) -> None:
        self.calls.append(("delete", message_id, None))
        error = self._fail.get("delete")
        if error is not None:
            raise error

    async def edit_text(
        self,
        bot_id: int,
        chat_id: int,
        message_id: int,
        text: str,
        entities: list[dict[str, Any]] | None = None,
    ) -> None:
        self.calls.append(("edit_text", message_id, text))
        error = self._fail.pop("edit_text", None)
        if error is not None:
            raise error

    async def edit_caption(
        self,
        bot_id: int,
        chat_id: int,
        message_id: int,
        caption: str,
        entities: list[dict[str, Any]] | None = None,
    ) -> None:
        self.calls.append(("edit_caption", message_id, caption))
        error = self._fail.pop("edit_caption", None)
        if error is not None:
            raise error


class FakeOwner:
    def __init__(self, *, connected: bool = True) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._connected = connected

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def delete_own_messages(self, peer_id: int, message_ids: list[int]) -> None:
        self.calls.append(("delete", (peer_id, list(message_ids))))

    async def edit_own_message(
        self,
        peer_id: int,
        message_id: int,
        text: str,
        *,
        entities: list[dict[str, Any]] | None = None,
    ) -> None:
        self.calls.append(("edit", (peer_id, message_id, text)))


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


async def _delivered(
    database: Database,
    *,
    kind: str,
    max_message_id: int = 1,
    parts: list[int] | None = None,
    owner_parts: list[int] | None = None,
    caption_on: int = 0,
) -> int:
    """One MAX message already in Telegram, with a job that says who put it there."""
    messages = MessageMapRepository(database)
    albums = MediaGroupRepository(database)
    outbox = OutboxRepository(database)

    link_id = await messages.claim_from_max(
        bridge_name=BRIDGE,
        max_chat_id=MAX_CHAT,
        max_message_id=max_message_id,
        telegram_bot_id=BOT,
        telegram_chat_id=OWNER_CHAT,
    )
    assert link_id is not None
    if kind == KIND_MAX_TO_TG_OWNER:
        await messages.attach_owner_message(
            link_id, (owner_parts or [6001])[0], telegram_owner_account_id=OWNER_ACCOUNT
        )
    else:
        await messages.attach_telegram_message(link_id, (parts or [9001])[0])

    if (parts and len(parts) > 1) or (owner_parts and len(owner_parts) > 1):
        count = len(parts or owner_parts or [])
        for index in range(count):
            await albums.add_part(
                media_group_id=f"max:{link_id}",
                bridge_name=BRIDGE,
                bot_id=BOT,
                payload={"kind": "photo"},
                link_id=link_id,
                direction=Direction.MAX_TO_TG,
                part_index=index,
                media_kind="photo",
                caption_present=index == caption_on,
                part_fingerprint=f"a1:{max_message_id}:{index}",
                telegram_message_id=parts[index] if parts else None,
                telegram_owner_account_id=OWNER_ACCOUNT if owner_parts else None,
                telegram_owner_message_id=owner_parts[index] if owner_parts else None,
            )

    job_id = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=kind,
        payload={},
        source_key=f"max:{MAX_CHAT}:{max_message_id}",
    )
    # Delivered, so a mutation behind it is not held back by the ordering rule.
    await outbox.mark_done(job_id, remote_message_id=(parts or [9001])[0])
    return link_id


def _repos(database: Database) -> dict[str, Any]:
    return {
        "messages": MessageMapRepository(database),
        "albums": MediaGroupRepository(database),
        "outbox": OutboxRepository(database),
    }


# --------------------------------------------------------------- source keys


def test_a_delete_is_keyed_by_the_message_and_nothing_else() -> None:
    """A message is deleted once and stays deleted; a replay finds that job."""
    assert delete_source_key(7, 42) == delete_source_key(7, 42)
    assert delete_source_key(7, 42) != delete_source_key(7, 43)


def test_two_edits_are_two_jobs() -> None:
    """Keying an edit by the message alone would drop everything after the first."""
    assert edit_source_key(7, 42, "v1") != edit_source_key(7, 42, "v2")


# ------------------------------------------------------------------ authorship


@pytest.mark.asyncio
async def test_the_delivering_job_decides_who_may_change_it(database: Database) -> None:
    """Both columns fill in eventually, so neither can tell the two apart."""
    messages = MessageMapRepository(database)
    bot_link = await _delivered(database, kind=KIND_MAX_TO_TG_TEXT, max_message_id=1)
    owner_link = await _delivered(database, kind=KIND_MAX_TO_TG_OWNER, max_message_id=2)
    # The bot's copy of an owner placement arrives later and fills the other
    # column; the echo of a bot delivery does the same in reverse.
    await messages.attach_owner_message(bot_link, 6100, telegram_owner_account_id=OWNER_ACCOUNT)
    await messages.attach_telegram_message(owner_link, 9100)

    bot_target = await resolve_target(link_id=bot_link, **_repos(database))
    owner_target = await resolve_target(link_id=owner_link, **_repos(database))
    assert bot_target is not None and bot_target.authorship is Authorship.BOT
    assert owner_target is not None and owner_target.authorship is Authorship.OWNER
    assert bot_target.message_ids == (9001,)
    assert owner_target.message_ids == (6001,)


@pytest.mark.asyncio
async def test_a_message_with_nothing_in_telegram_resolves_to_nothing(
    database: Database,
) -> None:
    messages = MessageMapRepository(database)
    link_id = await messages.claim_from_max(
        bridge_name=BRIDGE,
        max_chat_id=MAX_CHAT,
        max_message_id=77,
        telegram_bot_id=BOT,
        telegram_chat_id=OWNER_CHAT,
    )
    assert link_id is not None
    assert await resolve_target(link_id=link_id, **_repos(database)) is None


# ---------------------------------------------------------------------- delete


@pytest.mark.asyncio
async def test_a_canonical_text_delete_removes_one_message(database: Database) -> None:
    link_id = await _delivered(database, kind=KIND_MAX_TO_TG_TEXT)
    bot = FakeBot()
    await resolve_max_delete(
        bot=bot, owner=None, payload={"link_id": link_id}, **_repos(database)
    )
    assert bot.calls == [("delete", 9001, None)]


@pytest.mark.asyncio
async def test_a_bot_album_loses_every_part(database: Database) -> None:
    """The defect: one of three removed, the rest left as an orphaned group."""
    link_id = await _delivered(
        database, kind=KIND_MAX_TO_TG_MEDIA, parts=[9001, 9002, 9003]
    )
    bot = FakeBot()
    await resolve_max_delete(
        bot=bot, owner=None, payload={"link_id": link_id}, **_repos(database)
    )
    assert [call[1] for call in bot.calls] == [9001, 9002, 9003]


@pytest.mark.asyncio
async def test_an_owner_album_goes_through_the_owner_session(database: Database) -> None:
    link_id = await _delivered(
        database, kind=KIND_MAX_TO_TG_OWNER, owner_parts=[6001, 6002, 6003]
    )
    bot, owner = FakeBot(), FakeOwner()
    await resolve_max_delete(
        bot=bot, owner=owner, payload={"link_id": link_id}, **_repos(database)
    )
    assert bot.calls == [], "an owner-placed message must never go through the bot"
    assert owner.calls == [("delete", (BOT, [6001, 6002, 6003]))]


@pytest.mark.asyncio
async def test_already_deleted_is_success(database: Database) -> None:
    link_id = await _delivered(database, kind=KIND_MAX_TO_TG_TEXT)
    bot = FakeBot(fail={"delete": bad_request("Bad Request: message to delete not found")})
    await resolve_max_delete(
        bot=bot, owner=None, payload={"link_id": link_id}, **_repos(database)
    )


@pytest.mark.asyncio
async def test_a_timeout_is_retried_and_the_rest_are_no_ops(database: Database) -> None:
    """Partial batches heal themselves: what went is gone next time round."""
    link_id = await _delivered(
        database, kind=KIND_MAX_TO_TG_MEDIA, parts=[9001, 9002, 9003]
    )
    bot = FakeBot()
    bot._fail["delete"] = TelegramNetworkError(method=None, message="Request timeout error")  # type: ignore[arg-type]
    with pytest.raises(TelegramNetworkError):
        await resolve_max_delete(
            bot=bot, owner=None, payload={"link_id": link_id}, **_repos(database)
        )
    assert len(bot.calls) == 1, "the batch stops at the first unknown outcome"


@pytest.mark.asyncio
async def test_a_confirmed_refusal_marks_the_head_and_stops(database: Database) -> None:
    link_id = await _delivered(database, kind=KIND_MAX_TO_TG_TEXT)
    bot = FakeBot(fail={"delete": bad_request("Bad Request: message can't be deleted")})
    with pytest.raises(PermanentDeliveryError, match="refused to delete"):
        await resolve_max_delete(
            bot=bot, owner=None, payload={"link_id": link_id}, **_repos(database)
        )
    assert ("edit_text", 9001, DELETED_NOTICE) in bot.calls


@pytest.mark.asyncio
async def test_an_owner_delete_waits_for_the_session(database: Database) -> None:
    link_id = await _delivered(database, kind=KIND_MAX_TO_TG_OWNER)
    with pytest.raises(DeferDelivery):
        await resolve_max_delete(
            bot=FakeBot(),
            owner=FakeOwner(connected=False),
            payload={"link_id": link_id},
            **_repos(database),
        )


@pytest.mark.asyncio
async def test_deleting_a_message_that_was_never_delivered_is_a_no_op(
    database: Database,
) -> None:
    messages = MessageMapRepository(database)
    link_id = await messages.claim_from_max(
        bridge_name=BRIDGE,
        max_chat_id=MAX_CHAT,
        max_message_id=88,
        telegram_bot_id=BOT,
        telegram_chat_id=OWNER_CHAT,
    )
    bot = FakeBot()
    await resolve_max_delete(
        bot=bot, owner=None, payload={"link_id": link_id}, **_repos(database)
    )
    assert bot.calls == []


# ------------------------------------------------------------------------ edit


@pytest.mark.asyncio
async def test_a_text_message_is_edited_as_text(database: Database) -> None:
    link_id = await _delivered(database, kind=KIND_MAX_TO_TG_TEXT)
    bot = FakeBot()
    await resolve_max_edit(
        bot=bot,
        owner=None,
        payload={"link_id": link_id, "text": "новый текст"},
        **_repos(database),
    )
    assert bot.calls == [("edit_text", 9001, "новый текст")]


@pytest.mark.asyncio
async def test_a_media_message_is_edited_as_a_caption(database: Database) -> None:
    """`editMessageCaption` did not exist in the tree; every caption edit was lost."""
    link_id = await _delivered(database, kind=KIND_MAX_TO_TG_MEDIA)
    bot = FakeBot()
    await resolve_max_edit(
        bot=bot,
        owner=None,
        payload={"link_id": link_id, "text": "новая подпись"},
        **_repos(database),
    )
    assert bot.calls == [("edit_caption", 9001, "новая подпись")]


@pytest.mark.asyncio
async def test_an_album_caption_goes_to_the_part_that_carries_it(
    database: Database,
) -> None:
    """Read from the alias that recorded carrying it, not assumed to be part 0."""
    link_id = await _delivered(
        database,
        kind=KIND_MAX_TO_TG_MEDIA,
        parts=[9001, 9002, 9003],
        caption_on=1,
    )
    bot = FakeBot()
    await resolve_max_edit(
        bot=bot,
        owner=None,
        payload={"link_id": link_id, "text": "подпись"},
        **_repos(database),
    )
    assert bot.calls == [("edit_caption", 9002, "подпись")]


@pytest.mark.asyncio
async def test_an_owner_message_is_edited_over_the_owner_session(
    database: Database,
) -> None:
    link_id = await _delivered(database, kind=KIND_MAX_TO_TG_OWNER)
    bot, owner = FakeBot(), FakeOwner()
    await resolve_max_edit(
        bot=bot,
        owner=owner,
        payload={"link_id": link_id, "text": "правка"},
        **_repos(database),
    )
    assert bot.calls == []
    assert owner.calls == [("edit", (BOT, 6001, "правка"))]


@pytest.mark.asyncio
async def test_not_modified_is_success(database: Database) -> None:
    link_id = await _delivered(database, kind=KIND_MAX_TO_TG_TEXT)
    bot = FakeBot(fail={"edit_text": bad_request("Bad Request: message is not modified")})
    await resolve_max_edit(
        bot=bot, owner=None, payload={"link_id": link_id, "text": "то же"}, **_repos(database)
    )


@pytest.mark.asyncio
async def test_a_deleted_target_is_a_terminal_no_op(database: Database) -> None:
    link_id = await _delivered(database, kind=KIND_MAX_TO_TG_TEXT)
    bot = FakeBot(fail={"edit_text": bad_request("Bad Request: message to edit not found")})
    await resolve_max_edit(
        bot=bot, owner=None, payload={"link_id": link_id, "text": "поздно"}, **_repos(database)
    )


@pytest.mark.asyncio
async def test_a_permission_refusal_is_permanent(database: Database) -> None:
    link_id = await _delivered(database, kind=KIND_MAX_TO_TG_TEXT)
    bot = FakeBot(fail={"edit_text": bad_request("Bad Request: message can't be edited")})
    with pytest.raises(PermanentDeliveryError):
        await resolve_max_edit(
            bot=bot,
            owner=None,
            payload={"link_id": link_id, "text": "нельзя"},
            **_repos(database),
        )


@pytest.mark.asyncio
async def test_a_timeout_on_an_edit_is_retried(database: Database) -> None:
    link_id = await _delivered(database, kind=KIND_MAX_TO_TG_TEXT)
    bot = FakeBot(
        fail={"edit_text": TelegramNetworkError(method=None, message="Request timeout error")}  # type: ignore[arg-type]
    )
    with pytest.raises(TelegramNetworkError):
        await resolve_max_edit(
            bot=bot,
            owner=None,
            payload={"link_id": link_id, "text": "ещё раз"},
            **_repos(database),
        )


@pytest.mark.asyncio
async def test_the_wrong_edit_half_switches_once_and_only_on_that_answer(
    database: Database,
) -> None:
    """An album whose attachments all failed is a text message wearing a media job."""
    link_id = await _delivered(database, kind=KIND_MAX_TO_TG_MEDIA)
    bot = FakeBot(
        fail={
            "edit_caption": bad_request(
                "Bad Request: there is no caption in the message to edit"
            )
        }
    )
    await resolve_max_edit(
        bot=bot, owner=None, payload={"link_id": link_id, "text": "тело"}, **_repos(database)
    )
    assert [call[0] for call in bot.calls] == ["edit_caption", "edit_text"]


@pytest.mark.asyncio
async def test_a_timeout_never_switches_the_edit_half(database: Database) -> None:
    link_id = await _delivered(database, kind=KIND_MAX_TO_TG_MEDIA)
    bot = FakeBot(
        fail={
            "edit_caption": TelegramNetworkError(method=None, message="Request timeout error")  # type: ignore[arg-type]
        }
    )
    with pytest.raises(TelegramNetworkError):
        await resolve_max_edit(
            bot=bot,
            owner=None,
            payload={"link_id": link_id, "text": "тело"},
            **_repos(database),
        )
    assert [call[0] for call in bot.calls] == ["edit_caption"]


@pytest.mark.asyncio
async def test_an_owner_edit_waits_for_the_session(database: Database) -> None:
    link_id = await _delivered(database, kind=KIND_MAX_TO_TG_OWNER)
    with pytest.raises(DeferDelivery):
        await resolve_max_edit(
            bot=FakeBot(),
            owner=FakeOwner(connected=False),
            payload={"link_id": link_id, "text": "правка"},
            **_repos(database),
        )


@pytest.mark.asyncio
async def test_an_edit_is_idempotent(database: Database) -> None:
    link_id = await _delivered(database, kind=KIND_MAX_TO_TG_TEXT)
    bot = FakeBot()
    for _ in range(3):
        await resolve_max_edit(
            bot=bot,
            owner=None,
            payload={"link_id": link_id, "text": "одно и то же"},
            **_repos(database),
        )
    assert [call[2] for call in bot.calls] == ["одно и то же"] * 3


# ------------------------------------------------- through the router and the queue


class _Lookup:
    def bridge_for_max_chat(self, max_chat_id: int) -> Any:
        from bridge.routing.router import BridgeTarget

        return BridgeTarget(name=BRIDGE, max_chat_id=MAX_CHAT, bot_id=BOT)

    def bridge_for_bot(self, bot_id: int) -> Any:
        return self.bridge_for_max_chat(0)


class _NoSend:
    async def send_text(self, *args: Any, **kwargs: Any) -> int:
        raise AssertionError("a mutation must never send a message")


async def _router(database: Database, applied: list[dict[str, Any]]) -> Any:
    from bridge.routing.delivery import DeliveryPipe
    from bridge.routing.router import BridgeRouter
    from bridge.storage import BridgeStateRepository

    async def send(kind: str, direction: Direction, payload: dict[str, Any], sending: Any) -> int:
        # The mutation kinds never announce a remote boundary: nothing they do
        # can be duplicated by a repeat.
        applied.append({"kind": kind, **payload})
        return 0

    return BridgeRouter(
        lookup=_Lookup(),
        telegram=_NoSend(),
        max_sender=_NoSend(),
        messages=MessageMapRepository(database),
        state=BridgeStateRepository(database),
        owner_chat_id=OWNER_CHAT,
        pipe=DeliveryPipe(outbox=OutboxRepository(database), send=send),
        albums=MediaGroupRepository(database),
    )


@pytest.mark.asyncio
async def test_a_max_delete_becomes_exactly_one_durable_job(database: Database) -> None:
    from bridge.max_client import MessageDeleted

    await _delivered(database, kind=KIND_MAX_TO_TG_TEXT, max_message_id=1)
    applied: list[dict[str, Any]] = []
    router = await _router(database, applied)

    event = MessageDeleted(chat_id=MAX_CHAT, message_ids=(1,))
    await router.on_max_delete(event)
    await router.on_max_delete(event)  # a reconnect replaying it

    jobs = await database.query(
        "SELECT kind, source_key FROM outbox WHERE kind = 'max_to_tg_delete'"
    )
    assert len(jobs) == 1
    assert jobs[0]["source_key"] == delete_source_key(MAX_CHAT, 1)
    assert len(applied) == 1, "the replay must not apply the deletion twice"


@pytest.mark.asyncio
async def test_two_edits_are_applied_in_order_and_the_last_one_wins(
    database: Database,
) -> None:
    """FIFO per bridge and direction is the coalescing; nothing merges anything."""
    from bridge.max_client import IncomingMaxMessage

    await _delivered(database, kind=KIND_MAX_TO_TG_TEXT, max_message_id=1)
    applied: list[dict[str, Any]] = []
    router = await _router(database, applied)

    for version, text in ((10, "первая правка"), (20, "вторая правка")):
        await router.on_max_edit(
            IncomingMaxMessage(
                message_id=1,
                chat_id=MAX_CHAT,
                sender_id=None,
                text=text,
                timestamp=0,
                is_outgoing=False,
                edited_at=version,
            )
        )

    edits = [job for job in applied if job["kind"] == "max_to_tg_edit"]
    assert len(edits) == 2
    # The body carries a stamp and an edit mark around it; what matters is which
    # version landed last.
    assert "первая правка" in edits[0]["text"]
    assert "вторая правка" in edits[-1]["text"]


@pytest.mark.asyncio
async def test_the_same_edit_replayed_makes_one_job(database: Database) -> None:
    from bridge.max_client import IncomingMaxMessage

    await _delivered(database, kind=KIND_MAX_TO_TG_TEXT, max_message_id=1)
    applied: list[dict[str, Any]] = []
    router = await _router(database, applied)

    message = IncomingMaxMessage(
        message_id=1,
        chat_id=MAX_CHAT,
        sender_id=None,
        text="правка",
        timestamp=0,
        is_outgoing=False,
        edited_at=10,
    )
    await router.on_max_edit(message)
    await router.on_max_edit(message)

    jobs = await database.query("SELECT id FROM outbox WHERE kind = 'max_to_tg_edit'")
    assert len(jobs) == 1


# --------------------------------------- the body an edit wears depends on the author


@pytest.mark.asyncio
async def test_an_owner_edit_never_stamps_the_bot_marker(database: Database) -> None:
    """`Вы: ` on a message the owner placed is the wrong side of the screen.

    Caught by the live stand, not by a unit test: `on_max_edit` rendered with the
    bot renderer whatever the authorship, and the marker only became visible once
    the edit started landing at all.
    """
    link_id = await _delivered(database, kind=KIND_MAX_TO_TG_OWNER)
    bot, owner = FakeBot(), FakeOwner()
    await resolve_max_edit(
        bot=bot,
        owner=owner,
        payload={
            "link_id": link_id,
            "text": "Вы: [12:00]тело",
            "owner_text": "[12:00]тело",
        },
        **_repos(database),
    )
    assert owner.calls == [("edit", (BOT, 6001, "[12:00]тело"))]


@pytest.mark.asyncio
async def test_a_bot_edit_keeps_the_bot_rendering(database: Database) -> None:
    link_id = await _delivered(database, kind=KIND_MAX_TO_TG_TEXT)
    bot = FakeBot()
    await resolve_max_edit(
        bot=bot,
        owner=None,
        payload={
            "link_id": link_id,
            "text": "Вы: [12:00]тело",
            "owner_text": "[12:00]тело",
        },
        **_repos(database),
    )
    assert bot.calls == [("edit_text", 9001, "Вы: [12:00]тело")]


@pytest.mark.asyncio
async def test_an_edit_without_an_owner_rendering_still_applies(
    database: Database,
) -> None:
    """A job written before this change carries only `text`; it must still land."""
    link_id = await _delivered(database, kind=KIND_MAX_TO_TG_OWNER)
    owner = FakeOwner()
    await resolve_max_edit(
        bot=FakeBot(),
        owner=owner,
        payload={"link_id": link_id, "text": "старый формат"},
        **_repos(database),
    )
    assert owner.calls == [("edit", (BOT, 6001, "старый формат"))]


def test_the_ingress_renders_both_authorships_and_captions_as_captions() -> None:
    """`_render` appends `[photo]`; a caption never had it and must not gain it.

    And both renderings have to be produced here, because the job that applies
    the edit cannot re-render — the MAX message is gone by then.
    """
    import ast
    from pathlib import Path

    from bridge.routing import router as router_module

    tree = ast.parse(Path(router_module.__file__).read_text(encoding="utf-8"))
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_max_edit"
    )
    called = {
        node.func.attr
        for node in ast.walk(handler)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert {"_caption", "_caption_as_owner", "_render", "_render_as_owner"} <= called


@pytest.mark.asyncio
async def test_the_resolver_applies_the_body_it_was_given(database: Database) -> None:
    """The mark travels inside the body now, italics and all.

    It briefly did not: the resolver appended it for a bot's text edit only,
    because Telegram hides its own label exactly there. That asymmetry is gone —
    the mark is drawn on every rendering — so this side only picks the rendering
    that matches the author and sends it unchanged.
    """
    payload = {"text": "тело (изм. 10:13)", "owner_text": "тело (изм. 10:13)"}

    text_link = await _delivered(
        database, kind=KIND_MAX_TO_TG_TEXT, max_message_id=1, parts=[9101]
    )
    bot = FakeBot()
    await resolve_max_edit(
        bot=bot, owner=None, payload={"link_id": text_link, **payload}, **_repos(database)
    )
    assert bot.calls == [("edit_text", 9101, "тело (изм. 10:13)")]

    media_link = await _delivered(
        database, kind=KIND_MAX_TO_TG_MEDIA, max_message_id=2, parts=[9102]
    )
    bot = FakeBot()
    await resolve_max_edit(
        bot=bot, owner=None, payload={"link_id": media_link, **payload}, **_repos(database)
    )
    assert bot.calls == [("edit_caption", 9102, "тело (изм. 10:13)")]

    owner_link = await _delivered(
        database, kind=KIND_MAX_TO_TG_OWNER, max_message_id=3, owner_parts=[6101]
    )
    owner = FakeOwner()
    await resolve_max_edit(
        bot=FakeBot(), owner=owner, payload={"link_id": owner_link, **payload}, **_repos(database)
    )
    assert owner.calls == [("edit", (BOT, 6101, "тело (изм. 10:13)"))]
