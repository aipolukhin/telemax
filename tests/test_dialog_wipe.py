"""Deleting a conversation, and the gate that decides it is the right one.

`DeleteHistoryRequest(revoke=True)` is the most destructive call this project
makes: it removes a private chat entirely, for both sides, with nothing to undo
it. Aimed at the wrong peer it destroys a real conversation.

So the tests that matter here are the refusals. The happy path is one line; the
gate is the feature.
"""

from __future__ import annotations

from typing import Any

import pytest
from telethon.tl import types
from telethon.tl.functions.messages import DeleteHistoryRequest

from bridge.provisioning.mtproto import BorrowedBotFatherSession, WrongPeerError

pytestmark = pytest.mark.asyncio

BOT_ID = 9000000005


def _user(user_id: int, *, bot: bool) -> Any:
    return types.User(id=user_id, bot=bot, first_name="x", access_hash=1)


class Client:
    """A Telethon client that resolves one peer and records what was sent."""

    def __init__(self, entity: Any) -> None:
        self.entity = entity
        self.requests: list[Any] = []

    async def get_entity(self, peer: Any) -> Any:
        if isinstance(self.entity, Exception):
            raise self.entity
        return self.entity

    async def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        return True


def _session(entity: Any) -> tuple[BorrowedBotFatherSession, Client]:
    client = Client(entity)
    return BorrowedBotFatherSession(lambda: client, secrets_dir=None), client  # type: ignore[arg-type]


# ------------------------------------------------------------------ the gate


async def test_a_peer_that_is_not_a_bot_is_refused() -> None:
    """The one that would destroy a real conversation."""
    session, client = _session(_user(BOT_ID, bot=False))

    with pytest.raises(WrongPeerError, match="not a bot"):
        await session.wipe_dialog(BOT_ID)

    assert client.requests == [], "nothing was sent to Telegram"


async def test_a_peer_that_is_not_a_user_is_refused() -> None:
    group = types.Chat(
        id=BOT_ID, title="группа", participants_count=2, date=None, version=1, photo=None
    )
    session, client = _session(group)

    with pytest.raises(WrongPeerError, match="not a user-shaped peer"):
        await session.wipe_dialog(BOT_ID)

    assert client.requests == []


async def test_a_peer_that_resolves_to_a_different_id_is_refused() -> None:
    """Resolution is not trusted to have answered the question that was asked."""
    session, client = _session(_user(999, bot=True))

    with pytest.raises(WrongPeerError, match="resolved to 999"):
        await session.wipe_dialog(BOT_ID)

    assert client.requests == []


async def test_a_peer_that_cannot_be_resolved_is_refused() -> None:
    session, client = _session(ValueError("no such peer"))

    with pytest.raises(WrongPeerError, match="could not resolve"):
        await session.wipe_dialog(BOT_ID)

    assert client.requests == []


async def test_the_peer_is_asked_for_by_id_never_by_name() -> None:
    """Usernames outlive the bots that held them — measured on this install, a
    deleted bot's name resolved to its replacement while clients still opened
    the old chat from cache. A name is not an identity."""
    asked: list[Any] = []

    class Recording(Client):
        async def get_entity(self, peer: Any) -> Any:
            asked.append(peer)
            return _user(BOT_ID, bot=True)

    client = Recording(None)
    session = BorrowedBotFatherSession(lambda: client, secrets_dir=None)  # type: ignore[arg-type]

    await session.wipe_dialog(BOT_ID)

    assert len(asked) == 1
    assert isinstance(asked[0], types.PeerUser)
    assert asked[0].user_id == BOT_ID


# ------------------------------------------------------------- the happy path


async def test_a_teardown_removes_the_dialog_outright() -> None:
    """`just_clear=False` takes the chat out of the list as well. Right for a
    teardown: the bot is about to be deleted and the chat should not outlive it."""
    session, client = _session(_user(BOT_ID, bot=True))

    assert await session.wipe_dialog(BOT_ID) == BOT_ID

    assert len(client.requests) == 1
    sent = client.requests[0]
    assert isinstance(sent, DeleteHistoryRequest)
    assert sent.revoke is True, "both sides, or the bot keeps its copy"
    assert sent.just_clear is False
    assert sent.max_id == 0, "0 means every message, not a watermark"


