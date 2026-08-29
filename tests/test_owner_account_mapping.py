"""_V13: the owner account column on the existing mapping, and its lookup.

No new table — the MTProto transport keys owner-side deletes on the same
`message_map` rows, with the account beside the owner-side message id. What has
to hold: the column exists after migrating to head, the account+message lookup
resolves a peer-less delete, a different account never resolves it, and the
Secretary-Mode path that fills only the message id keeps working — old rows stay
valid.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bridge.storage import Database, MessageMapRepository
from bridge.storage.migrations import LATEST_VERSION

OWNER = 100000001
STRANGER = 999


async def _linked(tmp_path: Path) -> tuple[Database, MessageMapRepository, int]:
    database = await Database.connect(tmp_path / "bridge.db")
    messages = MessageMapRepository(database)
    link = await messages.claim_from_max(
        bridge_name="mom",
        max_chat_id=5,
        max_message_id=7,
        telegram_bot_id=100,
        telegram_chat_id=900,
    )
    assert link is not None
    return database, messages, link


async def test_head_is_v20(tmp_path: Path) -> None:
    """V14 adds `echo_fingerprint`: what a MAX→TG message will put in the chat,
    written with the claim so an owner-side echo has something to match. V15
    rebuilds `media_group_part` so one album part can be an alias of one such
    row — the per-part half of the same binding. V16 adds `forward_author`, the
    one fact only a contact bot can see: who wrote a forwarded message, as their
    own profile has them rather than as the owner filed them. V17 adds
    `owner_message_state`: what the puppet session last saw of an owner message,
    because `UpdateEditMessage` says what a message is and never what changed.
    V18 adds `owner_update_inbox`: every owner update written down before
    anything is derived from it, because Telethon does not replay one. V19 adds
    the two identity indexes: one bot serves one bridge and one deterministic
    username belongs to one bridge, both of which were true by construction and
    enforced by nothing. V20 adds `history_floor`: the line under which the
    automatic backfill never carries anything, because the delivery dedup is
    keyed on the bot and a chat given a new bot has an unclaimed tail again."""
    database = await Database.connect(tmp_path / "bridge.db")
    try:
        assert await database.schema_version() == LATEST_VERSION == 20
    finally:
        await database.close()


async def test_owner_account_lookup_resolves_a_peerless_delete(tmp_path: Path) -> None:
    database, messages, link = await _linked(tmp_path)
    try:
        await messages.attach_owner_message(link, 4242, telegram_owner_account_id=OWNER)

        found = await messages.by_owner_account_message(OWNER, 4242)
        assert found is not None
        assert found.id == link and found.max_message_id == 7
    finally:
        await database.close()


async def test_a_different_account_never_resolves_the_mapping(tmp_path: Path) -> None:
    database, messages, link = await _linked(tmp_path)
    try:
        await messages.attach_owner_message(link, 4242, telegram_owner_account_id=OWNER)
        assert await messages.by_owner_account_message(STRANGER, 4242) is None
    finally:
        await database.close()


async def test_the_secretary_path_still_fills_only_the_message_id(tmp_path: Path) -> None:
    """`attach_owner_message` without an account leaves the column null, and the
    existing owner-message lookup keeps working — old rows stay valid."""
    database, messages, link = await _linked(tmp_path)
    try:
        await messages.attach_owner_message(link, 4242)

        by_bot = await messages.by_owner_message(100, 4242)
        assert by_bot is not None and by_bot.id == link
        # No account was recorded, so the account-keyed lookup finds nothing.
        assert await messages.by_owner_account_message(OWNER, 4242) is None
    finally:
        await database.close()


@pytest.mark.parametrize("account", [OWNER, None])
async def test_the_column_reads_back_for_both_transports(
    tmp_path: Path, account: int | None
) -> None:
    database, messages, link = await _linked(tmp_path)
    try:
        await messages.attach_owner_message(link, 4242, telegram_owner_account_id=account)
        # Both writes succeed and leave the row readable; the account path adds
        # the extra key, the null path is the pre-existing behaviour.
        by_bot = await messages.by_owner_message(100, 4242)
        assert by_bot is not None
    finally:
        await database.close()
