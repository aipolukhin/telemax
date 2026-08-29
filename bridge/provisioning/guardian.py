"""The guardian bot: the one bot that is not a bridge.

It carries no conversation. It exists to ask the owner questions — "shall I make
a bot for this contact?", "which dialogs?", "pull the old messages in?" — and to
act on the answers.

Two rules shape every handler here:

* **the message the owner is looking at is the message that changes.** A picker
  that answers with a second message leaves two live keyboards in the chat, and
  the older one refers to a world that has moved on. So a tap edits in place,
  and when provisioning finishes the *same* message becomes the list of links.
* **a token pasted into the chat is deleted before it is used.** Whatever
  happens next, it must not stay in the history.

Anything that has to be decided rather than drawn lives in `flow.DialogFlow`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BusinessConnection,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bridge.telegram.design import esc

from . import byphone
from . import selection as ui
from .business import record_business_connection
from .flow import DialogFlow
from .picker import OPEN, DialogPicker
from .service import TOKEN_PATTERN, Provisioner, ProvisioningError

logger = logging.getLogger(__name__)

CREATE = "prov:create"
IGNORE = "prov:ignore"
BLOCK = "prov:block"

#: How much of an old conversation to pull when the owner asks for it. A dialog
#: can be years long; this is "the recent part", not "everything".
HISTORY_LIMIT = 50

BOTFATHER_URL = "https://t.me/BotFather"

#: The fallback for a deployment with no manager bot. Not the normal path any
#: more: Telegram's own creation dialog needs no instructions and no pasting.
INSTRUCTIONS = (
    "1. Откройте @BotFather → /newbot\n"
    "2. Придумайте имя и username — username должен заканчиваться на bot\n"
    "3. Пришлите сюда токен, который он выдаст\n\n"
    "Сообщение с токеном я удалю сразу после проверки."
)

NO_DIALOGS = "В MAX не нашлось личных диалогов, для которых ещё нет моста."

STALE_CALLBACK = ui.STALE_ALERT

#: Said when a callback carries something no handler understands. Not «Не
#: понял кнопку.», which is a developer describing their own parser: what the
#: owner needs to know is that the screen has moved on and where to look now.
UNKNOWN_BUTTON = "Эта кнопка больше не работает — откройте «Мосты» или /menu."

#: The answer to a button belonging to a question that has already been answered.
STALE_DECISION = "Этот вопрос уже закрыт — откройте «Мосты» или /dialogs."

HISTORY_PROMPT = (
    "Подтянуть сюда предыдущую переписку из MAX? "
    f"Возьму последние {HISTORY_LIMIT} сообщений."
)

ALREADY_IMPORTED_MARKUP = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Импортировать только новые", callback_data=ui.HISTORY_NEW),
            InlineKeyboardButton(text="Закрыть", callback_data=ui.HISTORY_NO),
        ]
    ]
)


def announce_text(display_name: str, buffered: int) -> str:
    tail = f" Уже накопилось сообщений: {buffered}." if buffered else ""
    return (
        f"Вам впервые написал <b>{esc(display_name)}</b> в MAX. "
        f"Сделать бота для чата с ним?{esc(tail)}"
    )


def announce_markup(max_chat_id: int, revision: int = 0) -> InlineKeyboardMarkup:
    """The three answers, each carrying the revision of the question they answer.

    «Создать» is the one button in the whole guardian that starts an irreversible
    remote effect and used to carry nothing but a chat id — so a second tap
    started a second walk, and a button from a question answered days ago still
    worked. The revision is the contact's `asked_at`: durable, per contact, and
    spent by the first tap that uses it.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Создать", callback_data=f"{CREATE}:{max_chat_id}:{revision}"
                ),
                InlineKeyboardButton(text="Игнорировать", callback_data=f"{IGNORE}:{max_chat_id}"),
                InlineKeyboardButton(text="Заблокировать", callback_data=f"{BLOCK}:{max_chat_id}"),
            ]
        ]
    )


