"""MAX is edited and deleted only through the durable mutation path.

Both Bot API entrances are gone. `/del` went when deletion got one gesture, and
the `edited_message` fallback went when the puppet session became the only
authority on what the owner did. What is left is one ingress per mutation, over
MTProto, and the resolvers behind it — which `tests/test_owner_mutation.py` owns.

What this file keeps is the structural half: nothing edits or deletes in MAX
outside that path, whatever else changes around it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest_asyncio

from bridge.routing.delivery import (
    KIND_TG_TO_MAX_DELETE,
    KIND_TG_TO_MAX_EDIT,
    DeliveryPipe,
)
from bridge.routing.owner_mutation import resolve_delete, resolve_edit
from bridge.routing.router import BridgeRouter, BridgeTarget
from bridge.storage import (
    BridgeStateRepository,
    Database,
    MessageMapRepository,
    OutboxRepository,
)

BOT = 9000000001
MAX_CHAT = 555
OWNER_CHAT = 100000001
ACCOUNT = 100000001


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


class MaxSpy:
    def __init__(self) -> None:
        self.edits: list[tuple[int, int, str]] = []
        self.deletes: list[tuple[int, list[int]]] = []

    async def send_text(self, *a: Any, **k: Any) -> int:
        return 1

    async def send_media(self, *a: Any, **k: Any) -> int:
        return 1

    async def send_contact(self, *a: Any, **k: Any) -> int:
        return 1

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> None:
        self.edits.append((chat_id, message_id, text))

    async def delete_messages(
        self, chat_id: int, message_ids: list[int], *, for_everyone: bool = True
    ) -> None:
        self.deletes.append((chat_id, list(message_ids)))


class TelegramSpy:
    def __init__(self) -> None:
        self.deleted: list[int] = []

    async def delete(self, bot_id: int, chat_id: int, message_id: int) -> None:
        self.deleted.append(message_id)

    async def send_text(self, *a: Any, **k: Any) -> int:
        return 1


class Lookup:
    def bridge_for_bot(self, bot_id: int) -> BridgeTarget | None:
        return BridgeTarget(name="mom", max_chat_id=MAX_CHAT, bot_id=BOT) if bot_id == BOT else None

    def bridge_for_max_chat(self, max_chat_id: int) -> BridgeTarget | None:
        return (
            BridgeTarget(name="mom", max_chat_id=MAX_CHAT, bot_id=BOT)
            if max_chat_id == MAX_CHAT
            else None
        )


class Live:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.messages = MessageMapRepository(database)
        self.outbox = OutboxRepository(database)
        self.max = MaxSpy()
        self.telegram = TelegramSpy()
        self.router = BridgeRouter(
            lookup=Lookup(),
            telegram=self.telegram,
            max_sender=self.max,
            messages=self.messages,
            state=BridgeStateRepository(database),
            owner_chat_id=OWNER_CHAT,
            pipe=DeliveryPipe(outbox=self.outbox, send=self._send),
        )

    async def _send(self, kind: str, direction: Any, payload: dict[str, Any], sending: Any) -> Any:
        """The mutation branches of `send_job`, verbatim."""
        if kind == KIND_TG_TO_MAX_EDIT:
            return await resolve_edit(
                outbox=self.outbox, messages=self.messages, max_sender=self.max, payload=payload
            )
        if kind == KIND_TG_TO_MAX_DELETE:
            return await resolve_delete(
                outbox=self.outbox, messages=self.messages, max_sender=self.max, payload=payload
            )
        return 1

    async def a_delivered_message(
        self, *, telegram_message_id: int, max_message_id: int, owner_message_id: int | None
    ) -> int:
        link_id = await self.messages.record_from_telegram(
            bridge_name="mom",
            max_chat_id=MAX_CHAT,
            telegram_bot_id=BOT,
            telegram_chat_id=OWNER_CHAT,
            telegram_message_id=telegram_message_id,
            telegram_owner_message_id=owner_message_id,
            telegram_owner_account_id=ACCOUNT if owner_message_id else None,
        )
        await self.messages.attach_max_message(link_id, max_message_id)
        return link_id

    async def jobs(self, kind: str) -> list[Any]:
        return await self.database.query(
            "SELECT source_key FROM outbox WHERE kind = ?", (kind,)
        )


# ------------------------------------------------------------- no direct calls


def test_nothing_edits_or_deletes_in_max_outside_the_mutation_path() -> None:
    """The structural half. `router` may still touch MAX for a *creating* send;
    what it may not do is edit or delete outside the resolvers and the no-queue
    fallback they share."""
    import ast

    allowed = {
        "bridge/routing/owner_mutation.py",  # the resolvers themselves
        "bridge/routing/adapters.py",  # the MaxClient adapter
        "bridge/max_client/client.py",  # and the client
    }
    offenders: list[str] = []
    for path in sorted(Path("bridge").rglob("*.py")):
        if str(path) in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in ("edit_text", "delete_messages"):
                continue
            owner = ast.unparse(node.func.value)
            if owner in ("self._max", "max_sender", "self._client"):
                enclosing = [
                    parent.name
                    for parent in ast.walk(tree)
                    if isinstance(parent, ast.AsyncFunctionDef | ast.FunctionDef)
                    and parent.lineno <= node.lineno <= (parent.end_lineno or 0)
                ]
                if "_apply_mutation_directly" in enclosing:
                    continue  # the documented no-queue fallback
                offenders.append(f"{path}:{node.lineno} {owner}.{node.func.attr}")
    assert offenders == [], f"direct MAX edit/delete outside the mutation path: {offenders}"
