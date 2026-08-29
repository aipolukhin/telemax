"""The one interface the rest of the bridge uses to own Telegram bots.

Everything above this line thinks in terms of *bots the owner has*, *usernames
that are free* and *how many more may exist*. Everything below it is @BotFather
prose and MTProto request objects. Keeping the seam here is what lets the
provisioning flow be tested at all: BotFather cannot be exercised without a real
account, and its wording is not a contract.

Three rules live in this module rather than in the callers, because forgetting
one of them costs somebody a bot:

* **only ever delete a bot Telegram itself says the owner owns**, matched by
  exact username. Local configuration is not evidence.
* **never invent a different username.** A deterministic name that is taken is
  information, not an obstacle to route around.
* **a released username is observed, not assumed.** Telegram frees a deleted
  bot's name on its own schedule; recreating too early fails in a way that looks
  like the name belongs to somebody else.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .mtproto import (
    BotFatherTooSoonError,
    CreatedBot,
    MtprotoError,
    OwnedBot,
    UsernameStillTakenError,
)

logger = logging.getLogger(__name__)

#: How long to keep asking Telegram whether a deleted bot's username is free.
#: Deletion is not instant and the delay is not documented; observed releases
#: happen within seconds, but a minute of patience is much cheaper than a
#: half-provisioned contact.
USERNAME_RELEASE_ATTEMPTS = 8
USERNAME_RELEASE_FIRST_DELAY = 1.0
USERNAME_RELEASE_MAX_DELAY = 15.0

#: What Telegram says when the account may not own another bot. The final word
#: on capacity, whatever the local preflight computed a moment earlier.
LIMIT_MARKERS = (
    "BOT CREATE LIMIT EXCEEDED",
    "BOTS TOO MUCH",
    "TOO MANY BOTS",
)

#: "Come back later" — a rate limit, which is not a capacity limit and must
#: never be shown as one. The two used to collapse into the same sentence, and
#: the difference is whether waiting helps or the owner has to delete a bot.
FLOOD_MARKERS = (
    "FLOOD WAIT",
    "FLOOD PREMIUM WAIT",
    "SLOWMODE WAIT",
    "TOO MANY REQUESTS",
    "RETRY AFTER",
)

#: Nothing about the account is wrong; Telegram was simply not reachable.
NETWORK_MARKERS = (
    "TIMEOUT",
    "TIMED OUT",
    "CONNECTION",
    "NETWORK",
    "TEMPORARILY UNAVAILABLE",
    "BAD GATEWAY",
    "SERVICE UNAVAILABLE",
)


class UsernameState(StrEnum):
    """Who holds a deterministic username right now."""

    #: Nobody. It can be created.
    FREE = "free"
    #: A bot of the current owner's, with exactly this username. Replaceable.
    OWNED = "owned"
    #: Somebody else. Not ours to delete, and not ours to rename around.
    FOREIGN = "foreign"
    #: A bot at *our* deterministic name that this manager cannot manage.
    #:
    #: Compatibility tests confirmed: `getManagedBotToken` answers `BOT_ACCESS_FORBIDDEN`
    #: for a bot created by hand in @BotFather, even though the same account
    #: owns it and owns the manager. Management is established when the bot is
    #: created through the managed-bots flow; it is not a consequence of owning
    #: both. An earlier note in `owned` said otherwise and was wrong.
    #:
    #: Its own state because the remedy is its own. "Somebody else has it" sends
    #: the owner to fix a collision that does not exist; the real answer is to
    #: delete that bot and let the guardian make it, or to hand its token over.
    #: The name is a hundred-bit hash of the owner's two account ids, so a
    #: stranger holding exactly it is not a case worth modelling.
    UNMANAGEABLE = "unmanageable"


class ProvisionerError(Exception):
    """Something the owner has to know about, in words meant for them."""


class ForeignUsernameError(ProvisionerError):
    """The deterministic username belongs to another account."""

    def __init__(self, username: str) -> None:
        super().__init__(username)
        self.username = username


class BotNotManageableError(ProvisionerError):
    """The bot is at our name and this guardian cannot manage it.

    Not a collision. `getManagedBotToken` answers `BOT_ACCESS_FORBIDDEN` for a
    bot created by hand in @BotFather even when the same account owns both it
    and the manager — management comes from the creation flow, not from
    ownership.
    """

    def __init__(self, username: str) -> None:
        super().__init__(username)
        self.username = username


class CreationLimitError(ProvisionerError):
    """Telegram refused: this account may not own another bot."""


class FloodWaitError(ProvisionerError):
    """Telegram is rate-limiting the account. Waiting fixes this; deleting does not."""

    def __init__(self, message: str, *, seconds: int | None = None) -> None:
        super().__init__(message)
        self.seconds = seconds


class ConfirmationTimeoutError(ProvisionerError):
    """The owner never pressed Create in Telegram's own dialog."""


