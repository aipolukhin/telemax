"""The dialog picker: what the owner taps, and what stops them tapping wrong.

Three things this screen has to get right, and all three are about state that
moves underneath it:

* **capacity is a rule, not a caption.** A number printed at the top that the
  buttons then ignore is worse than no number: the owner selects nine contacts,
  presses «Готово», and finds out about the limit halfway through a run that has
  already deleted a working bot. So the free-slot figure and the refusal to
  select come from the same object.
* **a replacement is free.** A contact whose bot already exists gets deleted and
  recreated; it holds a slot the whole time and needs none of the free ones.
  Charging it a slot would hide capacity the account actually has.
* **the message becomes the answer.** When provisioning finishes, this same
  message is edited into a list of links. Leaving the old keyboard alive next to
  a result is how somebody taps a selection button belonging to a run that has
  already happened.

Only rendering and selection bookkeeping live here. What a tap *does* is the
router's business, and what it costs is `capacity`'s.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bridge.telegram.design import BROKEN, BUSY, DONE, TODO, esc, title
from bridge.telegram.humanise import plural

from .byphone import ADD as BACK_TO_ADD
from .byphone import ascii_digits
from .capacity import Capacity, PeerBotStatus, PeerPlan
from .journal import ItemState, JournalEntry
from .owned import start_link
from .picker import OPEN as PICK_MORE

#: One screenful of buttons on a phone.
PAGE_SIZE = 6

#: `onboarding.screens.BRIDGES` and `.MENU`, by value: `screens` imports this
#: module for the bridge list's buttons, and importing back would be a cycle.
#: `tests/test_guardian_dead_ends.py` asserts the two agree.
BRIDGES = "onb:bridges"
HOME = "onb:menu"

SELECT = "prov:sel"
PAGE = "prov:page"
GO = "prov:go"
RETRY = "prov:retry"
REFRESH = "prov:refresh"

HISTORY_ALL = "prov:hist:all"
HISTORY_NEW = "prov:hist:new"
HISTORY_NO = "prov:hist:no"
#: The tick in the picker, set *before* anything is created. Asking afterwards
#: made the import a separate errand the owner had to remember; asking here makes
#: it part of the one decision they came to make.
HISTORY_PICK = "prov:hist:pick"

HISTORY_ON = "☑ Подтянуть переписку"
HISTORY_OFF = "☐ Подтянуть переписку"

HEADING = "Выберите диалоги MAX"

#: Said whenever the numbers are this install's own rather than Telegram's. Bot
#: API has no way to list the bots an account owns and no way to read its limit
#: (measured against Bot API 10.2), so a bot made by hand, or left behind by an
#: earlier install, is invisible here and still occupies a slot.
NOT_THE_WHOLE_ACCOUNT = (
    "Считаю только своих ботов и лимит из настроек — "
    "Telegram не отдаёт ни список, ни лимит аккаунта."
)

#: Shown in a callback alert — never as a new message. A refusal that scrolls
#: the keyboard off the screen is a refusal the owner has to hunt for.
NO_SLOTS_ALERT = (
    "Нет свободных слотов для новых ботов.\n\n"
    "Удалите ненужного бота или снимите выбор с другого диалога."
)
UNKNOWN_LIMIT_ALERT = (
    "Telegram не сообщил лимит ботов.\n\n"
    "Пересоздать существующий мост можно, добавить новый — нет."
)
FOREIGN_ALERT = (
    "Имя, которое получил бы этот бот, уже занято другим аккаунтом Telegram.\n\n"
    "Этот контакт подключить нельзя."
)
NOT_MANAGEABLE_ALERT = (
    "Бот с нужным именем уже есть, но страж им не управляет: он создан "
    "вручную в @BotFather.\n\n"
    "Удалите его там и создайте заново отсюда."
)
LOCKED_ALERT = "Создание мостов уже началось — список закрыт."
STALE_ALERT = "Список устарел — откройте его заново."
NOTHING_SELECTED_ALERT = "Сначала выберите хотя бы один диалог."

AT_LIMIT_TEXT = (
    "Достигнут лимит Telegram-ботов.\n\n"
    "Можно пересоздать уже существующий мост, "
    "но для нового контакта сначала освободите слот."
)


@dataclass
class Selection:
    """What is chosen right now, and which generation of the list it belongs to.

    The epoch is the guard against every stale tap: a keyboard drawn before the
    list was rebuilt carries the old number, is recognised, and does nothing.
    `locked` is the same idea in time rather than in generations — once «Готово»
    has been pressed, the selection it captured is the one being built.
    """

    epoch: int = 0
    page: int = 0
    chosen: set[int] = field(default_factory=set)
    locked: bool = False
    capacity: Capacity | None = None
    #: Pull the old conversation in as part of this run, rather than as a
    #: question afterwards.
    history: bool = False

    def reset(self, capacity: Capacity) -> None:
        self.epoch += 1
        self.page = 0
        self.chosen = set()
        self.locked = False
        self.history = False
        self.capacity = capacity

    @property
    def plans(self) -> tuple[PeerPlan, ...]:
        return self.capacity.plans if self.capacity else ()

    def priced(self) -> Capacity:
        """Capacity with the current selection applied, for the header and rules."""
        base = self.capacity
        if base is None:
            raise RuntimeError("selection has no capacity snapshot")
        return base.with_selection(frozenset(self.chosen))

    def toggle(self, max_chat_id: int) -> tuple[bool, str | None]:
        """Flip one dialog. Returns (changed, alert-to-show).

        A refusal must leave the selection exactly as it was: half-applying a
        tap is how a keyboard ends up disagreeing with the count under it.
        """
        if self.locked:
            return False, LOCKED_ALERT
        priced = self.priced()
        plan = priced.plan_for(max_chat_id)
        if plan is None:
            return False, STALE_ALERT
        if max_chat_id in self.chosen:
            self.chosen.discard(max_chat_id)
            return True, None
        if plan.status is PeerBotStatus.FOREIGN_USERNAME_COLLISION:
            return False, FOREIGN_ALERT
        if plan.status is PeerBotStatus.NOT_MANAGEABLE:
            return False, NOT_MANAGEABLE_ALERT
        if not priced.can_select(max_chat_id):
            return False, (
                UNKNOWN_LIMIT_ALERT if priced.free_new_slots is None else NO_SLOTS_ALERT
            )
        self.chosen.add(max_chat_id)
        return True, None

    def selected_plans(self) -> list[PeerPlan]:
        """In list order, so the progress display and the result agree."""
        return [plan for plan in self.plans if plan.max_chat_id in self.chosen]


# ------------------------------------------------------------------- callbacks


def select_callback(max_chat_id: int, *, epoch: int) -> str:
    return f"{SELECT}:{epoch}:{max_chat_id}"


def page_callback(page: int, *, epoch: int) -> str:
    return f"{PAGE}:{epoch}:{page}"


def go_callback(*, epoch: int) -> str:
    return f"{GO}:{epoch}"


def history_pick_callback(*, epoch: int) -> str:
    return f"{HISTORY_PICK}:{epoch}"


def retry_callback(max_chat_id: int, *, epoch: int) -> str:
    return f"{RETRY}:{epoch}:{max_chat_id}"


def parse_callback(data: str, prefix: str) -> tuple[int, int] | None:
    """`<prefix>:<epoch>:<value>` -> (epoch, value), or None when it is not one.

    ASCII digits only: `str.isdigit()` is true of `²`, which `int()` then
    refuses, and a `ValueError` raised inside a callback handler is a tap that
    does nothing and says nothing.
    """
    if not data.startswith(f"{prefix}:"):
        return None
    parts = data[len(prefix) + 1 :].split(":")
    if len(parts) != 2 or not all(ascii_digits(part, signed=True) for part in parts):
        return None
    return int(parts[0]), int(parts[1])


def parse_epoch(data: str, prefix: str) -> int | None:
    if not data.startswith(f"{prefix}:"):
        return None
    tail = data[len(prefix) + 1 :]
    return int(tail) if ascii_digits(tail, signed=True) else None


# --------------------------------------------------------------------- drawing


def page_count(total: int, *, size: int = PAGE_SIZE) -> int:
    return max(1, -(-total // size))


def capacity_lines(capacity: Capacity, *, selection: bool = True) -> list[str]:
    """The numbers, or an honest sentence when the limit is not published.

    `selection=False` drops the two lines about what is chosen right now, for the
    screens that show the same snapshot without a list under it. Same function
    either way on purpose: the home screen counting bots for itself is what let
    it disagree with the picker.
    """
    owned = capacity.owned_bot_count
    limit = capacity.bot_limit
    counted = "Боты этой установки" if capacity.counts_only_ours else "Боты Telegram"
    if limit is None:
        return [
            f"{counted}: {owned}",
            "Лимит Telegram неизвестен — доступно только пересоздание",
        ]
    lines = [
        f"{counted}: {owned} из {limit}",
        f"Свободно новых слотов: {capacity.free_new_slots}",
    ]
    if capacity.counts_only_ours:
        # The owner read «5 из 20» as a claim about their account and found six
        # bots in it. It was never that claim: Bot API cannot list the bots an
        # account owns, so this counts the ones this install created, and the
        # limit comes from configuration. Both are said out loud now — a number
        # that looks exact and is not costs more than a longer line.
        lines.append(NOT_THE_WHOLE_ACCOUNT)
    if not selection:
        return lines
    lines.append(
        f"Выбрано новых: {capacity.selected_new_count} из {capacity.free_new_slots}"
    )
    if capacity.replacement_count:
        lines.append(f"Уже создано, переиспользую: {capacity.replacement_count}")
    return lines


#: How few free slots is few enough to be worth saying. Above this the number
#: is an answer to a question the owner is not asking: they came here to pick a
#: person, and «Боты этой установки: 7 из 40 · Свободно новых слотов: 33» plus
#: two lines of caveat is four lines of accounting on top of the list.
CAPACITY_HINT_THRESHOLD = 5


def capacity_hint(capacity: Capacity) -> list[str]:
    """Capacity, only when it changes what the owner can do next.

    Three cases earn a line: there is no room at all, the limit is unknown so
    only replacements work, or the room left is small enough that it will run
    out during this selection. Everything else is arithmetic behind
    «Технические данные».
    """
    if capacity.at_limit:
        return [f"{BROKEN} {AT_LIMIT_TEXT}"]
    if capacity.bot_limit is None:
        return ["Лимит Telegram неизвестен — можно только пересоздать существующие мосты."]
    free = capacity.free_new_slots or 0
    if free <= CAPACITY_HINT_THRESHOLD:
        return [
            "Можно добавить ещё "
            f"{free} {plural(free, 'контакт', 'контакта', 'контактов')}."
        ]
    return []


def picker_text(capacity: Capacity) -> str:
    """The header. It used to carry five lines of accounting above the list.

    Bots owned, the limit, free slots, the sentence explaining that Telegram
    publishes neither, how many are chosen and how many of those are
    replacements — read by somebody who opened this screen to tap their mother's
    name. The numbers are still exact and still reachable; they appear here when
    they change what the next tap can do.
    """
    lines = ["Можно выбрать несколько."]
    hint = capacity_hint(capacity)
    if hint:
        lines += ["", *[esc(line) for line in hint]]
    if capacity.selected_new_count:
        lines += ["", esc(f"Выбрано: {capacity.selected_new_count}")]
    return title(HEADING, *lines)


def _label(plan: PeerPlan, *, chosen: bool) -> str:
    """A button label, never escaped: Telegram does not parse these as HTML."""
    name = plan.title[:52]
    if plan.status in {
        PeerBotStatus.FOREIGN_USERNAME_COLLISION,
        PeerBotStatus.NOT_MANAGEABLE,
    }:
        return f"{BROKEN} {name}"
    if plan.expected_username is None:
        return f"{BROKEN} {name}"
    if chosen:
        return f"{DONE} {name}"
    if plan.is_replacement:
        # Worth marking: the bot is already there, so this one costs no free
        # slot and needs no confirmation dialog.
        return f"↻ {name}"
    return f"{TODO} {name}"


def history_declined() -> tuple[str, InlineKeyboardMarkup]:
    """What «Не сейчас» / «Закрыть» leaves on the screen.

    Both used to answer a toast and change nothing at all, so a button labelled
    «Закрыть» left the screen it claimed to close exactly where it was — and the
    owner's next move is to press it again.
    """
    return (
        title(
            "Хорошо",
            "Старую переписку не трогаю.",
            "",
            "Новые сообщения будут приходить как обычно.",
        ),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Мосты", callback_data=BRIDGES)],
                [InlineKeyboardButton(text="← Панель", callback_data=HOME)],
            ]
        ),
    )


def picker_markup(selection: Selection) -> InlineKeyboardMarkup:
    capacity = selection.priced()
    plans = list(capacity.plans)
    pages = page_count(len(plans))
    page = min(max(selection.page, 0), pages - 1)
    window = plans[page * PAGE_SIZE : page * PAGE_SIZE + PAGE_SIZE]

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=_label(plan, chosen=plan.max_chat_id in selection.chosen),
                callback_data=select_callback(plan.max_chat_id, epoch=selection.epoch),
            )
        ]
        for plan in window
    ]

    if pages > 1:
        navigation: list[InlineKeyboardButton] = []
        if page > 0:
            navigation.append(
                InlineKeyboardButton(
                    text="◀", callback_data=page_callback(page - 1, epoch=selection.epoch)
                )
            )
        navigation.append(
            InlineKeyboardButton(
                text=f"{page + 1}/{pages}",
                callback_data=page_callback(page, epoch=selection.epoch),
            )
        )
        if page < pages - 1:
            navigation.append(
                InlineKeyboardButton(
                    text="▶", callback_data=page_callback(page + 1, epoch=selection.epoch)
                )
            )
        rows.append(navigation)

    rows.append(
        [
            InlineKeyboardButton(
                text=HISTORY_ON if selection.history else HISTORY_OFF,
                callback_data=history_pick_callback(epoch=selection.epoch),
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text=f"Создать · {len(selection.chosen)}",
                callback_data=go_callback(epoch=selection.epoch),
            )
        ]
    )
    # There was no Back and no Cancel anywhere on this screen. The only exits
    # were «Готово» — which creates bots — and typing a slash command, which
    # nothing on the screen mentioned.
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=BACK_TO_ADD)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --------------------------------------------------------------------- results

_MARKS = {
    ItemState.HEALTHY: DONE,
    ItemState.FAILED_PERMANENT: BROKEN,
    ItemState.FAILED_RETRYABLE: BROKEN,
}


def progress_text(entries: list[JournalEntry]) -> str:
    """The same message, redrawn as the batch walks. Never a second message."""
    done = sum(1 for entry in entries if entry.finished)
    lines = [esc(f"{done} из {len(entries)}"), ""]
    for entry in entries:
        mark = _MARKS.get(entry.state)
        if mark is None:
            mark = BUSY if entry.state is not ItemState.PENDING else TODO
        if entry.state is ItemState.AWAITING_CONFIRMATION:
            lines.append(f"{mark} {esc(entry.title)} — подтвердите создание")
            continue
        suffix = f" — {esc(entry.error)}" if entry.error else ""
        lines.append(f"{mark} {esc(entry.title)}{suffix}")
    if any(entry.state is ItemState.AWAITING_CONFIRMATION for entry in entries):
        lines += ["", "<i>Не закрывайте Telegram — нужно подтвердить создание бота.</i>"]
    return title("Создаём мосты", *lines)


def progress_markup(
    entries: list[JournalEntry], *, link_for: Callable[[JournalEntry], str | None]
) -> InlineKeyboardMarkup | None:
    """The button that opens Telegram's own bot-creation dialog.

    A URL button, not a callback: the dialog is Telegram's, the username and
    name are already in the link, and the owner only has to press Create. The
    answer comes back as a `managed_bot` update, not as a tap on this keyboard.
    """
    rows = [
        [InlineKeyboardButton(text=f"Подтвердить создание · {entry.title[:36]}", url=link)]
        for entry in entries
        if entry.state is ItemState.AWAITING_CONFIRMATION
        and (link := link_for(entry)) is not None
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def bot_link(username: str, bot_id: int | None = None) -> str:
    """Where "open the chat" points. By username, and `bot_id` is not used.

    It was pointed at `tg://user?id=` for one deploy, to route around a real
    client-side defect: a bot deleted and rebuilt at the same deterministic name
    left every `t.me/` tap opening the dead peer. tdesktop answers
    `resolveUsername` from `peerByUsername`, which checks nothing but
    `isLoaded()`, and TDLib keeps the mapping `USERNAME_CACHE_EXPIRE_TIME =
    86400` seconds and returns it *even when expired*.

    Telegram refuses the cure. Bot API allows `tg://user?id=` in a button to
    "mention a **user** by their identifier" — a bot is not one, the button is
    rejected, and because the whole keyboard is rejected with it the guardian's
    «Мосты» screen would not draw at all.

    `bot_id` is kept in the signature deliberately. Every caller has it, and the
    day Telegram accepts an id-shaped button this is the one line to change.
    """
    return f"https://t.me/{username}"


#: The one thing left for the owner to do, and the reason it cannot be done for
#: them: a bot may not open a conversation, and Bot API has no method that does.
OPEN_THE_CHAT = (
    "Осталось одно: откройте чат с каждым ботом "
    "и нажмите в нём <b>Старт</b> — без этого бот не сможет написать вам первым."
)


def result_text(entries: list[JournalEntry], *, imported: int | None = None) -> str:
    """A success is only claimed when every selected bridge answered.

    Nothing is sent beside this. The progress screen becomes the result in the
    same message, so the chat holds one account of the run rather than four that
    contradict each other.
    """
    done = [entry for entry in entries if entry.finished]
    if len(done) == len(entries) and entries:
        lines = [f"Создано мостов: {len(done)}", ""]
        lines += [f"{DONE} {esc(entry.title)}" for entry in entries]
        if imported is not None:
            lines.append(f"{DONE} Подтянуто сообщений: {imported}")
        if any(entry.needs_open for entry in done):
            # Said once, here, instead of one message per bot. The buttons below
            # are what fixes it, and they open the chat on its Start button.
            lines += ["", OPEN_THE_CHAT]
        else:
            lines += ["", "Сообщения будут передаваться автоматически."]
        return title("Готово 🎉", *lines)

    lines = [""]
    for entry in entries:
        if entry.finished:
            lines.append(f"{DONE} {esc(entry.title)}")
        else:
            reason = entry.error or "создание не завершено"
            lines.append(f"{BROKEN} {esc(entry.title)} — {esc(reason)}")
    if any(entry.needs_open for entry in done):
        lines += ["", OPEN_THE_CHAT]
    return title(f"Создано {len(done)} из {len(entries)} мостов", *lines)


def result_markup(
    entries: list[JournalEntry], *, epoch: int, history_done: bool = False
) -> InlineKeyboardMarkup:
    """Selection buttons become links; nothing tappable refers to the old list.

    Whether the link carries `?start=` depends on whether anybody has sent one.
    A bot cannot open a chat with a person, so on the managed path the owner's
    tap *is* the `/start` — and the link goes to `?start=`, which opens the chat
    on its Start button rather than on an empty conversation.

    The owner's own session can send it, and does. Then the same button asks for
    a second one: the owner reported a chat reading Start, then the imported
    history, then Start again. So the link is plain, and the label stops asking
    for a step that has already happened.

    `needs_open` is the discriminator and it is already written down — the batch
    sets it exactly when `/start` could not be sent.
    """
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=(
                    f"Открыть и нажать Старт · {entry.title[:30]}"
                    if entry.needs_open
                    else f"Открыть чат · {entry.title[:34]}"
                ),
                url=(
                    start_link(entry.expected_username)
                    if entry.needs_open
                    else bot_link(entry.expected_username, entry.telegram_bot_id)
                ),
            )
        ]
        for entry in entries
        if entry.finished
    ]

    for entry in entries:
        if entry.state is ItemState.FAILED_RETRYABLE:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"Повторить · {entry.title[:40]}",
                        callback_data=retry_callback(entry.max_chat_id, epoch=epoch),
                    )
                ]
            )

    if any(entry.finished for entry in entries) and not history_done:
        # History is offered only once something is actually carrying messages,
        # and not at all when the tick in the picker already pulled it in.
        rows.append(
            [
                InlineKeyboardButton(text="Подтянуть историю", callback_data=HISTORY_ALL),
                InlineKeyboardButton(text="Не сейчас", callback_data=HISTORY_NO),
            ]
        )
    # The way onwards, which this screen did not have at all. It ended a
    # provisioning run with links into the new chats, «Ещё контакт», and no
    # route to «Мосты» or to the panel — so the last frame of the most common
    # successful flow in the whole guardian was a dead end unless the owner
    # wanted to immediately make another bridge.
    onwards = [InlineKeyboardButton(text="Мосты", callback_data=BRIDGES)]
    if any(entry.finished for entry in entries):
        # Only when something came up. Offering «сделать ещё один» under a run
        # where nothing worked is the wrong next step to put in front of
        # somebody.
        onwards.append(
            InlineKeyboardButton(text="➕ Ещё контакт", callback_data=PICK_MORE)
        )
    rows.append(onwards)
    rows.append([InlineKeyboardButton(text="← Панель", callback_data=HOME)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
