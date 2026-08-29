"""The window between claiming a MAX message and writing its delivery job.

`claim_from_max` commits on its own — it has to, because the owner's session can
see an echo of the message before the job exists, and the row is what the echo
is matched against. Everything after it, up to `submit_in_order`, is work: a
render, a branch decision, an album's expected aliases. A process that dies in
there leaves a `message_map` row that every later MAX replay reads as "already
delivered", for a message that was never sent.

Two things close it, and both are general rather than per-kind:

* a MAX event that renders to nothing is **accounted** — an archived job with a
  reason — instead of returning in silence, so a deliberate non-delivery is
  distinguishable from a lost one;
* everything else is **released** at the next start: a claim with no Telegram id
  on either side and nothing in the queue under its `source_key` has provably
  reached no sender, so dropping the row lets the message be carried again.

Injected here for all three MAX→Telegram kinds, because the window belongs to
`on_max_message` and not to any one of them.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest_asyncio

from bridge.max_client import normalize_message
from bridge.routing.delivery import DeliveryPipe
from bridge.routing.owner_voice import OwnerVoice
from bridge.routing.router import BridgeRouter, BridgeTarget
from bridge.service.runtime import BridgeService
from bridge.storage import (
    BridgeStateRepository,
    Database,
    MediaGroupRepository,
    MessageMapRepository,
    OutboxRepository,
)

BOT = 9000000001
OWNER = 100000001
MAX_CHAT = 555


class _Lookup:
    def bridge_for_bot(self, bot_id: int) -> BridgeTarget:
        return BridgeTarget("mom", MAX_CHAT, BOT)

    def bridge_for_max_chat(self, max_chat_id: int) -> BridgeTarget:
        return BridgeTarget("mom", MAX_CHAT, BOT)

    def target_for_bot(self, bot_id: int) -> BridgeTarget:
        return self.bridge_for_bot(bot_id)


class _Telegram:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, bot_id: int, chat_id: int, text: str, **kw: Any) -> int | None:
        self.sent.append(text)
        return 500 + len(self.sent)

    async def send_media(self, *a: Any, **k: Any) -> int | None:
        return 1


class _Session:
    """A connected owner session that places whatever it is given."""

    def __init__(self) -> None:
        self.is_connected = True
        self.placed: list[str] = []

    async def send_own_message(self, peer_id: int, text: str, **kw: Any) -> int | None:
        self.placed.append(text)
        return 7000 + len(self.placed)

    async def send_own_file(self, *a: Any, **k: Any) -> int | None:
        return None


class _Media:
    """Stands in for `MaxMediaDelivery` on the bot path."""

    def __init__(self) -> None:
        self.delivered = 0

    async def deliver(self, *a: Any, **k: Any) -> int | None:
        self.delivered += 1
        return 600 + self.delivered

    async def deliver_receipt(self, *a: Any, **k: Any) -> Any:
        from bridge.media.delivery import DeliveryReceipt

        self.delivered += 1
        return DeliveryReceipt(head=600 + self.delivered)


class Stand:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.messages = MessageMapRepository(database)
        self.albums = MediaGroupRepository(database)
        self.outbox = OutboxRepository(database)
        self.telegram = _Telegram()
        self.session = _Session()
        self.media = _Media()

        #: Every job the queue actually attempted, in order. What "carried once"
        #: is measured against — the kind is what the branch chose, the count is
        #: what the claim let through.
        self.carried: list[str] = []

        async def send(kind: str, direction: Any, payload: dict[str, Any], sending: Any) -> int:
            await sending()
            self.carried.append(kind)
            return 999

        self.pipe = DeliveryPipe(outbox=self.outbox, send=send)
        self.router = BridgeRouter(
            lookup=_Lookup(),
            telegram=self.telegram,
            max_sender=_Telegram(),
            messages=self.messages,
            state=BridgeStateRepository(database),
            owner_chat_id=OWNER,
            pipe=self.pipe,
            media=self.media,
            own_voice=OwnerVoice(session=lambda: self.session),
            own_media=self.media,
            albums=self.albums,
        )

    async def jobs(self, max_message_id: int) -> list[dict[str, Any]]:
        rows = await self.database.query(
            "SELECT kind, state FROM outbox WHERE source_key = ?",
            (f"max:{MAX_CHAT}:{max_message_id}",),
        )
        return [dict(row) for row in rows]

    async def claims(self, max_message_id: int) -> int:
        rows = await self.database.query(
            "SELECT id FROM message_map WHERE max_message_id = ?", (max_message_id,)
        )
        return len(rows)

    async def strand(self, message: Any) -> int:
        """Exactly the window: claim committed, process dead before the job.

        The claim is written the way `on_max_message` writes it and nothing
        follows — which is what a `kill -9` between the two commits leaves.
        """
        link_id = await self.messages.claim_from_max(
            bridge_name="mom",
            max_chat_id=message.chat_id,
            max_message_id=message.message_id,
            telegram_bot_id=BOT,
            telegram_chat_id=OWNER,
            echo_fingerprint=None,
        )
        assert link_id is not None
        return link_id

    async def recover(self, *, since_ms: int = 0) -> int:
        """What the next start does about it."""
        return await BridgeService._release_unsent_claims(
            None,  # type: ignore[arg-type]
            self.messages,
            self.albums,
            since_ms=since_ms,
        )


@pytest_asyncio.fixture
async def stand(tmp_path: Path) -> Any:
    database = await Database.connect(tmp_path / "bridge.db")
    try:
        yield Stand(database)
    finally:
        await database.close()


def _message(message_id: int, *, text: str = "привет", outgoing: bool = False) -> Any:
    base = normalize_message(
        {
            "id": message_id,
            "chatId": MAX_CHAT,
            "sender": 1 if outgoing else 2,
            "text": text,
            "time": 1_785_000_000_000,
        },
        own_user_id=1,
    )
    return replace(base, is_outgoing=outgoing)


# --------------------------------------------------- the window, per direction


async def test_the_window_exists_and_is_closed_for_owner_delivery(stand: Stand) -> None:
    message = _message(4201, outgoing=True)
    link_id = await stand.strand(message)

    # Before recovery: the claim suppresses the replay and nothing was sent.
    await stand.router.on_max_message(message)
    assert await stand.jobs(4201) == [], "the claim swallowed the replay"
    assert stand.carried == []

    assert await stand.recover() == 1
    assert await stand.claims(4201) == 0, f"claim #{link_id} should be gone"

    await stand.router.on_max_message(message)
    assert [job["kind"] for job in await stand.jobs(4201)] == ["max_to_tg_owner"]
    assert stand.carried == ["max_to_tg_owner"], "carried exactly once"


async def test_the_window_exists_and_is_closed_for_text_delivery(stand: Stand) -> None:
    message = _message(4202)
    await stand.strand(message)

    await stand.router.on_max_message(message)
    assert await stand.jobs(4202) == []

    assert await stand.recover() == 1
    await stand.router.on_max_message(message)
    assert [job["kind"] for job in await stand.jobs(4202)] == ["max_to_tg_text"]
    assert stand.carried == ["max_to_tg_text"], "carried exactly once"


async def test_the_window_exists_and_is_closed_for_media_delivery(stand: Stand) -> None:
    from bridge.max_client import AttachmentKind, MaxAttachment

    message = replace(
        _message(4203, text=""),
        attachments=(
            MaxAttachment(kind=AttachmentKind.PHOTO, url="https://example.org/a.jpg"),
            MaxAttachment(kind=AttachmentKind.PHOTO, url="https://example.org/b.jpg"),
        ),
    )
    link_id = await stand.strand(message)
    # An album writes its expected aliases before the job; a released claim has
    # to take them with it or they alias a row that no longer exists.
    await stand.albums.add_part(
        media_group_id=f"max:{link_id}",
        bridge_name="mom",
        bot_id=BOT,
        payload={"kind": "photo"},
        link_id=link_id,
        direction=None,
        part_index=0,
    )

    await stand.router.on_max_message(message)
    assert await stand.jobs(4203) == []

    assert await stand.recover() == 1
    assert await stand.albums.parts_of_link(link_id) == [], "the aliases went with it"

    await stand.router.on_max_message(message)
    assert [job["kind"] for job in await stand.jobs(4203)] == ["max_to_tg_media"]


# ------------------------------------------------- what recovery must not take


async def test_a_claim_with_a_job_is_left_alone(stand: Stand) -> None:
    """A job means it reached a sender; the outcome is the queue's business."""
    message = _message(4204)
    await stand.router.on_max_message(message)
    assert await stand.jobs(4204)

    assert await stand.recover() == 0
    assert await stand.claims(4204) == 1