class NetworkError(ProvisionerError):
    """Telegram could not be reached. Nothing is known about the account."""


class LimitUnknownError(ProvisionerError):
    """Telegram did not publish a creation limit, so capacity cannot be claimed."""


class ProvisioningFailure(StrEnum):
    """Why a bridge could not be built, as something other than a sentence.

    The sentence is for the owner and changes with the wording; this is what the
    code branches on. They were the same thing once, and that is how a rate
    limit ended up telling somebody with thirty-nine free slots that they had
    run out.
    """

    BOT_CREATE_LIMIT_EXCEEDED = "bot_create_limit_exceeded"
    FLOOD_WAIT = "flood_wait"
    USERNAME_OCCUPIED = "username_occupied"
    #: The bot exists at our own name and this guardian has no access to it.
    BOT_NOT_MANAGEABLE = "bot_not_manageable"
    MANAGED_BOT_CONFIRMATION_REQUIRED = "managed_bot_confirmation_required"
    NETWORK_ERROR = "network_error"
    UNKNOWN_PROVISIONING_ERROR = "unknown_provisioning_error"

    @property
    def retryable(self) -> bool:
        """Whether pressing the button again could ever produce a different end.

        A username somebody else holds and a limit that is full both need the
        owner to go and change something first; everything else is worth another
        attempt.
        """
        return self not in {
            ProvisioningFailure.BOT_CREATE_LIMIT_EXCEEDED,
            ProvisioningFailure.USERNAME_OCCUPIED,
            ProvisioningFailure.BOT_NOT_MANAGEABLE,
        }


def _normalised(error: BaseException) -> str:
    # Underscores normalised away: the same refusal arrives as an MTProto
    # error code and as a sentence from @BotFather.
    return str(error).upper().replace("_", " ")


def is_limit_error(error: BaseException) -> bool:
    text = _normalised(error)
    if any(marker in text for marker in FLOOD_MARKERS):
        # `FLOOD_WAIT` has never meant "no more bots", and a text match that
        # does not check this first will read one as the other.
        return False
    return any(marker in text for marker in LIMIT_MARKERS)


def is_flood_error(error: BaseException) -> bool:
    text = _normalised(error)
    return any(marker in text for marker in FLOOD_MARKERS)


def classify_failure(error: BaseException) -> ProvisioningFailure:
    """What went wrong, decided by type first and by wording only as a fallback.

    Order matters twice over: an exception class is a deliberate statement and
    beats any string, and among the strings a flood wait is checked before a
    limit — Telegram's rate-limit text has carried the word "bot" often enough
    to match a limit marker by accident.
    """
    if isinstance(error, FloodWaitError):
        return ProvisioningFailure.FLOOD_WAIT
    if isinstance(error, CreationLimitError):
        return ProvisioningFailure.BOT_CREATE_LIMIT_EXCEEDED
    if isinstance(error, BotNotManageableError):
        return ProvisioningFailure.BOT_NOT_MANAGEABLE
    if isinstance(error, ForeignUsernameError):
        return ProvisioningFailure.USERNAME_OCCUPIED
    if isinstance(error, ConfirmationTimeoutError):
        return ProvisioningFailure.MANAGED_BOT_CONFIRMATION_REQUIRED
    if isinstance(error, NetworkError):
        return ProvisioningFailure.NETWORK_ERROR

    if is_flood_error(error):
        return ProvisioningFailure.FLOOD_WAIT
    if is_limit_error(error):
        return ProvisioningFailure.BOT_CREATE_LIMIT_EXCEEDED
    text = _normalised(error)
    if any(marker in text for marker in NETWORK_MARKERS):
        return ProvisioningFailure.NETWORK_ERROR
    return ProvisioningFailure.UNKNOWN_PROVISIONING_ERROR


