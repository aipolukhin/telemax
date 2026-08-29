"""The guardian's handlers: the deep link, the MAX conversation, and control.

Everything the owner does after leaving the terminal enters here. Four rules
shape the file:

* **the anchor is the only screen.** Nothing here sends a message. Every command
  and every button redraws the one message the guardian owns, through `show`.
  The old shape answered each tap with a fresh message, which is how a chat ends
  up holding a status, a confirmation, a result and a menu that all look current.
* **the owner's own commands are cleaned up.** `/status` typed into the chat has
  served its purpose the moment it is handled; leaving it there is the same
  clutter by another route.
* **a message carrying a secret is deleted before it is used.** Whatever happens
  next, the code or the password must not stay in the chat history; if deletion
  fails the owner is told, without the value being quoted back.
* **nothing technical reaches the chat.** Errors become one sanitised sentence;
  the traceback goes to the log, redacted.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from . import screens, tokens, views
from .fsm import MaxOnboarding
from .screens import Screen
from .state import StateStore
from .tokens import Verdict
from .views import HomeView

logger = logging.getLogger(__name__)

#: Draws a screen into the guardian's anchor message. The same callable the
#: state machine is given, so onboarding and control cannot drift onto two
#: different messages.
Show = Callable[[Screen], Awaitable[None]]

#: Shown to anybody who is not the owner. Says nothing about what this bot is.
DENIED = "У вас нет доступа к этому боту."

#: Said when Telegram refuses the deletion, without ever repeating the value.
SECRET_NOT_DELETED = "Не смог удалить сообщение — удалите его сами, в нём был код."  # noqa: S105

RESTARTED = "Мост перезапущен"
DISCONNECTED = "Мост отключён"
DISCONNECT_FAILED = "Не нашёл этот мост — обновите список"
REPULL_FAILED = "Не смог перезалить — посмотрите статус"
RESTART_FAILED = "Перезапустить не удалось — посмотрите статус"
RESTART_CANCELLED = "Отменено"

#: Answers to the problem centre's buttons. Toasts rather than messages: the
#: screen underneath is redrawn either way, and it is the screen that is true.
RETRY_QUEUED = "Отправлю ещё раз"
SETTLED = "Хорошо, вопрос закрыт"
ARCHIVED = "Не буду отправлять"
NO_SUCH_JOB = "Этого уже нет в очереди — список обновлён"
RESUMING = "Продолжаю…"
ABANDONED = "Больше не спрашиваю"


def _tail_int(data: str, prefix: str) -> int | None:
    """The one signed integer after a prefix, or None. ASCII digits only."""
    if not data.startswith(f"{prefix}:"):
        return None
    tail = data[len(prefix) + 1 :]
    body = tail[1:] if tail.startswith("-") else tail
    return int(tail) if body.isascii() and body.isdigit() else None


def _problem_screen(
    problem: views.UserProblem, *, back: tuple[str, str] | None = None
) -> Screen:
    """Which screen one problem gets. Three of them can be acted on."""
    if problem.kind is views.ProblemKind.DELIVERY_FAILED and problem.job_id is not None:
        return screens.failed_job_screen(problem, back=back)
    if problem.kind is views.ProblemKind.DELIVERY_AMBIGUOUS and problem.job_id is not None:
        return screens.ambiguous_job_screen(problem, back=back)
    if problem.kind is views.ProblemKind.PROVISIONING and problem.max_chat_id is not None:
        return screens.attempt_screen(problem, back=back)
    return screens.problem_screen(problem, back=back)


def _locate(
    groups: list[views.ProblemGroup], key: str
) -> tuple[views.UserProblem | None, views.ProblemGroup | None]:
    """One problem and the family it belongs to, by its callback key."""
    for group in groups:
        for problem in group.items:
            if problem.key == key:
                return problem, group
    return None, None


class Control(Protocol):
    """What the runtime lets the guardian do to the bridge."""

    async def status_lines(self) -> list[str]: ...

    async def restart_bridge(self) -> bool: ...


async def _home_view(control: Control) -> HomeView:
    """The first screen's model.

    Written with `getattr` rather than a wider Protocol on purpose: a control
    surface that predates a screen should keep working rather than raise inside
    a callback the owner is looking at. An install with no worker is exactly the
    case this covers, and it is also the case where the answer matters most.
    """
    provider = getattr(control, "home_view", None)
    if provider is None:
        return views.home_view(views.AttentionFacts())
    model: HomeView = await provider()
    return model


async def _problems(control: Control) -> list[views.UserProblem]:
    provider = getattr(control, "problems", None)
    if provider is None:
        return []
    found: list[views.UserProblem] = await provider()
    return found


async def _bridges(control: Control) -> list[views.BridgeView]:
    provider = getattr(control, "bridge_views", None)
    if provider is None:
        return []
    items: list[views.BridgeView] = await provider()
    return items


async def _timezone_label(control: Control) -> str | None:
    provider = getattr(control, "timezone_label", None)
    if provider is None:
        return None
    label: str | None = await provider()
    return label


async def _diagnostic_lights(control: Control) -> dict[str, bool]:
    provider = getattr(control, "diagnostic_lights", None)
    if provider is None:
        return {}
    lights: dict[str, bool] = await provider()
    return lights


async def _diagnostic_section(control: Control, section: str) -> list[str]:
    provider = getattr(control, "diagnostic_sections", None)
    if provider is None:
        return []
    sections: dict[str, list[str]] = await provider()
    return sections.get(section, [])


async def _own_mirror(control: Control) -> bool:
    """Whether own MAX messages are being carried, for the home toggle.

    `getattr` like the rest: a control surface that predates the toggle reads as
    "off", which is also the default, so the button is honest either way.
    """
    provider = getattr(control, "own_messages_mirror", None)
    if provider is None:
        return False
    value: bool = await provider()
    return value


def _can_wipe(control: Control) -> bool:
    """Whether the owner's session can empty the chat with a bot."""
    return bool(getattr(control, "can_wipe_dialogs", False))


