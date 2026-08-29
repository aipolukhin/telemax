"""Creating contact bots the way Telegram means them to be created.

The first version of this drove @BotFather in a chat over a user session:
`/newbot`, a name, a username, parse the prose, hope the wording has not moved.
It worked, and then it rate-limited the account for sixteen hours in the middle
of a provisioning run — which is the failure that shows the shape was wrong, not
the tuning.

Telegram has an official mechanism for exactly this. A bot with
`can_manage_bots` may ask the owner to create another bot; Telegram shows its
own confirmation dialog with the name and username pre-filled; the manager gets
a `managed_bot` update and fetches the token with `getManagedBotToken`. No
prose, no user session, no imitation of a person typing.

The consequence that matters most is not the absence of parsing. It is this:

    **a bot is never deleted and recreated again.**

`getManagedBotToken` returns the token of a bot that already exists, and
`replaceManagedBotToken` rotates one that leaked. There is no
`deleteManagedBot` in the API surface at all, and there does not need to be —
the reason the old flow deleted anything was to get a token it had lost, which
is now a method call. So a re-run reuses what is there: no freed username, no
window where the owner's chat points at nothing, no creation limit spent.

What still needs the user session is unchanged and small: the list of bots the
account owns (`bots.getAdminedBots`, for capacity) and the `/start` that opens
the chat with a new bot, because a bot may not write first.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from .mtproto import CreatedBot, OwnedBot
from .provisioner import (
    BotLimit,
    BotNotManageableError,
    ConfirmationTimeoutError,
    ForeignUsernameError,
    ProvisionerError,
    UsernameState,
    typed_error,
)

logger = logging.getLogger(__name__)

#: Where Telegram's own bot-creation dialog lives. The manager's username and
#: the suggested username go in the path, the display name in the query — so the
#: owner confirms rather than types, and the username cannot drift from the
#: deterministic one.
NEWBOT_LINK = "https://t.me/newbot/{manager}/{username}"

#: How long to wait for the owner to press "Create" in Telegram's dialog. Long
#: enough to walk to another room, short enough that an abandoned provisioning
#: run does not hold a slot in the journal for ever.
CONFIRMATION_TIMEOUT_SECONDS = 600.0

NOT_A_MANAGER = (
    "Бот-страж пока не может создавать ботов.\n\n"
    "Откройте @BotFather → Mini App → выберите бота → Bot Management Mode → Enable.\n"
    "Это делается один раз."
)


class ManagerNotEnabledError(ProvisionerError):
    """The guardian does not have `can_manage_bots`. One toggle, once, by hand.

    Deliberately not worked around. The old workaround *was* driving @BotFather
    by hand, and it is what this module exists to replace.
    """

    def __init__(self) -> None:
        super().__init__(NOT_A_MANAGER)


def creation_link(manager_username: str, username: str, display_name: str) -> str:
    """The deep link that opens Telegram's own creation dialog, pre-filled."""
    from urllib.parse import quote

    link = NEWBOT_LINK.format(
        manager=manager_username.lstrip("@"), username=username.lstrip("@")
    )
    return f"{link}?name={quote(display_name)}" if display_name else link


@dataclass(frozen=True, slots=True)
class ManagedBot:
    """A bot this manager can fetch a token for."""

    bot_id: int
    username: str


class ManagerBot(Protocol):
    """The Bot API calls this needs, one manager bot's worth."""

    async def get_me(self) -> Any: ...

    async def __call__(self, method: Any) -> Any: ...


class OwnedBotSource(Protocol):
    """Where the list of the account's bots comes from — the user session."""

    async def admined_bots(self) -> list[OwnedBot]: ...

    async def username_holder(self, username: str) -> int | None: ...

    async def bot_creation_limit(self) -> int | None: ...

    async def is_premium(self) -> bool: ...

    async def send_start(self, username: str, bot_id: int | None = None) -> None: ...


