"""`python -m bridge cutover-v2` — the console half of the V2 cutover.

Read-only unless told twice. The default run prints the inventory, takes a
proved backup, and stops at the first gate; `--yes-destroy` is the only thing
that empties anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from bridge.config import ConfigError, LoadedConfig, load_config
from bridge.cutover import backup as backup_module
from bridge.cutover import inventory as inventory_module
from bridge.cutover.flow import GATE_ONE, GATE_TWO, RETIRED, Phase, record
from bridge.cutover.purge import purge
from bridge.provisioning.provisioner import UsernameState
from bridge.provisioning.secrets import ContactBotSecretStore
from bridge.storage import BridgeRepository, Database

logger = logging.getLogger(__name__)

UNIT = "telemax"


def systemctl(*args: str) -> int:
    binary = shutil.which("systemctl")
    if binary is None:
        print("systemctl не найден — сервис не трогаю")
        return 127
    # The binary is resolved from PATH and every argument below is a literal in
    # this module; nothing the owner typed reaches it.
    return subprocess.run([binary, "--user", *args], check=False).returncode  # noqa: S603


async def _remote(loaded: LoadedConfig) -> tuple[Any, int, str]:
    """The guardian bot, its id and its username — all from `getMe`."""
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties

    variable = loaded.app.provisioning.guardian_bot_token_env or ""
    token = os.environ.get(variable, "").strip()
    if not token:
        raise RuntimeError(f"${variable} holds no token")
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=None))
    me = await bot.get_me()
    return bot, int(me.id), str(me.username or "")


async def _max_owner(loaded: LoadedConfig) -> int:
    """The owner's MAX id, from the session that already exists.

    Read-only: no login, no prompt, nothing created. It is half of every V2
    username, so without it there is nothing to compute and the run stops.
    """
    from bridge.max_client import MaxClient

    app = loaded.app
    phone = os.environ.get(app.max.phone_env, "").strip()
    client = MaxClient(
        phone=phone, session_dir=app.max_session_dir, session_name=app.max.session_name
    )
    await client.start()
    try:
        return client.own_user_id or 0
    finally:
        with contextlib.suppress(Exception):
            await client.stop()


async def _peers(bridges: Any) -> list[tuple[int, int, str]]:
    """The contacts that get a V2 bot: exactly the ones bridged today.

    Like for like, and deliberately not "every dialog MAX lists". The picker
    offers forty-seven of those on this account — service accounts, one-off
    notifications, people nobody bridged — and creating a bot for each would
    spend the whole twenty-bot allowance on conversations the owner never asked
    to carry. Adding somebody new stays what it already is: `/dialogs`.
    """
    return [
        (int(record.max_user_id), int(record.max_chat_id), record.title or "")
        for record in await bridges.all()
        if record.max_user_id is not None
    ]


async def _run(loaded: LoadedConfig, *, destroy: bool, config_path: Path | None) -> int:
    app = loaded.app
    data_dir = app.paths.data_dir

    bot, guardian_id, guardian_username = await _remote(loaded)
    try:
        max_owner = await _max_owner(loaded)
        if not max_owner:
            print("Не удалось прочитать MAX-аккаунт владельца. Останавливаюсь.")
            return 2

        from bridge.provisioning.managed import ManagedBotProvisioner
        from bridge.provisioning.owned import BotApiOwnedBots, RepositoryKnownBots

        database = await Database.connect(app.paths.db_path)
        try:
            bridges = BridgeRepository(database)
            peers = await _peers(bridges)
            owned = BotApiOwnedBots(
                manager=bot, known=RepositoryKnownBots(bridges.all), assumed_limit=20
            )
            provisioner = ManagedBotProvisioner(
                manager=bot, manager_username=guardian_username, owned=owned
            )

            async def token_alive(token_env: str) -> bool:
                return bool(os.environ.get(token_env, "").strip())

            found = await inventory_module.take(
                database=database,
                bridges=bridges,
                guardian_bot_id=guardian_id,
                guardian_username=guardian_username,
                telegram_owner_user_id=app.telegram.owner_user_id,
                max_owner_user_id=max_owner,
                peers=peers,
                check_username=provisioner.check_username,
                token_alive=token_alive,
            )
            print(inventory_module.report(found))

            if found.blocked:
                print("\nОстановка: имя занято другим аккаунтом. Ничего не изменено.")
                return 2

            taken = backup_module.take(
                data_dir=data_dir,
                config_path=config_path,
                destination=Path("backups"),
                label="pre-cutover-v2",
            )
            print("\nБЭКАП снят и проверен:")
            print(taken.report())
            missing = backup_module.verify(taken)
            if missing:
                print("\nБэкап неполон: " + ", ".join(missing) + ". Останавливаюсь.")
                return 1
            record(data_dir, Phase.BACKED_UP, str(taken.archive))

            if not destroy:
                print(GATE_ONE.format(archive=taken.archive, database=taken.database))
                return 0

            # ------------------------------------------------------- destructive
            systemctl("stop", UNIT)
            record(data_dir, Phase.STOPPED)
            print("\nсервис остановлен")

            floors = {
                chat: newest
                for chat, newest in [
                    (
                        int(row["max_chat_id"]),
                        int(row["newest"] or 0),
                    )
                    for row in await database.query(
                        "SELECT max_chat_id, MAX(max_message_id) AS newest FROM message_map"
                        " WHERE max_message_id IS NOT NULL GROUP BY max_chat_id"
                    )
                ]
                if newest
            }

            removed = await purge(
                database=database,
                scoped=found.tables,
                store=ContactBotSecretStore(app.secrets_file),
                data_dir=data_dir,
            )
            record(data_dir, Phase.PURGED)
            print("\nОЧИЩЕНО:")
            print(removed.report())

            # The floors survive the purge because they are re-applied to the
            # rows the guardian is about to create. Written to a file rather than
            # to `bridges`, which no longer has any rows at all.
            from bridge.cutover.floors import write_floors

            write_floors(data_dir, floors)
            record(data_dir, Phase.FLOORED, f"{len(floors)} chats")
            print(f"\nграница истории записана для {len(floors)} чатов")

            systemctl("start", UNIT)
            record(data_dir, Phase.STARTED)
            print("сервис запущен")

            print(RETIRED)
            print(GATE_TWO)
            print("V2, которые предстоит создать:")
            for item in found.planned:
                if item.state is UsernameState.FREE:
                    print("  " + item.line())
            record(data_dir, Phase.DONE)
            return 0
        finally:
            await database.close()
    finally:
        with contextlib.suppress(Exception):
            await bot.session.close()


def run(path: Path | None = None, *, destroy: bool = False) -> int:
    try:
        loaded = load_config(path)
    except ConfigError as error:
        print(f"config error:\n{error}")
        return 2
    try:
        return asyncio.run(_run(loaded, destroy=destroy, config_path=loaded.config_path))
    except KeyboardInterrupt:
        print("\nПрервано. Ничего не изменено.")
        return 130