async def test_a_repull_empties_the_dialog_without_removing_it() -> None:
    """Removing it is how Telegram represents *stopping* a bot.

    Measured on the first re-pull that used this: the chat went, the bot could
    no longer write until the owner pressed Start again, and the import that
    followed immediately put every message into a chat that refused them — so
    the bridge looked restarted and no history arrived.
    """
    session, client = _session(_user(BOT_ID, bot=True))

    await session.wipe_dialog(BOT_ID, keep_dialog=True)

    sent = client.requests[0]
    assert sent.just_clear is True, "empty it, do not stop the bot"
    assert sent.revoke is True, "still both sides"


async def test_an_absent_client_is_a_wait_not_a_wipe() -> None:
    from bridge.provisioning.mtproto import SessionUnavailableError

    session = BorrowedBotFatherSession(lambda: None, secrets_dir=None)  # type: ignore[arg-type]

    with pytest.raises(SessionUnavailableError):
        await session.wipe_dialog(BOT_ID)


# ------------------------------------------------ reusing a bot empties its chat


async def test_a_reused_bot_gets_its_chat_emptied_first(tmp_path: Any) -> None:
    """The bridge that inherits the last one's conversation is the bridge whose
    echoes have nothing to bind to.

    Ten `ambiguous` jobs and a still-open `delivery-ambiguous` incident came
    from exactly that. An empty chat has no echoes to strand.
    """
    from bridge.provisioning.batch import ProvisioningBatch
    from bridge.provisioning.journal import JournalEntry, ProvisioningJournal
    from tests.fake_provisioning import FakeGateway, FakeProvisioner

    class Wiping(FakeProvisioner):
        wipes_dialogs = True

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.wiped: list[int] = []

        async def wipe_dialog(self, bot_id: int) -> int:
            self.wiped.append(bot_id)
            return bot_id

    prov = Wiping(owned={"c1_max_bot": 77})
    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.begin([JournalEntry(max_chat_id=1, expected_username="c1_max_bot", title="Наталья")])
    gateway = FakeGateway()

    await ProvisioningBatch(
        provisioner=prov, gateway=gateway, journal=journal,
        display_name_of=lambda e: e.title,
    ).run()

    assert prov.reused == ["c1_max_bot"]
    assert prov.wiped == [77], "the chat was emptied before the worker started"
    assert gateway.started == [1]


async def test_a_newly_created_bot_has_nothing_to_empty(tmp_path: Any) -> None:
    from bridge.provisioning.batch import ProvisioningBatch
    from bridge.provisioning.journal import JournalEntry, ProvisioningJournal
    from tests.fake_provisioning import FakeGateway, FakeProvisioner

    class Wiping(FakeProvisioner):
        wipes_dialogs = True

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.wiped: list[int] = []

        async def wipe_dialog(self, bot_id: int) -> int:
            self.wiped.append(bot_id)
            return bot_id

    prov = Wiping()
    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.begin([JournalEntry(max_chat_id=1, expected_username="c1_max_bot", title="Новый")])

    await ProvisioningBatch(
        provisioner=prov, gateway=FakeGateway(), journal=journal,
        display_name_of=lambda e: e.title,
    ).run()

    assert prov.created == ["c1_max_bot"]
    assert prov.wiped == []


async def test_the_owner_can_decline_the_wipe(tmp_path: Any) -> None:
    from bridge.provisioning.batch import ProvisioningBatch
    from bridge.provisioning.journal import JournalEntry, ProvisioningJournal
    from tests.fake_provisioning import FakeGateway, FakeProvisioner

    class Wiping(FakeProvisioner):
        wipes_dialogs = True

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.wiped: list[int] = []

        async def wipe_dialog(self, bot_id: int) -> int:
            self.wiped.append(bot_id)
            return bot_id

    prov = Wiping(owned={"c1_max_bot": 77})
    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.begin([JournalEntry(max_chat_id=1, expected_username="c1_max_bot", title="Наталья")])

    await ProvisioningBatch(
        provisioner=prov, gateway=FakeGateway(), journal=journal,
        display_name_of=lambda e: e.title, wipe_existing=False,
    ).run()

    assert prov.reused == ["c1_max_bot"]
    assert prov.wiped == []


