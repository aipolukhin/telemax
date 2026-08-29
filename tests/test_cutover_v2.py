"""The destructive cutover: what it removes, what it must not, and the floor.

The dangerous assertions are the negative ones. A cutover that takes the MAX
session with it costs a re-login and a new device identity; one that takes the
sticker caches litters the owner's MAX account with duplicate stickers; one that
forgets the history floor delivers the last fifty messages of every conversation
a second time.
"""

from __future__ import annotations

import json
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from bridge.cutover import backup as backup_module
from bridge.cutover.floors import apply_floor, read_floors, write_floors
from bridge.cutover.flow import Phase, read_phase, record
from bridge.cutover.inventory import KEEP, Role, classify_tables, report, take
from bridge.cutover.purge import purge
from bridge.provisioning.provisioner import UsernameState
from bridge.provisioning.secrets import ContactBotSecretStore
from bridge.storage import BridgeRecord, BridgeRepository, Database

pytestmark = pytest.mark.asyncio

OWNER_TG = 100000001
OWNER_MAX = 200000002


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    connection = await Database.connect(tmp_path / "data" / "bridge.db")
    try:
        yield connection
    finally:
        await connection.close()


async def seed(database: Database) -> BridgeRepository:
    bridges = BridgeRepository(database)
    await bridges.upsert(
        BridgeRecord(
            bridge_name="aaaa",
            max_chat_id=100,
            max_user_id=11,
            telegram_bot_id=1,
            token_env="TELEMAX_BOT_AAAA",
            expected_username="aaaa_max_bot",
        )
    )
    await database.execute(
        "INSERT INTO message_map (bridge_name, max_chat_id, max_message_id, telegram_bot_id,"
        " telegram_chat_id, direction, source_marker, created_at)"
        " VALUES ('aaaa', 100, 900, 1, 5, 'max_to_tg', 'from_max', 0)"
    )
    await database.execute(
        "INSERT INTO sticker_cache (png_sha256, max_sticker_id, created_at)"
        " VALUES ('abc', 42, 0)"
    )
    return bridges


# ------------------------------------------------------------ classification


async def test_the_destructive_set_is_derived_from_the_columns(database: Database) -> None:
    tables = {item.name: item for item in await classify_tables(database)}

    assert tables["bridges"].scoped
    assert tables["message_map"].scoped
    assert tables["outbox"].scoped
    assert tables["telegram_inbox"].scoped
    assert tables["read_state"].scoped
    assert tables["pending_contacts"].scoped
    assert "keyed on" in tables["message_map"].why


async def test_account_level_tables_are_never_in_it(database: Database) -> None:
    """A sticker sent to MAX creates one in the owner's collection."""
    tables = {item.name: item for item in await classify_tables(database)}

    for name in KEEP:
        assert not tables[name].scoped, name
    assert not tables["sticker_cache"].scoped
    assert not tables["sticker_origin"].scoped
    assert not tables["schema_version"].scoped


# ------------------------------------------------------------------- purge


