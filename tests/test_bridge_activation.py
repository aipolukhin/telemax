"""A bridge is the row first, the transport second, and `active` last.

The old order was `registry.add` — which starts polling — and only then the
database row. A crash in that window left a bot carrying somebody's messages
with nothing in the register naming it: it worked until the next restart and
then disappeared, and no reconciliation could have found it.

The identity check is the other half. A token that Telegram accepted was enough
to bring a bridge up, so a rotated, restored or mis-pasted token silently bound
one contact's chat to another contact's bot — measured, and nothing anywhere
said so.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiogram import Dispatcher
from pydantic import SecretStr

from bridge.config import BridgeSource, ResolvedBridge
from bridge.provisioning.batch import BridgeConflictError
from bridge.service.runtime import LiveBridgeGateway
from bridge.storage import BridgeRecord, BridgeRepository, BridgeState, Database
from bridge.telegram.registry import (
    BotIdentityError,
    BotRegistry,
    BridgeRegistryError,
    ExpectedBot,
)

pytestmark = pytest.mark.asyncio


class FakeSession:
    async def close(self) -> None:
        return None


def bot_factory(username_of: dict[int, str]) -> Any:
    class FakeBot:
        def __init__(self, token: str, **_: object) -> None:
            self.token = token
            self.session = FakeSession()
            self.bot_id = int(token.split(":")[0])

        async def get_me(self) -> Any:
            name = username_of.get(self.bot_id, f"bot{self.bot_id}")
            return type("Me", (), {"id": self.bot_id, "username": name})()

    return FakeBot


def bridge(name: str, chat: int, token: str) -> ResolvedBridge:
    return ResolvedBridge(
        name=name,
        max_chat_id=chat,
        token_env=f"TELEMAX_BOT_{name.upper()}",
        token=SecretStr(token),
        source=BridgeSource.MTPROTO,
    )


# ------------------------------------------------------------------ identity


async def test_a_token_for_the_wrong_bot_is_refused() -> None:
    registry = BotRegistry(Dispatcher(), bot_factory=bot_factory({111: "other_max_bot"}))

    with pytest.raises(BotIdentityError, match="111"):
        await registry.add(
            bridge("aaaa", 1, "111:AAA"),
            start=False,
            expected=ExpectedBot(bot_id=222, username="aaaa_max_bot"),
        )
    assert registry.live == ()


async def test_a_username_that_does_not_match_is_refused() -> None:
    registry = BotRegistry(Dispatcher(), bot_factory=bot_factory({111: "somebody_max_bot"}))

    with pytest.raises(BotIdentityError, match="aaaa_max_bot"):
        await registry.add(
            bridge("aaaa", 1, "111:AAA"),
            start=False,
            expected=ExpectedBot(username="aaaa_max_bot"),
        )


async def test_the_username_comparison_is_case_insensitive() -> None:
    """Telegram hands usernames back in whatever case the owner typed."""
    registry = BotRegistry(Dispatcher(), bot_factory=bot_factory({111: "AaAa_Max_Bot"}))

    live = await registry.add(
        bridge("aaaa", 1, "111:AAA"),
        start=False,
        expected=ExpectedBot(username="aaaa_max_bot"),
    )
    assert live.identity.bot_id == 111


async def test_a_legacy_row_with_no_id_binds_once() -> None:
    registry = BotRegistry(Dispatcher(), bot_factory=bot_factory({111: "aaaa_max_bot"}))

    live = await registry.add(
        bridge("aaaa", 1, "111:AAA"),
        start=False,
        expected=ExpectedBot(bot_id=None, username="aaaa_max_bot"),
    )
    assert live.identity.bot_id == 111


async def test_the_guardians_own_token_is_never_a_contact_bot() -> None:
    registry = BotRegistry(
        Dispatcher(), bot_factory=bot_factory({999: "guard_telemax_bot"}), guardian_bot_id=999
    )

    with pytest.raises(BotIdentityError):
        await registry.add(bridge("aaaa", 1, "999:GGG"), start=False)


async def test_nothing_about_the_token_reaches_the_message() -> None:
    registry = BotRegistry(Dispatcher(), bot_factory=bot_factory({111: "other_max_bot"}))

    with pytest.raises(BridgeRegistryError) as raised:
        await registry.add(
            bridge("aaaa", 1, "111:SECRETSECRETSECRET"),
            start=False,
            expected=ExpectedBot(bot_id=222),
        )
    assert "SECRET" not in str(raised.value)


# ---------------------------------------------------------------- the order


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    connection = await Database.connect(tmp_path / "bridge.db")
    try:
        yield connection
    finally:
        await connection.close()


def gateway_for(
    database: Database, registry: BotRegistry, *, contact: int | None = 4242
) -> LiveBridgeGateway:
    async def contact_of_chat(max_chat_id: int) -> int | None:
        return contact

    from bridge.provisioning.secrets import ContactBotSecretStore

    return LiveBridgeGateway(
        registry=registry,
        bridges=BridgeRepository(database),
        secrets=ContactBotSecretStore(Path("/nonexistent/bots.env")),
        contact_of_chat=contact_of_chat,
    )


async def test_the_row_exists_before_anything_polls(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = BotRegistry(Dispatcher(), bot_factory=bot_factory({111: "aaaa_max_bot"}))
    bridges = BridgeRepository(database)
    gateway = gateway_for(database, registry)
    monkeypatch.setenv("TELEMAX_BOT_AAAA", "111:AAA")
    seen: list[BridgeState | None] = []

    original = registry.add

    async def watching(*args: Any, **kwargs: Any) -> Any:
        row = await bridges.by_max_chat(1)
        seen.append(row.state if row else None)
        return await original(*args, **kwargs)

    monkeypatch.setattr(registry, "add", watching)

    await gateway.start_worker(
        max_chat_id=1,
        username="aaaa_max_bot",
        token_env="TELEMAX_BOT_AAAA",
        title="Мама",
    )

    assert seen == [BridgeState.PROVISIONING], "the register knew before the poller ran"


async def test_a_bridge_is_not_active_until_it_is_checked(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = BotRegistry(Dispatcher(), bot_factory=bot_factory({111: "aaaa_max_bot"}))
    bridges = BridgeRepository(database)
    gateway = gateway_for(database, registry)
    monkeypatch.setenv("TELEMAX_BOT_AAAA", "111:AAA")

    await gateway.start_worker(
        max_chat_id=1, username="aaaa_max_bot", token_env="TELEMAX_BOT_AAAA", title="Мама"
    )
    before = await bridges.by_max_chat(1)
    await gateway.mark_active(1)
    after = await bridges.by_max_chat(1)

    assert before is not None and before.state is BridgeState.PROVISIONING
    assert after is not None and after.state is BridgeState.ACTIVE
    assert after.telegram_bot_id == 111, "getMe proved it, so it is written down"


async def test_a_failed_start_leaves_a_row_that_reconciliation_can_find(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = BotRegistry(Dispatcher(), bot_factory=bot_factory({111: "somebody_else_bot"}))
    bridges = BridgeRepository(database)
    gateway = gateway_for(database, registry)
    monkeypatch.setenv("TELEMAX_BOT_AAAA", "111:AAA")
    await bridges.upsert(
        BridgeRecord(
            bridge_name="aaaa",
            max_chat_id=1,
            token_env="TELEMAX_BOT_AAAA",
            expected_username="aaaa_max_bot",
            state=BridgeState.PROVISIONING,
        )
    )

    with pytest.raises(BotIdentityError):
        await gateway.start_worker(
            max_chat_id=1, username="aaaa_max_bot", token_env="TELEMAX_BOT_AAAA", title="Мама"
        )

    row = await bridges.by_max_chat(1)
    assert row is not None and row.state is BridgeState.PROVISIONING
    assert registry.live == (), "nothing is polling"


# ----------------------------------------------------- one chat, one bridge


async def test_a_legacy_row_keeps_its_name(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`chat300000006` and `examplebridge02` are the same bridge, not two."""
    registry = BotRegistry(Dispatcher(), bot_factory=bot_factory({111: "aaaa_max_bot"}))
    bridges = BridgeRepository(database)
    await bridges.upsert(
        BridgeRecord(
            bridge_name="chat000001",
            max_chat_id=1,
            token_env="TELEMAX_BOT_CHAT000001",
            source="guardian",
        )
    )
    gateway = gateway_for(database, registry)
    monkeypatch.setenv("TELEMAX_BOT_AAAA", "111:AAA")

    name = await gateway.start_worker(
        max_chat_id=1, username="aaaa_max_bot", token_env="TELEMAX_BOT_AAAA", title="Мама"
    )

    assert name == "chat000001", "the existing row wins"
    assert len(await bridges.all()) == 1, "no second row for one MAX chat"