async def test_a_failed_wipe_never_costs_the_bridge(tmp_path: Any) -> None:
    """A bridge carrying messages under some old history is working; one that
    refused to come up because a cleanup failed is not."""
    from bridge.provisioning.batch import ProvisioningBatch
    from bridge.provisioning.journal import ItemState, JournalEntry, ProvisioningJournal
    from tests.fake_provisioning import FakeGateway, FakeProvisioner

    class Refusing(FakeProvisioner):
        wipes_dialogs = True

        async def wipe_dialog(self, bot_id: int) -> int:
            raise RuntimeError("session is down")

    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.begin([JournalEntry(max_chat_id=1, expected_username="c1_max_bot", title="Наталья")])
    gateway = FakeGateway()

    await ProvisioningBatch(
        provisioner=Refusing(owned={"c1_max_bot": 77}), gateway=gateway, journal=journal,
        display_name_of=lambda e: e.title,
    ).run()

    assert gateway.started == [1]
    assert journal.get(1).state is ItemState.HEALTHY


# --------------------------------------------- /start goes to an id, not a name


async def test_the_start_is_sent_to_a_proven_peer_not_a_name() -> None:
    """The chain that left a rebuilt bot mute.

    `/start` went to the *username*, the session answered it from its own cache
    with the dead peer, the send failed — and because the chat never opened,
    `my_chat_member` never arrived and the greeting that repairs the cache was
    never sent. Measured live: `could not send /start … MtprotoError`, twice.
    """
    sent: list[Any] = []

    class Client:
        async def get_entity(self, peer: Any) -> Any:
            return _user(BOT_ID, bot=True)

        async def send_message(self, peer: Any, text: str) -> None:
            sent.append((peer, text))

    session = BorrowedBotFatherSession(lambda: Client(), secrets_dir=None)  # type: ignore[arg-type]

    await session.send_start("example_contact_max_bot", BOT_ID)

    assert len(sent) == 1
    peer, text = sent[0]
    assert text == "/start"
    assert not isinstance(peer, str), "a name is what the cache answers wrongly"
    assert int(peer.id) == BOT_ID


async def test_without_an_id_the_name_is_still_the_fallback() -> None:
    """The managed path has no id to give, and a bridge must still open."""
    sent: list[Any] = []

    class Client:
        async def send_message(self, peer: Any, text: str) -> None:
            sent.append(peer)

    session = BorrowedBotFatherSession(lambda: Client(), secrets_dir=None)  # type: ignore[arg-type]

    await session.send_start("example_contact_max_bot")

    assert sent == ["example_contact_max_bot"]


async def test_a_start_never_goes_to_something_that_is_not_our_bot() -> None:
    """Writing "/start" to whoever the resolver happened to return is a message
    to a stranger. Same gate as the wipe, one definition."""
    sent: list[Any] = []

    class Client:
        async def get_entity(self, peer: Any) -> Any:
            return _user(BOT_ID, bot=False)

        async def send_message(self, peer: Any, text: str) -> None:
            sent.append(peer)

    session = BorrowedBotFatherSession(lambda: Client(), secrets_dir=None)  # type: ignore[arg-type]

    with pytest.raises(WrongPeerError):
        await session.send_start("example_contact_max_bot", BOT_ID)

    assert sent == []


async def test_a_freshly_created_bot_knows_its_own_id() -> None:
    """@BotFather never names it, but every token *is* `<bot_id>:<secret>`.

    Left as None it sent `/start` and the dialog wipe back to the username —
    the one thing the cache gets wrong about a rebuilt bot.
    """
    from bridge.provisioning.mtproto import bot_id_of

    assert bot_id_of(f"{BOT_ID}:AA{'x' * 33}") == BOT_ID
    assert bot_id_of("no-colon") is None
    assert bot_id_of("abc:def") is None
    assert bot_id_of("") is None
