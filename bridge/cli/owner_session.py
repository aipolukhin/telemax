"""`python -m bridge telegram-sync` — link the owner's Telegram, by QR, once.

The owner scans a QR from their own Telegram app; Telemax becomes an additional
device that can *see* the owner's own messages, edits and deletes in the chats
with contact bots. This is the console step for that link. It authorises the
session and verifies it belongs to the Guardian owner — it does **not** switch
production intake to it; that stays an explicit, probe-gated step.

Secrets discipline: `api_id` is typed in the clear (it is not a secret), the
`api_hash` and the QR url are read but never printed, and the session file is
hardened where `telegram/user_session.py` puts it. Nothing here reaches a log,
the database, diagnostics or the Guardian chat.
"""

from __future__ import annotations

import asyncio
import os
from getpass import getpass
from pathlib import Path

from bridge.config import ConfigError, LoadedConfig, load_config
from bridge.telegram.user_session import (
    SYNC_ACCESS_WARNING,
    SYNC_HEADER,
    SYNC_NOT_ENABLED_YET,
    SYNC_SCAN_HINT,
    AuthorizedOwner,
    OwnerMismatchError,
    authorize_owner_session,
    qr_ascii,
    user_session_path,
)


def _credentials(loaded: LoadedConfig) -> tuple[int, str]:
    """api_id and api_hash — from the environment if present, else asked here.

    The variable names come from the existing provisioning config, not a
    hardcoded pair. Their values are never printed: if both are already in the
    environment the owner is told so by name, and otherwise the hash is read
    hidden.
    """
    provisioning = loaded.app.provisioning
    id_env, hash_env = provisioning.mtproto_api_id_env, provisioning.mtproto_api_hash_env
    env_id = os.environ.get(id_env, "").strip()
    env_hash = os.environ.get(hash_env, "").strip()
    if env_id and env_hash:
        print(f"Использую ${id_env} и ${hash_env} из окружения.")
        api_id, api_hash = env_id, env_hash
    else:
        print("api_id и api_hash берутся на my.telegram.org → API development tools.")
        api_id = input("api_id: ").strip()
        api_hash = getpass("api_hash (ввод скрыт): ").strip()
    if not api_id.isdigit():
        raise ValueError("api_id должен быть числом")
    if not api_hash:
        raise ValueError("api_hash пустой")
    return int(api_id), api_hash


def _mask(username: str | None) -> str:
    if not username:
        return "без username"
    return f"@{username[0]}…" if len(username) > 1 else "@…"


def _report(owner: AuthorizedOwner, session: Path) -> None:
    print("\nГотово. Сессия принадлежит владельцу Telemax.")
    print(f"  Аккаунт:  {owner.name or 'без имени'}")
    print(f"  Username: {_mask(owner.username)}")
    print(f"  User ID:  {owner.account_id}  (совпадает с владельцем)")
    print(f"  Сессия:   {session}  (0600)")
    print("\n" + SYNC_ACCESS_WARNING)
    print("\n" + SYNC_NOT_ENABLED_YET)


async def _link(loaded: LoadedConfig) -> int:
    owner_user_id = loaded.app.telegram.owner_user_id
    secrets_dir = loaded.app.paths.resolved_secrets_dir
    api_id, api_hash = _credentials(loaded)

    print("\n" + SYNC_HEADER)
    print(f"\nВладелец Telemax: user ID {owner_user_id}. Сверю его после входа.\n")

    def show_qr(url: str) -> None:
        print(qr_ascii(url))
        print("\n" + SYNC_SCAN_HINT + "\n")

    try:
        owner = await authorize_owner_session(
            api_id=api_id,
            api_hash=api_hash,
            secrets_dir=secrets_dir,
            owner_user_id=owner_user_id,
            on_qr=show_qr,
            password_provider=lambda prompt: getpass(prompt),
        )
    except OwnerMismatchError as error:
        print(f"\n{error}")
        print("Отсканируйте код из аккаунта владельца и повторите.")
        return 1

    _report(owner, user_session_path(secrets_dir))
    return 0


def run(path: Path | None = None) -> int:
    try:
        loaded = load_config(path)
    except ConfigError as error:
        print(f"config error:\n{error}")
        return 2
    try:
        return asyncio.run(_link(loaded))
    except (ValueError, TimeoutError) as error:
        print(f"не получилось: {error}")
        return 1
