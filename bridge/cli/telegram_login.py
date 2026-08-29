"""`python -m bridge telegram-login` — the user session, logged in once, by hand.

Creating a bot has no Bot API method, so it goes through the owner's own Telegram
account. That login is interactive: Telegram sends a code, and a two-factor
account also wants a password. A background service cannot answer either — it
has no terminal — so re-opening it is its own command.

Afterwards the service only ever *reuses* the stored session and never prompts.
The file it writes is as sensitive as the account itself: 0600, in the secrets
directory, backed up like a password or not at all.
"""

from __future__ import annotations

import asyncio
import os
from getpass import getpass
from pathlib import Path

from bridge.config import ConfigError, LoadedConfig, load_config
from bridge.provisioning.mtproto import MtprotoError, connect, session_path


def _credentials(loaded: LoadedConfig) -> tuple[int, str, str]:
    """api_id, api_hash and phone — from the environment, never from the config.

    They identify the owner's account, so they belong with the tokens and not in
    a file that is meant to be readable and shareable.
    """
    provisioning = loaded.app.provisioning
    api_id = os.environ.get(provisioning.mtproto_api_id_env, "").strip()
    api_hash = os.environ.get(provisioning.mtproto_api_hash_env, "").strip()
    phone = os.environ.get(provisioning.mtproto_phone_env, "").strip()

    missing = [
        name
        for name, value in (
            (provisioning.mtproto_api_id_env, api_id),
            (provisioning.mtproto_api_hash_env, api_hash),
            (provisioning.mtproto_phone_env, phone),
        )
        if not value
    ]
    if missing:
        raise MtprotoError(
            "не заданы переменные: " + ", ".join(f"${name}" for name in missing) + "\n"
            "api_id и api_hash берутся на my.telegram.org → API development tools."
        )
    if not api_id.isdigit():
        raise MtprotoError(f"${provisioning.mtproto_api_id_env} должен быть числом")
    return int(api_id), api_hash, phone


async def _login(loaded: LoadedConfig) -> int:
    api_id, api_hash, phone = _credentials(loaded)
    secrets_dir = loaded.app.paths.resolved_secrets_dir

    print(f"вход в Telegram как {phone[:4]}…{phone[-2:]}, сессия ляжет в {secrets_dir}")
    await connect(
        api_id=api_id,
        api_hash=api_hash,
        phone=phone,
        secrets_dir=secrets_dir,
        code_provider=lambda prompt: getpass(prompt),
        password_provider=lambda prompt: getpass(prompt),
    )

    print(f"готово: {session_path(secrets_dir)} (0600)")
    print("этот файл — доступ ко всему аккаунту, храните как пароль")
    print("теперь в чате со стражем работает /dialogs")
    return 0


def run(path: Path | None = None) -> int:
    try:
        loaded = load_config(path)
    except ConfigError as error:
        print(f"config error:\n{error}")
        return 2
    try:
        return asyncio.run(_login(loaded))
    except MtprotoError as error:
        print(f"не получилось: {error}")
        return 1