def _can_delete(control: Control) -> bool:
    """Whether this install can really delete a bot, asked of the runtime.

    Through the same facade every other action goes through. Reading it off the
    flow directly is how the button came to exist in tests and never on screen:
    the router talks to the runtime, and the runtime had no such method.
    """
    return bool(getattr(control, "can_delete_bots", False))


async def _bridge_view(control: Control, max_chat_id: int) -> views.BridgeView | None:
    provider = getattr(control, "bridge_view", None)
    if provider is None:
        return None
    view: views.BridgeView | None = await provider(max_chat_id)
    return view


async def _bridge_lines(control: Control, max_chat_id: int) -> list[str]:
    """The bridge's own raw status block, for «Подробнее» and nowhere else."""
    provider = getattr(control, "bridge_card", None)
    if provider is None:
        return []
    _, lines = await provider(max_chat_id)
    return list(lines)


def build_onboarding_router(
    *,
    owner_user_id: int,
    store: StateStore,
    onboarding: MaxOnboarding,
    control: Control,
    is_guardian: Callable[[int], bool],
    show: Show,
) -> Router:
    router = Router(name="onboarding")

    def owns(user: Any) -> bool:
        return user is not None and int(getattr(user, "id", 0)) == owner_user_id

    async def refuse(message: Message) -> None:
        await message.answer(DENIED)

    async def home() -> None:
        await show(screens.home(await _home_view(control)))

    async def settings() -> None:
        await show(
            screens.settings(
                mirror_own=await _own_mirror(control),
                timezone=await _timezone_label(control),
            )
        )

    async def diagnostics() -> None:
        lights = await _diagnostic_lights(control)
        await show(
            screens.diagnostics(
                telegram=lights.get("telegram", True),
                max_connected=lights.get("max", False),
                delivery=lights.get("delivery", False),
                database=lights.get("database", False),
            )
        )

    async def bridges() -> None:
        await show(screens.bridges_screen(await _bridges(control)))

    def bump() -> int:
        """Move the revision on and hand back the new one.

        Called when a screen that can change something is drawn *and* when its
        button is honoured, so the second tap on the same confirmation carries a
        number that no longer exists.
        """
        revision = store.load().screen_revision + 1
        store.update(screen_revision=revision)
        return revision

    @router.message(Command("start"))
    async def _start(message: Message, command: CommandObject) -> None:
        if not owns(message.from_user):
            await refuse(message)
            return

        record = store.load()
        payload = (command.args or "").strip()
        # The deep link carries a one-time token; it has no business staying in
        # the history any longer than the rest of the commands.
        await _forget(message)

        if payload:
            verdict = tokens.verify(
                presented=payload,
                expected_digest=record.token_digest,
                expires_at=record.token_expires_at,
                used_at=record.token_used_at,
                owner_user_id=owner_user_id,
                sender_user_id=int(message.from_user.id) if message.from_user else 0,
            )
            if verdict is Verdict.WRONG_OWNER:
                await refuse(message)
                return
            if verdict is not Verdict.OK:
                # A refused link is not a dead end: the owner is the owner, and
                # the buttons work regardless of how they got here. Said in the
                # anchor rather than beside it.
                logger.info("setup link refused: %s", verdict.value)
            else:
                store.update(token_used_at=int(time.time()))
                logger.info("setup link accepted")

        await onboarding.offer()

    @router.message(Command("status"))
    async def _status(message: Message) -> None:
        """Now the diagnostics page, not the thirty-line block it used to be.

        The block is still one tap in, under «Технические данные». What changed
        is what the owner is handed first when they ask how it is going.
        """
        if not owns(message.from_user):
            await refuse(message)
            return
        await _forget(message)
        await diagnostics()

    @router.message(Command("restart"))
    async def _restart(message: Message) -> None:
        if not owns(message.from_user):
            await refuse(message)
            return
        await _forget(message)
        await show(screens.restart_confirm(bump()))

    @router.message(Command("menu"))
    async def _menu_command(message: Message) -> None:
        if not owns(message.from_user):
            await refuse(message)
            return
        await _forget(message)
        await home()

    @router.message(Command("bridges"))
    async def _bridges_command(message: Message) -> None:
        """Published, because it is one of the three things an owner does."""
        if not owns(message.from_user):
            await refuse(message)
            return
        await _forget(message)
        await bridges()

    @router.callback_query(F.data == screens.BEGIN)
    async def _begin(query: CallbackQuery) -> None:
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        await query.answer()
        await onboarding.begin()

    @router.callback_query(F.data == screens.TZ_AUTO)
    async def _timezone_auto(query: CallbackQuery) -> None:
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        await query.answer()
        await onboarding.detect_timezone()

    @router.callback_query(F.data == screens.TZ_LIST)
    async def _timezone_list(query: CallbackQuery) -> None:
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        await query.answer()
        await onboarding.offer_timezone_list()

    @router.callback_query(F.data.startswith(f"{screens.TZ_SET}:"))
    async def _timezone_set(query: CallbackQuery) -> None:
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        name = (query.data or "")[len(screens.TZ_SET) + 1 :]
        await query.answer()
        # The value is validated inside the state machine, against `zoneinfo`:
        # a callback is user-supplied data like any other.
        await onboarding.set_timezone(name)

    @router.callback_query(F.data == screens.PHONE_SAME)
    async def _same_phone(query: CallbackQuery) -> None:
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        await query.answer()
        await onboarding.use_telegram_phone()

    @router.callback_query(F.data == screens.PHONE_OTHER)
    async def _other_phone(query: CallbackQuery) -> None:
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        await query.answer()
        await onboarding.ask_other_phone()

    @router.callback_query(F.data == screens.MENU)
    async def _menu(query: CallbackQuery) -> None:
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        await query.answer()
        await home()

    @router.callback_query(F.data == screens.SETTINGS)
    async def _settings(query: CallbackQuery) -> None:
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        await query.answer()
        await settings()

    async def problems() -> list[views.ProblemGroup]:
        groups = views.group_problems(await _problems(control))
        await show(screens.problems_screen(groups))
        return groups

    async def category(kind: Any, page: int) -> None:
        """One family's page, or the index when the family has emptied.

        The last item of a category being settled is the good outcome, and it
        must not leave the owner on a page that no longer exists.
        """
        groups = views.group_problems(await _problems(control))
        group = next((one for one in groups if one.kind is kind), None)
        if group is None:
            await show(screens.problems_screen(groups))
            return
        await show(screens.category_screen(group, page))

    @router.callback_query(F.data.startswith(f"{screens.PROBLEM_CATEGORY}:"))
    async def _problem_category(query: CallbackQuery) -> None:
        """Registered before `onb:problems:one` and `onb:problems`, its parents."""
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        await query.answer()
        parsed = screens.parse_category(query.data or "")
        if parsed is None:
            await problems()
            return
        await category(*parsed)

    @router.callback_query(F.data.startswith(f"{screens.PROBLEM}:"))
    async def _one_problem(query: CallbackQuery) -> None:
        """Registered before the plain `onb:problems`, which is its parent."""
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        await query.answer()
        parsed = screens.parse_problem(query.data or "")
        groups = views.group_problems(await _problems(control))
        wanted, group = _locate(groups, ":".join(parsed) if parsed else "")
        if wanted is None or group is None:
            # The problem went away while the list was on screen — which is the
            # good outcome, and the list now says so.
            await show(screens.problems_screen(groups))
            return
        back = (
            screens.category_back(group, group.page_of(wanted)) if group.paged else None
        )
        await show(_problem_screen(wanted, back=back))

    @router.callback_query(F.data == screens.PROBLEMS)
    async def _problems_screen(query: CallbackQuery) -> None:
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        await query.answer()
        await problems()

    @router.callback_query(F.data.startswith(f"{screens.JOB_RETRY}:"))
    async def _job_retry(query: CallbackQuery) -> None:
        """The existing `retry_now`, reached by a button instead of by typing.

        Nothing different happens to the job: it is the same operation `/retry`
        calls, with the same refusal when the payload has already been cleared.
        """
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        await _act(query, screens.JOB_RETRY, "retry_job", RETRY_QUEUED)

    @router.callback_query(F.data.startswith(f"{screens.JOB_SETTLE}:"))
    async def _job_settle(query: CallbackQuery) -> None:
        """«Дошло» — the existing settlement, and only the owner can say it."""
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        await _act(query, screens.JOB_SETTLE, "settle_job", SETTLED)

    @router.callback_query(F.data.startswith(f"{screens.JOB_ARCHIVE}:"))
    async def _job_archive(query: CallbackQuery) -> None:
        """«Не отправлять» — the existing archive. Evidence is kept, not deleted."""
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        await _act(query, screens.JOB_ARCHIVE, "archive_job", ARCHIVED)

    async def _act(query: CallbackQuery, prefix: str, method: str, said: str) -> None:
        """Do the one thing, then put the owner back where they were.

        Where they were is a page of a category, and the page can have moved:
        settling the last item on page eleven leaves ten pages. So the family is
        located *before* the action — afterwards the job is gone and there is
        nothing left to look it up by — and the page is clamped on redraw.
        """
        job_id = screens.parse_job(query.data or "", prefix)
        action = getattr(control, method, None)
        if job_id is None or action is None:
            await query.answer(NO_SUCH_JOB, show_alert=True)
            await problems()
            return
        before = views.group_problems(await _problems(control))
        problem, group = _locate(before, f"job:{job_id}")
        kind = group.kind if group is not None else None
        page = group.page_of(problem) if group is not None and problem is not None else 0

        ok = bool(await action(job_id))
        await query.answer(said if ok else NO_SUCH_JOB, show_alert=not ok)
        if kind is None:
            await problems()
            return
        await category(kind, page)

    @router.callback_query(F.data.startswith(f"{screens.ATTEMPT_RETRY}:"))
    async def _attempt_retry(query: CallbackQuery) -> None:
        """The same reconciliation `/provretry` runs, through the coordinator."""
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        parsed = _tail_int(query.data or "", screens.ATTEMPT_RETRY)
        resume = getattr(control, "resume_attempt", None)
        await query.answer(RESUMING)
        if parsed is not None and resume is not None:
            await resume(parsed)
        await category(views.ProblemKind.PROVISIONING, 0)

    @router.callback_query(F.data.startswith(f"{screens.ATTEMPT_DROP}:"))
    async def _attempt_drop(query: CallbackQuery) -> None:
        """The same journal note `/provabandon` writes. Nothing remote happens."""
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        parsed = _tail_int(query.data or "", screens.ATTEMPT_DROP)
        abandon = getattr(control, "abandon_attempt", None)
        await query.answer(ABANDONED)
        if parsed is not None and abandon is not None:
            await abandon(parsed)
        await category(views.ProblemKind.PROVISIONING, 0)

    @router.callback_query(F.data == screens.DIAGNOSTICS)
    async def _diagnostics(query: CallbackQuery) -> None:
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        await query.answer()
        await diagnostics()

    @router.callback_query(F.data.startswith(f"{screens.DIAGNOSTICS_PART}:"))
    async def _diagnostics_part(query: CallbackQuery) -> None:
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        await query.answer()
        section = screens.parse_diagnostics_part(query.data or "")
        if section is None:
            await diagnostics()
            return
        heading = dict(screens.DIAGNOSTIC_SECTIONS)[section]
        await show(
            screens.diagnostics_section(heading, await _diagnostic_section(control, section))
        )

    @router.callback_query(F.data.startswith(f"{screens.OWN_SET}:"))
    async def _set_own_messages(query: CallbackQuery) -> None:
        """Registered before the plain `onb:own`, which is its parent prefix."""
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        wanted = screens.parse_own_set(query.data or "")
        setter = getattr(control, "set_own_messages_mirror", None)
        await query.answer()
        if wanted is not None and setter is not None:
            await setter(wanted)
        await show(screens.own_messages(mirror_own=await _own_mirror(control)))

    @router.callback_query(F.data.in_({screens.OWN_MSGS, screens.SETTINGS_OWN}))
    async def _own_messages(query: CallbackQuery) -> None:
        """One setting, on its own screen, with both answers visible at once.

        It used to be a toggle on the home screen whose label showed the state
        and whose tap flipped it — the single most reliably misread control
        there is, because half of everybody reads such a label as a promise
        about what pressing it will do.
        """
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        await query.answer()
        await show(screens.own_messages(mirror_own=await _own_mirror(control)))

    @router.callback_query(F.data.startswith(f"{screens.SETTINGS_TZ_SET}:"))
    async def _settings_timezone_set(query: CallbackQuery) -> None:
        """The same write and the same restart; a different place to land.

        Onboarding's timezone answer ends on «MAX подключён», which is right
        when it is the third question of setup and wrong when it is a setting
        the owner came in to change.
        """
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        name = (query.data or "")[len(screens.SETTINGS_TZ_SET) + 1 :]
        await query.answer()
        if await onboarding.set_timezone(name):
            await settings()

    @router.callback_query(F.data == screens.SETTINGS_TZ)
    async def _settings_timezone(query: CallbackQuery) -> None:
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        await query.answer()
        from bridge.bootstrap.timezones import RUSSIAN_TIMEZONES, offset_label

        await show(
            screens.timezone_list(
                [(f"{city} · {offset_label(name)}", name) for city, name in RUSSIAN_TIMEZONES],
                back=screens.SETTINGS,
                current=await _timezone_label(control),
                callback=screens.settings_timezone_callback,
            )
        )

    @router.callback_query(F.data == screens.BRIDGES)
    async def _bridge_list(query: CallbackQuery) -> None:
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        await query.answer()
        await bridges()

    @router.callback_query(F.data.startswith(f"{screens.BRIDGE_OFF_YES}:"))
    async def _bridge_off_confirmed(query: CallbackQuery) -> None:
        """The only handler here that changes anything, so it checks the revision."""
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        parsed = screens.parse_bridge_off_yes(query.data or "")
        if parsed is None or parsed[1] != store.load().screen_revision:
            await query.answer(screens.STALE_SCREEN)
            await bridges()
            return
        max_chat_id, _ = parsed
        bump()
        # Read before the disconnect: afterwards the bridge is no longer active,
        # and the screen still has to name the contact it just switched off.
        item = await _bridge_view(control, max_chat_id)
        await query.answer("Отключаю…")
        disconnect = getattr(control, "disconnect_bridge", None)
        ok = bool(await disconnect(max_chat_id)) if disconnect is not None else False
        if not ok or item is None:
            await query.answer(DISCONNECT_FAILED, show_alert=True)
            await bridges()
            return
        await show(screens.disconnected(item))
        await query.answer(DISCONNECTED)

    @router.callback_query(F.data.startswith(f"{screens.BRIDGE_REPULL_YES}:"))
    async def _repull_confirmed(query: CallbackQuery) -> None:
        """Wipe and pull again. Revision-guarded: the wipe is not repeatable."""
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        parsed = screens.parse_bridge_repull_yes(query.data or "")
        if parsed is None or parsed[1] != store.load().screen_revision:
            await query.answer(screens.STALE_SCREEN)
            await bridges()
            return
        max_chat_id, _ = parsed
        bump()
        item = await _bridge_view(control, max_chat_id)
        repull = getattr(control, "repull_history", None)
        if item is None or repull is None:
            await query.answer(REPULL_FAILED, show_alert=True)
            await bridges()
            return
        await query.answer("Перезаливаю…")
        await show(screens.repulling(item))
        counts = await repull(max_chat_id)
        if counts is None:
            await query.answer(REPULL_FAILED, show_alert=True)
            await show(screens.bridge_screen(item, can_delete=_can_delete(control)))
            return
        gone, kept = counts
        await show(screens.repulled(item, gone=gone, kept=kept))

    @router.callback_query(F.data.startswith(f"{screens.BRIDGE_REPULL}:"))
    async def _repull(query: CallbackQuery) -> None:
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        max_chat_id = screens.parse_bridge_repull(query.data or "")
        await query.answer()
        item = None
        if max_chat_id is not None:
            item = await _bridge_view(control, max_chat_id)
        if item is None:
            await bridges()
            return
        await show(screens.repull_confirm(item, bump(), by_session=_can_wipe(control)))

    @router.callback_query(F.data.startswith(f"{screens.BRIDGE_FREE_YES}:"))
    async def _bridge_free_confirmed(query: CallbackQuery) -> None:
        """Delete the bot for real. Irreversible, and the slot comes back."""
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        parsed = screens.parse_bridge_free_yes(query.data or "")
        if parsed is None or parsed[1] != store.load().screen_revision:
            await query.answer(screens.STALE_SCREEN, show_alert=True)
            await bridges()
            return
        max_chat_id, _ = parsed
        await query.answer()
        # Read before the deletion: afterwards the row no longer names a bot.
        item = await _bridge_view(control, max_chat_id)
        tear_down = getattr(control, "tear_down_bridge", None)
        if item is None or tear_down is None:
            await bridges()
            return
        await show(screens.tearing_down(item))
        try:
            outcome = await tear_down(max_chat_id)
        except Exception as error:  # noqa: BLE001 - shown, not raised at the owner
            # A teardown stops at the first remote step it cannot finish, with
            # every local row intact. Say where it stopped: the owner needs to
            # know whether the bot is gone, not merely that something failed.
            logger.warning("teardown did not finish: %s", type(error).__name__)
            await show(screens.teardown_stalled(item, str(error)))
            return
        if outcome is None:
            await bridges()
            return
        await show(screens.slot_freed(item, outcome))

    @router.callback_query(F.data.startswith(f"{screens.BRIDGE_FREE}:"))
    async def _bridge_free(query: CallbackQuery) -> None:
        """How to delete the bot itself. Instructions, because no bot can do it."""
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        if (query.data or "").startswith(f"{screens.BRIDGE_FREE_YES}:"):
            # Registration order already sends this to the handler above, and
            # relying on that alone is how «Да, снести» was swallowed: this
            # prefix is a parent of that one, `parse_bridge_free` returned None,
            # and the screen silently redrew the list. Two guards, because the
            # failure is invisible — a tap that does nothing looks like a tap
            # that was not registered.
            return
        max_chat_id = screens.parse_bridge_free(query.data or "")
        await query.answer()
        item = None
        if max_chat_id is not None:
            item = await _bridge_view(control, max_chat_id)
        if item is None:
            await bridges()
            return
        # A button when the owner's session can drive `/deletebot`, and the
        # recipe when it cannot. Never a button that pretends: Bot API has no
        # `deleteManagedBot` at all.
        await show(
            screens.free_slot(
                item,
                can_delete=_can_delete(control),
                can_wipe=_can_wipe(control),
                revision=bump(),
            )
        )

    @router.callback_query(F.data.startswith(f"{screens.BRIDGE_OFF}:"))
    async def _bridge_off(query: CallbackQuery) -> None:
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        max_chat_id = screens.parse_bridge_off(query.data or "")
        await query.answer()
        if max_chat_id is None:
            await bridges()
            return
        item = await _bridge_view(control, max_chat_id)
        if item is None:
            await bridges()
            return
        await show(screens.disconnect_confirm(item, bump()))

    @router.callback_query(F.data.startswith(f"{screens.BRIDGE_SETTINGS}:"))
    async def _bridge_settings(query: CallbackQuery) -> None:
        """Where disconnecting and deleting live now — one level under the card."""
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        max_chat_id = screens.parse_bridge_settings(query.data or "")
        await query.answer()
        item = None if max_chat_id is None else await _bridge_view(control, max_chat_id)
        if item is None:
            await bridges()
            return
        await show(screens.bridge_settings_screen(item, can_delete=_can_delete(control)))

    @router.callback_query(F.data.startswith(f"{screens.BRIDGE_HISTORY}:"))
    async def _bridge_history(query: CallbackQuery) -> None:
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        max_chat_id = screens.parse_bridge_history(query.data or "")
        await query.answer()
        item = None if max_chat_id is None else await _bridge_view(control, max_chat_id)
        if item is None:
            await bridges()
            return
        await show(screens.bridge_history_screen(item))

    @router.callback_query(F.data.startswith(f"{screens.BRIDGE_TECH}:"))
    async def _bridge_tech(query: CallbackQuery) -> None:
        """«Подробнее». The raw block the card used to print unasked."""
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        max_chat_id = screens.parse_bridge_tech(query.data or "")
        await query.answer()
        item = None if max_chat_id is None else await _bridge_view(control, max_chat_id)
        if item is None or max_chat_id is None:
            await bridges()
            return
        await show(screens.bridge_tech_screen(item, await _bridge_lines(control, max_chat_id)))

    @router.callback_query(F.data.startswith(f"{screens.BRIDGE}:"))
    async def _bridge(query: CallbackQuery) -> None:
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        max_chat_id = screens.parse_bridge(query.data or "")
        await query.answer()
        if max_chat_id is None:
            await bridges()
            return
        item = await _bridge_view(control, max_chat_id)
        if item is None:
            await bridges()
            return
        await show(screens.bridge_screen(item, can_delete=_can_delete(control)))

    @router.callback_query(F.data == screens.CANCEL)
    async def _cancel(query: CallbackQuery) -> None:
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        await query.answer()
        await onboarding.cancel()

    @router.callback_query(F.data == screens.RETRY)
    async def _retry(query: CallbackQuery) -> None:
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        await query.answer()
        await onboarding.retry()

    @router.callback_query(F.data == screens.LAUNCH)
    async def _launch(query: CallbackQuery) -> None:
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        await query.answer("Запускаю…")
        await onboarding.retry_launch()

    @router.callback_query(F.data == screens.STATUS)
    async def _status_button(query: CallbackQuery) -> None:
        """«Технические данные» — the old `/status`, kept whole, one level in."""
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        await query.answer()
        await show(screens.technical_details(await control.status_lines()))

    @router.callback_query(F.data == screens.RESTART)
    async def _restart_button(query: CallbackQuery) -> None:
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        await query.answer()
        await show(screens.restart_confirm(bump()))

    @router.callback_query(F.data == screens.RESTART_NO)
    async def _restart_declined(query: CallbackQuery) -> None:
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        await query.answer(RESTART_CANCELLED)
        # Back where the owner was, in the same message. It used to land on
        # home, which is not where the button was tapped from — a decision not
        # to restart should leave nothing behind, including the navigation.
        await diagnostics()

    @router.callback_query(F.data.startswith(f"{screens.RESTART_YES}:"))
    async def _restart_confirmed(query: CallbackQuery) -> None:
        if not owns(query.from_user):
            await query.answer(DENIED, show_alert=True)
            return
        presented = screens.parse_restart_revision(query.data or "")
        if presented is None or presented != store.load().screen_revision:
            # A confirmation from a screen that has already been answered. The
            # anchor is edited in place, so the owner cannot see that by looking.
            await query.answer(screens.STALE_SCREEN)
            await home()
            return
        bump()
        await query.answer("Перезапускаю…")
        await show(screens.restarting())
        ok = await control.restart_bridge()
        await home()
        # The outcome as a toast, not as a message: nothing about a restart is
        # worth keeping in the history once the screen below says what is true.
        await query.answer(RESTARTED if ok else RESTART_FAILED, show_alert=not ok)

    def _is_an_answer(message: Message, bot: Any) -> bool:
        """Whether this text is an answer to an outstanding question.

        A *filter*, not a check inside the handler, and that distinction is
        load-bearing: in aiogram a handler that runs stops the routing, so an
        early `return` here would swallow `/dialogs` and every other command
        owned by the guardian router.
        """
        if not onboarding.expects_text:
            return False
        # Only the guardian asks these questions. The same text sent to a
        # bridge bot is a message for a contact, and must go to MAX untouched.
        if not is_guardian(int(getattr(bot, "id", 0))):
            return False
        return not (message.text or "").startswith("/")

    @router.message(F.text, _is_an_answer)
    async def _answer(message: Message) -> None:
        """A plain message during onboarding is an answer to the open question."""
        if not owns(message.from_user):
            await refuse(message)
            return

        text = message.text or ""
        secret = onboarding.expects_secret
        if secret:
            await _delete_secret(message)

        await onboarding.submit(text)

    return router


async def _forget(message: Message) -> None:
    """Remove a command the owner typed. Best effort, and never announced.

    Unlike a code or a password this is not a secret, so a refusal is not worth
    a sentence: the screen the command asked for is drawn either way.
    """
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 - too old, no rights, already gone
        logger.debug("could not delete a command message")


async def _delete_secret(message: Message) -> None:
    """Remove the message before its contents are used anywhere else.

    Telegram has already carried and stored it — this is not a claim that the
    value was never seen, only that it does not stay in the chat.
    """
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 - too old, no rights, already gone
        logger.warning("could not delete a message containing a credential")
        await message.answer(SECRET_NOT_DELETED)
