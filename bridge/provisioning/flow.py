"""Choosing dialogs and building their bridges, as one conversation.

The guardian router below this is deliberately thin — it turns taps into method
calls and draws what comes back. Everything that has to be *decided* is here,
because all of it is about state that moves between one tap and the next: the
account gains a bot, a contact's username turns out to belong to somebody else,
the limit is not what it was when the list was drawn.

Two decisions are worth naming.

**A username is verified when it is tapped, not when the list is drawn.**
Resolving sixty usernames to render a keyboard is sixty requests for a screen
the owner will spend two seconds on, and it would rate-limit long before it
helped. Ownership is known for free from `getAdminedBots`; the only expensive
question is "does a stranger hold this name", and that one is asked about the
contact actually being selected — and asked again, for all of them, immediately
before anything is created.

**Capacity is rechecked at «Готово».** The list may have been on screen for an
hour. Bots can be created and deleted from a phone in that time, and the first
thing provisioning does is delete a working bot — so the numbers are taken
again, against the same rules, before that happens.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from aiogram.types import InlineKeyboardMarkup

from bridge.storage.models import BridgeState

from . import byphone
from . import selection as ui
from .batch import BridgeGateway, ProvisioningBatch
from .capacity import (
    SOURCE_LOCAL,
    SOURCE_TELEGRAM,
    Capacity,
    PeerBotStatus,
    PeerPlan,
    classify,
)
from .coordinator import ProvisioningCoordinator
from .history import (
    DEFAULT_LIMIT,
    REPULL_LIMIT,
    HistoryImporter,
    HistorySource,
    ImportTarget,
)
from .history import progress_text as history_progress
from .history import summary_markup as history_buttons
from .history import summary_text as history_summary
from .journal import ItemState, JournalEntry, ProvisioningJournal
from .naming_v2 import contact_bot_username_v2
from .picker import DialogOption, DialogPicker
from .provisioner import ProvisionerError, UsernameState

logger = logging.getLogger(__name__)

Screen = tuple[str, InlineKeyboardMarkup | None]

#: Edits the message the flow owns. Returns nothing; a failed edit is logged.
Draw = Callable[[str, InlineKeyboardMarkup | None], Awaitable[None]]

NO_DIALOGS = "В MAX не нашлось личных диалогов."
NOT_RUNNING = "Мост ещё не запущен — сначала подключите MAX."
NO_PROVISIONER = (
    "Автосоздание ботов недоступно: нет пользовательской сессии Telegram."
)
NO_PEER_ID = (
    "MAX не сообщил, кто на той стороне этого диалога.\n\n"
    "Имя бота выводится из id контакта, поэтому подключить его пока нельзя."
)
CAPACITY_CHANGED = (
    "Список ботов Telegram изменился, пока вы выбирали.\n\n"
    "Откройте список заново — ёмкость пересчитана."
)


@dataclass(frozen=True, slots=True)
class BridgeSummary:
    """One line of the «Мосты» screen and of the main menu count."""

    title: str
    username: str
    bridge_name: str
    max_chat_id: int
    #: Which bot, so "open the chat" can point at it by id rather than by name.
    #: A name is answered from the client's own cache; an id is not.
    bot_id: int | None = None
    #: Whether it is carrying messages right now. A disconnected bridge is still
    #: listed — its bot exists, occupies a slot, and is the one worth deleting.
    running: bool = True


class DialogFlow:
    """The dialog picker, provisioning and history import, in one place."""

    def __init__(
        self,
        *,
        picker: DialogPicker,
        provisioner: Any,
        gateway: BridgeGateway,
        journal: ProvisioningJournal,
        bridges: Any,
        telegram_owner_user_id: int | None = None,
        history_source: HistorySource | None = None,
        display_name_of: Callable[[JournalEntry], str] | None = None,
        directory: byphone.ContactDirectory | None = None,
        coordinator: ProvisioningCoordinator | None = None,
    ) -> None:
        # One per service, shared by every entry point. Without it the picker,
        # the by-number path, the deep link, the announcement button and startup
        # reconciliation each guard a lock nobody else can see.
        self._coordinator = coordinator or ProvisioningCoordinator()
        self._picker = picker
        self._directory = directory
        self._provisioner = provisioner
        self._gateway = gateway
        self._journal = journal
        self._bridges = bridges
        # Half of every new contact bot's name. The other half is the contact's
        # MAX id; neither the installation nor any secret is involved, which is
        # what lets a clean machine with the same two accounts work out the same
        # names.
        self._telegram_owner_user_id = telegram_owner_user_id
        self._history_source = history_source
        self._display_name_of = display_name_of or (lambda entry: entry.title)
        self._selection = ui.Selection()
        #: The contacts the most recent walk was about. What the history import
        #: and the result screen are scoped to — the journal is not, because it
        #: keeps every attempt now rather than only the current selection.
        self._last_run: list[int] = []
        # The one snapshot every screen counts bots from. Kept here rather than
        # fetched per screen: two screens asking Telegram separately is exactly
        # how the home screen came to promise free slots the picker refused.
        self._capacity: Capacity | None = None

    @property
    def selection(self) -> ui.Selection:
        return self._selection

    async def capacity_snapshot(self, *, refresh: bool = False) -> Capacity | None:
        """The account's bot capacity, read at most once a minute.

        `refresh=True` for anything that is about to *act* on the number; the
        cached answer is for screens that only display it. None means this
        deployment cannot own bots at all.
        """
        if self._provisioner is None:
            return None
        cached = self._capacity
        if not refresh and cached is not None and cached.is_fresh():
            return cached
        try:
            fresh, _ = await self._read_capacity()
        except Exception:
            logger.debug("could not read the bot capacity", exc_info=True)
            # A stale number with a timestamp beats no number: the picker will
            # take it again before it creates anything.
            return cached
        return fresh

    async def _read_capacity(self) -> tuple[Capacity, set[str]]:
        """Ask Telegram, and remember when.

        The only place in the project that counts bots. Everything else — the
        picker, the home screen, the recheck at «Готово» — goes through here, so
        two screens can disagree about the wording and never about the number.
        """
        owned = await self._provisioner.list_owned_bots()
        limit = await self._provisioner.get_creation_limit()
        usernames = {(bot.username or "").lower() for bot in owned if bot.username}
        # Two different questions. `owned` is what this install can name — the
        # list that decides whether a username is ours to drive. The count is how
        # full the *account* is, which includes bots that have nothing to do with
        # Telemax and occupy the same slots. Only a user session can answer the
        # second; when it cannot, the local list stands in and the screen says so.
        account = await self._account_bot_count()
        self._capacity = Capacity(
            limit=limit,
            owned_bot_count=len(owned) if account is None else account,
            checked_at=time.time(),
            source=SOURCE_LOCAL if account is None else SOURCE_TELEGRAM,
        )
        # Three numbers and where they came from. Logged because "the count is
        # wrong" was reported from a screenshot and could only be answered by
        # reading the code; now the answer is in the journal.
        logger.info(
            "bot capacity: %s of %s (count from %s, %s known to this install)",
            self._capacity.owned_bot_count,
            limit.value,
            self._capacity.source,
            len(owned),
        )
        return self._capacity, usernames

    async def _account_bot_count(self) -> int | None:
        """The account's own total, or None. Never fatal: this draws a caption."""
        ask = getattr(self._provisioner, "account_bot_count", None)
        if ask is None:
            return None
        try:
            count: int | None = await ask()
        except Exception as error:  # noqa: BLE001 - a caption is never worth a crash
            # Info, not debug: this decides whether the number on screen is
            # about the account or about this install, and it went unnoticed
            # once already because the only trace of it was below the log level.
            logger.info("could not count the account's bots: %s", type(error).__name__)
            return None
        return count

    # -------------------------------------------------------------- the list

    async def open(self) -> Screen:
        """Rebuild the list, the capacity and the selection, all at once.

        The manager check comes first and stops here: without it the owner
        would pick contacts, press «Готово», and only then be told about a
        toggle they have to go and flip in another app.
        """
        require = getattr(self._provisioner, "require_manager", None)
        if require is not None:
            try:
                await require()
            except ProvisionerError as error:
                # Escaped: this can carry a sentence Telegram wrote, and the
                # guardian speaks HTML.
                return byphone.refusal(str(error))

        options = await self._picker.options(exclude=set())
        if not options:
            # With a keyboard. An empty picker used to replace the whole
            # control surface with one sentence and nothing to tap.
            return byphone.refusal(NO_DIALOGS)

        capacity = await self._capacity_for(options)
        self._selection.reset(capacity)
        return ui.picker_text(self._selection.priced()), ui.picker_markup(self._selection)

    async def _capacity_for(self, options: list[DialogOption]) -> Capacity:
        capacity, owned_usernames = await self._read_capacity()

        plans: list[PeerPlan] = []
        for option in options:
            record = await self._bridges.by_max_chat(option.max_chat_id)
            username = self._username_of(record, option)
            claimed = record is not None
            if username is None:
                status = PeerBotStatus.NOT_CREATED
            else:
                state = (
                    UsernameState.OWNED
                    if username in owned_usernames
                    else UsernameState.FREE
                )
                status = classify(state, locally_claimed=claimed)
            plans.append(
                PeerPlan(
                    max_chat_id=option.max_chat_id,
                    max_peer_id=option.max_user_id,
                    title=option.title,
                    expected_username=username,
                    status=status,
                    last_activity=option.last_activity,
                )
            )
        return capacity.with_plans(tuple(plans))

    def _username_of(self, record: Any, option: DialogOption) -> str | None:
        """What this contact's bot is called: what it *is*, or what it would be.

        A username already written down wins over anything either scheme
        computes. Bridges made before V2 were named under an HMAC of the
        installation's `naming-secret`, that bot exists under that name, and
        Telegram does not rename bots — so the stored value is the whole truth
        for an existing bridge and the derivation below is only ever about a
        contact who has no bot yet.
        """
        stored = getattr(record, "expected_username", None) if record is not None else None
        if stored:
            return str(stored)
        return self._new_username_for(option.max_user_id)

    def _new_username_for(self, max_user_id: int | None) -> str | None:
        """The V2 name for a contact who has no bot yet, or None if unnameable."""
        if max_user_id is None:
            # Nothing stable to hash. A username derived from the chat id would
            # be a different bot for the same person after a dialog is recreated.
            return None
        if self._telegram_owner_user_id is None:
            # V2 is a function of *both* accounts. Without the owner's Telegram
            # id there is half a formula, and half a formula must not invent the
            # other half — a wrong name here creates a bot at the wrong address.
            logger.warning("no Telegram owner id: cannot name a new contact bot")
            return None
        return contact_bot_username_v2(self._telegram_owner_user_id, max_user_id)

    async def _locally_claimed(self, max_chat_id: int) -> bool:
        record = await self._bridges.by_max_chat(max_chat_id)
        return record is not None

    # --------------------------------------------------------------- tapping

    async def toggle(self, epoch: int, max_chat_id: int) -> tuple[Screen | None, str | None]:
        """Flip one dialog, verifying the username the moment it is chosen."""
        if epoch != self._selection.epoch:
            return None, ui.STALE_ALERT
        if self._selection.locked:
            return None, ui.LOCKED_ALERT

        plan = self._selection.priced().plan_for(max_chat_id)
        if plan is None:
            return None, ui.STALE_ALERT

        if max_chat_id not in self._selection.chosen and plan.expected_username is not None:
            verified = await self._verify(plan)
            if verified is not plan:
                self._replace_plan(verified)
                plan = verified

        changed, alert = self._selection.toggle(max_chat_id)
        if not changed:
            return None, alert
        return (
            ui.picker_text(self._selection.priced()),
            ui.picker_markup(self._selection),
        ), None

    async def _verify(self, plan: PeerPlan) -> PeerPlan:
        """Ask Telegram who actually holds this username. One call, one tap."""
        assert plan.expected_username is not None
        try:
            state = await self._provisioner.check_username(plan.expected_username)
        except Exception:
            logger.debug("could not verify @%s", plan.expected_username, exc_info=True)
            return plan
        claimed = plan.status is PeerBotStatus.LOCAL_STATE_MISMATCH or (
            plan.status is PeerBotStatus.OWNED_REPLACEABLE
        )
        status = classify(state, locally_claimed=claimed)
        return plan if status is plan.status else replace(plan, status=status)

    def _replace_plan(self, plan: PeerPlan) -> None:
        capacity = self._selection.capacity
        if capacity is None:
            return
        self._selection.capacity = capacity.with_plans(
            tuple(
                plan if item.max_chat_id == plan.max_chat_id else item
                for item in capacity.plans
            )
        )

    def note_managed_bot(self, bot_id: int, username: str) -> bool:
        """Pass a confirmed creation through to whatever is waiting for it."""
        handler = getattr(self._provisioner, "on_managed_bot", None)
        if handler is None:
            return False
        return bool(handler(bot_id, username))

    def turn_page(self, epoch: int, page: int) -> tuple[Screen | None, str | None]:
        if epoch != self._selection.epoch:
            return None, ui.STALE_ALERT
        self._selection.page = page
        return (
            ui.picker_text(self._selection.priced()),
            ui.picker_markup(self._selection),
        ), None

    # ---------------------------------------------------------- provisioning

    async def commit(self, epoch: int, draw: Draw) -> str | None:
        """«Готово»: freeze the choice, recheck everything, then build.

        Returns an alert when the batch could not even be started; otherwise it
        has already drawn the result into the message it was given.
        """
        if epoch != self._selection.epoch:
            return ui.STALE_ALERT
        if self._selection.locked:
            return ui.LOCKED_ALERT
        chosen = self._selection.selected_plans()
        if not chosen:
            return ui.NOTHING_SELECTED_ALERT

        self._selection.locked = True
        snapshot = list(chosen)

        recheck = await self._recheck(snapshot)
        if recheck is not None:
            self._selection.locked = False
            return recheck

        entries = [
            JournalEntry(
                max_chat_id=plan.max_chat_id,
                max_peer_id=plan.max_peer_id,
                expected_username=plan.expected_username or "",
                title=plan.title,
                state=ItemState.PENDING,
            )
            for plan in snapshot
            if plan.expected_username
        ]
        self._journal.begin(entries)

        result = await self._build([entry.max_chat_id for entry in entries], draw)
        await self._remember(result.entries)

        # The tick was set before anything was created, so the import is part of
        # this run rather than an errand the owner has to remember afterwards.
        imported: int | None = None
        if self._selection.history:
            imported = await self._pull_history(draw, only_new=False)

        await draw(
            ui.result_text(list(result.entries), imported=imported),
            ui.result_markup(
                list(result.entries),
                epoch=self._selection.epoch,
                history_done=imported is not None,
            ),
        )
        return None

    def toggle_history(self, epoch: int) -> tuple[Screen | None, str | None]:
        """Flip «подтянуть переписку» before the run, not after it."""
        if epoch != self._selection.epoch:
            return None, ui.STALE_ALERT
        if self._selection.locked:
            return None, ui.LOCKED_ALERT
        self._selection.history = not self._selection.history
        return (
            ui.picker_text(self._selection.priced()),
            ui.picker_markup(self._selection),
        ), None

    async def _pull_history(self, draw: Draw, *, only_new: bool) -> int | None:
        """Import into the bridges that just passed their health check.

        Returns how many messages were carried, or None when there was nothing
        to import — the result screen says «подтянуто N» only when it happened.
        """
        reports = await self._import(draw, only_new=only_new)
        if reports is None:
            return None
        return sum(int(getattr(report, "delivered", 0) or 0) for report in reports)

    def _progress_into(self, draw: Draw) -> Any:
        """Redraw the batch's progress, with a confirmation button when it waits."""

        async def progress(current: list[JournalEntry]) -> None:
            await draw(
                ui.progress_text(current),
                ui.progress_markup(current, link_for=self._confirmation_link),
            )

        return progress

    def _confirmation_link(self, entry: JournalEntry) -> str | None:
        """Telegram's own creation dialog, pre-filled, or None when unavailable."""
        builder = getattr(self._provisioner, "confirmation_link", None)
        if builder is None:
            return None
        link: str = builder(entry.expected_username, self._display_name_of(entry))
        return link

    async def _recheck(self, chosen: list[PeerPlan]) -> str | None:
        """Capacity and ownership, taken again against the live account."""
        capacity, owned_usernames = await self._read_capacity()

        fresh: list[PeerPlan] = []
        for plan in chosen:
            if plan.expected_username is None:
                continue
            state = (
                UsernameState.OWNED
                if plan.expected_username in owned_usernames
                else await self._provisioner.check_username(plan.expected_username)
            )
            status = classify(state, locally_claimed=plan.is_replacement)
            fresh.append(replace(plan, status=status))

        needed = sum(plan.slot_cost for plan in fresh)
        free = capacity.with_plans(tuple(fresh)).free_new_slots
        if needed and (free is None or needed > free):
            return CAPACITY_CHANGED
        return None

    async def provision_one(self, max_chat_id: int, draw: Draw) -> str | None:
        """Build one bridge, for the contact who has just written for the first time.

        The same batch as «Готово» with a selection of one: the confirmation
        dialog, the journal, the health check and the resume rule are the same
        code, so the announcement path cannot drift away from the picker's.
        """
        option = next(
            (
                item
                for item in await self._picker.options(exclude=set())
                if item.max_chat_id == max_chat_id
            ),
            None,
        )
        if option is None:
            return NO_DIALOGS
        return await self.provision_option(option, draw)

    async def provision_option(self, option: DialogOption, draw: Draw) -> str | None:
        """Build one bridge for one contact, whether or not MAX lists the dialog.

        Split out from `provision_one` for the by-number path: there the dialog
        is addressable but not yet listed, so there is nothing to look up — the
        option is constructed from the contact the search returned.
        """
        record = await self._bridges.by_max_chat(option.max_chat_id)
        username = self._username_of(record, option)
        if username is None:
            return NO_PEER_ID

        self._journal.begin(
            [
                JournalEntry(
                    max_chat_id=option.max_chat_id,
                    max_peer_id=option.max_user_id,
                    expected_username=username,
                    title=option.title,
                    state=ItemState.PENDING,
                )
            ]
        )
        result = await self._build([option.max_chat_id], draw)
        await self._remember(result.entries)
        await draw(
            ui.result_text(list(result.entries)),
            ui.result_markup(list(result.entries), epoch=self._selection.epoch),
        )
        return None

    # -------------------------------------------------------- adding by number

    @property
    def directory_ready(self) -> bool:
        """Whether this deployment can look a number up at all."""
        return self._directory is not None

    async def resolve_phone(self, raw: str) -> byphone.Resolution:
        """One number in, one verdict out. Reads only — nothing is written.

        Deliberately not "find or create": every branch that cannot end in an
        existing dialog ends in a sentence for the owner instead. The bridge is
        keyed on a MAX chat id, and the only chat id a not-yet-existing dialog
        has is a locally computed guess.
        """
        phone = byphone.normalize(raw)
        if phone is None:
            return byphone.Resolution(verdict=byphone.Verdict.BAD_PHONE)
        directory = self._directory
        if directory is None:
            return byphone.Resolution(verdict=byphone.Verdict.NOT_FOUND, phone=phone)

        contact = await directory.search_by_phone(phone)
        if contact is None:
            return byphone.Resolution(verdict=byphone.Verdict.NOT_FOUND, phone=phone)
        return await self._place(contact, phone)

    async def resolve_card(self, phones: list[str]) -> list[byphone.Resolution]:
        """Every number on a shared card, resolved. Order preserved.

        Sequential on purpose. `search_by_phone` is a MAX lookup, a card holds
        two or three numbers, and firing them at once buys milliseconds against
        a directory that rate-limits — while a person waits for one screen.
        """
        return [await self.resolve_phone(phone) for phone in phones]

    async def resolve_user_id(self, user_id: int) -> byphone.Resolution:
        """One MAX user id in, one verdict out — the by-contactId entry.

        A contact shared into Telegram arrives with the person's MAX user id
        already on it, so there is nothing to search for: the id *is* the answer
        `resolve_phone` spends a lookup to get. From there the path is identical —
        `_place` computes `own ^ id`, checks for an existing bridge, and the same
        confirmation screen decides the rest. Reads only; nothing is written and
        no phone is ever needed.
        """
        directory = self._directory
        if directory is None:
            return byphone.Resolution(verdict=byphone.Verdict.NOT_FOUND)
        contact = await directory.contact_profile(int(user_id))
        if contact is None:
            return byphone.Resolution(verdict=byphone.Verdict.NOT_FOUND)
        # No phone on this path: the card carries none, and `_place` uses it only
        # for display. The screens fall back to the contact's own name.
        return await self._place(contact, phone="")

    async def _place(self, contact: Any, phone: str) -> byphone.Resolution:
        """Where a found person stands: ourselves, bridged, or ready to be.

        "No dialog yet" is not a dead end. A personal MAX chat id is
        `own ^ peer` — the same formula MAX's own web client computes locally —
        so the conversation has an address before it has a message, and the
        bridge can be built on it. What creates the dialog on the server is the
        owner's first message, sent by the owner, from their new bot.
        """
        directory = self._directory
        own = getattr(directory, "own_user_id", None)
        if own is not None and int(contact.user_id) == int(own):
            return byphone.Resolution(
                verdict=byphone.Verdict.SELF, phone=phone, contact=contact
            )

        option = await self._picker.dialog_with(int(contact.user_id))
        if option is None:
            if own is None:
                # Without our own id there is no formula, only half of one.
                return byphone.Resolution(
                    verdict=byphone.Verdict.NOT_FOUND, phone=phone, contact=contact
                )
            chat_id = byphone.dm_chat_id(int(own), int(contact.user_id))
            record = await self._bridges.by_max_chat(chat_id)
            if record is not None:
                return byphone.Resolution(
                    verdict=byphone.Verdict.ALREADY_BRIDGED,
                    phone=phone,
                    contact=contact,
                    max_chat_id=chat_id,
                    bridged_as=record.title or record.bridge_name,
                )
            return byphone.Resolution(
                verdict=byphone.Verdict.NEW_DIALOG,
                phone=phone,
                contact=contact,
                max_chat_id=chat_id,
            )

        record = await self._bridges.by_max_chat(option.max_chat_id)
        if record is not None:
            return byphone.Resolution(
                verdict=byphone.Verdict.ALREADY_BRIDGED,
                phone=phone,
                contact=contact,
                max_chat_id=option.max_chat_id,
                bridged_as=record.title or record.bridge_name,
            )
        return byphone.Resolution(
            verdict=byphone.Verdict.READY,
            phone=phone,
            contact=contact,
            max_chat_id=option.max_chat_id,
        )

    async def import_contact(self, phone: str, name: str) -> byphone.Resolution:
        """The fallback, and the only write: one contact, after an explicit yes.

        Called from exactly one button, which is drawn on exactly one screen,
        which says what MAX is about to be told. The result is re-resolved rather
        than trusted: an import that lands still has to end at a dialog before
        anything can be bridged.
        """
        directory = self._directory
        if directory is None:
            return byphone.Resolution(verdict=byphone.Verdict.NOT_FOUND, phone=phone)
        contact = await directory.import_contact(phone, name)
        if contact is None:
            return byphone.Resolution(verdict=byphone.Verdict.NOT_FOUND, phone=phone)
        logger.info("imported one contact into the owner's MAX account")
        return await self._place(contact, phone)

    async def add_resolved(self, resolution: byphone.Resolution, draw: Draw) -> str | None:
        """Build the bridge for a resolved contact, dialog or no dialog.

        The same batch «Готово» and the announcement path use — the confirmation
        dialog, the journal, the health check and the resume rule are one piece
        of code, so a contact added by number cannot drift onto a creation path
        of its own.
        """
        contact = resolution.contact
        if resolution.max_chat_id is None or contact is None:
            return byphone.STALE_ALERT
        if resolution.verdict is byphone.Verdict.READY:
            return await self.provision_one(resolution.max_chat_id, draw)
        if resolution.verdict is not byphone.Verdict.NEW_DIALOG:
            return byphone.STALE_ALERT
        # A dialog nobody has written in yet is in no chat list, so the option
        # the picker would have handed over has to be built here.
        return await self.provision_option(
            DialogOption(
                max_chat_id=resolution.max_chat_id,
                title=contact.display_name or resolution.phone or "MAX",
                last_activity=0,
                max_user_id=int(contact.user_id),
            ),
            draw,
        )

    async def retry(self, epoch: int, max_chat_id: int, draw: Draw) -> str | None:
        """Run the batch again for one contact that did not finish."""
        entry = self._journal.get(max_chat_id)
        if entry is None:
            return ui.STALE_ALERT
        if entry.state is ItemState.FAILED_PERMANENT:
            return ui.FOREIGN_ALERT
        self._journal.note(max_chat_id, state=ItemState.PENDING, error=None, failure=None)

        result = await self._build([max_chat_id], draw)
        await self._remember(result.entries)
        await draw(
            ui.result_text(list(result.entries)),
            ui.result_markup(list(result.entries), epoch=epoch),
        )
        return None

    async def _build(self, chat_ids: list[int], draw: Draw) -> Any:
        """One walk over exactly these contacts.

        The batch is told which contacts it is about. The journal holds every
        attempt now that `begin` merges, and a run that iterated the whole file
        would adopt an interrupted attempt for somebody the owner did not select.
        """
        self._last_run = list(chat_ids)
        batch = ProvisioningBatch(
            provisioner=self._provisioner,
            gateway=self._gateway,
            journal=self._journal,
            display_name_of=self._display_name_of,
            progress=self._progress_into(draw),
            chat_ids=chat_ids,
            coordinator=self._coordinator,
            own_user_id=self._own_user_id(),
        )
        return await batch.run()

    def _own_user_id(self) -> int | None:
        """The owner's MAX id, so a chat id folds onto the peer it belongs to."""
        own = getattr(self._directory, "own_user_id", None)
        return int(own) if own is not None else None

    @property
    def coordinator(self) -> ProvisioningCoordinator:
        return self._coordinator

    @property
    def gateway(self) -> BridgeGateway:
        """The live process, for start-up reconciliation: the same ports a tap uses."""
        return self._gateway

    @property
    def display_name_of(self) -> Callable[[JournalEntry], str]:
        return self._display_name_of

    async def _remember(self, entries: tuple[JournalEntry, ...]) -> None:
        """Write the deterministic name and the health next to the bridge row."""
        for entry in entries:
            if entry.bridge_name is None:
                continue
            try:
                await self._bridges.set_lifecycle(
                    entry.bridge_name,
                    expected_username=entry.expected_username,
                    lifecycle_state=entry.state.value,
                    health="healthy" if entry.finished else "unhealthy",
                )
            except Exception:
                logger.debug("could not record bridge lifecycle", exc_info=True)

    # -------------------------------------------------------------- history

    def healthy_targets(self) -> list[ImportTarget]:
        """Bridges this run made and that passed their health check.

        Scoped to the run rather than to the journal: `begin` merges now, so the
        file also holds every bridge made months ago, and «подтянуть переписку»
        after one new contact must not re-pull four other conversations.

        Before this object has run anything — a fresh process looking at a
        result screen left over from the last one — the journal is the only
        scope there is, and it is used.
        """
        entries = (
            self._journal.selected(self._last_run)
            if self._last_run
            else self._journal.entries()
        )
        return [
            ImportTarget(
                max_chat_id=entry.max_chat_id,
                title=entry.title,
                bridge_name=entry.bridge_name or "",
            )
            for entry in entries
            if entry.finished and entry.bridge_name
        ]

    async def import_history(self, draw: Draw, *, only_new: bool = False) -> str | None:
        """Only ever offered for bridges that passed their health check."""
        reports = await self._import(draw, only_new=only_new)
        if reports is None:
            return NOT_RUNNING
        # With buttons. This was the guardian's most reliable dead end: the
        # anchor ended on a list of counts and the only way on was `/menu`.
        await draw(history_summary(reports), history_buttons())
        return None

    async def import_one(self, max_chat_id: int, draw: Draw) -> str | None:
        """Pull one dialog from the beginning, whatever the journal says.

        `import_history` works from the provisioning journal, which only knows
        the bridges made by the run that is still on screen. Re-pulling a
        conversation months later has no journal entry to stand on — the bridge
        row does.
        """
        if self._history_source is None:
            return NOT_RUNNING
        summary = await self.summary_of(max_chat_id)
        if summary is None:
            return NOT_RUNNING
        target = ImportTarget(
            max_chat_id=max_chat_id,
            title=summary.title,
            bridge_name=summary.bridge_name,
        )
        # The chat was emptied first, so the tail is not enough: bringing back
        # fifty of the messages that were just removed is a deletion dressed as
        # a refresh.
        reports = await self._run_import(
            [target], draw, only_new=False, limit=REPULL_LIMIT
        )
        await draw(history_summary(reports), history_buttons())
        return None

    async def _import(self, draw: Draw, *, only_new: bool) -> list[Any] | None:
        """The import itself, drawing progress but not the summary.

        Split out because it has two endings: on its own it finishes with a
        summary screen, and inside a provisioning run it finishes with the
        result screen, which mentions the count instead.
        """
        if self._history_source is None:
            return None
        targets = self.healthy_targets()
        if not targets:
            return None
        return await self._run_import(targets, draw, only_new=only_new)

    async def _run_import(
        self,
        targets: list[ImportTarget],
        draw: Draw,
        *,
        only_new: bool,
        limit: int | None = DEFAULT_LIMIT,
    ) -> list[Any]:
        assert self._history_source is not None

        async def progress(reports: list[Any]) -> None:
            await draw(history_progress(reports), None)

        # Passed straight through. It used to be forwarded only when it was not
        # `None` — written when `None` meant "unspecified" and left standing
        # when `None` started meaning *unlimited*, so a re-pull asking for the
        # whole conversation quietly fell back to the fifty-message tail.
        importer = HistoryImporter(
            source=self._history_source,
            cursors=self._bridges,
            progress=progress,
            limit=limit,
        )
        return await importer.run(targets, only_new=only_new)

    async def already_imported(self) -> bool:
        if self._history_source is None:
            return False
        importer = HistoryImporter(source=self._history_source, cursors=self._bridges)
        return await importer.already_imported(self.healthy_targets())

    async def disconnect(self, max_chat_id: int) -> bool:
        """Stop one bridge and mark its row disabled. False when there is none.

        Not a deletion, and the wording in the UI says so. There is no
        `deleteManagedBot`, so the Telegram bot survives whatever we do here —
        keeping the row is what lets the same contact be reconnected to the same
        bot later for no free slot at all.
        """
        record = await self._bridges.by_max_chat(max_chat_id)
        if record is None:
            return False
        await self._gateway.stop_bridge(max_chat_id)
        await self._bridges.set_state(record.bridge_name, BridgeState.DISABLED)
        # The journal records *attempts*, and a disconnected bridge has none
        # standing. Leaving the entry at `healthy` was how a contact the owner
        # had disconnected could not be connected again: the batch skips a
        # terminal entry, so the run did nothing, and the result screen drew a
        # button to the bot — which by then had been deleted in @BotFather, so
        # the tap landed in a chat with a Deleted Account.
        self._journal.forget(max_chat_id)
        logger.info("bridge %s disconnected by the owner", record.bridge_name)
        return True

    @property
    def can_delete_bots(self) -> bool:
        """Whether the bot itself can be deleted from here.

        False on the managed path, and that is not a limitation to work around:
        Bot API has no `deleteManagedBot` at all. The owner's own session can,
        through @BotFather, so the screen offers a button when there is one and
        instructions when there is not — never a button that pretends.
        """
        return getattr(self._provisioner, "deletes_bots", False) is True

    async def delete_bot(self, max_chat_id: int) -> str | None:
        """Delete this bridge's Telegram bot for real. The username, or None.

        Irreversible, and the slot comes back. Everything local that names the
        bot is dropped in the same breath — the journal entry and the row's bot
        id — because a row still pointing at a deleted bot is what makes the
        *next* bridge at the same username fail its identity check.

        The row itself stays: it holds the contact, the deterministic username
        and the history cursor, none of which the bot owned.
        """
        record = await self._bridges.by_max_chat(max_chat_id)
        if record is None or not record.expected_username:
            return None
        username = str(record.expected_username)
        await self._gateway.stop_bridge(max_chat_id)
        await self._provisioner.delete_owned_bot(username)
        self._journal.forget(max_chat_id)
        forget_bot = getattr(self._bridges, "forget_bot", None)
        if forget_bot is not None:
            await forget_bot(record.bridge_name)
        await self._bridges.set_state(record.bridge_name, BridgeState.DISABLED)
        logger.info("bot @%s deleted by the owner; its slot is free", username)
        return username

    @property
    def can_wipe_dialogs(self) -> bool:
        """Whether the chat with a bot can be emptied from here."""
        return getattr(self._provisioner, "wipes_dialogs", False) is True

    async def tear_down(self, max_chat_id: int) -> Any:
        """Remove one bridge entirely: its conversation, its bot, its rows.

        The order is forced — see `provisioning/teardown.py`. Each step is
        written down before it is attempted, and a remote failure stops the walk
        with the local rows intact so a retry can finish rather than strand.

        Returns a `TornDown` describing what happened, or None when there is no
        such bridge. Never partially succeeds silently: `stopped_at` names the
        last step that completed.
        """
        from . import teardown

        record = await self._bridges.by_max_chat(max_chat_id)
        if record is None or not record.expected_username:
            return None
        data_dir = getattr(self._gateway, "data_dir", None)
        outcome = teardown.TornDown(
            bridge_name=record.bridge_name,
            username=str(record.expected_username),
            bot_id=record.telegram_bot_id,
        )

        def note(step: teardown.Step) -> None:
            outcome.stopped_at = step
            if data_dir is not None:
                with contextlib.suppress(Exception):
                    teardown.record(data_dir, record.bridge_name, step)

        note(teardown.Step.ARMED)
        await self._gateway.stop_bridge(max_chat_id)
        note(teardown.Step.WORKER_STOPPED)

        # Remote, and irreversible. Both must succeed before anything local is
        # dropped: a row is the only record that the resource ever existed.
        if outcome.bot_id is not None and self.can_wipe_dialogs:
            await self._provisioner.wipe_dialog(int(outcome.bot_id))
            outcome.dialog_wiped = True
            note(teardown.Step.DIALOG_WIPED)

        await self._provisioner.delete_owned_bot(outcome.username)
        outcome.bot_deleted = True
        note(teardown.Step.BOT_DELETED)

        # Only now. Everything below this line is local and safe to repeat.
        outcome.rows = await teardown.purge_rows(
            database=self._bridges.database,
            bridge_name=record.bridge_name,
            telegram_bot_id=outcome.bot_id,
            max_chat_id=max_chat_id,
        )
        self._journal.forget(max_chat_id)
        if data_dir is not None:
            outcome.floor_dropped = teardown.drop_floor(data_dir, max_chat_id)
        forget_token = getattr(self._gateway, "forget_token", None)
        if forget_token is not None:
            with contextlib.suppress(Exception):
                outcome.token_variable = await forget_token(record.token_env)
        note(teardown.Step.PURGED)

        note(teardown.Step.DONE)
        if data_dir is not None:
            with contextlib.suppress(Exception):
                teardown.forget(data_dir, record.bridge_name)
        logger.warning(
            "bridge %s torn down: dialog=%s bot=%s rows=%s",
            record.bridge_name,
            outcome.dialog_wiped,
            outcome.bot_deleted,
            outcome.row_count,
        )
        return outcome

    async def summaries(self) -> list[BridgeSummary]:
        """Every bridge with a bot, running or not, for the «Мосты» screen.

        Disconnected ones are listed too, and that is the fix rather than the
        clutter: the list was `active()` only, so a bridge the owner had just
        switched off vanished from the menu — taking its card with it, and with
        the card the only way to delete its bot. The bot of a disconnected
        bridge is precisely the one worth deleting, and it was the one that
        could not be reached.
        """
        return [
            summary
            for record in await self._bridges.all()
            if (summary := self._summary(record)) is not None
        ]

    async def summary_of(self, max_chat_id: int) -> BridgeSummary | None:
        """One bridge whatever its state — a disabled row is still a bot.

        `summaries()` lists only what is running; this is for the screen that
        comes *after* disconnecting and still has to name the bot the owner now
        has to delete in @BotFather.
        """
        record = await self._bridges.by_max_chat(max_chat_id)
        return self._summary(record) if record is not None else None

    def _summary(self, record: Any) -> BridgeSummary | None:
        username = record.expected_username
        if not username and record.max_user_id is not None:
            # A row with no name recorded. Under V2 the name is a function of the
            # two accounts, so it can be worked out; under V1 it was a function
            # of a file, and a row from before V2 whose username was never
            # written down cannot be recovered from the ids at all. Both cases
            # are the derivation below returning something or nothing.
            username = self._new_username_for(record.max_user_id)
        if not username:
            return None
        return BridgeSummary(
            title=record.title or record.bridge_name,
            username=username,
            bot_id=record.telegram_bot_id,
            running=record.state is BridgeState.ACTIVE,
            bridge_name=record.bridge_name,
            max_chat_id=record.max_chat_id,
        )