async def test_the_purge_empties_the_bridges_and_keeps_the_account(
    database: Database, tmp_path: Path
) -> None:
    bridges = await seed(database)
    store = ContactBotSecretStore(tmp_path / "data" / "secrets" / "bots.env")
    await store.save("TELEMAX_BOT_AAAA", "1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    tables = await classify_tables(database)

    removed = await purge(
        database=database, scoped=tables, store=store, data_dir=tmp_path / "data"
    )

    assert await bridges.all() == []
    assert removed.tables["message_map"] == 1
    assert removed.token_variables == ["TELEMAX_BOT_AAAA"]
    assert store.read() == {}
    stickers = await database.query("SELECT COUNT(*) AS n FROM sticker_cache")
    assert stickers[0]["n"] == 1, "the owner's own sticker collection is not ours to reset"


async def test_the_purge_never_touches_a_session_file(tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "max-session").mkdir(parents=True)
    (data / "max-session" / "identity.json").write_text("{}", encoding="utf-8")
    (data / "max-session" / "max-session.db").write_bytes(b"session")
    (data / "secrets").mkdir(parents=True)
    (data / "secrets" / "telegram-user.session").write_bytes(b"owner")
    (data / "secrets" / "naming-secret").write_bytes(b"secret")
    (data / "state").mkdir(parents=True)
    (data / "state" / "provisioning.json").write_text("[]", encoding="utf-8")
    (data / "state" / "onboarding.json").write_text("{}", encoding="utf-8")

    database = await Database.connect(data / "bridge.db")
    try:
        await purge(
            database=database,
            scoped=await classify_tables(database),
            store=ContactBotSecretStore(data / "secrets" / "bots.env"),
            data_dir=data,
        )
    finally:
        await database.close()

    assert (data / "max-session" / "identity.json").exists()
    assert (data / "max-session" / "max-session.db").exists()
    assert (data / "secrets" / "telegram-user.session").exists()
    assert (data / "secrets" / "naming-secret").exists()
    assert (data / "state" / "onboarding.json").exists(), "the owner's own preferences stay"
    assert not (data / "state" / "provisioning.json").exists()


# -------------------------------------------------------------- the floor


async def test_the_floor_is_what_stops_the_old_tail_arriving_twice(
    database: Database, tmp_path: Path
) -> None:
    """The dedup is keyed on the bot, so a new bot has an unclaimed tail."""
    data = tmp_path / "data"
    bridges = BridgeRepository(database)
    write_floors(data, {100: 900})

    await bridges.upsert(
        BridgeRecord(bridge_name="bbbb", max_chat_id=100, token_env="E_B")
    )
    applied = await apply_floor(bridges, data_dir=data, bridge_name="bbbb", chat_id=100)

    assert applied == 900
    record_ = await bridges.by_max_chat(100)
    assert record_ is not None and record_.history_floor == 900


async def test_an_installation_that_never_cut_over_has_no_floor(
    database: Database, tmp_path: Path
) -> None:
    bridges = BridgeRepository(database)
    await bridges.upsert(BridgeRecord(bridge_name="bbbb", max_chat_id=100, token_env="E_B"))

    assert await apply_floor(bridges, data_dir=tmp_path, bridge_name="bbbb", chat_id=100) == 0
    row = await bridges.by_max_chat(100)
    assert row is not None and row.history_floor is None


async def test_the_floor_only_moves_forward(database: Database, tmp_path: Path) -> None:
    bridges = BridgeRepository(database)
    await bridges.upsert(BridgeRecord(bridge_name="bbbb", max_chat_id=100, token_env="E_B"))
    await bridges.set_history_floor("bbbb", 900)
    await bridges.set_history_floor("bbbb", 500)

    row = await bridges.by_max_chat(100)
    assert row is not None and row.history_floor == 900


async def test_a_damaged_floors_file_is_ignored_not_obeyed(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "state" / "history-floors.json").write_text("{not json", encoding="utf-8")
    assert read_floors(tmp_path) == {}


async def test_floors_round_trip(tmp_path: Path) -> None:
    write_floors(tmp_path, {100: 900, 200: 1000})
    assert read_floors(tmp_path) == {100: 900, 200: 1000}
    raw = json.loads((tmp_path / "state" / "history-floors.json").read_text(encoding="utf-8"))
    assert raw == {"100": 900, "200": 1000}


# ------------------------------------------------------------------ backup


async def test_the_backup_proves_itself_and_carries_the_max_session(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    for name in ("secrets", "state", "max-session"):
        (data / name).mkdir(parents=True)
        (data / name / "file").write_text("x", encoding="utf-8")
    database = await Database.connect(data / "bridge.db")
    await database.close()
    config = tmp_path / "config.yaml"
    config.write_text("telegram:\n  owner_user_id: 1\n", encoding="utf-8")

    taken = backup_module.take(
        data_dir=data, config_path=config, destination=tmp_path / "backups"
    )

    assert taken.integrity == "ok"
    assert taken.ok
    assert backup_module.verify(taken) == []
    with tarfile.open(taken.archive) as tar:
        names = {item.name for item in tar.getmembers()}
    assert "data/max-session/file" in names, "the procedure in use before this left it out"
    assert "data/secrets/file" in names
    assert taken.database.stat().st_mode & 0o777 == 0o600
    assert taken.archive.stat().st_mode & 0o777 == 0o600


async def test_a_missing_member_is_reported_rather_than_assumed(tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "secrets").mkdir(parents=True)
    (data / "secrets" / "file").write_text("x", encoding="utf-8")
    database = await Database.connect(data / "bridge.db")
    await database.close()

    taken = backup_module.take(
        data_dir=data, config_path=None, destination=tmp_path / "backups"
    )

    assert "data/max-session" in backup_module.verify(taken)


# ------------------------------------------------------------------ gates


async def test_the_inventory_names_everything_and_leaks_nothing(
    database: Database,
) -> None:
    bridges = await seed(database)

    async def check(username: str) -> UsernameState:
        return UsernameState.FREE

    async def alive(token_env: str) -> bool:
        return True

    found = await take(
        database=database,
        bridges=bridges,
        guardian_bot_id=999,
        guardian_username="old_telemax_bot",
        telegram_owner_user_id=OWNER_TG,
        max_owner_user_id=OWNER_MAX,
        peers=[(11, 100, "Мама")],
        check_username=check,
        token_alive=alive,
    )
    text = report(found)

    assert found.legacy[0].role is Role.GUARDIAN
    assert any(item.role is Role.CONTACT for item in found.legacy)
    assert [item.username for item in found.planned] == [
        "rf2kxavwxaw7xyiii6gf_telemax_bot",
        "sov4wcc4h663ic4lb6ev_max_bot",
    ]
    assert "sticker_cache" in text and "СОХРАНЯЕТСЯ" in text
    assert "AAAAAAAA" not in text, "no token material in what the owner is shown"


async def test_a_foreign_username_blocks_the_whole_cutover(database: Database) -> None:
    bridges = await seed(database)

    async def check(username: str) -> UsernameState:
        return UsernameState.FOREIGN

    async def alive(token_env: str) -> bool:
        return True

    found = await take(
        database=database,
        bridges=bridges,
        guardian_bot_id=999,
        guardian_username="old_telemax_bot",
        telegram_owner_user_id=OWNER_TG,
        max_owner_user_id=OWNER_MAX,
        peers=[(11, 100, "Мама")],
        check_username=check,
        token_alive=alive,
    )

    assert found.blocked
    assert found.to_create == []


async def test_the_phase_survives_a_crash(tmp_path: Path) -> None:
    record(tmp_path, Phase.PURGED, "5 tables")
    outcome = read_phase(tmp_path)
    assert outcome is not None
    assert outcome.phase is Phase.PURGED
    assert outcome.detail == "5 tables"
    assert not outcome.complete


async def test_an_absent_phase_file_reads_as_nothing(tmp_path: Path) -> None:
    assert read_phase(tmp_path) is None
