"""Getting the owner from the terminal into the bot, once, safely.

The link this produces is public the moment it exists: it goes through
Telegram's servers and sits in Saved Messages. So the tests here are mostly
about what it is worth to somebody who is not the owner — which should be
nothing at all.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bridge.bootstrap.handoff import Delivery, hand_off, invitation
from bridge.observability.redaction import redact
from bridge.onboarding import tokens
from bridge.onboarding.state import Stage, StateStore
from bridge.onboarding.tokens import Verdict
from tests.fake_telegram import FakeBot

OWNER = 100000001
STRANGER = 999


@dataclass
class FakeSession:
    saved: list[str] = field(default_factory=list)
    works: bool = True

    async def send_to_saved(self, text: str) -> bool:
        if not self.works:
            return False
        self.saved.append(text)
        return True


def store_for(tmp_path: Path) -> StateStore:
    return StateStore.for_data_dir(tmp_path)


# ------------------------------------------------------------------- delivery


async def test_an_existing_chat_is_used_directly(tmp_path: Path) -> None:
    """No link needed when the owner has already pressed Start."""
    bot, session = FakeBot("1:aaa"), FakeSession()

    result = await hand_off(
        store=store_for(tmp_path),
        session=session,
        bot=bot,
        bot_username="telemax_guard_bot",
        owner_user_id=OWNER,
    )

    assert result.delivery is Delivery.BOT
    sent = bot.method_calls("send_message")
    assert sent and sent[0]["chat_id"] == OWNER
    assert session.saved == [], "Saved Messages is the fallback, not the default"
    assert "Начать настройку" in str(sent[0]["reply_markup"])


async def test_without_a_chat_the_link_goes_to_saved_messages(tmp_path: Path) -> None:
    """A bot may not write first, and there is no method that says whether it may."""
    bot, session = FakeBot("1:aaa"), FakeSession()
    bot.send_fails = True

    result = await hand_off(
        store=store_for(tmp_path),
        session=session,
        bot=bot,
        bot_username="telemax_guard_bot",
        owner_user_id=OWNER,
    )

    assert result.delivery is Delivery.SAVED_MESSAGES
    assert len(session.saved) == 1
    assert result.link in session.saved[0]
    assert "t.me/telemax_guard_bot?start=" in result.link


async def test_the_console_is_the_last_resort(tmp_path: Path) -> None:
    bot, session = FakeBot("1:aaa"), FakeSession(works=False)
    bot.send_fails = True

    result = await hand_off(
        store=store_for(tmp_path),
        session=session,
        bot=bot,
        bot_username="telemax_guard_bot",
        owner_user_id=OWNER,
    )

    assert result.delivery is Delivery.CONSOLE
    assert result.link.startswith("https://t.me/")


# ---------------------------------------------------------------------- token


def test_the_token_is_not_predictable() -> None:
    issued = {tokens.issue().plaintext for _ in range(200)}
    assert len(issued) == 200
    # 32 bytes, url-safe: 43 characters. Anything shorter is a different design.
    assert all(len(token) >= 40 for token in issued)


def test_the_token_belongs_to_one_account() -> None:
    issued = tokens.issue()

    assert (
        tokens.verify(
            presented=issued.plaintext,
            expected_digest=issued.digest,
            expires_at=issued.expires_at,
            used_at=None,
            owner_user_id=OWNER,
            sender_user_id=OWNER,
        )
        is Verdict.OK
    )
    assert (
        tokens.verify(
            presented=issued.plaintext,
            expected_digest=issued.digest,
            expires_at=issued.expires_at,
            used_at=None,
            owner_user_id=OWNER,
            sender_user_id=STRANGER,
        )
        is Verdict.WRONG_OWNER
    )


def test_the_token_expires() -> None:
    issued = tokens.issue(ttl_seconds=60, now=1000)
    assert issued.expires_at == 1060

    assert (
        tokens.verify(
            presented=issued.plaintext,
            expected_digest=issued.digest,
            expires_at=issued.expires_at,
            used_at=None,
            owner_user_id=OWNER,
            sender_user_id=OWNER,
            now=2000,
        )
        is Verdict.EXPIRED
    )


def test_the_token_works_once() -> None:
    issued = tokens.issue()

    assert (
        tokens.verify(
            presented=issued.plaintext,
            expected_digest=issued.digest,
            expires_at=issued.expires_at,
            used_at=int(time.time()),
            owner_user_id=OWNER,
            sender_user_id=OWNER,
        )
        is Verdict.USED
    )


def test_somebody_elses_token_is_refused() -> None:
    mine, theirs = tokens.issue(), tokens.issue()

    assert (
        tokens.verify(
            presented=theirs.plaintext,
            expected_digest=mine.digest,
            expires_at=mine.expires_at,
            used_at=None,
            owner_user_id=OWNER,
            sender_user_id=OWNER,
        )
        is Verdict.UNKNOWN
    )


async def test_only_the_hash_is_written_down(tmp_path: Path) -> None:
    store = store_for(tmp_path)
    bot, session = FakeBot("1:aaa"), FakeSession()
    bot.send_fails = True

    result = await hand_off(
        store=store,
        session=session,
        bot=bot,
        bot_username="telemax_guard_bot",
        owner_user_id=OWNER,
    )

    plaintext = result.link.rpartition("=")[2]
    on_disk = store.path.read_text(encoding="utf-8")

    assert plaintext not in on_disk, "a stored token would be as good as the link"
    assert tokens.digest_of(plaintext) in on_disk
    assert (store.path.stat().st_mode & 0o777) == 0o600


def test_the_plaintext_token_never_reaches_a_log(caplog: Any) -> None:
    issued = tokens.issue()
    link = tokens.deep_link("telemax_guard_bot", issued.plaintext)

    assert issued.plaintext not in redact(link)
    assert issued.plaintext not in redact(f"/start {issued.plaintext}")
    assert issued.plaintext not in redact(invitation("telemax_guard_bot", link))

    with caplog.at_level(logging.DEBUG, logger="bridge.bootstrap.handoff"):
        logging.getLogger("bridge.bootstrap.handoff").info("setup link delivered")
    assert issued.plaintext not in caplog.text


async def test_after_the_handoff_the_console_is_no_longer_needed(tmp_path: Path) -> None:
    """Everything left to do has a home in the bot."""
    store = store_for(tmp_path)
    bot, session = FakeBot("1:aaa"), FakeSession()
    bot.send_fails = True

    await hand_off(
        store=store,
        session=session,
        bot=bot,
        bot_username="telemax_guard_bot",
        owner_user_id=OWNER,
    )

    record = store.load()
    assert record.stage is Stage.MAX_ONBOARDING_PENDING
    assert record.owner_user_id == OWNER
    assert record.guardian_username == "telemax_guard_bot"
    assert record.token_used_at is None
