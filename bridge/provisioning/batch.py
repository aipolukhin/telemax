"""Building the selected bridges, one contact at a time, survivably.

The shape of this file is a reaction to the obvious implementation. Deleting
every old bot first and then creating the replacements is shorter, reads better,
and is catastrophic: a failure on the third contact has already destroyed the
first two, and the owner is left with no bridges and no way back. So each
contact is walked from start to finish on its own, and a failure stops that
contact and nothing else.

The second thing shaping it is restart. Every step is written to the journal
before the next one begins, and the resume rule is a single sentence: *if the
token was saved and Telegram still says we own the bot, pick up from starting
the worker; otherwise rebuild from scratch.* That covers the two crashes worth
worrying about — after deleting and before creating, and after creating and
before the token was written down — without needing to guess which happened.

Nothing here talks to Telegram or to MAX directly. It moves a journal through
two ports: a `BotProvisioner` for owning bots, and a `BridgeGateway` for the
running process.

**No lock lives here.** One used to, on the batch object, with a comment saying
it stopped a second «Готово» racing the first. It never did: a batch is built
per tap, so each tap took its own lock and contended with nobody. Serialising
one contact against itself is the `ProvisioningCoordinator`'s job, and it is the
only object that outlives a tap.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from .capacity import PeerBotStatus, classify
from .coordinator import ContactKey, ProvisioningCoordinator
from .journal import TERMINAL, ItemState, JournalEntry, ProvisioningJournal
from .owned import OwnerMustOpenChatError
from .provisioner import (
    ProvisionerError,
    ProvisioningFailure,
    UsernameState,
    classify_failure,
)

logger = logging.getLogger(__name__)

#: Sanitised sentences. The owner sees these; the detail goes to the log.
FOREIGN_MESSAGE = "детерминированный username занят другим Telegram-аккаунтом"
#: Not a collision, and saying "somebody else has it" would send the owner to
#: fix something that is not wrong. Measured: a bot created by hand in
#: @BotFather answers `BOT_ACCESS_FORBIDDEN` to `getManagedBotToken` even on the
#: account that owns both it and the guardian.
NOT_MANAGEABLE_MESSAGE = (
    "бот с этим именем есть, но страж им не управляет — он создан вручную. "
    "Удалите его в @BotFather и создайте заново через стража"
)
CREATE_MESSAGE = "Telegram не выдал токен бота"
LIMIT_MESSAGE = "достигнут лимит ботов Telegram-аккаунта"
FLOOD_MESSAGE = "Telegram просит подождать — попробуйте через несколько минут"
CONFIRMATION_MESSAGE = "создание бота не подтверждено"
NETWORK_MESSAGE = "Telegram не ответил"
START_MESSAGE = "бот создан, но чат с ним не открылся"
HEALTH_MESSAGE = "бот создан, но мост ещё не отвечает"

#: What each failure looks like to the owner. One sentence per *cause*, because
#: the cause is what decides what they do next: wait, delete a bot, press a
#: button, or try again. Collapsing these into one message is how somebody with
#: thirty-nine free slots was told they had run out.
FAILURE_MESSAGES = {
    ProvisioningFailure.BOT_CREATE_LIMIT_EXCEEDED: LIMIT_MESSAGE,
    ProvisioningFailure.FLOOD_WAIT: FLOOD_MESSAGE,
    ProvisioningFailure.USERNAME_OCCUPIED: FOREIGN_MESSAGE,
    ProvisioningFailure.BOT_NOT_MANAGEABLE: NOT_MANAGEABLE_MESSAGE,
    ProvisioningFailure.MANAGED_BOT_CONFIRMATION_REQUIRED: CONFIRMATION_MESSAGE,
    ProvisioningFailure.NETWORK_ERROR: NETWORK_MESSAGE,
    ProvisioningFailure.UNKNOWN_PROVISIONING_ERROR: CREATE_MESSAGE,
}


class BridgeConflictError(Exception):
    """This contact cannot be bridged onto this bot. Nothing has been created.

    Raised by the preflight, before any remote effect, because the two shapes it
    catches — one bot on two chats, one chat on two bots — used to be found by
    `UNIQUE(max_chat_id)` *after* a bot had been created and its token written
    down, leaving a resource nothing could use.
    """


class BridgeGateway(Protocol):
    """What the live process can do for one bridge.

    Deliberately narrow: the batch never learns how a token is stored or how a
    worker is polled, which is what keeps it testable without a Telegram at all.
    """

    async def stop_bridge(self, max_chat_id: int) -> None:
        """Stop the worker and its polling loop. Safe when there is none."""

    async def save_token(self, *, max_chat_id: int, username: str, token: str) -> str:
        """Persist the token at 0600 and return the variable name holding it."""

    async def start_worker(
        self,
        *,
        max_chat_id: int,
        username: str,
        token_env: str,
        title: str,
        max_user_id: int | None = None,
    ) -> str:
        """Bring the bridge up on the running process, returning its name.

        `max_user_id` is passed when the caller already knows who is on the other
        end. It is not a shortcut: a dialog created by adding somebody by number
        does not exist in MAX until the first message, so asking the chat who its
        participants are answers nothing, and without this the bot would come up
        nameless and faceless.
        """

    async def is_healthy(self, max_chat_id: int) -> bool:
        """Whether the bridge is actually carrying messages."""

    async def mark_active(self, max_chat_id: int) -> None:
        """Promote the row from `provisioning` to `active`. After the health check.

        The last step, and the only one that makes the bridge something a restart
        will serve. Everything before it is recoverable; this is the statement
        that there is nothing left to recover.
        """


@dataclass(frozen=True, slots=True)
class BatchResult:
    entries: tuple[JournalEntry, ...]

    @property
    def healthy(self) -> tuple[JournalEntry, ...]:
        return tuple(entry for entry in self.entries if entry.finished)

    @property
    def failed(self) -> tuple[JournalEntry, ...]:
        return tuple(entry for entry in self.entries if entry.failed)

    @property
    def complete(self) -> bool:
        """True only when every selected bridge passed its health check."""
        return bool(self.entries) and all(entry.finished for entry in self.entries)


#: Called after every state change, so one message can be edited into a progress
#: display instead of a new one being sent per step.
Progress = Callable[[list[JournalEntry]], Awaitable[None]]


class ProvisioningBatch:
    """Walks a journal of selected contacts to working bridges."""

    def __init__(
        self,
        *,
        provisioner: object,
        gateway: BridgeGateway,
        journal: ProvisioningJournal,
        display_name_of: Callable[[JournalEntry], str],
        progress: Progress | None = None,
        chat_ids: list[int] | None = None,
        coordinator: ProvisioningCoordinator | None = None,
        own_user_id: int | None = None,
        doing: str = "provisioning",
        wipe_existing: bool = True,
    ) -> None:
        self._provisioner = provisioner
        self._gateway = gateway
        self._journal = journal
        self._display_name_of = display_name_of
        self._progress = progress
        # Which contacts this run is about. The journal holds every attempt now
        # that `begin` merges, and a run must not adopt one nobody selected.
        self._chat_ids = list(chat_ids) if chat_ids is not None else None
        # Serialises one contact against itself, across every entry point. Built
        # by the service and shared; a coordinator made here would be a lock
        # nobody else could take, which is exactly what the old one was.
        self._coordinator = coordinator or ProvisioningCoordinator()
        self._own_user_id = own_user_id
        self._doing = doing
        # Whether a bot that already exists gets its chat emptied before it is
        # reused. On by default: inheriting the last bridge's conversation is
        # what strands echoes. The owner can decline it per run.
        self._wipe_existing = wipe_existing

    def _walking(self) -> list[JournalEntry]:
        if self._chat_ids is None:
            return self._journal.entries()
        return self._journal.selected(self._chat_ids)

    def _key(self, entry: JournalEntry) -> ContactKey:
        if entry.max_peer_id is not None:
            return ContactKey.for_user(entry.max_peer_id)
        return ContactKey.for_chat(entry.max_chat_id, own_user_id=self._own_user_id)

    async def run(self) -> BatchResult:
        for entry in self._walking():
            if entry.state in TERMINAL and not await self._went_away(entry):
                # Finished, or failed for a reason retrying cannot change — a
                # username that belongs to somebody else stays theirs.
                continue
            async with self._coordinator.claim(self._key(entry), doing=self._doing) as mine:
                if not mine:
                    # Somebody is already walking this contact. Not a queue: the
                    # result screen reads the journal, so the caller sees the
                    # state of the run that is actually happening.
                    continue
                await self._provision(entry)
            await self._report()
        return BatchResult(entries=tuple(self._walking()))

    async def _went_away(self, entry: JournalEntry) -> bool:
        """Whether an entry that claims to be finished no longer is.

        The journal says what the last run achieved; it cannot say what has
        happened since. A bridge the owner disconnected, or whose bot they
        deleted in @BotFather, leaves an entry reading `healthy` — and a
        `healthy` entry is skipped, so choosing that contact again did nothing
        at all while the result screen offered a link to a bot that was gone.

        Only `HEALTHY` is re-checked. A permanent failure and an abandoned
        attempt are decisions, not observations, and re-opening those on a
        health check would walk into the same wall on every tap.

        Re-opened rather than repaired in place: `_provision` re-reads the
        username from Telegram and does whatever is right — reuse the bot if it
        is still there, make one if it is not.
        """
        if entry.state is not ItemState.HEALTHY:
            return False
        try:
            alive = await self._gateway.is_healthy(entry.max_chat_id)
        except Exception:
            # Cannot tell. The entry stands: re-provisioning a working bridge
            # over a failed health check is worse than leaving it alone.
            logger.debug("could not re-check bridge %s", entry.max_chat_id, exc_info=True)
            return False
        if alive:
            return False
        logger.info(
            "MAX chat %s is no longer bridged; reopening its journal entry",
            entry.max_chat_id,
        )
        self._journal.note(entry.max_chat_id, state=ItemState.PENDING)
        return True

    # ------------------------------------------------------------------ one item

    async def _provision(self, entry: JournalEntry) -> None:
        chat_id = entry.max_chat_id
        username = entry.expected_username

        # Before anything remote. One bot on two chats and one chat on two bots
        # were both found by a UNIQUE violation *after* a bot had been created
        # and its token written down — a resource nothing could use and nothing
        # would clean up.
        conflict = await self._preflight(chat_id, username)
        if conflict is not None:
            self._fail(chat_id, retryable=False, reason=conflict)
            return

        try:
            state = await self._provisioner.check_username(username)  # type: ignore[attr-defined]
        except Exception as error:  # noqa: BLE001 - retryable by definition
            logger.warning("could not check @%s: %s", username, type(error).__name__)
            self._fail_with(chat_id, classify_failure(error))
            return

        if state is UsernameState.FOREIGN:
            # Never deleted, never renamed around: this is somebody else's bot.
            self._fail_with(chat_id, ProvisioningFailure.USERNAME_OCCUPIED)
            return

        if state is UsernameState.UNMANAGEABLE:
            # Our name, our account, and no access. A different problem with a
            # different answer, so it gets a different sentence.
            self._fail_with(chat_id, ProvisioningFailure.BOT_NOT_MANAGEABLE)
            return

        if state is UsernameState.OWNED:
            # Telegram just said the bot is ours. Write down which bot before
            # anything else can fail: from here on the entry names a real
            # resource even if nothing else in this walk works.
            await self._remember_bot_id(entry)

        resuming = entry.token_env is not None and state is UsernameState.OWNED
        if not resuming and not await self._ensure_bot(entry, state):
            return

        current = self._journal.get(chat_id) or entry
        token_env = current.token_env
        if token_env is None:
            self._fail(chat_id, retryable=True, reason="токен не сохранился")
            return

        if not await self._launch(current, token_env):
            return
        if not await self._open_chat(current):
            return
        await self._verify(current)

    async def _preflight(self, max_chat_id: int, username: str) -> str | None:
        """The reason this contact cannot be bridged, or None. Reads only."""
        preflight = getattr(self._gateway, "preflight", None)
        if preflight is None:
            return None
        try:
            await preflight(max_chat_id=max_chat_id, username=username)
        except BridgeConflictError as clash:
            logger.warning("cannot bridge MAX chat %s: %s", max_chat_id, clash)
            return str(clash)
        except Exception:
            # An unreadable database is not a conflict. Let the walk get as far
            # as the step that actually needs it and fail there, retryably.
            logger.debug("could not run the bridge preflight", exc_info=True)
        return None

    async def _prepare(self, username: str) -> None:
        """Register the confirmation before its link is drawn, if the port can.

        Best effort: a provisioner that has no confirmation step (there is one
        in the tests, and there was one in production before Managed Bots) has
        no `prepare`, and the walk is unchanged for it. A failure here is not
        reported — whatever is wrong will be raised properly by `create_bot`
        a moment later, and raising it twice would only make the screen worse.
        """
        prepare = getattr(self._provisioner, "prepare", None)
        if prepare is None:
            return
        try:
            await prepare(username)
        except Exception:
            logger.debug("could not pre-register the confirmation", exc_info=True)

    async def _forget_old_bot(self, max_chat_id: int, *, keeping: int | None) -> None:
        """Unbind the row from the bot it used to have. Best effort, never fatal.

        `keeping` is the bot the walk ended up with. When the row already names
        it, nothing happened worth unbinding — an existing bot was reused, which
        is the common case and must not disturb the row.

        Through the port rather than the repository: the batch does not own the
        database, and a gateway that cannot do this (the tests, the standalone
        shape) simply has a row with nothing stale in it.
        """
        forget = getattr(self._gateway, "forget_bot", None)
        if forget is None:
            return
        try:
            await forget(max_chat_id, keeping=keeping)
        except Exception:
            logger.debug("could not unbind bridge %s from its old bot", max_chat_id, exc_info=True)

    async def _remember_bot_id(self, entry: JournalEntry) -> None:
        """Record which Telegram bot this attempt owns, if the port can say.

        Best effort and never fatal: the id is evidence for a person, not a
        precondition for the next step.
        """
        if entry.telegram_bot_id is not None:
            return
        resolve = getattr(self._provisioner, "bot_id_for", None)
        if resolve is None:
            return
        try:
            bot_id = await resolve(entry.expected_username)
        except Exception:
            logger.debug("could not read the id of @%s", entry.expected_username, exc_info=True)
            return
        if bot_id:
            self._journal.note(entry.max_chat_id, telegram_bot_id=int(bot_id))

    async def _ensure_bot(self, entry: JournalEntry, state: UsernameState) -> bool:
        """Get a usable token for this contact's bot, making it if it is absent.

        The old shape deleted the bot and created it again, because a token it
        had not written down was unrecoverable. With Managed Bots it is a
        method call, so an existing bot is *reused* — which is why there is no
        stop, no delete, and no waiting for a username to come free.
        """
        chat_id = entry.max_chat_id
        username = entry.expected_username

        if state is not UsernameState.OWNED:
            # Telegram will show the owner its own confirmation dialog, and the
            # call below blocks until they answer it. So the screen has to be
            # drawn *now* — reporting after the step would show a button for a
            # question that has already been answered.
            #
            # The order inside this is what a fast tap depends on: the
            # expectation is registered *before* the link reaches the screen.
            # It used to be registered two Bot API round trips after, and an
            # owner who tapped Create inside that window had their confirmation
            # dropped on the floor — followed by a ten-minute wait and a failure,
            # on a bot that by then existed.
            self._journal.note(chat_id, state=ItemState.AWAITING_CONFIRMATION)
            await self._prepare(username)
            await self._report()

        try:
            created = await self._provisioner.create_bot(  # type: ignore[attr-defined]
                name=self._display_name_of(entry), username=username
            )
        except ProvisionerError as error:
            failure = classify_failure(error)
            if failure is ProvisioningFailure.UNKNOWN_PROVISIONING_ERROR:
                # Everything else is a known shape and already says enough.
                logger.warning("could not provision @%s: %s", username, error)
            self._fail_with(chat_id, failure)
            return False
        # A bot was *created*, so whatever the bridge row remembered is a bot
        # that is no longer this bridge's — deleted by hand in @BotFather, most
        # likely, which is how the owner frees a slot. Left in place it is not a
        # stale field but a live refusal: `_expected_bot` reads it, and the new
        # bot's id will not match, so the bridge is rejected as somebody else's.
        await self._forget_old_bot(chat_id, keeping=created.bot_id)
        # The id in the same write as the state: it is what an operator cleaning
        # up an orphan by hand has to work from, and it used to be a field
        # nothing ever filled in. Never written as None over a known one — an
        # unnamed answer is silence, not a correction.
        if created.bot_id is None:
            self._journal.note(chat_id, state=ItemState.BOT_CREATED)
        else:
            self._journal.note(
                chat_id, state=ItemState.BOT_CREATED, telegram_bot_id=int(created.bot_id)
            )

        try:
            token_env = await self._gateway.save_token(
                max_chat_id=chat_id, username=username, token=created.token
            )
        except Exception:
            logger.exception("could not store the token of @%s", username)
            self._fail(chat_id, retryable=True, reason="токен не сохранился")
            return False
        self._journal.note(chat_id, state=ItemState.TOKEN_SAVED, token_env=token_env)

        # A bridge already polling with the previous token has to let go before
        # the new worker starts, or two pollers share one bot.
        await self._gateway.stop_bridge(chat_id)

        if state is UsernameState.OWNED:
            # The bot already existed, so its chat already holds the previous
            # bridge's conversation. Here, and only here: the old worker has let
            # go and the new one has not started, so nothing can land mid-wipe.
            await self._wipe_dialog(entry, created.bot_id)
        return True

    async def _wipe_dialog(self, entry: JournalEntry, bot_id: int | None) -> None:
        """Empty the chat with a bot that is being reused. Never fatal.

        A rebuilt bridge inheriting the last one's conversation is not merely
        untidy. The owner's earlier messages in that chat have no counterpart in
        the new mapping, so every echo of them arrives with nothing to bind to —
        which is exactly where the ten `ambiguous` jobs and the still-open
        `delivery-ambiguous` incident came from. An empty chat has no echoes to
        strand.

        Best effort by design. A bridge that carries messages into a chat with
        some history above them is working; one that refused to come up because
        a cleanup failed is not.
        """
        if not self._wipe_existing or bot_id is None:
            return
        wipe = getattr(self._provisioner, "wipe_dialog", None)
        if wipe is None:
            return
        try:
            await wipe(int(bot_id))
        except Exception as error:  # noqa: BLE001 - a cleanup is never worth the bridge
            logger.warning(
                "could not clear the chat with bot %s before reusing it: %s",
                bot_id,
                type(error).__name__,
            )

    async def _launch(self, entry: JournalEntry, token_env: str) -> bool:
        try:
            bridge_name = await self._gateway.start_worker(
                max_chat_id=entry.max_chat_id,
                username=entry.expected_username,
                token_env=token_env,
                title=entry.title,
                max_user_id=entry.max_peer_id,
            )
        except Exception as error:  # noqa: BLE001 - shown as a retry, not a trace
            logger.warning(
                "the worker for @%s did not start: %s",
                entry.expected_username,
                type(error).__name__,
            )
            self._fail(entry.max_chat_id, retryable=True, reason="мост не запустился")
            return False
        self._journal.note(
            entry.max_chat_id, state=ItemState.WORKER_STARTED, bridge_name=bridge_name
        )
        return True

    async def _open_chat(self, entry: JournalEntry) -> bool:
        """`/start` from the owner's own account: a bot cannot write first.

        Sent after the worker is polling, so the bot answers instead of banking
        an update nobody reads.

        Without a user session there is nobody to press it — Bot API has no way
        to open a conversation — so the owner has to. That is not a failure: the
        bridge is up, it simply cannot speak until the chat exists, and retrying
        would raise the same thing for ever.

        Recorded on the entry rather than announced. A message per bridge saying
        "open this one too" is three messages for three contacts, and the result
        screen already ends in a button per bot — it only had to say why.
        """
        try:
            # By id when the walk has one. The username resolves out of the
            # session's own cache, and a bot rebuilt at a deterministic name is
            # exactly where that cache is wrong.
            await self._provisioner.send_start(  # type: ignore[attr-defined]
                entry.expected_username, entry.telegram_bot_id
            )
        except OwnerMustOpenChatError as ask:
            logger.info("the owner has to open the chat with @%s", ask.username)
            self._journal.note(
                entry.max_chat_id, state=ItemState.START_SENT, needs_open=True
            )
            return True
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "could not send /start to @%s: %s",
                entry.expected_username,
                type(error).__name__,
            )
            self._fail(entry.max_chat_id, retryable=True, reason=START_MESSAGE)
            return False
        self._journal.note(entry.max_chat_id, state=ItemState.START_SENT)
        return True

    async def _verify(self, entry: JournalEntry) -> None:
        try:
            healthy = await self._gateway.is_healthy(entry.max_chat_id)
        except Exception:  # noqa: BLE001
            healthy = False
        if not healthy:
            # The row stays `provisioning`: it exists, it names the bot, and the
            # next start picks it up. What it does not do is claim to be serving.
            self._fail(entry.max_chat_id, retryable=True, reason=HEALTH_MESSAGE)
            return
        try:
            await self._gateway.mark_active(entry.max_chat_id)
        except Exception:  # noqa: BLE001 - the worker runs; the row does not say so
            logger.warning("could not mark bridge for MAX chat %s active", entry.max_chat_id)
            self._fail(entry.max_chat_id, retryable=True, reason=HEALTH_MESSAGE)
            return
        self._journal.note(
            entry.max_chat_id, state=ItemState.HEALTHY, error=None, failure=None
        )

    def _fail(
        self,
        max_chat_id: int,
        *,
        retryable: bool,
        reason: str,
        failure: ProvisioningFailure | None = None,
    ) -> None:
        self._journal.note(
            max_chat_id,
            state=ItemState.FAILED_RETRYABLE if retryable else ItemState.FAILED_PERMANENT,
            error=reason,
            failure=failure.value if failure is not None else None,
        )

    def _fail_with(self, max_chat_id: int, failure: ProvisioningFailure) -> None:
        """Record a classified failure: the code decides retryable, not the caller."""
        self._fail(
            max_chat_id,
            retryable=failure.retryable,
            reason=FAILURE_MESSAGES[failure],
            failure=failure,
        )

    async def _report(self) -> None:
        if self._progress is None:
            return
        try:
            await self._progress(self._walking())
        except Exception:
            # A progress display that fails must not abort provisioning.
            logger.debug("could not draw provisioning progress", exc_info=True)


def status_from(state: UsernameState, *, locally_claimed: bool) -> PeerBotStatus:
    """Re-exported so the picker and the batch classify identically."""
    return classify(state, locally_claimed=locally_claimed)