def parse_decision(data: str) -> tuple[str, int, int | None] | None:
    """`prov:create:<chat>:<revision>` or `prov:ignore:<chat>` to their parts.

    ASCII digits only. `str.isdigit()` is true of `²`, which `int()` then
    refuses — and a `ValueError` raised inside a callback handler is a tap that
    does nothing and says nothing.
    """
    parts = (data or "").split(":")
    if len(parts) < 3 or parts[0] != "prov":
        return None
    action = parts[1]
    chat = parts[2]
    if not _is_number(chat):
        return None
    revision: int | None = None
    if len(parts) > 3:
        if not _is_number(parts[3]):
            return None
        revision = int(parts[3])
    return action, int(chat), revision


def _is_number(text: str) -> bool:
    return byphone.ascii_digits(text, signed=True)


#: Puts a screen on the chat. The anchor's renderer has this shape, and so does
#: the per-message fallback, so nothing below has to know which it got.
Draw = Callable[[str, "InlineKeyboardMarkup | None"], Awaitable[None]]


@dataclass
class GuardianContext:
    """What the guardian router acts on *right now*.

    The bridge worker is rebuilt on every restart, and a `Dispatcher` only
    accumulates routers — registering the handlers a second time would deliver
    every update twice. So the handlers are registered once against this object
    and the objects inside it are swapped instead.
    """

    provisioner: Provisioner | None = None
    picker: DialogPicker | None = None
    flow: DialogFlow | None = None
    #: Draws into the guardian's anchor message. Set by the runtime; without it
    #: the handlers fall back to editing whatever message the tap arrived on,
    #: which is what the picker used to do — and why it ended up as a second
    #: keyboard beside the one the owner was already looking at.
    draw: Draw | None = None
    #: The running worker's delivery queue, and the bridges it serves. Same
    #: reason again: `/failed` and `/retry` are registered once on the guardian's
    #: dispatcher, and what they act on is replaced with every restart.
    outbox: Any = None
    bridge_names: tuple[str, ...] = ()
    #: The provisioning journal and the one thing that can push an attempt on.
    #: Registered here for the same reason as the queue: `/attempts` lives on the
    #: guardian's dispatcher and the worker under it is rebuilt on every restart.
    journal: Any = None
    resume_attempt: Any = None
    #: Whether a given bot id is the guardian. The same router is registered on
    #: the worker's dispatcher in deployments without a guardian context, where a
    #: contact card or a phone number belongs to the *contact*, not to setup —
    #: so the handlers that read those ask this before touching anything.
    is_guardian: Callable[[int], bool] | None = None


NOT_RUNNING = "Мост ещё не запущен — сначала подключите MAX."
NO_AUTOMATION = (
    "Автосоздание ботов недоступно: нет пользовательской сессии Telegram."
)


def _drawer(message: Message) -> Draw:
    """Edit this message, whatever happens. A failed edit is not a failed run."""

    async def draw(text: str, markup: InlineKeyboardMarkup | None) -> None:
        try:
            await message.edit_text(text, reply_markup=markup)
        except Exception:
            logger.debug("could not edit the provisioning message", exc_info=True)

    return draw


def _replier(message: Message) -> Draw:
    """Post beside this message. Only ever the anchorless fallback for a command."""

    async def draw(text: str, markup: InlineKeyboardMarkup | None) -> None:
        await message.answer(text, reply_markup=markup)

    return draw


async def _nowhere(_text: str, _markup: InlineKeyboardMarkup | None) -> None:
    logger.debug("no anchor and nothing to edit: the screen was dropped")


