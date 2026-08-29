"""WP19 — a new MAX contact becomes a bridge without a restart."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from bridge.config import AppConfig, ProvisioningMode, UnknownChatPolicy
from bridge.max_client import normalize_message
from bridge.provisioning import Provisioner, ProvisioningError
from bridge.storage import (
    BridgeRepository,
    Database,
    PendingContactRepository,
    PendingContactState,
)

MAX_CHAT = 777
CONTACT = 4242
TOKEN = "123456789:AAdummy-token-value-for-tests-0000"


@dataclass(slots=True)
class FakeActivator:
    added: list[Any] = field(default_factory=list)
    fail_with: Exception | None = None

    async def add(self, bridge: Any, *, start: bool = True) -> object:
        if self.fail_with is not None:
            raise self.fail_with
        self.added.append(bridge)
        return bridge


@dataclass(slots=True)
class FakeReplayer:
    replayed: list[tuple[str, list[dict[str, Any]]]] = field(default_factory=list)

    async def replay(self, bridge_name: str, messages: list[dict[str, Any]]) -> None:
        self.replayed.append((bridge_name, messages))


@dataclass(slots=True)
class Harness:
    provisioner: Provisioner
    activator: FakeActivator
    replayer: FakeReplayer
    pending: PendingContactRepository
    bridges: BridgeRepository
    config: AppConfig


@pytest_asyncio.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    database = await Database.connect(tmp_path / "bridge.db")
    config = AppConfig.model_validate(
        {
            "telegram": {"owner_user_id": 1},
            "paths": {"data_dir": str(tmp_path / "data")},
            "provisioning": {"mode": ProvisioningMode.GUARDIAN.value},
        }
    )
    activator, replayer = FakeActivator(), FakeReplayer()
    pending = PendingContactRepository(database)
    bridges = BridgeRepository(database)
    try:
        yield Harness(
            provisioner=Provisioner(
                config=config,
                bridges=bridges,
                pending=pending,
                activator=activator,
                replayer=replayer,
            ),
            activator=activator,
            replayer=replayer,
            pending=pending,
            bridges=bridges,
            config=config,
        )
    finally:
        await database.close()
        os.environ.pop("TELEMAX_BOT_CHAT777", None)


def incoming(message_id: int, text: str) -> Any:
    return normalize_message(
        {"id": message_id, "chatId": MAX_CHAT, "sender": CONTACT, "text": text, "time": 1},
        own_user_id=999,
    )


async def test_first_message_asks_the_owner_once(harness: Harness) -> None:
    first = await harness.provisioner.on_unbridged_message(
        incoming(1, "привет"), display_name="Тётя"
    )
    second = await harness.provisioner.on_unbridged_message(
        incoming(2, "ты тут?"), display_name=None
    )

    assert first is not None
    assert first.display_name == "Тётя"
    assert second is None, "the owner must be asked once, not per message"

    contact = await harness.pending.get(MAX_CHAT)
    assert contact is not None
    assert contact.state is PendingContactState.ASKED
    assert contact.buffered == 2, "both messages are held for replay"


async def test_simultaneous_messages_do_not_ask_twice(harness: Harness) -> None:
    """Two events arriving together must not produce two questions."""
    results = await asyncio.gather(
        harness.provisioner.on_unbridged_message(incoming(1, "a"), display_name="Тётя"),
        harness.provisioner.on_unbridged_message(incoming(2, "b"), display_name="Тётя"),
    )

    assert sum(1 for item in results if item is not None) == 1


async def test_ignored_contact_is_not_asked_about_again(harness: Harness) -> None:
    await harness.provisioner.on_unbridged_message(incoming(1, "привет"), display_name="Тётя")
    await harness.provisioner.ignore(MAX_CHAT)

    assert await harness.provisioner.on_unbridged_message(incoming(2, "?"), display_name=None) is (
        None
    )


async def test_blocking_also_drops_the_backlog(harness: Harness) -> None:
    await harness.provisioner.on_unbridged_message(incoming(1, "привет"), display_name="Тётя")
    await harness.provisioner.ignore(MAX_CHAT, block=True)

    contact = await harness.pending.get(MAX_CHAT)
    assert contact is not None
    assert contact.buffered == 0


async def test_ignore_policy_never_buffers(tmp_path: Path, harness: Harness) -> None:
    harness.provisioner._config = AppConfig.model_validate(
        {
            "telegram": {"owner_user_id": 1},
            "paths": {"data_dir": str(tmp_path / "data")},
            "provisioning": {"unknown_chat_policy": UnknownChatPolicy.IGNORE.value},
        }
    )

    assert await harness.provisioner.on_unbridged_message(incoming(1, "hi"), display_name=None) is (
        None
    )
    assert await harness.pending.get(MAX_CHAT) is None


async def test_activation_brings_the_bridge_up_and_replays(harness: Harness) -> None:
    await harness.provisioner.on_unbridged_message(incoming(1, "первое"), display_name="Тётя")
    await harness.provisioner.on_unbridged_message(incoming(2, "второе"), display_name=None)

    name = await harness.provisioner.activate(max_chat_id=MAX_CHAT, token=TOKEN)

    assert harness.activator.added, "the bot must be started on the live process"
    assert harness.activator.added[0].max_chat_id == MAX_CHAT

    record = await harness.bridges.get(name)
    assert record is not None
    assert record.source == "guardian"

    bridge_name, replayed = harness.replayer.replayed[0]
    assert bridge_name == name
    assert [item["text"] for item in replayed] == ["первое", "второе"], "order must survive"

    contact = await harness.pending.get(MAX_CHAT)
    assert contact is not None
    assert contact.state is PendingContactState.PROVISIONED
    assert contact.buffered == 0


async def test_token_goes_to_a_private_file_not_the_database(harness: Harness) -> None:
    """WP2's invariant survives provisioning: the database stores a name."""
    await harness.provisioner.activate(max_chat_id=MAX_CHAT, token=TOKEN)

    secrets = harness.config.secrets_file
    assert TOKEN in secrets.read_text(encoding="utf-8")
    assert (secrets.stat().st_mode & 0o777) == 0o600

    record = await harness.bridges.by_max_chat(MAX_CHAT)
    assert record is not None
    assert TOKEN not in record.token_env
    assert os.environ[record.token_env] == TOKEN


