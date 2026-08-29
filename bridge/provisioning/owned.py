"""The five questions the user session used to answer, answered by Bot API.

Managed Bots removed the need for a user session to *create* a bot. They did not,
by themselves, remove the session: the provisioning flow also asks which bots the
account owns, who holds a username, what the creation limit is, and it sends the
`/start` that opens the chat with a new bot. This module answers all of that
without an account credential on disk, which is what lets a production install
drop Telethon entirely (`provisioning.mode: managed`).

The compatibility boundary follows these rules:

* **`getManagedBotToken` answers for bots the manager created, and only those.**
  A bot made by hand in @BotFather on the same account can answer
  `Bad Request: BOT_ACCESS_FORBIDDEN`. Management comes from the creation flow,
  not from ownership. So "is this bot mine" is really two questions, and only the
  second has a Bot API answer: whether it can be *driven*. A bot that refuses is
  `UsernameState.UNMANAGEABLE`, not somebody else's.
* **A bot can resolve a username.** `getChat("@BotFather")` returns the chat;
  `getChat` on a free name fails with `Bad Request: chat not found`. That is the
  "taken by somebody else" pre-check the session used `contacts.resolveUsername`
  for.
* **There is no way to list managed bots and no way to read the account's bot
  limit.** Neither exists in Bot API 10.2. So the count comes from the bridges
  this install created and the limit from configuration — both are estimates,
  and Telegram's own refusal at creation time stays the final word.
* **A bot still cannot write first.** Nothing replaces `/start`; it becomes one
  tap by the owner on a link the guardian sends.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .mtproto import OwnedBot
from .provisioner import ProvisionerError

logger = logging.getLogger(__name__)

#: What an account may own without Premium. Not readable over Bot API — the real
#: number lives in `bots_create_limit_default`, which only the app config knows.
#: Used to draw a count, never to refuse: Telegram refuses for itself.
ASSUMED_BOT_LIMIT = 20


class AccountBotsPort(Protocol):
    """The two questions Bot API cannot answer, asked of the owner's session.

    Optional everywhere. An install with no user session keeps the old estimate
    and says so on screen; one that has a session gets the real numbers.
    """

    async def count(self) -> int | None: ...

    async def creation_limit(self) -> int | None: ...

    async def premium(self) -> bool: ...


class OwnerMustOpenChatError(ProvisionerError):
    """A bot may not write first, and there is no session to press Start.

    Carries the link, because the only way out is the owner tapping it — this is
    not a failure to retry, it is a step that belongs to a person.
    """

    def __init__(self, username: str, link: str) -> None:
        super().__init__(f"владелец должен открыть чат с @{username}")
        self.username = username
        self.link = link


class KnownBots(Protocol):
    """The bots this install has created, from its own database."""

    async def known_bots(self) -> list[OwnedBot]: ...


class BotApiOwnedBots:
    """`OwnedBotSource` without a user session, for `provisioning.mode: managed`."""

    def __init__(
        self,
        *,
        manager: Any,
        known: KnownBots,
        assumed_limit: int = ASSUMED_BOT_LIMIT,
        account: AccountBotsPort | None = None,
        guardian: OwnedBot | None = None,
    ) -> None:
        self._manager = manager
        self._known = known
        self._assumed_limit = assumed_limit
        # The owner's own session, when there is one. Read-only, best effort,
        # and never on the critical path: every question it answers has an
        # honest fallback below.
        self._account = account
        # The guardian is a bot this account owns and it occupies one of the
        # twenty. Leaving it out made the free-slot figure one too generous, and
        # the figure is what the picker refuses selections on.
        self._guardian = guardian

    async def admined_bots(self) -> list[OwnedBot]:
        """What this install made — every bridge, whatever state it is in.

        The old answer came from `bots.getAdminedBots` and covered the whole
        account. Bot API has no equivalent, so the count can only ever be a lower
        bound — which is honest for a capacity display and useless as a guard,
        and it is used for exactly the first.

        It used to be a lower bound with a hole in it: only *active* bridges were
        listed, so a bot belonging to a disabled bridge was not "ours". Asked
        about its username, this then fell through to `getChat`, found the bot,
        and reported a stranger — which made reconnecting a contact the owner had
        disconnected impossible from the interface.
        """
        bots = await self._known.known_bots()
        if self._guardian is None:
            return bots
        known = {bot.bot_id for bot in bots}
        if self._guardian.bot_id in known:
            return bots
        return [*bots, self._guardian]

    async def username_holder(self, username: str) -> int | None:
        """The id holding this username, or None when nothing does.

        `getChat` is the substitute for `contacts.resolveUsername`: a free name
        answers `chat not found`, which is the one error that must not be
        reported as "somebody else has it".
        """
        target = username.lstrip("@")
        try:
            chat = await self._manager.get_chat(f"@{target}")
        except Exception as error:
            if "not found" in str(error).lower():
                return None
            logger.debug("could not resolve @%s", target, exc_info=True)
            raise
        return int(chat.id)

    async def account_bot_count(self) -> int | None:
        """How many bots the *account* owns — not how many this install made.

        None over Bot API alone, which is the honest answer: `getAdminedBots`
        has no Bot API equivalent. The owner has bots that predate Telemax and
        bots that have nothing to do with it, and every one of them occupies a
        slot this install cannot see.
        """
        if self._account is None:
            return None
        try:
            return await self._account.count()
        except Exception:
            logger.debug("could not count the account's bots", exc_info=True)
            return None

    async def bot_creation_limit(self) -> int | None:
        """Telegram's real cap when the session can read it, configuration otherwise."""
        if self._account is not None:
            try:
                measured = await self._account.creation_limit()
            except Exception:
                logger.debug("could not read the account's bot limit", exc_info=True)
            else:
                if measured is not None:
                    return measured
        return self._assumed_limit

    async def is_premium(self) -> bool:
        """Unknowable over Bot API — it only ever scaled the limit — but askable
        of a session, and `assumed_bot_limit` cannot know which cap applies."""
        if self._account is None:
            return False
        try:
            return await self._account.premium()
        except Exception:
            logger.debug("could not read the Premium status", exc_info=True)
            return False

    async def send_start(self, username: str, bot_id: int | None = None) -> None:
        raise OwnerMustOpenChatError(username.lstrip("@"), start_link(username))


def start_link(username: str) -> str:
    return f"https://t.me/{username.lstrip('@')}?start=telemax"


class RepositoryKnownBots:
    """`KnownBots` over the bridges table — *every* row, not only the live ones.

    Ownership is not a statement about whether a bridge is running. A disabled
    bridge's bot exists, holds its username and occupies a slot, and reading it
    as somebody else's is worse than not knowing about it at all: the picker
    refuses the contact outright and says the name belongs to another account.
    """

    def __init__(self, rows: Callable[[], Awaitable[list[Any]]]) -> None:
        self._rows = rows

    async def known_bots(self) -> list[OwnedBot]:
        return [
            OwnedBot(
                bot_id=int(record.telegram_bot_id),
                username=record.expected_username,
                name=record.title,
            )
            for record in await self._rows()
            # A row with no proven bot id names nothing: counting it would spend
            # a slot on a bot that may never have been created.
            if record.telegram_bot_id
        ]