def build_guardian_router(
    source: Provisioner | GuardianContext,
    *,
    picker: DialogPicker | None = None,
    flow: DialogFlow | None = None,
) -> Router:
    context = (
        source
        if isinstance(source, GuardianContext)
        else GuardianContext(provisioner=source, picker=picker, flow=flow)
    )
    router = Router(name="guardian")

    # Which contact the owner is currently setting up by hand. One at a time is
    # enough for a personal bridge, and it keeps the token message unambiguous.
    awaiting: dict[int, int] = {}

    def _anchor_or(carrier: Any) -> Draw:
        """The anchor if the runtime gave one, else the message the tap came from.

        The fallback is not a nicety — a router built without a board, which is
        every test that only cares about the picker, still has to draw somewhere.
        """
        if context.draw is not None:
            return context.draw
        return _drawer(carrier) if isinstance(carrier, Message) else _nowhere

    async def _open(draw: Draw) -> None:
        flow_now = context.flow
        if flow_now is None:
            await draw(
                *byphone.refusal(
                    NOT_RUNNING if context.provisioner is None else NO_AUTOMATION
                )
            )
            return
        text, markup = await flow_now.open()
        await draw(text, markup)

    @router.message(Command("dialogs"))
    async def _dialogs(message: Message) -> None:
        """Offer the MAX dialogs, newest first, with the capacity above them."""
        # Answered in the anchor, so the command itself is litter. Deleted before
        # the list is drawn: a list that takes a second to build should not leave
        # the command sitting visible underneath it in the meantime.
        await _forget(message)
        # A command has no message of the bot's own to edit, so without an anchor
        # the only place left is a new one.
        await _open(context.draw or _replier(message))

    @router.message(Command("add"))
    async def _add(message: Message) -> None:
        """The published way in. Three doors behind it, of which the MAX dialog
        picker is only one — and the least useful for somebody whose relative
        has no MAX dialogs to pick from yet."""
        await _forget(message)
        draft.restart()
        await (context.draw or _replier(message))(*byphone.menu(dialogs=OPEN))

    @router.callback_query(F.data == OPEN)
    async def _open_from_button(query: CallbackQuery) -> None:
        await query.answer()
        await _open(_anchor_or(query.message))

    # ------------------------------------------------------------- selecting

    @router.callback_query(F.data.startswith(f"{ui.SELECT}:"))
    async def _toggle(query: CallbackQuery) -> None:
        parsed = ui.parse_callback(query.data or "", ui.SELECT)
        flow_now = context.flow
        if parsed is None or flow_now is None:
            await query.answer(STALE_CALLBACK, show_alert=True)
            return
        epoch, max_chat_id = parsed
        screen, alert = await flow_now.toggle(epoch, max_chat_id)
        await query.answer(alert or "", show_alert=bool(alert))
        if screen is not None:
            await _anchor_or(query.message)(*screen)

    @router.callback_query(F.data.startswith(f"{ui.PAGE}:"))
    async def _turn_page(query: CallbackQuery) -> None:
        parsed = ui.parse_callback(query.data or "", ui.PAGE)
        flow_now = context.flow
        if parsed is None or flow_now is None:
            await query.answer(STALE_CALLBACK, show_alert=True)
            return
        epoch, page = parsed
        screen, alert = flow_now.turn_page(epoch, page)
        await query.answer(alert or "", show_alert=bool(alert))
        if screen is not None:
            await _anchor_or(query.message)(*screen)

    @router.callback_query(F.data.startswith(f"{ui.HISTORY_PICK}:"))
    async def _pick_history(query: CallbackQuery) -> None:
        """The tick that decides whether this run also pulls the old messages."""
        epoch = ui.parse_epoch(query.data or "", ui.HISTORY_PICK)
        flow_now = context.flow
        if epoch is None or flow_now is None:
            await query.answer(STALE_CALLBACK, show_alert=True)
            return
        screen, alert = flow_now.toggle_history(epoch)
        await query.answer(alert or "", show_alert=bool(alert))
        if screen is not None:
            await _anchor_or(query.message)(*screen)

    @router.callback_query(F.data.startswith(f"{ui.GO}:"))
    async def _commit(query: CallbackQuery) -> None:
        epoch = ui.parse_epoch(query.data or "", ui.GO)
        flow_now = context.flow
        if epoch is None or flow_now is None:
            await query.answer(STALE_CALLBACK, show_alert=True)
            return
        await query.answer("Создаю…")
        alert = await flow_now.commit(epoch, _anchor_or(query.message))
        if alert:
            await query.answer(alert, show_alert=True)

    @router.callback_query(F.data.startswith(f"{ui.RETRY}:"))
    async def _retry(query: CallbackQuery) -> None:
        parsed = ui.parse_callback(query.data or "", ui.RETRY)
        flow_now = context.flow
        if parsed is None or flow_now is None:
            await query.answer(STALE_CALLBACK, show_alert=True)
            return
        epoch, max_chat_id = parsed
        await query.answer("Повторяю…")
        alert = await flow_now.retry(epoch, max_chat_id, _anchor_or(query.message))
        if alert:
            await query.answer(alert, show_alert=True)

    # --------------------------------------------------------------- history

    @router.callback_query(F.data.in_({ui.HISTORY_ALL, ui.HISTORY_NEW}))
    async def _history(query: CallbackQuery) -> None:
        """The old conversation. Never automatic, and only for live bridges."""
        flow_now = context.flow
        if flow_now is None:
            await query.answer(NOT_RUNNING, show_alert=True)
            return

        draw = _anchor_or(query.message)
        only_new = query.data == ui.HISTORY_NEW
        if not only_new and await flow_now.already_imported():
            await query.answer()
            # In the anchor, not beside it: "already imported" is a screen with
            # two buttons, and it replaces whatever asked the question.
            await draw("История уже импортирована.", ALREADY_IMPORTED_MARKUP)
            return

        await query.answer("Тяну историю…")
        alert = await flow_now.import_history(draw, only_new=only_new)
        if alert:
            await query.answer(alert, show_alert=True)

    @router.callback_query(F.data == ui.HISTORY_NO)
    async def _no_history(query: CallbackQuery) -> None:
        """«Закрыть» / «Не сейчас». It used to answer a toast and change nothing.

        A button labelled «Закрыть» that leaves the screen exactly as it was is
        a button that looks broken, and the owner's next move is to press it
        again. It closes now — the anchor goes back to the bridge list.
        """
        await query.answer("Хорошо, начинаем с чистого листа.")
        await _anchor_or(query.message)(*ui.history_declined())

    # ------------------------------------------------------- managed bots

    @router.managed_bot()
    async def _managed_bot(update: Any) -> None:
        """The owner pressed Create in Telegram's own dialog.

        Telegram tells the *manager* bot directly, so this is the only place
        provisioning learns that a contact bot now exists. Handing it to the
        provisioner resolves whatever is waiting on it; an update nobody is
        waiting for is not an error — the owner may create a bot from
        Telegram's own interface whenever they like.
        """
        flow_now = context.flow
        created = getattr(update, "bot_user", None)
        if flow_now is None or created is None:
            return
        bot_id = int(getattr(created, "id", 0) or 0)
        username = str(getattr(created, "username", "") or "")
        if not bot_id or not username:
            logger.warning("a managed_bot update arrived without a usable bot")
            return
        logger.info("owner confirmed @%s (%s)", username, bot_id)
        flow_now.note_managed_bot(bot_id, username)

    # ------------------------------------------------- adding by number

    # The contact being added right now. One at a time, in memory, and gone the
    # moment the screen it belongs to is answered — a phone number is not
    # something to keep because it might be useful later.
    draft = byphone.ContactDraft()

    def _for_guardian(_message: Message, bot: Any = None) -> bool:
        """Whether this update reached the bot that runs setup.

        Without an answer, treat it as the guardian's: the deployments that pass
        no checker register this router on a dispatcher the guardian alone polls.
        """
        checker = context.is_guardian
        if checker is None:
            return True
        return checker(int(getattr(bot, "id", 0) or 0))

    def _expects(kind: byphone.Awaiting) -> Callable[..., bool]:
        """A filter, not a check inside the handler.

        In aiogram a handler that runs stops the routing, so an early `return`
        here would swallow a contact card meant for a person and a message meant
        for onboarding's own question.
        """

        def check(message: Message, bot: Any = None) -> bool:
            return draft.awaiting is kind and _for_guardian(message, bot)

        return check

    async def _draw_resolution(draw: Draw) -> None:
        resolution = draft.resolution
        if resolution is None:
            return
        await draw(*byphone.screen_for(resolution, epoch=draft.epoch, name=draft.name))

    async def _resolve(carrier: Message, *, raw: str, name: str | None) -> None:
        """A number arrived, from a typed line or from an attached contact."""
        flow_now = context.flow
        draw = context.draw or _replier(carrier)
        # The number is the owner's message, and it is about somebody else. It has
        # served its purpose the moment it is read.
        await _forget(carrier)
        if flow_now is None:
            await draw(*byphone.refusal(NOT_RUNNING))
            return
        if not flow_now.directory_ready:
            await draw(*byphone.refusal(byphone.NO_DIRECTORY))
            return

        resolution = await flow_now.resolve_phone(raw)
        draft.phone = resolution.phone
        draft.name = name
        draft.resolution = resolution
        # A number nobody has, typed by hand, has no name to import under yet.
        draft.awaiting = (
            byphone.Awaiting.NAME
            if resolution.verdict is byphone.Verdict.NOT_FOUND and name is None
            else byphone.Awaiting.NOTHING
        )
        await _draw_resolution(draw)

    @router.message(CommandStart(deep_link=True))
    async def _bridge_from_deep_link(message: Message, command: CommandObject) -> None:
        """`/start mb<id>`: the "поднять мост" button on a shared contact.

        A contact shared from MAX into Telegram carries the person's MAX user id.
        The card under it links here, so the whole flow stays in the guardian —
        no logic in the contact bot, no sidecar. Everything after the resolve is
        the by-phone path exactly: the same confirmation screen, the same
        `_confirm_contact` button, because a MAX user id and a searched one land
        in the same `Resolution`.

        A start payload this handler does not recognise (onboarding's own token,
        a hand-typed word) is left alone — the filter matched a deep link, but a
        non-bridge one belongs to whatever else reads `/start`.
        """
        user_id = byphone.parse_bridge_deep_link(command.args or "")
        if user_id is None:
            return
        flow_now = context.flow
        draw = context.draw or _replier(message)
        await _forget(message)
        if flow_now is None:
            await draw(*byphone.refusal(NOT_RUNNING))
            return
        if not flow_now.directory_ready:
            await draw(*byphone.refusal(byphone.NO_DIRECTORY))
            return
        resolution = await flow_now.resolve_user_id(user_id)
        # A fresh epoch, so a stale card's button cannot confirm this one and
        # this card's button cannot be replayed against a later draft.
        draft.restart(awaiting=byphone.Awaiting.NOTHING)
        draft.resolution = resolution
        await _draw_resolution(draw)

    @router.callback_query(F.data == byphone.ADD)
    async def _add_menu(query: CallbackQuery) -> None:
        draft.clear()
        await query.answer()
        await _anchor_or(query.message)(*byphone.menu(dialogs=OPEN))

    @router.callback_query(F.data == byphone.BY_PHONE)
    async def _ask_phone(query: CallbackQuery) -> None:
        flow_now = context.flow
        if flow_now is None:
            await query.answer(NOT_RUNNING, show_alert=True)
            return
        if not flow_now.directory_ready:
            await query.answer(byphone.NO_DIRECTORY, show_alert=True)
            return
        draft.restart(awaiting=byphone.Awaiting.PHONE)
        await query.answer()
        await _anchor_or(query.message)(*byphone.ask_phone())

    @router.callback_query(F.data == byphone.BY_CONTACT)
    async def _ask_contact(query: CallbackQuery) -> None:
        flow_now = context.flow
        if flow_now is None:
            await query.answer(NOT_RUNNING, show_alert=True)
            return
        if not flow_now.directory_ready:
            await query.answer(byphone.NO_DIRECTORY, show_alert=True)
            return
        draft.restart(awaiting=byphone.Awaiting.PHONE)
        await query.answer()
        await _anchor_or(query.message)(*byphone.ask_contact())

    @router.callback_query(F.data.startswith(f"{byphone.CONFIRM}:"))
    async def _confirm_contact(query: CallbackQuery) -> None:
        """The one button that creates anything on this path."""
        epoch = byphone.parse_epoch(query.data or "", byphone.CONFIRM)
        flow_now = context.flow
        resolution = draft.resolution
        if epoch is None or flow_now is None or resolution is None or not draft.holds(epoch):
            await query.answer(byphone.STALE_ALERT, show_alert=True)
            return
        # Cleared before the work, not after: provisioning takes seconds, and a
        # second tap in that window must not start a second run.
        draft.clear()
        await query.answer("Делаю бота…")
        alert = await flow_now.add_resolved(resolution, _anchor_or(query.message))
        if alert:
            await query.answer(alert, show_alert=True)

    @router.callback_query(F.data.startswith(f"{byphone.IMPORT}:"))
    async def _import_contact(query: CallbackQuery) -> None:
        """The separate, explicit yes to writing one contact into MAX."""
        epoch = byphone.parse_epoch(query.data or "", byphone.IMPORT)
        flow_now = context.flow
        phone, name = draft.phone, draft.name
        if (
            epoch is None
            or flow_now is None
            or phone is None
            or name is None
            or not draft.holds(epoch)
        ):
            await query.answer(byphone.STALE_ALERT, show_alert=True)
            return
        await query.answer("Импортирую один контакт…")
        resolution = await flow_now.import_contact(phone, name)
        draft.resolution = resolution
        draft.awaiting = byphone.Awaiting.NOTHING
        draw = _anchor_or(query.message)
        if resolution.verdict is byphone.Verdict.NOT_FOUND:
            # Offering the same import again would read as "press it harder".
            draft.clear()
            await draw(*byphone.import_failed(phone, name))
            return
        await _draw_resolution(draw)

    @router.callback_query(F.data.startswith(f"{byphone.PICK}:"))
    async def _pick_candidate(query: CallbackQuery) -> None:
        """One row of the picker. Carries a position, never a number."""
        parsed = byphone.parse_pick(query.data or "")
        if parsed is None or parsed[0] != draft.epoch:
            await query.answer(STALE_CALLBACK, show_alert=True)
            return
        await query.answer()
        if draft.choose(parsed[1]) is None:
            await _anchor_or(query.message)(*byphone.menu(dialogs=OPEN))
            return
        await _draw_resolution(_anchor_or(query.message))

    # ----------------------------------------------------- one new contact

    @router.callback_query(F.data.startswith("prov:"))
    async def _decide(query: CallbackQuery) -> None:
        """The announcement's three buttons — and the last stop in `prov:`.

        It is a parent of every other prefix in this namespace, so registration
        order is the only thing keeping it from swallowing them. It swallowed
        `prov:add:pick:` for months: `parse_decision` cannot read `"pick"` as a
        chat id, so every row of the «Несколько номеров» screen answered «Не
        понял кнопку.» and did nothing at all. Two guards now, because the
        failure is invisible — a tap that does nothing looks exactly like a tap
        nobody registered — and `tests/test_callback_prefixes.py` walks this
        file as well as the onboarding router.
        """
        data = query.data or ""
        if any(
            data.startswith(f"{child}:")
            for child in (byphone.CONFIRM, byphone.IMPORT, byphone.PICK)
        ):
            return
        parsed = parse_decision(data)
        if parsed is None:
            await query.answer(UNKNOWN_BUTTON)
            return
        action, max_chat_id, revision = parsed

        owner_id = query.from_user.id

        if action == "create":
            provisioner = context.provisioner
            # Spent before any remote work, and spent atomically: two taps in the
            # same second must not become two walks, and a button from a question
            # already answered must not work at all — including after a restart,
            # which is why the nonce lives in the database and not in memory.
            if provisioner is not None and revision is not None:
                if not await provisioner.consume_announcement(max_chat_id, revision):
                    await query.answer(STALE_DECISION, show_alert=True)
                    return

            flow_now = context.flow
            draw = _anchor_or(query.message)
            if flow_now is None:
                # No manager bot in this process: the owner can still make one
                # by hand and paste the token back.
                awaiting[owner_id] = max_chat_id
                await query.answer()
                await draw(
                    f"Создайте бота и пришлите токен.\n\n{INSTRUCTIONS}\n{BOTFATHER_URL}",
                    None,
                )
                return
            await query.answer("Делаю бота…")
            alert = await flow_now.provision_one(max_chat_id, draw)
            if alert:
                await query.answer(alert, show_alert=True)
            return

        provisioner = context.provisioner
        if provisioner is None:
            await query.answer(NOT_RUNNING, show_alert=True)
            return

        block = action == "block"
        await provisioner.ignore(max_chat_id, block=block)
        awaiting.pop(owner_id, None)
        await query.answer("Заблокирован." if block else "Больше не спрашиваю.")

    @router.message(F.text.regexp(TOKEN_PATTERN.pattern))
    async def _token(message: Message) -> None:
        owner_id = message.from_user.id if message.from_user else 0
        max_chat_id = awaiting.get(owner_id)

        # Delete first, ask questions later: whatever happens next, the token
        # must not stay in the chat history.
        await _delete(message)
        # The answer goes where every other answer goes. The message it replied
        # to has just been deleted anyway.
        draw = context.draw or _replier(message)

        if max_chat_id is None:
            await draw(
                *byphone.refusal("Сначала нажмите «Создать» под сообщением о контакте.")
            )
            return

        provisioner = context.provisioner
        if provisioner is None:
            await draw(*byphone.refusal(NOT_RUNNING))
            return

        try:
            bridge_name = await provisioner.activate(
                max_chat_id=max_chat_id, token=message.text or ""
            )
        except ProvisioningError as error:
            await draw(*byphone.refusal(str(error)))
            return

        awaiting.pop(owner_id, None)
        await draw(
            f"Готово: мост «{esc(bridge_name)}» поднят, "
            "накопленные сообщения отправлены в новый чат.",
            None,
        )

    # Registered after the token handler on purpose: a pasted token is a secret
    # and must be consumed by the handler that deletes it, whatever the guardian
    # happens to be waiting for.
    @router.message(F.contact, _expects(byphone.Awaiting.PHONE))
    async def _attached_contact(message: Message) -> None:
        """A contact card: the name and *every* number, without either being typed.

        Telegram's structured `phone_number` holds one; a person routinely has
        three, and the rest are in the vCard. Reading only the first is how a
        contact who *is* in MAX under their work number was reported as not
        being there at all.
        """
        card = message.contact
        parts = [getattr(card, "first_name", "") or "", getattr(card, "last_name", "") or ""]
        name = " ".join(part for part in parts if part).strip() or None
        numbers = byphone.phones_from_card(
            getattr(card, "phone_number", None), getattr(card, "vcard", None)
        )
        if len(numbers) <= 1:
            await _resolve(message, raw=numbers[0] if numbers else "", name=name)
            return
        await _resolve_card(message, numbers, name=name)

    async def _resolve_card(carrier: Message, numbers: list[str], *, name: str | None) -> None:
        """Several numbers on one card: search them all, then ask if it matters.

        Only a card that reaches *more than one* person costs the owner a tap.
        One hit behaves exactly as a single number always did, and no hits ends
        on the same «не нашёл» screen — with the first number, because that is
        the one Telegram calls primary and the one worth retyping.
        """
        flow_now = context.flow
        draw = context.draw or _replier(carrier)
        await _forget(carrier)
        if flow_now is None:
            await draw(*byphone.refusal(NOT_RUNNING))
            return
        if not flow_now.directory_ready:
            await draw(*byphone.refusal(byphone.NO_DIRECTORY))
            return

        resolutions = await flow_now.resolve_card(numbers)
        reachable = [
            item for item in resolutions if item.verdict is not byphone.Verdict.NOT_FOUND
        ]
        if len(reachable) > 1:
            draft.candidates = reachable
            draft.name = name
            draft.awaiting = byphone.Awaiting.NOTHING
            await draw(*byphone.several(reachable, epoch=draft.epoch))
            return

        single = reachable[0] if reachable else resolutions[0]
        draft.phone = single.phone
        draft.name = name
        draft.resolution = single
        draft.awaiting = (
            byphone.Awaiting.NAME
            if single.verdict is byphone.Verdict.NOT_FOUND and name is None
            else byphone.Awaiting.NOTHING
        )
        await _draw_resolution(draw)

    @router.message(F.text, _expects(byphone.Awaiting.PHONE))
    async def _typed_number(message: Message) -> None:
        await _resolve(message, raw=message.text or "", name=None)

    @router.message(F.text, _expects(byphone.Awaiting.NAME))
    async def _typed_name(message: Message) -> None:
        """The name the single import will carry, asked only after a miss."""
        name = (message.text or "").strip()
        draw = context.draw or _replier(message)
        await _forget(message)
        if not name or draft.phone is None or draft.resolution is None:
            draft.clear()
            await draw(*byphone.menu(dialogs=OPEN))
            return
        draft.name = name
        draft.awaiting = byphone.Awaiting.NOTHING
        await _draw_resolution(draw)

    @router.business_connection()
    async def business_connected(connection: BusinessConnection) -> None:
        """The owner connected — or disconnected — the guardian as a secretary.

        Recorded rather than acted on: writing as the owner is only useful for the
        history import, and whether that is possible at all depends on answers
        Telegram gives to a live attempt, not to a reading of the docs (see
        HANDOFF, «Режим секретаря»). The connection id is what any such attempt
        needs, and it arrives exactly once — losing it means asking the owner to
        reconnect.

        The update only arrives because `business_connection` is named in
        `DEFAULT_ALLOWED_UPDATES`; without that the handler would never run and
        the log would be clean.
        """
        rights = connection.rights
        record_business_connection(
            connection_id=connection.id,
            user_chat_id=connection.user_chat_id,
            enabled=connection.is_enabled,
            can_reply=bool(getattr(rights, "can_reply", None) or connection.can_reply),
            can_read=bool(getattr(rights, "can_read_messages", None)),
            is_premium=connection.user.is_premium,
        )
        logger.info(
            "business connection %s: enabled=%s can_reply=%s can_read_messages=%s premium=%s",
            connection.id,
            connection.is_enabled,
            getattr(rights, "can_reply", None),
            getattr(rights, "can_read_messages", None),
            connection.user.is_premium,
        )

    return router


async def _delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 - any failure here means the same thing
        # Deletion can fail (too old, no rights). Say so rather than pretend.
        logger.warning("could not delete a message containing a token")
        await message.answer("Не смог удалить сообщение с токеном — удалите его вручную.")


async def _forget(message: Message) -> None:
    """Remove a command the owner typed. Best effort, and never announced."""
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 - too old, no rights, already gone
        logger.debug("could not delete a command message")