async def test_a_malformed_token_is_refused(harness: Harness) -> None:
    with pytest.raises(ProvisioningError, match="токен"):
        await harness.provisioner.activate(max_chat_id=MAX_CHAT, token="hello there")

    assert harness.activator.added == []


async def test_a_second_bridge_for_the_same_chat_is_refused(harness: Harness) -> None:
    await harness.provisioner.activate(max_chat_id=MAX_CHAT, token=TOKEN)

    with pytest.raises(ProvisioningError, match="уже есть мост"):
        await harness.provisioner.activate(max_chat_id=MAX_CHAT, token=TOKEN)


async def test_a_bot_that_will_not_start_is_reported(harness: Harness) -> None:
    """The row survives the failure, and it does not claim to be serving.

    It used to be written only after the poller started, so a crash in that
    window left a bot carrying somebody's messages with nothing naming it. Now
    the row is durable first and says `provisioning` until the transport is up —
    `active()` does not return it, and startup reconciliation can find it.
    """
    from bridge.storage import BridgeState

    harness.activator.fail_with = RuntimeError("token was revoked")

    with pytest.raises(ProvisioningError, match="revoked"):
        await harness.provisioner.activate(max_chat_id=MAX_CHAT, token=TOKEN)

    record = await harness.bridges.by_max_chat(MAX_CHAT)
    assert record is not None, "the register remembers what was attempted"
    assert record.state is BridgeState.PROVISIONING
    assert [item.bridge_name for item in await harness.bridges.active()] == []


# ------------------------------------------------- the announcement's one answer


async def test_the_announcement_carries_a_nonce_its_button_spends(
    harness: Harness,
) -> None:
    """«Создать» starts an irreversible remote effect and used to carry only a
    chat id, so a second tap started a second walk."""
    announcement = await harness.provisioner.on_unbridged_message(
        incoming(1, "привет"), display_name="Тётя"
    )
    assert announcement is not None
    assert announcement.revision > 0

    assert await harness.provisioner.consume_announcement(MAX_CHAT, announcement.revision)
    assert not await harness.provisioner.consume_announcement(
        MAX_CHAT, announcement.revision
    ), "the second tap answers a question that is already answered"


async def test_two_taps_in_the_same_moment_spend_the_nonce_once(
    harness: Harness,
) -> None:
    announcement = await harness.provisioner.on_unbridged_message(
        incoming(1, "привет"), display_name="Тётя"
    )
    assert announcement is not None

    outcomes = await asyncio.gather(
        harness.provisioner.consume_announcement(MAX_CHAT, announcement.revision),
        harness.provisioner.consume_announcement(MAX_CHAT, announcement.revision),
    )

    assert sorted(outcomes) == [False, True]


async def test_a_button_from_a_forgotten_question_does_nothing(
    harness: Harness,
) -> None:
    await harness.provisioner.on_unbridged_message(incoming(1, "привет"), display_name="Тётя")
    assert not await harness.provisioner.consume_announcement(MAX_CHAT, 1)


async def test_a_group_dialog_is_not_offered_a_bot(harness: Harness) -> None:
    """One bot is one person; the picker filters groups and this used to not."""
    group = normalize_message(
        {"id": 1, "chatId": -400000000004, "sender": CONTACT, "text": "hi", "time": 1},
        own_user_id=999,
    )

    assert await harness.provisioner.on_unbridged_message(group, display_name="Чат") is None
    assert await harness.pending.get(-400000000004) is None