class ManagedBotProvisioner:
    """`BotProvisioner` over Managed Bots, with the user session for the rest.

    Fits the same seam the @BotFather implementation did, so the picker, the
    capacity arithmetic and the batch did not have to learn a new vocabulary —
    with one deliberate difference in behaviour, `delete_owned_bot`, which does
    nothing here and says so.
    """

    #: `deleteManagedBot` does not exist in the Bot API surface at all, so this
    #: path can never free a slot. Saying so is what keeps the screen honest.
    deletes_bots = False

    #: Nor can a bot empty its own chat: Bot API deletes only what the bot sent,
    #: only within forty-eight hours, and never the chat itself.
    wipes_dialogs = False

    def __init__(
        self,
        *,
        manager: ManagerBot,
        manager_username: str,
        owned: OwnedBotSource,
        confirmation_timeout: float = CONFIRMATION_TIMEOUT_SECONDS,
    ) -> None:
        self._manager = manager
        self._manager_username = manager_username.lstrip("@")
        self._owned_source = owned
        self._timeout = confirmation_timeout
        self._owned: list[OwnedBot] | None = None
        # One pending confirmation per username: the owner is shown one dialog
        # at a time, and a second future for the same bot would strand the first.
        self._awaiting: dict[str, asyncio.Future[ManagedBot]] = {}

    @property
    def manager_username(self) -> str:
        return self._manager_username

    # ------------------------------------------------------------------ reading

    async def can_manage(self) -> bool:
        """Whether the toggle is on. Asked, never assumed — it is set by hand."""
        me = await self._manager.get_me()
        return bool(getattr(me, "can_manage_bots", False))

    async def require_manager(self) -> None:
        if not await self.can_manage():
            raise ManagerNotEnabledError

    async def list_owned_bots(self, *, refresh: bool = True) -> list[OwnedBot]:
        if refresh or self._owned is None:
            self._owned = await self._owned_source.admined_bots()
        return list(self._owned or [])

    async def account_bot_count(self) -> int | None:
        """How many bots the account owns, when anything can say. Never guessed.

        Separate from `list_owned_bots` on purpose, and the separation is load
        bearing. That list decides *identity* — whether a username is ours to
        drive — and must stay the local one: a bot the account owns but this
        manager cannot manage is exactly `UNMANAGEABLE`, and folding it into the
        list would call it `OWNED` and hand out a token that never arrives.
        This answers a different question, for a different screen: how full the
        account is.
        """
        ask = getattr(self._owned_source, "account_bot_count", None)
        if ask is None:
            return None
        count: int | None = await ask()
        return count

    async def get_creation_limit(self) -> BotLimit:
        """Managed Bots do not lift the account limit — they only stop us
        burning it on bots we already had."""
        premium = await self._owned_source.is_premium()
        try:
            value = await self._owned_source.bot_creation_limit()
        except Exception:  # noqa: BLE001 - an unknown limit is a real answer
            logger.warning("could not read the bot creation limit from Telegram")
            value = None
        return BotLimit(value=value, premium=premium)

    async def check_username(self, username: str) -> UsernameState:
        """Free, ours, or ours-in-name-only. Never `FOREIGN` on this path.

        A manager cannot tell a stranger's bot from an unmanaged bot of the
        owner's: both answer `BOT_ACCESS_FORBIDDEN`. So this does not guess. The
        deterministic username is a hundred-bit hash of the owner's own two
        account ids, which makes "a stranger happens to hold exactly it" a case
        not worth modelling and "the owner made it by hand" the common one — and
        `UNMANAGEABLE` is the honest name for both, with a remedy that fits
        either. `FOREIGN` stays reachable where a user session can actually
        prove another owner.
        """
        target = username.lower().lstrip("@")
        for bot in await self.list_owned_bots():
            if (bot.username or "").lower() == target:
                return UsernameState.OWNED
        try:
            holder = await self._owned_source.username_holder(target)
        except Exception:
            logger.debug("could not resolve @%s", target, exc_info=True)
            return UsernameState.FREE
        if holder is None:
            return UsernameState.FREE
        # Somebody holds it, and the local inventory is a lower bound rather
        # than the truth: a bot created a moment ago has no bridge row yet.
        # Telegram settles it — a managed token comes back only for a bot this
        # manager can manage.
        manageable = await self._can_manage_bot(holder)
        if manageable is None:
            # Could not ask. Not an answer, and certainly not "somebody else's":
            # saying that would mark the attempt permanently failed over a
            # network blip.
            raise ProvisionerError(f"не удалось спросить Telegram про @{target}")
        if manageable:
            return UsernameState.OWNED
        # Ours by name and not ours to drive. The username is a hundred-bit
        # hash of the owner's own two account ids, so this is a bot the owner
        # made by hand far more often than it is a stranger's.
        return UsernameState.UNMANAGEABLE

    async def _can_manage_bot(self, bot_id: int) -> bool | None:
        """Whether this manager can drive that bot. `None` means "could not ask".

        `getManagedBotToken` answers for bots the manager itself created and refuses with
        `BOT_ACCESS_FORBIDDEN` for a bot the *same account* made by hand in
        @BotFather. Management comes from the creation flow, not from ownership.

        The three-valued answer is the point. A refusal and a lost packet look
        alike in a log line and mean opposite things — one is permanent, the
        other is a blip — and the caller marks bridges permanently failed on the
        strength of it. So the two are told apart by exception *type* rather than
        by matching words in Telegram's prose, which is not a contract.
        """
        try:
            await self.token_for(bot_id)
        except ProvisionerError as error:
            if isinstance(error.__cause__, TimeoutError | OSError):
                logger.debug("could not ask about bot %s: %s", bot_id, error)
                return None
            return False
        except (TimeoutError, OSError) as error:
            logger.debug("could not ask about bot %s: %s", bot_id, error)
            return None
        except Exception:
            # Nothing else should reach here — `token_for` wraps — but an
            # unexpected shape is "could not ask", never "refused".
            logger.debug("could not ask about bot %s", bot_id, exc_info=True)
            return None
        return True

    async def bot_id_for(self, username: str) -> int | None:
        """Which bot holds this username, if it is one of ours.

        The local inventory is a lower bound, not the answer. A bot created by
        hand in @BotFather — which is what the owner falls back to when
        Telegram's own dialog refuses — has no bridge row, so it is not in the
        list, and returning None for it meant provisioning kept waiting for a
        confirmation of a bot that already existed.

        So the account is asked, and manageability is proved rather than
        assumed. A bot that answers `BOT_ACCESS_FORBIDDEN` is deliberately *not*
        returned here: this is a hand-made bot the manager cannot drive, and
        handing its id to the batch would produce a bridge whose token can never
        be rotated. `check_username` reports that case as `UNMANAGEABLE`, which
        has a remedy; a half-adopted bridge does not.
        """
        target = username.lower().lstrip("@")
        for bot in await self.list_owned_bots():
            if (bot.username or "").lower() == target:
                return bot.bot_id
        try:
            holder = await self._owned_source.username_holder(target)
        except Exception:
            logger.debug("could not resolve @%s", target, exc_info=True)
            return None
        if holder is None:
            return None
        return holder if await self._can_manage_bot(holder) is True else None

    # ------------------------------------------------------------------ tokens

    async def token_for(self, bot_id: int) -> str:
        """The token of a bot that already exists. The whole point of this file.

        The old flow reached this state by deleting the bot and creating it
        again, because a token it had not written down was unrecoverable. It is
        one call now.
        """
        from aiogram.methods import GetManagedBotToken

        try:
            return str(await self._manager(GetManagedBotToken(user_id=bot_id)))
        except Exception as error:
            raise ProvisionerError(f"не удалось получить токен бота {bot_id}: {error}") from error

    async def rotate_token(self, bot_id: int) -> str:
        """Issue a new token and invalidate the old one.

        The honest answer to "the stored token no longer works" — and to a token
        that may have leaked. Both used to mean deleting the bot.
        """
        from aiogram.methods import ReplaceManagedBotToken

        try:
            return str(await self._manager(ReplaceManagedBotToken(user_id=bot_id)))
        except Exception as error:
            raise ProvisionerError(f"не удалось перевыпустить токен {bot_id}: {error}") from error

    # ---------------------------------------------------------------- creating

    def confirmation_link(self, username: str, display_name: str) -> str:
        return creation_link(self._manager_username, username, display_name)

    def expect(self, username: str) -> asyncio.Future[ManagedBot]:
        """Register interest in a bot the owner is about to confirm.

        Called *before* the link is shown: the update can arrive faster than the
        message the owner tapped it from is edited.
        """
        target = username.lower().lstrip("@")
        pending = self._awaiting.get(target)
        if pending is not None:
            # Returned even when it is already done. A completed future here is
            # a confirmation that has arrived and not yet been claimed — which
            # is exactly what happens when the owner taps Create faster than the
            # two round trips between registering and waiting. Replacing it with
            # a fresh one threw that confirmation away and then timed out.
            return pending
        created: asyncio.Future[ManagedBot] = asyncio.get_running_loop().create_future()
        self._awaiting[target] = created
        return created

    def forget(self, username: str) -> None:
        """Drop an expectation nothing is going to wait on any more."""
        self._awaiting.pop(username.lower().lstrip("@"), None)

    async def prepare(self, username: str) -> bool:
        """Everything that must happen *before* the Create link is drawn.

        Returns True when the bot already exists and no confirmation is needed.

        The order this fixes is not cosmetic. It used to be: draw the link, then
        `getMe`, then `getAdminedBots`, then register the expectation. An owner
        who tapped Create inside that window — two Bot API round trips wide — hit
        `on_managed_bot` with nobody waiting; the update was dropped, and the run
        then sat out the whole ten-minute timeout and failed, on a bot that
        existed. Registering first costs nothing and closes it.
        """
        target = username.lower().lstrip("@")
        await self.require_manager()
        if await self.bot_id_for(target) is not None:
            return True
        self.expect(target)
        return False

    def on_managed_bot(self, bot_id: int, username: str) -> bool:
        """Feed in a `managed_bot` update. True when somebody was waiting.

        False is not an error: the owner may create a bot from Telegram's own
        interface at a moment nothing is provisioning, and the mapping is
        rebuilt from `getAdminedBots` the next time the list is drawn.

        The resolved future stays in the map until whoever registered it takes
        it. Popping it here was a second way to lose a confirmation: an update
        that arrived between `prepare` and `await_confirmation` set a result on
        a future nobody held any more, and the wait that followed made a fresh
        one and timed out on a bot that had just been created.
        """
        target = (username or "").lower().lstrip("@")
        pending = self._awaiting.get(target)
        self._owned = None
        if pending is None or pending.done():
            return False
        pending.set_result(ManagedBot(bot_id=bot_id, username=target))
        return True

    async def await_confirmation(self, username: str) -> ManagedBot:
        """Block until the owner presses Create, or give up saying so.

        Its own error type, not a bare `ProvisionerError`: nobody pressing a
        button is the one failure here that is entirely the owner's to fix, and
        the screen for it says «подтвердите создание», not «что-то пошло не так».
        """
        pending = self.expect(username)
        try:
            return await asyncio.wait_for(pending, timeout=self._timeout)
        except TimeoutError as error:
            raise ConfirmationTimeoutError("владелец не подтвердил создание бота") from error
        finally:
            # Claimed or given up on, the expectation is this call's to clear.
            self.forget(username)

    async def create_bot(self, *, name: str, username: str) -> CreatedBot:
        """Reuse the bot if it is there; otherwise wait for the owner to confirm.

        `name` reaches Telegram's dialog as a suggestion — the owner can change
        it, and that is fine. The *username* is the part that must not drift,
        and Telegram fills it in from the link rather than asking.
        """
        target = username.lower().lstrip("@")
        await self.require_manager()

        try:
            existing = await self.bot_id_for(target)
        except ProvisionerError:
            raise
        except Exception as error:
            # Typed rather than let out raw: the batch catches `ProvisionerError`,
            # and a bare `TimeoutError` from here would abandon the whole walk
            # instead of failing this one contact as retryable.
            self.forget(target)
            raise typed_error(error) from error
        if existing is not None:
            logger.info("@%s already exists; fetching its token", target)
            # Whatever `prepare` registered is not going to be waited on.
            self.forget(target)
            return CreatedBot(
                username=target, token=await self.token_for(existing), bot_id=existing
            )

        try:
            confirmed = await self.await_confirmation(target)
        except ConfirmationTimeoutError:
            # Nobody pressed Create — *or* the update was lost. A timeout is not
            # evidence that the bot does not exist, so the question is asked of
            # Telegram before the owner is told anything.
            return await self._after_timeout(target)
        except ProvisionerError as error:
            self.forget(target)
            raise typed_error(error) from error

        if confirmed.username != target:
            # Telegram let the owner change the username. Adopting it would
            # break every stored link, so it is refused rather than absorbed.
            raise ForeignUsernameError(target)
        self._owned = None
        return CreatedBot(
            username=target,
            token=await self.token_for(confirmed.bot_id),
            bot_id=confirmed.bot_id,
        )

    async def _after_timeout(self, target: str) -> CreatedBot:
        """Ask Telegram what actually happened, then decide.

        Three answers and three different things to do, and the old code did the
        same thing for all of them: report that nobody confirmed.

        * ours — the bot was created and the update went missing. Adopt it.
        * free — nothing was created. Retryable, and deliberately *not* by
          inventing a different username.
        * somebody else's — a permanent collision, which is not a timeout at all.
        """
        self.forget(target)
        try:
            existing = await self.bot_id_for(target)
            if existing is not None:
                logger.warning(
                    "@%s exists although no confirmation arrived; adopting it", target
                )
                return CreatedBot(
                    username=target, token=await self.token_for(existing), bot_id=existing
                )
            state = await self.check_username(target)
        except ProvisionerError:
            raise
        except Exception as error:
            raise ConfirmationTimeoutError(
                "владелец не подтвердил создание бота"
            ) from error

        if state is UsernameState.FOREIGN:
            raise ForeignUsernameError(target)
        if state is UsernameState.UNMANAGEABLE:
            raise BotNotManageableError(target)
        raise ConfirmationTimeoutError("владелец не подтвердил создание бота")

    async def delete_owned_bot(self, username: str) -> None:
        """Nothing. There is no `deleteManagedBot`, and nothing needs one.

        Kept so the batch can keep one shape for both provisioners: the step
        that used to free a username is now a no-op, because the bot is reused
        instead of rebuilt.
        """
        logger.debug("managed bots are never deleted; @%s stays", username)

    async def wait_for_username_release(self, username: str, **_: Any) -> bool:
        """Always true: nothing was deleted, so nothing has to be released."""
        return True

    async def send_start(self, username: str, bot_id: int | None = None) -> None:
        await self._owned_source.send_start(username)
