"""Who the two accounts are, before anything is named after them.

A V2 guardian username is a function of the Telegram owner's id and the MAX
owner's id. Both have to be *known* before the bot exists, which inverts the
order the managed console used: it created the guardian first and learned the
Telegram id from whoever pressed Start on it.

So the identities are acquired first, from sources that prove them:

* **Telegram** — `telegram.owner_user_id` if a previous run wrote one, otherwise
  `get_me` on the owner's own session. Never the Telegram username, never the
  phone, and never the sender id of an unverified callback: a bot name that can
  be moved by anything an attacker can send is not an identity.
* **MAX** — `own_user_id` from a MAX session that already exists on this machine.
  Read-only: this opens the stored session and asks who it belongs to. It never
  logs in, never prompts and never creates anything.

When either is missing the answer is to stop and say which one, not to guess.
Half a formula would put a bot at an address nothing else will ever compute
again.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from bridge.provisioning.naming_v2 import guardian_bot_username_v2

from .plan import Plan

logger = logging.getLogger(__name__)

NO_TELEGRAM_ID = (
    "Не знаю Telegram-аккаунт владельца.\n\n"
    "Имя бота-стража выводится из ваших Telegram и MAX аккаунтов, поэтому его\n"
    "нельзя вычислить до того, как оба известны.\n\n"
    "Подключите аккаунт владельца по QR и повторите setup."
)

NO_MAX_ID = (
    "Не знаю MAX-аккаунт владельца.\n\n"
    "Имя бота-стража выводится из ваших Telegram и MAX аккаунтов, поэтому его\n"
    "нельзя вычислить до того, как оба известны.\n\n"
    "Либо подключите MAX и повторите setup, либо примите уже созданного стража:\n"
    "  python -m bridge setup --adopt-guardian"
)


@dataclass(frozen=True, slots=True)
class OwnerIdentities:
    """The two accounts this installation belongs to."""

    telegram_user_id: int
    max_user_id: int

    @property
    def guardian_username(self) -> str:
        return guardian_bot_username_v2(self.telegram_user_id, self.max_user_id)


class IdentityUnavailableError(Exception):
    """One of the two owner ids could not be established. The text says which."""


def telegram_owner_id(plan: Plan) -> int | None:
    """The owner's Telegram id, from the config a previous run validated."""
    return plan.owner_user_id


async def telegram_owner_id_from_session(
    *, api_id: int, api_hash: str, secrets_dir: Path
) -> int | None:
    """`get_me` on the owner's own session, when one is already authorised.

    Read-only and silent about failure: an absent or unauthorised session is a
    reason to ask for one, not an error to raise from a getter.
    """
    from bridge.telegram.user_session import user_session_path

    path = user_session_path(secrets_dir)
    if not path.exists():
        return None
    try:
        from telethon import TelegramClient  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover - optional dependency
        return None

    client = TelegramClient(str(path.with_suffix("")), api_id, api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return None
        me = await client.get_me()
        identifier = getattr(me, "id", None)
        return int(identifier) if identifier is not None else None
    except Exception:
        logger.debug("could not read the owner's Telegram id from the session", exc_info=True)
        return None
    finally:
        try:
            await client.disconnect()
        except Exception:
            logger.debug("could not close the owner session", exc_info=True)


async def max_owner_id(plan: Plan, *, phone_env: str = "MAX_PHONE") -> int | None:
    """The owner's MAX id, from the session already on this machine.

    Opens the stored session and asks who it belongs to. Never logs in, never
    prompts, never creates anything: a first-ever install has no MAX session and
    this answers None, which is what sends the console down the adopt path with
    an honest explanation rather than inventing a guardian name.
    """
    session_dir = plan.data_dir / "max-session"
    if not any(session_dir.glob("*.db")):
        return None
    phone = os.environ.get(phone_env, "").strip()
    if not phone:
        return None

    from bridge.max_client import MaxClient

    client = MaxClient(phone=phone, session_dir=session_dir, session_name="max-session.db")
    try:
        await client.start()
        return client.own_user_id
    except Exception:
        logger.debug("could not read the owner's MAX id", exc_info=True)
        return None
    finally:
        try:
            await client.stop()
        except Exception:
            logger.debug("could not close the MAX session", exc_info=True)


async def owner_identities(
    plan: Plan, *, telegram_user_id: int | None = None
) -> OwnerIdentities:
    """Both ids, or a refusal that names the one that is missing."""
    telegram = telegram_user_id if telegram_user_id is not None else telegram_owner_id(plan)
    if not telegram:
        raise IdentityUnavailableError(NO_TELEGRAM_ID)
    maximum = await max_owner_id(plan)
    if not maximum:
        raise IdentityUnavailableError(NO_MAX_ID)
    return OwnerIdentities(telegram_user_id=int(telegram), max_user_id=int(maximum))