async def test_the_preflight_refuses_a_chat_already_held_by_another_bot(
    database: Database,
) -> None:
    bridges = BridgeRepository(database)
    await bridges.upsert(
        BridgeRecord(
            bridge_name="aaaa",
            max_chat_id=1,
            token_env="E_A",
            expected_username="aaaa_max_bot",
        )
    )
    gateway = gateway_for(database, BotRegistry(Dispatcher()))

    with pytest.raises(BridgeConflictError, match="aaaa"):
        await gateway.preflight(max_chat_id=1, username="bbbb_max_bot")


async def test_the_preflight_refuses_a_bot_already_serving_another_chat(
    database: Database,
) -> None:
    bridges = BridgeRepository(database)
    await bridges.upsert(
        BridgeRecord(
            bridge_name="aaaa",
            max_chat_id=1,
            token_env="E_A",
            expected_username="aaaa_max_bot",
        )
    )
    gateway = gateway_for(database, BotRegistry(Dispatcher()))

    with pytest.raises(BridgeConflictError, match="MAX 1"):
        await gateway.preflight(max_chat_id=2, username="aaaa_max_bot")


async def test_the_preflight_allows_the_ordinary_case(database: Database) -> None:
    gateway = gateway_for(database, BotRegistry(Dispatcher()))
    await gateway.preflight(max_chat_id=1, username="aaaa_max_bot")