#: The exception each failure is raised as, so a caller can catch the specific
#: one rather than re-reading the text a layer further up.
_AS_EXCEPTION: dict[ProvisioningFailure, type[ProvisionerError]] = {
    ProvisioningFailure.BOT_CREATE_LIMIT_EXCEEDED: CreationLimitError,
    ProvisioningFailure.FLOOD_WAIT: FloodWaitError,
    ProvisioningFailure.MANAGED_BOT_CONFIRMATION_REQUIRED: ConfirmationTimeoutError,
    ProvisioningFailure.NETWORK_ERROR: NetworkError,
    ProvisioningFailure.BOT_NOT_MANAGEABLE: BotNotManageableError,
}


def typed_error(error: BaseException) -> ProvisionerError:
    """Turn whatever Telegram raised into the narrowest error we have for it."""
    failure = classify_failure(error)
    return _AS_EXCEPTION.get(failure, ProvisionerError)(str(error))


@dataclass(frozen=True, slots=True)
class BotLimit:
    """How many bots the account may own, and whether Telegram actually said so.

    `value is None` is a real answer and is handled as one everywhere: it means
    new bots cannot be planned for safely, not that the limit is twenty.
    """

    value: int | None
    premium: bool = False

    @property
    def known(self) -> bool:
        return self.value is not None


class BotProvisioner(Protocol):
    """Owning Telegram bots, with no reference to how it is done."""

    async def list_owned_bots(self) -> list[OwnedBot]: ...

    #: How many bots the *account* owns, or None when it cannot be asked. Not
    #: the same question as `list_owned_bots`, which is about what is ours to
    #: drive — see `ManagedBotProvisioner.account_bot_count`.
    async def account_bot_count(self) -> int | None: ...

    async def get_creation_limit(self) -> BotLimit: ...

    async def check_username(self, username: str) -> UsernameState: ...

    async def delete_owned_bot(self, username: str) -> None: ...

    async def create_bot(self, *, name: str, username: str) -> CreatedBot: ...

    async def send_start(self, username: str, bot_id: int | None = None) -> None: ...


#: Least time between two `/newbot` walks. @BotFather rate-limits creation by
#: the hour and says so only after the fact, so a batch of five contacts used to
#: go out back to back — which is how a live account was locked out of creating
#: bots for sixteen hours in the middle of a run. Slow is not the cost here: the
#: owner is watching one bridge come up, and the next one is already waiting on
#: their tap.
NEWBOT_MIN_INTERVAL_SECONDS = 20.0

#: Used only when @BotFather refuses without naming a wait, which has not been
#: observed. It always says: 62 seconds at the low end, 58000 after five walks
#: back to back. The stated number is honoured; this is the floor under a
#: refusal that says nothing.
BOTFATHER_COOLDOWN_SECONDS = 600.0