async def test_a_delivered_claim_is_left_alone(stand: Stand) -> None:
    """Even with the job gone: an id on either side means something was sent."""
    message = _message(4205)
    link_id = await stand.strand(message)
    await stand.messages.attach_telegram_message(link_id, 12345)

    assert await stand.recover() == 0
    assert await stand.claims(4205) == 1


async def test_older_claims_are_history_and_not_a_startup_decision(stand: Stand) -> None:
    """The sweep is bounded to the run that just died.

    Older rows are inventory: their MAX messages are long outside any catch-up
    window, and releasing them is a decision with the owner's name on it.
    """
    message = _message(4206)
    await stand.strand(message)

    assert await stand.recover(since_ms=9_999_999_999_999) == 0
    assert await stand.claims(4206) == 1


# ------------------------------------------------- nothing to deliver, on purpose


async def test_a_message_that_renders_to_nothing_is_accounted_not_dropped(
    stand: Stand,
) -> None:
    """The path that left thirty rows in the live database.

    No text, no attachment, nothing passed on. There is genuinely nothing to
    send — and returning in silence made that indistinguishable from a message
    lost in the window above.
    """
    message = _message(4207, text="")
    await stand.router.on_max_message(message)

    jobs = await stand.jobs(4207)
    assert [(job["kind"], job["state"]) for job in jobs] == [("max_to_tg_text", "archived")]
    assert stand.carried == [], "and nothing was sent"

    row = await stand.database.query_one(
        "SELECT last_error, payload_json FROM outbox WHERE source_key = ?",
        (f"max:{MAX_CHAT}:4207",),
    )
    assert row is not None
    assert "nothing to deliver" in row["last_error"]
    assert "renders to no text" in row["last_error"]


async def test_an_accounted_non_delivery_is_not_released_by_recovery(
    stand: Stand,
) -> None:
    """Otherwise the claim/release would loop on every start.

    `_render` is deterministic for a given message, so a replay decides the same
    thing. The archived job is what tells recovery this was a decision.
    """
    message = _message(4208, text="")
    await stand.router.on_max_message(message)

    assert await stand.recover() == 0
    assert await stand.claims(4208) == 1


async def test_a_replay_of_an_accounted_message_stays_accounted(stand: Stand) -> None:
    message = _message(4209, text="")
    await stand.router.on_max_message(message)
    await stand.router.on_max_message(message)

    assert len(await stand.jobs(4209)) == 1
    assert await stand.claims(4209) == 1
