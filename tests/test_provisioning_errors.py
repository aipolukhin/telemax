"""Why a bridge failed, told apart from every other reason it could have failed.

This file exists because of one screen. The owner was shown «Боты Telegram: 1 из
40 · Свободно новых слотов: 39» and, a moment later, «достигнут лимит ботов
Telegram-аккаунта» — two statements that cannot both be true. The count was
right; the *classification* was not. Every refusal Telegram made was being read
through one text match, so a rate limit, a network blip and a genuinely full
account all arrived at the owner as the same sentence, and only one of the three
is fixed by deleting a bot.

So the rule under everything here: an exception class is a deliberate statement
and beats any string, a flood wait is never a capacity limit, and the sentence
the owner reads is derived from the classification rather than the other way
round.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bridge.provisioning.batch import (
    CONFIRMATION_MESSAGE,
    FLOOD_MESSAGE,
    LIMIT_MESSAGE,
    NETWORK_MESSAGE,
    ProvisioningBatch,
)
from bridge.provisioning.journal import ItemState, JournalEntry, ProvisioningJournal
from bridge.provisioning.provisioner import (
    ConfirmationTimeoutError,
    CreationLimitError,
    FloodWaitError,
    ForeignUsernameError,
    NetworkError,
    ProvisionerError,
    ProvisioningFailure,
    classify_failure,
    is_flood_error,
    is_limit_error,
)
from tests.fake_provisioning import FakeGateway, FakeProvisioner

# --------------------------------------------------------------- classifying


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (CreationLimitError("BOTS_TOO_MUCH"), ProvisioningFailure.BOT_CREATE_LIMIT_EXCEEDED),
        (FloodWaitError("FLOOD_WAIT_X", seconds=42), ProvisioningFailure.FLOOD_WAIT),
        (ForeignUsernameError("mama_max_bot"), ProvisioningFailure.USERNAME_OCCUPIED),
        (
            ConfirmationTimeoutError("владелец не подтвердил создание бота"),
            ProvisioningFailure.MANAGED_BOT_CONFIRMATION_REQUIRED,
        ),
        (NetworkError("connection reset"), ProvisioningFailure.NETWORK_ERROR),
        (
            ProvisionerError("что-то неизвестное"),
            ProvisioningFailure.UNKNOWN_PROVISIONING_ERROR,
        ),
    ],
)
def test_the_exception_class_decides(
    raised: BaseException, expected: ProvisioningFailure
) -> None:
    assert classify_failure(raised) is expected


@pytest.mark.parametrize(
    "text",
    ["FLOOD_WAIT_600", "flood wait 600", "Too Many Requests: retry after 30", "SLOWMODE_WAIT_5"],
)
def test_a_rate_limit_is_never_a_full_account(text: str) -> None:
    """The bug, in one assertion: waiting fixes this and deleting a bot does not."""
    error = RuntimeError(text)

    assert is_flood_error(error)
    assert not is_limit_error(error)
    assert classify_failure(error) is ProvisioningFailure.FLOOD_WAIT


@pytest.mark.parametrize("text", ["BOT_CREATE_LIMIT_EXCEEDED", "BOTS_TOO_MUCH", "too many bots"])
def test_a_full_account_still_reads_as_one(text: str) -> None:
    assert classify_failure(RuntimeError(text)) is ProvisioningFailure.BOT_CREATE_LIMIT_EXCEEDED


def test_an_unreachable_telegram_is_not_an_account_problem() -> None:
    for text in ("Connection reset by peer", "read timed out", "502 Bad Gateway"):
        assert classify_failure(RuntimeError(text)) is ProvisioningFailure.NETWORK_ERROR


def test_only_the_owner_s_own_limits_are_permanent() -> None:
    """Retryable is a property of the cause, not a flag a caller passes in."""
    assert not ProvisioningFailure.BOT_CREATE_LIMIT_EXCEEDED.retryable
    assert not ProvisioningFailure.USERNAME_OCCUPIED.retryable
    assert ProvisioningFailure.FLOOD_WAIT.retryable
    assert ProvisioningFailure.MANAGED_BOT_CONFIRMATION_REQUIRED.retryable
    assert ProvisioningFailure.NETWORK_ERROR.retryable


# ------------------------------------------------------------------- the batch


async def run_one(tmp_path: Path, failure: Exception) -> JournalEntry:
    """Provision a single contact whose creation fails in a given way."""
    provisioner = FakeProvisioner()
    provisioner.fail_create["mama_max_bot"] = failure
    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.begin(
        [
            JournalEntry(
                max_chat_id=1,
                expected_username="mama_max_bot",
                title="Мама",
                state=ItemState.PENDING,
            )
        ]
    )
    batch = ProvisioningBatch(
        provisioner=provisioner,
        gateway=FakeGateway(),
        journal=journal,
        display_name_of=lambda entry: entry.title,
    )
    result = await batch.run()
    return result.entries[0]


async def test_a_flood_wait_does_not_claim_the_account_is_full(tmp_path: Path) -> None:
    entry = await run_one(tmp_path, FloodWaitError("FLOOD_WAIT_600", seconds=600))

    assert entry.failure == ProvisioningFailure.FLOOD_WAIT.value
    assert entry.error == FLOOD_MESSAGE
    assert LIMIT_MESSAGE not in (entry.error or "")
    assert entry.state is ItemState.FAILED_RETRYABLE, "waiting is worth another go"


async def test_a_real_limit_is_permanent_and_says_so(tmp_path: Path) -> None:
    entry = await run_one(tmp_path, CreationLimitError("BOTS_TOO_MUCH"))

    assert entry.failure == ProvisioningFailure.BOT_CREATE_LIMIT_EXCEEDED.value
    assert entry.error == LIMIT_MESSAGE
    assert entry.state is ItemState.FAILED_PERMANENT


async def test_an_unconfirmed_creation_asks_again_rather_than_blaming_the_account(
    tmp_path: Path,
) -> None:
    entry = await run_one(tmp_path, ConfirmationTimeoutError("не подтвердил"))

    assert entry.failure == ProvisioningFailure.MANAGED_BOT_CONFIRMATION_REQUIRED.value
    assert entry.error == CONFIRMATION_MESSAGE
    assert entry.state is ItemState.FAILED_RETRYABLE


async def test_a_network_failure_is_shown_as_one(tmp_path: Path) -> None:
    entry = await run_one(tmp_path, NetworkError("read timed out"))

    assert entry.error == NETWORK_MESSAGE
    assert entry.state is ItemState.FAILED_RETRYABLE


async def test_a_retry_clears_the_previous_classification(tmp_path: Path) -> None:
    """A stale code under a fresh attempt is how a fixed problem stays on screen."""
    journal = ProvisioningJournal.for_data_dir(tmp_path)
    journal.begin(
        [
            JournalEntry(
                max_chat_id=1,
                expected_username="mama_max_bot",
                title="Мама",
                state=ItemState.PENDING,
            )
        ]
    )
    journal.note(
        1,
        state=ItemState.FAILED_RETRYABLE,
        error=FLOOD_MESSAGE,
        failure=ProvisioningFailure.FLOOD_WAIT.value,
    )

    batch = ProvisioningBatch(
        provisioner=FakeProvisioner(),
        gateway=FakeGateway(),
        journal=journal,
        display_name_of=lambda entry: entry.title,
    )
    entry = (await batch.run()).entries[0]

    assert entry.state is ItemState.HEALTHY
    assert entry.error is None
    assert entry.failure is None