class MtprotoProvisioner:
    """`BotProvisioner` over the owner's own Telegram session.

    Every @BotFather operation is serialised behind one lock. BotFather is a
    stateful conversation with a single position in it — two `/newbot` walks
    interleaved would answer each other's questions, and the failure looks like
    a bot created with the wrong name.

    The lock is not the whole of the politeness. A refusal from @BotFather is
    sticky: once it has said "too many", this stops asking for an hour instead
    of walking the rest of the batch into the same wall. The alternative was
    measured — five walks back to back, and sixteen hours of an account that
    could not make a bot.
    """

    def __init__(
        self,
        session: Any = None,
        *,
        factory: Callable[[], Awaitable[Any]] | None = None,
        sleep: Any = asyncio.sleep,
    ) -> None:
        # A user session is a whole-account credential. Most runs of the bridge
        # never create a bot, so it is opened on the first operation that needs
        # it rather than held open for the lifetime of the process.
        self._session = session
        self._factory = factory
        # Injected so a test can watch the backoff without living through it.
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._owned: list[OwnedBot] | None = None
        # Monotonic, from the running loop: a wall clock that steps backwards
        # would turn a cooldown into a no-op.
        self._last_newbot: float | None = None
        self._quiet_until: float | None = None

    async def _connected(self) -> Any:
        if self._session is not None:
            return self._session
        if self._factory is None:
            raise ProvisionerError("нет пользовательской сессии Telegram")
        async with self._connect_lock:
            if self._session is None:
                self._session = await self._factory()
        return self._session

    # ------------------------------------------------------------------ reading

    async def list_owned_bots(self, *, refresh: bool = True) -> list[OwnedBot]:
        if refresh or self._owned is None:
            session = await self._connected()
            self._owned = await session.admined_bots()
        return list(self._owned or [])

    #: This path can really delete a bot: @BotFather does it, over the owner's
    #: own session. The managed one cannot — there is no `deleteManagedBot` —
    #: and the difference decides whether the screen shows a button or a recipe.
    deletes_bots = True

    #: And it can empty the chat with one. Bot API cannot: it deletes only what
    #: the bot itself sent, only within forty-eight hours, and never the chat.
    wipes_dialogs = True

    async def account_bot_count(self) -> int | None:
        """The whole account, because this path lists the whole account.

        From the cache: the snapshot has just asked, and two `getAdminedBots`
        in one reading is the same question twice. The fake that stands in for
        this object is already required not to do it.
        """
        return len(await self.list_owned_bots(refresh=False))

    async def get_creation_limit(self) -> BotLimit:
        session = await self._connected()
        premium = await session.is_premium()
        try:
            value = await session.bot_creation_limit()
        except MtprotoError:
            logger.warning("could not read the bot creation limit from Telegram")
            value = None
        return BotLimit(value=value, premium=premium)

    async def check_username(self, username: str) -> UsernameState:
        """Classify a username against what Telegram says, not what we stored."""
        target = username.lower().lstrip("@")
        for bot in await self.list_owned_bots():
            if (bot.username or "").lower() == target:
                return UsernameState.OWNED
        session = await self._connected()
        holder = await session.username_holder(target)
        return UsernameState.FREE if holder is None else UsernameState.FOREIGN

    # ------------------------------------------------------------------ writing

    async def delete_owned_bot(self, username: str) -> None:
        """Delete one bot, having first proved it is ours and named as expected.

        The ownership check is inside the deleting function on purpose. A caller
        that forgets it would delete a stranger's bot — an operation with no undo
        and no apology — so it is not a precondition anybody can skip.
        """
        target = username.lower().lstrip("@")
        state = await self.check_username(target)
        if state is UsernameState.FREE:
            logger.info("@%s is already gone; nothing to delete", target)
            return
        if state is not UsernameState.OWNED:
            raise ForeignUsernameError(target)

        session = await self._connected()
        async with self._lock:
            await session.delete_bot(target)
        self._owned = None
        logger.info("deleted @%s", target)

    async def wait_for_username_release(
        self,
        username: str,
        *,
        attempts: int = USERNAME_RELEASE_ATTEMPTS,
        sleep: Any = None,
    ) -> bool:
        """Poll until Telegram stops resolving the username. False on timeout.

        Backoff rather than a fixed sleep: the usual case is free on the first
        look, and the unusual one is not helped by asking faster.
        """
        pause = sleep or self._sleep
        delay = USERNAME_RELEASE_FIRST_DELAY
        target = username.lower().lstrip("@")
        session = await self._connected()
        for attempt in range(1, attempts + 1):
            if await session.username_holder(target) is None:
                return True
            if attempt == attempts:
                break
            await pause(delay)
            delay = min(delay * 2, USERNAME_RELEASE_MAX_DELAY)
        logger.warning("@%s has not been released yet", target)
        return False

    async def create_bot(self, *, name: str, username: str) -> CreatedBot:
        """Create exactly this bot, or say why it could not be created.

        Paced, and it stops itself. Both are about the same incident: nothing
        here is slow enough to matter to a person, and the run that was not
        paced cost the account sixteen hours.
        """
        target = username.lower().lstrip("@")
        session = await self._connected()

        # An existing bot is reused, never rebuilt. The caller asks for "a
        # usable bot at this username" and a deterministic name that is already
        # ours is the commonest way to get here — a contact the owner
        # disconnected and reconnected, a token lost, a journal entry retired.
        # Walking `/newbot` at it answers "username is taken", which the batch
        # then reports as somebody else's name: a lie, and a dead end.
        #
        # This is what Managed Bots did with `getManagedBotToken`, and it is why
        # a bot never has to be deleted to recover its token.
        existing = await self._token_of_existing(session, target)
        if existing is not None:
            token, bot_id = existing
            return CreatedBot(username=target, token=token, bot_id=bot_id)

        async with self._lock:
            self._refuse_while_quiet()
            await self._pace()
            try:
                created = await session.create_bot(name=name, username=target)
            except UsernameStillTakenError as error:
                raise ForeignUsernameError(target) from error
            except BotFatherTooSoonError as error:
                # Sticky, and for exactly as long as @BotFather asked. The rest
                # of the batch must not find this out one walk at a time, and
                # neither must the retry button.
                wait = float(error.seconds or BOTFATHER_COOLDOWN_SECONDS)
                self._quiet_until = self._now() + wait
                logger.warning(
                    "@BotFather asked to wait %.0f s; not asking again until then", wait
                )
                raise FloodWaitError(str(error), seconds=error.seconds) from error
            except MtprotoError as error:
                raise typed_error(error) from error
            finally:
                # On the failure too: a refused walk is still a conversation
                # @BotFather has just had with us.
                self._last_newbot = self._now()
        self._owned = None
        return CreatedBot(username=created.username, token=created.token)

    async def _token_of_existing(self, session: Any, target: str) -> tuple[str, int | None] | None:
        """The token and id of a bot at this username the account already owns.

        None when there is no such bot — the ordinary case, and it costs one
        cached listing rather than a round trip. The id comes back with the
        token because the caller has to be able to tell a reuse from a creation:
        only a creation supersedes what the bridge row remembers.
        """
        held = {
            (bot.username or "").lower(): bot.bot_id for bot in await self.list_owned_bots()
        }
        if target not in held:
            return None
        recover = getattr(session, "token_of", None)
        if recover is None:
            return None
        logger.info("@%s already exists on this account; taking its token", target)
        try:
            token: str = await recover(target)
        except MtprotoError as error:
            raise typed_error(error) from error
        return token, held[target]

    @staticmethod
    def _now() -> float:
        return asyncio.get_running_loop().time()

    def _refuse_while_quiet(self) -> None:
        """Fail fast while the cooldown runs, rather than asking again."""
        if self._quiet_until is None:
            return
        left = self._quiet_until - self._now()
        if left <= 0:
            self._quiet_until = None
            return
        # A flood wait, never a capacity error: the account has slots, and the
        # difference is whether waiting helps or the owner has to delete a bot.
        raise FloodWaitError(
            f"@BotFather ограничил создание ботов; повторите через {int(left // 60) + 1} мин",
            seconds=int(left) + 1,
        )

    async def _pace(self) -> None:
        """Wait out the minimum interval since the last walk. Usually instant."""
        if self._last_newbot is None:
            return
        wait = NEWBOT_MIN_INTERVAL_SECONDS - (self._now() - self._last_newbot)
        if wait > 0:
            logger.info("pausing %.0fs before the next /newbot", wait)
            await self._sleep(wait)

    async def wipe_dialog(self, bot_id: int, *, keep_dialog: bool = False) -> int:
        """Empty the chat with one of our bots, over the owner's session.

        `keep_dialog` empties it without removing it — which is what a re-pull
        needs, because removing the dialog stops the bot and the import that
        follows would have nowhere to write.

        Not behind the @BotFather lock: this touches a private chat, not the
        one stateful conversation that lock exists to serialise, and it is not
        rate-limited the way `/newbot` is.
        """
        session = await self._connected()
        wipe = getattr(session, "wipe_dialog", None)
        if wipe is None:
            raise ProvisionerError("эта сессия не умеет чистить диалоги")
        try:
            wiped: int = await wipe(bot_id, keep_dialog=keep_dialog)
        except MtprotoError as error:
            raise typed_error(error) from error
        return wiped

    async def delete_messages(self, bot_id: int, message_ids: list[int]) -> int:
        """Remove messages the bridge placed, over the owner's session.

        The forty-eight-hour rule is the bot's, not the owner's. Nothing here is
        bounded by it.
        """
        session = await self._connected()
        remove = getattr(session, "delete_messages", None)
        if remove is None:
            raise ProvisionerError("эта сессия не умеет удалять сообщения")
        try:
            removed: int = await remove(bot_id, message_ids)
        except MtprotoError as error:
            raise typed_error(error) from error
        return removed

    async def send_start(self, username: str, bot_id: int | None = None) -> None:
        session = await self._connected()
        await session.send_start(username.lower().lstrip("@"), bot_id)

    async def close(self) -> None:
        """Drop the user session, if one was ever opened. Never raises."""
        session, self._session = self._session, None
        self._owned = None
        if session is None or not hasattr(session, "close"):
            return
        try:
            await session.close()
        except Exception:
            logger.debug("could not close the user session", exc_info=True)
