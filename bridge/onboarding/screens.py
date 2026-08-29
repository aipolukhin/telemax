"""What the guardian bot says, and the buttons under it.

Telegram is not a terminal: box drawing wraps into rubble on a phone, and a
monospace table is unreadable at any width. So every screen here is a bold
heading, five or six short lines, a leading glyph from `design`, and inline
buttons — and the whole guardian, onboarding *and* control, lives in one message
that gets edited rather than a scroll of status updates.

Five rules the screens follow:

* **the first screen answers three questions.** Is Telemax working, are Telegram
  and MAX working, and does the owner have to do something. Bot slots, free
  slots, the Telegram-capacity caveat, the timezone and the own-message toggle
  were all on it; every one of them is an answer to a question nobody opens this
  bot to ask, and each pushed the three that matter further down the message.
* **one main action per screen.** The primary button is alone on its row; the
  secondary ones share the next; the way back is last, and it names where it
  goes — `← Мосты`, `← Настройки`, `← Мама` — because there is no navigation
  stack for the owner to reason about, only a fixed hierarchy.
* **a dangerous action is never next to an ordinary one.** «Отключить мост» and
  «Снести мост целиком 🗑» used to be adjacent rows on the bridge card, one
  reversible and one not, separated by an emoji. Both live a level in now, and
  the confirmation button names the action rather than agreeing with a question.
* **nothing technical is shown for its own sake.** `Europe/Moscow` is a value
  the code needs; `Москва · UTC+03:00` is the answer the owner asked for. A raw
  epoch, a stored exception, a supervised task's name and an enum's `.value`
  have exactly one home: Diagnostics, which says on the tin that it is technical.
* **every stable screen has a way off it.** Transient «⏳ …» frames have no
  keyboard by design; a *final* frame with nothing to tap leaves the owner
  typing `/menu` to escape their own bot.

Nothing in this module knows how to do anything. It renders a view model built
in `views.py`; the state machine and the runtime decide.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bridge.onboarding.views import (
    BridgeUiState,
    BridgeView,
    HomeState,
    HomeView,
    UserProblem,
)
from bridge.provisioning.byphone import ADD as ADD_CONTACT
from bridge.provisioning.picker import OPEN as DIALOGS
from bridge.telegram import humanise
from bridge.telegram.design import ATTENTION, BROKEN, BUSY, DONE, RUNNING, TODO, bold, esc, title

# One namespace for every button this package owns, so a stale callback from an
# older screen is recognisable rather than mysterious. Dialog picking is not
# one of them: that keyboard belongs to provisioning, and pointing at its
# callback is better than a second implementation of the same list.
NS = "onb"

BEGIN = f"{NS}:begin"
CANCEL = f"{NS}:cancel"
RETRY = f"{NS}:retry"
LAUNCH = f"{NS}:launch"
STATUS = f"{NS}:status"
RESTART = f"{NS}:restart"
RESTART_YES = f"{NS}:restart:yes"
RESTART_NO = f"{NS}:restart:no"

#: The timezone, asked here rather than in the console. A terminal cannot know
#: what a phone's clock says, and the owner is holding the phone.
TZ_AUTO = f"{NS}:tz:auto"
TZ_LIST = f"{NS}:tz:list"
TZ_SET = f"{NS}:tz:set"

#: Reusing the number the console already asked for, instead of asking twice.
PHONE_SAME = f"{NS}:phone:same"
PHONE_OTHER = f"{NS}:phone:other"

MENU = f"{NS}:menu"
#: Everything that is a preference rather than a fact about right now. The home
#: screen used to carry two of these — the own-message toggle and the timezone —
#: which is how a screen that should answer «работает ли оно» came to answer
#: «в каком вы часовом поясе» as well.
SETTINGS = f"{NS}:settings"
SETTINGS_OWN = f"{NS}:settings:own"
SETTINGS_TZ = f"{NS}:settings:tz"
#: Picking a zone from settings rather than from onboarding: the same write and
#: the same restart, and afterwards the owner is back in settings instead of on
#: the "MAX подключён" screen that onboarding ends with.
SETTINGS_TZ_SET = f"{NS}:settings:tz:set"
#: Where `/status` now lands. The old status screen is still here, one level in,
#: as «Технические данные» — it was never a page a person could read, and it was
#: the only page the guardian offered.
DIAGNOSTICS = f"{NS}:diag"
DIAGNOSTICS_PART = f"{NS}:diag:part"
#: What needs doing, if anything does. Shown on home only when it is not empty.
PROBLEMS = f"{NS}:problems"
#: One problem, opened from the list. The tail is `job:41`, `prov:-682…` or
#: `kind:max-offline` — an operator's number, carried in the callback where
#: nobody reads it, instead of printed on the screen as the owner's only handle.
PROBLEM = f"{NS}:problems:one"
#: One family of problems, paginated. The tail is a short slug and a page
#: number: `onb:problems:cat:amb:3`. The kind's own value would spend a
#: third of the 64-byte callback budget on the word "delivery".
PROBLEM_CATEGORY = f"{NS}:problems:cat"
#: The three answers to a stuck delivery, each mapped onto the operation that
#: already existed: `retry_now`, `resolve`, `archive`. Nothing new happens to a
#: job because it was reached by a button rather than by typing `/retry 41`.
JOB_RETRY = f"{NS}:job:retry"
JOB_SETTLE = f"{NS}:job:done"
JOB_ARCHIVE = f"{NS}:job:drop"
#: And the two for a provisioning attempt: the same reconciliation `/provretry`
#: runs, and the same journal note `/provabandon` writes.
ATTEMPT_RETRY = f"{NS}:prov:go"
ATTEMPT_DROP = f"{NS}:prov:stop"
#: The home toggle for carrying the owner's own MAX messages into Telegram. Off
#: by default. The toggle moved into settings; this is the callback the settings
#: screen's two radio rows carry.
OWN_MSGS = f"{NS}:own"
OWN_SET = f"{NS}:own:set"
BRIDGES = f"{NS}:bridges"
#: One bridge's card, and the disconnect behind it. Two separate callbacks with
#: a confirmation between them: the dangerous action is never a neighbour of an
#: ordinary one.
BRIDGE = f"{NS}:bridge"
BRIDGE_OFF = f"{NS}:bridge:off"
BRIDGE_OFF_YES = f"{NS}:bridge:off:yes"
#: The step after disconnecting: the bot itself, which only @BotFather can
#: remove. Offered rather than done, because doing it needs the owner's account.
BRIDGE_FREE = f"{NS}:bridge:free"
#: Actually delete the bot, over the owner's session. Irreversible and it frees
#: a slot, so it sits behind its own revision-guarded confirmation — the same
#: shape as the repull, for the same reason.
BRIDGE_FREE_YES = f"{NS}:bridge:free:yes"
#: Wipe what was delivered and pull the conversation again. Destructive, so it
#: sits behind its own screen and its own revision-guarded confirmation.
BRIDGE_REPULL = f"{NS}:bridge:repull"
BRIDGE_REPULL_YES = f"{NS}:bridge:repull:yes"
#: One level under the card, so the card itself carries no destructive row.
BRIDGE_SETTINGS = f"{NS}:bridge:cfg"
#: The per-bridge history operations, which are not the same as its settings.
BRIDGE_HISTORY = f"{NS}:bridge:hist"
#: Raw values for one bridge. Reached from «Подробнее» on a broken card, and
#: the only bridge screen allowed to print an exception.
BRIDGE_TECH = f"{NS}:bridge:tech"

#: Said in a toast when a button belongs to a screen that has moved on.
STALE_SCREEN = "Экран уже обновился — посмотрите, что на нём сейчас."

__all__ = [
    "ADD_CONTACT",
    "ATTEMPT_DROP",
    "ATTEMPT_RETRY",
    "BEGIN",
    "BRIDGE",
    "BRIDGES",
    "BRIDGE_FREE",
    "BRIDGE_FREE_YES",
    "BRIDGE_HISTORY",
    "BRIDGE_OFF",
    "BRIDGE_OFF_YES",
    "BRIDGE_REPULL",
    "BRIDGE_REPULL_YES",
    "BRIDGE_SETTINGS",
    "BRIDGE_TECH",
    "CANCEL",
    "DIAGNOSTICS",
    "DIAGNOSTICS_PART",
    "DIALOGS",
    "JOB_ARCHIVE",
    "JOB_RETRY",
    "JOB_SETTLE",
    "LAUNCH",
    "MENU",
    "OWN_MSGS",
    "OWN_SET",
    "PHONE_OTHER",
    "PHONE_SAME",
    "PROBLEM",
    "PROBLEMS",
    "PROBLEM_CATEGORY",
    "RESTART",
    "RESTART_NO",
    "RESTART_YES",
    "RETRY",
    "SETTINGS",
    "SETTINGS_OWN",
    "SETTINGS_TZ",
    "SETTINGS_TZ_SET",
    "STALE_SCREEN",
    "STATUS",
    "TZ_AUTO",
    "TZ_LIST",
    "TZ_SET",
    "Screen",
    "ambiguous_job_screen",
    "ask_code",
    "ask_max_phone",
    "ask_password",
    "attempt_screen",
    "bridge_callback",
    "bridge_free_callback",
    "bridge_free_yes_callback",
    "bridge_history_callback",
    "bridge_history_screen",
    "bridge_off_callback",
    "bridge_off_yes_callback",
    "bridge_repull_callback",
    "bridge_repull_yes_callback",
    "bridge_screen",
    "bridge_settings_callback",
    "bridge_settings_screen",
    "bridge_tech_callback",
    "bridge_tech_screen",
    "bridges_screen",
    "cancelled",
    "category_back",
    "category_callback",
    "category_screen",
    "confirm_timezone",
    "diagnostics",
    "diagnostics_part_callback",
    "diagnostics_section",
    "disconnect_confirm",
    "disconnected",
    "failed_job_screen",
    "free_slot",
    "handoff_invite",
    "home",
    "job_callback",
    "launch_failed",
    "login_failed",
    "offer_telegram_phone",
    "own_messages",
    "own_set_callback",
    "parse_bridge",
    "parse_bridge_free",
    "parse_bridge_free_yes",
    "parse_bridge_history",
    "parse_bridge_off",
    "parse_bridge_off_yes",
    "parse_bridge_repull",
    "parse_bridge_repull_yes",
    "parse_bridge_settings",
    "parse_bridge_tech",
    "parse_category",
    "parse_diagnostics_part",
    "parse_job",
    "parse_own_set",
    "parse_problem",
    "parse_restart_revision",
    "problem_callback",
    "problem_screen",
    "problems_screen",
    "ready",
    "repull_confirm",
    "repulled",
    "repulling",
    "restart_confirm",
    "restart_yes_callback",
    "restarting",
    "resume_offer",
    "saving",
    "sending_code",
    "settings",
    "settings_timezone_callback",
    "slot_freed",
    "starting",
    "teardown_stalled",
    "tearing_down",
    "technical_details",
    "timezone_list",
    "timezone_offer",
    "timezone_set_callback",
    "timezone_undetectable",
    "validating",
    "welcome",
]


def _keyboard(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


Screen = tuple[str, InlineKeyboardMarkup | None]

BACK_ARROW = "←"
BACK = f"{BACK_ARROW} Назад"
#: Where «← Назад» goes, said out loud. A back button that only says «Назад»
#: makes the owner guess which of three screens they are about to land on, and
#: this guardian has no navigation stack to guess from — the hierarchy is fixed,
#: so the label can simply name the place.
BACK_HOME = f"{BACK_ARROW} Панель"
BACK_BRIDGES = f"{BACK_ARROW} Мосты"
BACK_SETTINGS = f"{BACK_ARROW} Настройки"
BACK_DIAGNOSTICS = f"{BACK_ARROW} Диагностика"


def _back_to(label: str) -> str:
    """`← Мама` — the contact's name is a better signpost than «Назад»."""
    return f"{BACK_ARROW} {label[:24]}"

#: The four steps of onboarding, in the order they are asked. Drawn on the first
#: screen so the owner knows how long this is before starting it.
STEPS = ("Часовой пояс", "Подключение MAX", "Выбор диалогов", "Создание мостов")


def _progress(done: int) -> list[str]:
    """`● Часовой пояс` for the step being asked, `✅`/`○` either side of it."""
    lines = []
    for index, step in enumerate(STEPS):
        if index < done:
            lines.append(f"{DONE} {esc(step)}")
        elif index == done:
            lines.append(f"● {bold(step)}")
        else:
            lines.append(f"{TODO} {esc(step)}")
    return lines


def welcome() -> Screen:
    return (
        title(
            "Настройка Telemax",
            "Соединим личные диалоги MAX",
            "с отдельными чатами Telegram.",
            "",
            f"Шаг 1 из {len(STEPS)}",
            *_progress(0),
        ),
        _keyboard(
            [("Начать настройку", BEGIN)],
            [("Отменить настройку", CANCEL)],
        ),
    )


# ------------------------------------------------------------------- timezone


def timezone_set_callback(name: str) -> str:
    """`onb:tz:set:Europe/Moscow`. Well inside the 64-byte callback limit."""
    return f"{TZ_SET}:{name}"


def settings_timezone_callback(name: str) -> str:
    """The same choice, tapped from settings rather than from onboarding.

    A separate callback only so the handler knows where to put the owner
    afterwards. The write, the validation and the restart are the one existing
    path — `MaxOnboarding.set_timezone` — in both cases.
    """
    return f"{SETTINGS_TZ_SET}:{name}"


def timezone_offer() -> Screen:
    """First screen of onboarding. Nothing else can be right until this is.

    Telegram renders every timestamp in the *reader's* zone and never tells a
    bot what that is, so a stamped message is either right or an hour out, and
    there is no way to notice which from the server side.
    """
    return (
        title(
            "Часовой пояс",
            "Нужен, чтобы время сообщений из MAX совпадало",
            "с тем, что показывает Telegram.",
        ),
        _keyboard(
            [("Определить автоматически", TZ_AUTO)],
            [("Выбрать из списка", TZ_LIST)],
        ),
    )


def confirm_timezone(name: str, offset: str, *, city: str | None = None) -> Screen:
    """The human answer leads; the IANA name is the quiet second line.

    `Europe/Moscow` is what the config stores and what nobody asked for. The
    owner asked what time it is, and «Москва · UTC+03:00» is that answer.
    """
    headline = f"{city or name.rsplit('/', 1)[-1].replace('_', ' ')} · {offset}"
    return (
        title(
            "Часовой пояс",
            "Определён автоматически:",
            bold(headline),
            f"<i>{esc(name)}</i>",
        ),
        _keyboard(
            [("Подтвердить", timezone_set_callback(name))],
            [("Выбрать другой", TZ_LIST)],
        ),
    )


def timezone_list(
    choices: list[tuple[str, str]],
    *,
    back: str | None = None,
    current: str | None = None,
    callback: Callable[[str], str] = timezone_set_callback,
) -> Screen:
    """`choices` is `(label, IANA name)`, already ordered west to east.

    Two entry points, and they owe the owner different things. During onboarding
    this is the first question and there is nothing behind it to go back to.
    Opened from settings it is a change to something already answered, so it says
    what the answer currently is and it has a way out — reaching this screen and
    being unable to leave without picking a city was the one navigation dead end
    an owner could hit without doing anything unusual.
    """
    rows = [[(label, callback(name))] for label, name in choices]
    if back is not None:
        rows.append([(f"{BACK_ARROW} Настройки", back)])
    return (
        title(
            "Часовой пояс",
            *([f"Сейчас: {esc(current)}", ""] if current else []),
            "Выберите ближайший город.",
        ),
        _keyboard(*rows),
    )


def timezone_undetectable() -> Screen:
    return (
        title(
            "Часовой пояс",
            "Определить автоматически не вышло.",
            "Выберите из списка.",
        ),
        _keyboard([("Выбрать из списка", TZ_LIST)]),
    )


# ---------------------------------------------------------------------- phone


def offer_telegram_phone(masked: str) -> Screen:
    """The console already asked for a number; asking again is the bug.

    Only the masked form is ever drawn: this message sits in a chat history for
    as long as the owner keeps it.
    """
    return (
        title(
            "Подключение MAX",
            "Для Telegram используется номер:",
            bold(masked),
            "",
            "Он же привязан к MAX?",
        ),
        _keyboard(
            [("Да, использовать его", PHONE_SAME)],
            [("Указать другой номер", PHONE_OTHER)],
            [("Отменить настройку", CANCEL)],
        ),
    )


def ask_max_phone() -> Screen:
    return (
        title(
            "Подключение MAX",
            "Отправьте номер телефона, привязанный к MAX,",
            "в международном виде: +79001234567.",
            "",
            "<i>Сообщение будет удалено сразу после обработки.</i>",
        ),
        _keyboard([("Отменить настройку", CANCEL)]),
    )


def sending_code() -> Screen:
    return (title("Подключение MAX", f"{BUSY} Запрашиваю код у MAX…"), None)


def ask_code(*, again: bool = False) -> Screen:
    if again:
        return (
            title(
                "Код не подошёл",
                "Проверьте код или запросите новый.",
                "",
                "Отправьте код следующим сообщением.",
                "<i>Сообщение будет удалено сразу после обработки.</i>",
            ),
            _keyboard(
                [("Запросить новый код", RETRY)],
                [("Отменить настройку", CANCEL)],
            ),
        )
    return (
        title(
            "Подключение MAX",
            f"{DONE} Номер принят",
            "MAX отправил код подтверждения.",
            "",
            "Отправьте код следующим сообщением.",
            "<i>Сообщение будет удалено сразу после обработки.</i>",
        ),
        _keyboard([("Отменить настройку", CANCEL)]),
    )


def ask_password(*, again: bool = False, hint: str | None = None) -> Screen:
    lines = [f"{DONE} Номер принят", f"{DONE} Код подтверждён"]
    if again:
        lines.append(f"{BROKEN} Пароль не подошёл.")
    lines += ["", "У аккаунта включён второй фактор. Отправьте пароль MAX."]
    if hint:
        lines.append(f"Подсказка: {esc(hint)}")
    lines += ["", "<i>Сообщение будет удалено сразу после обработки.</i>"]
    return (
        title("Подключение MAX", *lines),
        _keyboard([("Отменить настройку", CANCEL)]),
    )


def validating() -> Screen:
    return (
        title("Подключение MAX", f"{DONE} Данные приняты", f"{BUSY} Проверяю сессию MAX…"),
        None,
    )


def saving() -> Screen:
    return (
        title(
            "Подключение MAX",
            f"{DONE} Аккаунт авторизован",
            f"{DONE} Сессия проверена",
            f"{BUSY} Сохраняю конфигурацию…",
        ),
        None,
    )


def starting() -> Screen:
    return (
        title(
            "Подключение MAX",
            f"{DONE} Аккаунт авторизован",
            f"{DONE} Сессия проверена",
            f"{DONE} Конфигурация сохранена",
            f"{BUSY} Запускаю мост…",
        ),
        None,
    )


def _readable_timezone(name: str | None) -> str | None:
    """«Москва · UTC+03:00» rather than `Europe/Moscow`.

    The IANA name is what the config stores. It appeared verbatim on this screen
    and in `/status` while the home screen said the human form for the same
    value, which reads as two different settings.
    """
    if not name:
        return None
    from bridge.bootstrap.timezones import human_label, is_known_timezone

    return human_label(name) if is_known_timezone(name) else name


def ready(*, timezone: str | None) -> Screen:
    """MAX is connected. One thing left, and it is the button under this."""
    lines = [f"{DONE} Аккаунт авторизован", f"{DONE} Мост запущен"]
    readable = _readable_timezone(timezone)
    if readable:
        lines.append(f"{DONE} Часовой пояс: {esc(readable)}")
    lines += ["", "Теперь выберите диалоги для переноса."]
    return (
        title("MAX подключён", *lines),
        _keyboard(
            [("Выбрать диалоги", DIALOGS)],
            [(BACK_HOME, MENU)],
        ),
    )


def login_failed(reason: str) -> Screen:
    return (
        title("Не удалось подключить MAX", esc(reason)),
        _keyboard(
            [("Попробовать снова", RETRY)],
            [("Отменить настройку", CANCEL)],
        ),
    )


def launch_failed(reason: str) -> Screen:
    return (
        title(
            "Telemax не смог запуститься",
            "MAX подключён, но доставка не поднялась.",
            "",
            esc(reason),
        ),
        _keyboard(
            [("Повторить запуск", LAUNCH)],
            [("Диагностика", DIAGNOSTICS)],
        ),
    )


def cancelled() -> Screen:
    return (
        title(
            "Настройка отменена",
            "Ничего не сохранено.",
            "Когда будете готовы — нажмите кнопку ниже.",
        ),
        _keyboard([("Начать настройку", BEGIN)]),
    )


def resume_offer() -> Screen:
    return (
        title("Настройка не завершена", "Подключение MAX осталось незаконченным.", "Продолжим?"),
        _keyboard(
            [("Продолжить настройку", BEGIN)],
            [("Отменить настройку", CANCEL)],
        ),
    )


def handoff_invite() -> Screen:
    """Sent by the bot itself when a chat with the owner already exists."""
    return (
        title("Telemax готов к настройке", "Нажмите кнопку ниже, чтобы подключить MAX."),
        _keyboard([("Начать настройку", BEGIN)]),
    )


# ---------------------------------------------------------------------- home


#: The own-message setting's two faces, as radio rows rather than a toggle. A
#: toggle that shows its *current* state is read by half of everybody as showing
#: what it will do when pressed, and this one had to be right the first time.
OWN_MSGS_ON = "Переносить"
OWN_MSGS_OFF = "Не переносить"


def _link_lines(view: HomeView) -> list[str]:
    """Are Telegram and MAX working — question two of the three."""
    return [
        f"Telegram {'●' if view.telegram_connected else '○'}"
        f" {'подключён' if view.telegram_connected else 'нет связи'}",
        f"MAX {'●' if view.max_connected else '○'}"
        f" {'подключён' if view.max_connected else 'нет связи'}",
    ]


def _load_lines(view: HomeView) -> list[str]:
    """Question three: is there anything for the owner to do.

    Healthy installs get one line with the queue folded into it. The moment
    there is something waiting the numbers separate, because "5 мостов · очередь
    пуста" and "28 сообщений ждут отправки" are not the same news.
    """
    bridges = humanise.count(view.bridges, "мост", "моста", "мостов")
    if view.state is HomeState.HEALTHY and not view.queued:
        return [f"{bridges} · очередь пуста"]
    lines = [bridges]
    if view.queued:
        lines.append(
            f"{humanise.count(view.queued, 'сообщение', 'сообщения', 'сообщений')}"
            f" {humanise.plural(view.queued, 'ждёт', 'ждут', 'ждут')} отправки"
        )
    if view.action_required:
        lines.append(
            f"{humanise.count(view.action_required, 'сообщение', 'сообщения', 'сообщений')}"
            f" {humanise.plural(view.action_required, 'требует', 'требуют', 'требуют')} решения"
        )
    return lines


def home(view: HomeView) -> Screen:
    """Where `/start`, `/menu` and every «← Панель» land once MAX is connected.

    Three questions and nothing else. Is Telemax working, are Telegram and MAX
    working, and does the owner have to do something. Everything the old home
    screen carried besides those — bot slots, the free-slot count, the "Telegram
    does not tell us the limit" caveat, the timezone and the own-message toggle —
    is still computed and still reachable; none of it is an answer to a question
    somebody opens this bot to ask.

    Not a second panel either: this is the same anchor message, redrawn.
    """
    rows: list[list[tuple[str, str]]] = []
    if view.problems:
        # Only when it is not empty. A permanent «Проблемы · 0» is a button that
        # teaches the owner to stop looking at that corner of the screen.
        rows.append([(f"⚠️ Проблемы · {view.problems}", PROBLEMS)])
    rows += [
        # One entry, three ways in behind it: a MAX dialog, a Telegram contact
        # card, or a number typed by hand. The picker is no longer the only
        # door, because the people this bridge exists for have no dialogs to
        # pick from yet.
        [("Мосты", BRIDGES), ("➕ Добавить", ADD_CONTACT)],
        [("Настройки", SETTINGS)],
    ]
    return (
        title(
            "Telemax",
            f"{view.glyph} {esc(view.headline)}",
            "",
            *_link_lines(view),
            "",
            *_load_lines(view),
        ),
        _keyboard(*rows),
    )


def problem_callback(problem: UserProblem) -> str:
    return f"{PROBLEM}:{problem.key}"


def parse_problem(data: str) -> tuple[str, str] | None:
    """`onb:problems:one:job:41` -> `("job", "41")`."""
    if not data.startswith(f"{PROBLEM}:"):
        return None
    tail = data[len(PROBLEM) + 1 :]
    head, _, rest = tail.partition(":")
    return (head, rest) if head and rest else None


def job_callback(prefix: str, job_id: int) -> str:
    return f"{prefix}:{job_id}"


def parse_job(data: str, prefix: str) -> int | None:
    parsed = _tail_ints(data, prefix, 1)
    return parsed[0] if parsed else None


#: Short labels for the singleton rows on the index. The families that can have
#: many members take their label from `views.CATEGORY_LABELS` instead.
_PROBLEM_LABELS = {
    "max-offline": "Связь с MAX",
    "database": "Сохранение событий",
    "owner-session": "Ваш Telegram",
    "queue-stalled": "Очередь",
    "degraded": "Telemax",
    "bridge-down": "Мосты",
    "provisioning": "Создание моста",
    # Two rows for one contact are common — one send that failed and one whose
    # result is unknown — so the label has to say which is which. «Сообщение ·
    # Мама» twice is a list the owner has to tap through to read.
    "delivery-failed": "Не отправлено",
    "delivery-ambiguous": "Неясно",
}


def category_callback(kind: Any, page: int = 0) -> str:
    """`onb:problems:cat:amb:3` — a slug, because 64 bytes is the whole budget."""
    from bridge.onboarding.views import CATEGORY_SLUGS

    return f"{PROBLEM_CATEGORY}:{CATEGORY_SLUGS[kind]}:{page}"


def parse_category(data: str) -> tuple[Any, int] | None:
    """`onb:problems:cat:amb:3` -> `(ProblemKind.DELIVERY_AMBIGUOUS, 3)`."""
    from bridge.onboarding.views import KIND_BY_SLUG

    if not data.startswith(f"{PROBLEM_CATEGORY}:"):
        return None
    slug, _, page = data[len(PROBLEM_CATEGORY) + 1 :].partition(":")
    kind = KIND_BY_SLUG.get(slug)
    if kind is None or not page.isascii() or not page.isdigit():
        return None
    return kind, int(page)


def _index_body(groups: list[Any]) -> list[str]:
    """The index's own words: what is wrong, in totals rather than in rows.

    «98 сообщений требуют решения» is what the owner reads before deciding which
    of the two questions to answer first. The categories underneath carry the
    exact split, so this line never has to be precise about which is which.
    """
    from bridge.onboarding.views import ProblemKind

    body: list[str] = []
    decisions = sum(
        group.count
        for group in groups
        if group.kind in {ProblemKind.DELIVERY_FAILED, ProblemKind.DELIVERY_AMBIGUOUS}
    )
    if decisions:
        body.append(
            f"{humanise.count(decisions, 'сообщение', 'сообщения', 'сообщений')}"
            f" {humanise.plural(decisions, 'требует', 'требуют', 'требуют')} решения"
        )
    building = next(
        (group.count for group in groups if group.kind is ProblemKind.PROVISIONING), 0
    )
    if building:
        body.append(
            f"{humanise.count(building, 'мост', 'моста', 'мостов')}"
            f" {humanise.plural(building, 'не создан', 'не созданы', 'не созданы')}"
        )
    # Everything that is not a per-item family says its own piece, because there
    # is only ever one of each and it has something to explain.
    for group in groups:
        if group.paged:
            continue
        problem = group.items[0]
        if body:
            body.append("")
        body.append(f"{problem.glyph} {esc(problem.title)}")
        body.extend(esc(line) for line in problem.lines if line)
    return body


def problems_screen(groups: list[Any]) -> Screen:
    """The index. Bounded by *kinds*, never by how many jobs are stuck.

    One row per job used to be the whole of this screen. That is fine at three
    problems and a message Telegram refuses to send at a hundred: eighty-two
    ambiguous sends and sixteen failed ones came to 8173 characters and 101
    keyboard rows against limits of 4096 and 100, so the screen the owner most
    needed simply did not open. Eighty-two is not an unusual number after a bad
    afternoon.

    So the families that can have many members get a row with a count and a
    paginated list behind it, and the ones there is only ever one of keep their
    own row. Nothing is hidden: every job is still reachable, two taps in
    instead of one.
    """
    if not groups:
        return (
            title("Проблем нет", "Всё работает, делать ничего не нужно."),
            _keyboard([(BACK_HOME, MENU)]),
        )
    rows: list[list[tuple[str, str]]] = []
    for group in groups:
        if group.paged:
            label = f"{group.glyph} {group.label} · {group.count}"
            rows.append([(label, category_callback(group.kind))])
            continue
        problem = group.items[0]
        label = f"{group.glyph} {group.label}"
        if problem.contact:
            label = f"{label} · {problem.contact[:20]}"
        rows.append([(label, problem_callback(problem))])
    rows.append([(BACK_HOME, MENU)])
    return (title("Проблемы", *_index_body(groups)), _keyboard(*rows))



def category_back(group: Any, page: int = 0) -> tuple[str, str]:
    """The «← Неясные отправки» a per-job screen carries back to its page."""
    return (f"{BACK_ARROW} {group.heading}", category_callback(group.kind, page))


def category_screen(group: Any, page: int = 0) -> Screen:
    """One family, one page. Every row opens the job it stands for.

    The pager is drawn only when there is more than one page, so a category with
    three items looks like a list of three things rather than a paginated
    report.
    """
    from bridge.onboarding.views import PAGE_SIZE

    pages = group.pages
    page = max(0, min(page, pages - 1))
    rows: list[list[tuple[str, str]]] = []
    for problem in group.page(page):
        label = problem.contact or problem.title
        if problem.when:
            label = f"{label} · {problem.when}"
        rows.append([(label[:60], problem_callback(problem))])

    if pages > 1:
        pager: list[tuple[str, str]] = []
        if page > 0:
            pager.append(("◀", category_callback(group.kind, page - 1)))
        pager.append((f"{page + 1} / {pages}", category_callback(group.kind, page)))
        if page < pages - 1:
            pager.append(("▶", category_callback(group.kind, page + 1)))
        rows.append(pager)
    rows.append([(f"{BACK_ARROW} Проблемы", PROBLEMS)])

    first = page * PAGE_SIZE
    body = [
        f"{humanise.count(group.count, 'сообщение', 'сообщения', 'сообщений')}"
        f" {humanise.plural(group.count, 'требует', 'требуют', 'требуют')} решения"
        if group.kind.value.startswith("delivery")
        else f"{humanise.count(group.count, 'мост', 'моста', 'мостов')}"
        f" {humanise.plural(group.count, 'не создан', 'не созданы', 'не созданы')}"
    ]
    if pages > 1:
        body.append(f"Показаны {first + 1}–{first + len(group.page(page))}.")
    return (title(group.heading, *body), _keyboard(*rows))


def problem_screen(
    problem: UserProblem, *, back: tuple[str, str] | None = None
) -> Screen:
    """One problem the owner cannot do anything about, said in three parts.

    What happened, what Telemax is doing, and whether anything is wanted. The
    third part is the one that used to be missing: «⚠️ Очередь выросла: 27
    сообщений ждут отправки» is a fact with no verb, and an owner reading it at
    two in the morning has no way to know it is already handled.
    """
    return (
        title(
            f"{problem.glyph} {problem.title}",
            *(
                [f"Контакт: {bold(problem.contact)}", ""]
                if problem.contact
                else []
            ),
            *[esc(line) for line in problem.lines if line],
            *(
                ["", "Telemax восстановит связь сам. Делать ничего не нужно."]
                if problem.kind.value in {"max-offline", "database", "queue-stalled"}
                else []
            ),
        ),
        _keyboard([(back or (f"{BACK_ARROW} Проблемы", PROBLEMS))]),
    )


def failed_job_screen(
    problem: UserProblem, *, back: tuple[str, str] | None = None
) -> Screen:
    """A send Telemax knows did not happen. Retry is safe; say so plainly."""
    job_id = problem.job_id or 0
    return (
        title(
            "Сообщение не отправлено",
            f"{bold(problem.contact or 'Контакт')}",
            *[esc(line) for line in problem.lines if line],
            "",
            "Telemax точно знает, что отправка не удалась.",
        ),
        _keyboard(
            [("Повторить", job_callback(JOB_RETRY, job_id))],
            [("Не отправлять", job_callback(JOB_ARCHIVE, job_id))],
            [(back or (f"{BACK_ARROW} Проблемы", PROBLEMS))],
        ),
    )


def ambiguous_job_screen(
    problem: UserProblem, *, back: tuple[str, str] | None = None
) -> Screen:
    """The one state only the owner can settle, and the reason why.

    The send went out and the confirmation did not come back. Retrying puts a
    second copy in a real person's chat, so nothing here retries by itself and
    «Дошло» is the first answer offered — see ADR 0002.
    """
    job_id = problem.job_id or 0
    return (
        title(
            "Неясно, дошло ли сообщение",
            f"{bold(problem.contact or 'Контакт')}",
            *[esc(line) for line in problem.lines if line],
            "",
            "Результат отправки неизвестен.",
            "Повтор может создать дубликат.",
        ),
        _keyboard(
            [("Дошло", job_callback(JOB_SETTLE, job_id))],
            [("Повторить", job_callback(JOB_RETRY, job_id))],
            [("Не отправлять", job_callback(JOB_ARCHIVE, job_id))],
            [(back or (f"{BACK_ARROW} Проблемы", PROBLEMS))],
        ),
    )


def attempt_screen(
    problem: UserProblem, *, back: tuple[str, str] | None = None
) -> Screen:
    """A bridge that was being made and stopped part-way.

    `шаг awaiting_confirmation · timeout · бот 8123456789` was the whole of what
    the owner was told, three enum values and an id, in a message with no
    buttons. The two things they can actually do are here.
    """
    chat = problem.max_chat_id or 0
    return (
        title(
            "Мост не создан",
            f"Контакт: {bold(problem.contact or 'Контакт')}",
            "",
            *[esc(line) for line in problem.lines if line],
            "",
            "Можно продолжить с того места, где остановилось.",
        ),
        _keyboard(
            [("Продолжить", f"{ATTEMPT_RETRY}:{chat}")],
            [("Больше не спрашивать", f"{ATTEMPT_DROP}:{chat}")],
            [(back or (f"{BACK_ARROW} Проблемы", PROBLEMS))],
        ),
    )


# ------------------------------------------------------------------ settings


def own_set_callback(on: bool) -> str:
    return f"{OWN_SET}:{1 if on else 0}"


def parse_own_set(data: str) -> bool | None:
    if not data.startswith(f"{OWN_SET}:"):
        return None
    tail = data[len(OWN_SET) + 1 :]
    return tail == "1" if tail in {"0", "1"} else None


def settings(*, mirror_own: bool, timezone: str | None) -> Screen:
    """Everything that is a preference, in one place, off the first screen.

    There was no settings screen at all. The two settings that existed lived on
    home, the rest lived in `config.yaml`, and «Диагностика» was the whole of
    `/status` presented as if it were a page for a person.
    """
    return (
        title("Настройки"),
        _keyboard(
            [(f"Мои сообщения из MAX · {'✓' if mirror_own else 'выкл'}", SETTINGS_OWN)],
            [(f"Часовой пояс · {timezone or 'не выбран'}", SETTINGS_TZ)],
            [("Диагностика", DIAGNOSTICS)],
            [(BACK_HOME, MENU)],
        ),
    )


def own_messages(*, mirror_own: bool) -> Screen:
    """One setting, said in full, with both answers on the screen at once."""
    return (
        title(
            "Мои сообщения из MAX",
            "Ваши сообщения, отправленные из самого приложения MAX,",
            "будут появляться и в Telegram.",
        ),
        _keyboard(
            [(f"{'✓' if mirror_own else '○'} {OWN_MSGS_ON}", own_set_callback(True))],
            [(f"{'○' if mirror_own else '✓'} {OWN_MSGS_OFF}", own_set_callback(False))],
            [(BACK_SETTINGS, SETTINGS)],
        ),
    )


# --------------------------------------------------------------- diagnostics


#: The four sections of the old `/status` block, named for what they answer.
DIAGNOSTIC_SECTIONS = (
    ("link", "Связь"),
    ("queue", "Очередь"),
    ("sessions", "Сессии"),
    ("media", "Медиа"),
)


def diagnostics_part_callback(section: str) -> str:
    return f"{DIAGNOSTICS_PART}:{section}"


def parse_diagnostics_part(data: str) -> str | None:
    if not data.startswith(f"{DIAGNOSTICS_PART}:"):
        return None
    tail = data[len(DIAGNOSTICS_PART) + 1 :]
    return tail if tail in {key for key, _ in DIAGNOSTIC_SECTIONS} else None


def diagnostics(*, telegram: bool, max_connected: bool, delivery: bool, database: bool) -> Screen:
    """Four lights and the way in behind each of them.

    The old `/status` printed thirty conditional lines into the anchor and was
    the guardian's only page about its own health. It is still here — under
    «Технические данные» — but it is no longer the thing an owner is handed when
    they ask whether the bridge is all right.
    """
    def light(ok: bool) -> str:
        return RUNNING if ok else BROKEN

    return (
        title(
            "Диагностика",
            f"{light(telegram)} Telegram",
            f"{light(max_connected)} MAX",
            f"{light(delivery)} Доставка",
            f"{light(database)} База данных",
        ),
        _keyboard(
            [
                (label, diagnostics_part_callback(key))
                for key, label in DIAGNOSTIC_SECTIONS[:2]
            ],
            [
                (label, diagnostics_part_callback(key))
                for key, label in DIAGNOSTIC_SECTIONS[2:]
            ],
            [("Технические данные", STATUS)],
            [("Перезапустить Telemax…", RESTART)],
            [(BACK_SETTINGS, SETTINGS)],
        ),
    )


def diagnostics_section(heading: str, lines: list[str]) -> Screen:
    """One section of the old status block. Raw values are allowed in here."""
    return (
        title(
            heading,
            *([esc(line) for line in lines] or ["Записывать нечего — всё в норме."]),
        ),
        _keyboard([(BACK_DIAGNOSTICS, DIAGNOSTICS)]),
    )


def technical_details(lines: list[str]) -> Screen:
    """The whole of the old `/status`, unchanged, one level under Diagnostics."""
    return (
        title("Технические данные", *[esc(line) for line in lines]),
        _keyboard([(BACK_DIAGNOSTICS, DIAGNOSTICS)]),
    )


# -------------------------------------------------------------------- restart


def restart_yes_callback(revision: int) -> str:
    """`onb:restart:yes:7` — the revision the confirmation was drawn at."""
    return f"{RESTART_YES}:{revision}"


def parse_restart_revision(data: str) -> int | None:
    """The number back out, or None when this is not a confirmation at all."""
    if not data.startswith(f"{RESTART_YES}:"):
        return None
    tail = data[len(RESTART_YES) + 1 :]
    return int(tail) if tail.isdigit() else None


def restart_confirm(revision: int) -> Screen:
    return (
        title(
            "Перезапустить Telemax?",
            "Доставка прервётся на несколько секунд.",
            "Ничего не потеряется — очередь переживает перезапуск.",
        ),
        _keyboard(
            # The button names the action rather than agreeing with a question
            # the owner has already stopped reading by the time they tap.
            [("Перезапустить", restart_yes_callback(revision))],
            [(f"{BACK_ARROW} Отмена", RESTART_NO)],
        ),
    )


def restarting() -> Screen:
    return (title("Перезапуск", f"{BUSY} Перезапускаю Telemax…"), None)


# -------------------------------------------------------------------- bridges


def bridge_callback(max_chat_id: int) -> str:
    return f"{BRIDGE}:{max_chat_id}"


def bridge_off_callback(max_chat_id: int) -> str:
    return f"{BRIDGE_OFF}:{max_chat_id}"


def bridge_free_callback(max_chat_id: int) -> str:
    return f"{BRIDGE_FREE}:{max_chat_id}"


def bridge_free_yes_callback(max_chat_id: int, revision: int) -> str:
    return f"{BRIDGE_FREE_YES}:{max_chat_id}:{revision}"


def bridge_repull_callback(max_chat_id: int) -> str:
    return f"{BRIDGE_REPULL}:{max_chat_id}"


def bridge_repull_yes_callback(max_chat_id: int, revision: int) -> str:
    return f"{BRIDGE_REPULL_YES}:{max_chat_id}:{revision}"


def bridge_off_yes_callback(max_chat_id: int, revision: int) -> str:
    return f"{BRIDGE_OFF_YES}:{max_chat_id}:{revision}"


def bridge_settings_callback(max_chat_id: int) -> str:
    return f"{BRIDGE_SETTINGS}:{max_chat_id}"


def bridge_history_callback(max_chat_id: int) -> str:
    return f"{BRIDGE_HISTORY}:{max_chat_id}"


def bridge_tech_callback(max_chat_id: int) -> str:
    return f"{BRIDGE_TECH}:{max_chat_id}"


def _tail_ints(data: str, prefix: str, count: int) -> tuple[int, ...] | None:
    if not data.startswith(f"{prefix}:"):
        return None
    parts = data[len(prefix) + 1 :].split(":")
    if len(parts) != count or not all(part.lstrip("-").isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def parse_bridge(data: str) -> int | None:
    """`onb:bridge:123` -> 123. Refuses the `:off` forms, which are longer."""
    parsed = _tail_ints(data, BRIDGE, 1)
    return parsed[0] if parsed else None


def parse_bridge_off(data: str) -> int | None:
    parsed = _tail_ints(data, BRIDGE_OFF, 1)
    return parsed[0] if parsed else None


def parse_bridge_free_yes(data: str) -> tuple[int, int] | None:
    parsed = _tail_ints(data, BRIDGE_FREE_YES, 2)
    return (parsed[0], parsed[1]) if parsed else None


def parse_bridge_free(data: str) -> int | None:
    parsed = _tail_ints(data, BRIDGE_FREE, 1)
    return parsed[0] if parsed else None


def parse_bridge_repull(data: str) -> int | None:
    parsed = _tail_ints(data, BRIDGE_REPULL, 1)
    return parsed[0] if parsed else None


def parse_bridge_repull_yes(data: str) -> tuple[int, int] | None:
    parsed = _tail_ints(data, BRIDGE_REPULL_YES, 2)
    return (parsed[0], parsed[1]) if parsed else None


def parse_bridge_off_yes(data: str) -> tuple[int, int] | None:
    parsed = _tail_ints(data, BRIDGE_OFF_YES, 2)
    return (parsed[0], parsed[1]) if parsed else None


def parse_bridge_settings(data: str) -> int | None:
    parsed = _tail_ints(data, BRIDGE_SETTINGS, 1)
    return parsed[0] if parsed else None


def parse_bridge_history(data: str) -> int | None:
    parsed = _tail_ints(data, BRIDGE_HISTORY, 1)
    return parsed[0] if parsed else None


def parse_bridge_tech(data: str) -> int | None:
    parsed = _tail_ints(data, BRIDGE_TECH, 1)
    return parsed[0] if parsed else None


#: What a row says after the contact's name, when it has to say anything. An
#: active bridge says nothing: the green light is the whole message, and a suffix
#: on every row would bury the one row that needs reading.
_ROW_SUFFIX = {
    BridgeUiState.PROVISIONING: "создаётся",
    BridgeUiState.BROKEN: "не работает",
    BridgeUiState.DISABLED: "отключён",
}


def _row_label(view: BridgeView) -> str:
    """Button labels are never parsed as HTML: escaping one would show the
    owner a literal `&amp;` in their contact's name."""
    suffix = _ROW_SUFFIX.get(view.state)
    if suffix is None:
        return f"{view.glyph} {view.title[:38]}"
    return f"{view.glyph} {view.title[:26]} · {suffix}"


def _bridges_summary(bridges: list[BridgeView]) -> str:
    """«5 работают · 1 отключён», and the two states that used to be silent."""
    tally = [
        (
            sum(1 for b in bridges if b.state is BridgeUiState.ACTIVE),
            ("работает", "работают", "работают"),
        ),
        (
            sum(1 for b in bridges if b.state is BridgeUiState.BROKEN),
            ("не работает", "не работают", "не работают"),
        ),
        (
            sum(1 for b in bridges if b.state is BridgeUiState.PROVISIONING),
            ("создаётся", "создаются", "создаются"),
        ),
        (
            sum(1 for b in bridges if b.state is BridgeUiState.DISABLED),
            ("отключён", "отключены", "отключены"),
        ),
    ]
    return " · ".join(
        f"{number} {humanise.plural(number, *words)}" for number, words in tally if number
    )


def bridges_screen(bridges: list[BridgeView]) -> Screen:
    """The list. One row per contact, and the row is the whole story.

    The `↗` column is gone. It was a second button on every row for the second
    most common thing to do with a bridge, and it made the list read as a table
    of controls rather than a list of people. Opening the chat is one tap
    further in, on the card, where it is the primary action.

    The four states are now visible. A bridge being provisioned used to be
    absent from this screen altogether and a bridge whose bot never came up used
    to be drawn exactly like a working one — the lifecycle knew, and the list
    did not ask.
    """
    from bridge.provisioning.selection import HISTORY_NEW

    if not bridges:
        return (
            title(
                "Мостов пока нет",
                "Выберите диалоги MAX, и я подниму для них ботов.",
            ),
            _keyboard([("➕ Добавить контакт", ADD_CONTACT)], [(BACK_HOME, MENU)]),
        )

    rows: list[list[tuple[str, str]]] = [
        [(_row_label(view), bridge_callback(view.max_chat_id))] for view in bridges
    ]
    rows += [
        [("➕ Добавить контакт", ADD_CONTACT)],
        [("Подтянуть новую историю", HISTORY_NEW)],
        [(BACK_HOME, MENU)],
    ]
    return (
        title("Мосты", _bridges_summary(bridges)),
        _keyboard(*rows),
    )


def bridge_screen(view: BridgeView, *, can_delete: bool = False) -> Screen:
    """One bridge, in the words the owner would use about a person.

    What is *not* here is the point. The card used to print the bridge bot's own
    `/status` block — a raw epoch-millisecond integer for the last delivery, up
    to five hundred characters of stored exception text, and the names of dead
    background tasks — and it carried «Отключить мост» next to «Снести мост
    целиком 🗑», two adjacent rows separated by an emoji, one of them reversible
    and one of them not.

    Now: a verdict, when it last carried something, how much is waiting. Both
    destructive actions are one screen further in, and the technical values are
    two.
    """
    from bridge.provisioning.selection import bot_link

    body = [f"{view.glyph} {esc(view.headline)}", ""]
    if view.state is BridgeUiState.ACTIVE:
        if view.last_delivery:
            body.append(f"Последняя доставка · {esc(view.last_delivery)}")
        body.append(f"Очередь · {esc(view.queue)}")
    else:
        if view.cause:
            body.append(esc(view.cause))
        if view.last_delivery:
            body.append(f"Последняя успешная доставка · {esc(view.last_delivery)}")
        if view.held:
            body.append(
                esc(
                    f"{humanise.count(view.held, 'сообщение', 'сообщения', 'сообщений')}"
                    f" {humanise.plural(view.held, 'сохранено', 'сохранены', 'сохранены')}."
                )
            )

    rows: list[list[tuple[str, str]]] = []
    if view.state is BridgeUiState.BROKEN:
        # Only because there is a safe existing action behind it. Restarting is
        # the one thing that fixes a bridge that did not come up, and it already
        # asks before it does anything.
        rows.append([("Исправить", RESTART)])
    keyboard_rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
        for row in rows
    ]
    if view.state is not BridgeUiState.PROVISIONING:
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text="Открыть чат ↗", url=bot_link(view.username, view.bot_id)
                )
            ]
        )
    tail: list[tuple[str, str]] = []
    if view.state is BridgeUiState.BROKEN:
        tail.append(("Подробнее", bridge_tech_callback(view.max_chat_id)))
    if view.state is BridgeUiState.ACTIVE:
        tail.append(("История", bridge_history_callback(view.max_chat_id)))
    tail.append(("Настройки", bridge_settings_callback(view.max_chat_id)))
    keyboard_rows.append(
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in tail]
    )
    keyboard_rows.append(
        [InlineKeyboardButton(text=BACK_BRIDGES, callback_data=BRIDGES)]
    )
    # `can_delete` is honoured one screen in, on the settings card. Kept in the
    # signature because the router reads it from the same place either way.
    _ = can_delete
    return (title(view.title, *body), InlineKeyboardMarkup(inline_keyboard=keyboard_rows))


def bridge_settings_screen(view: BridgeView, *, can_delete: bool = False) -> Screen:
    """Where the two irreversible-ish actions live, and nothing else.

    Disconnecting stops the bridge and keeps everything; deleting ends the bot
    and frees its slot. Both are a level below the card on purpose: a
    destructive row that sits beside «Открыть чат» will eventually be tapped by
    somebody who meant to open the chat.
    """
    rows: list[list[tuple[str, str]]] = []
    if view.state is not BridgeUiState.DISABLED:
        rows.append([("Отключить мост", bridge_off_callback(view.max_chat_id))])
    if can_delete or view.state is BridgeUiState.DISABLED:
        rows.append([("Удалить мост…", bridge_free_callback(view.max_chat_id))])
    rows.append([(_back_to(view.title), bridge_callback(view.max_chat_id))])
    body = (
        ["Мост отключён. Чтобы включить его снова, выберите этот же диалог в «Добавить контакт»."]
        if view.state is BridgeUiState.DISABLED
        else []
    )
    return (title(f"Настройки · {esc(view.title)}", *body), _keyboard(*rows))


def bridge_history_screen(view: BridgeView) -> Screen:
    """The per-bridge history operations, away from the settings that end it."""
    return (
        title(
            f"История · {esc(view.title)}",
            "Перезалить — значит очистить этот чат в Telegram",
            "и подтянуть переписку из MAX заново.",
            "",
            "<i>Переписка в MAX останется на месте: она и есть оригинал.</i>",
        ),
        _keyboard(
            [("Перезалить переписку…", bridge_repull_callback(view.max_chat_id))],
            [(_back_to(view.title), bridge_callback(view.max_chat_id))],
        ),
    )


def bridge_tech_screen(view: BridgeView, lines: list[str]) -> Screen:
    """«Подробнее» on a broken card. The one bridge screen allowed raw values."""
    return (
        title(
            f"Технические данные · {esc(view.title)}",
            *([esc(line) for line in lines] or ["Мост ничего о себе не сообщает."]),
        ),
        _keyboard([(_back_to(view.title), bridge_callback(view.max_chat_id))]),
    )


def disconnect_confirm(item: Any, revision: int) -> Screen:
    """Says what disconnecting does *not* do, because that is the surprising part."""
    return (
        title(
            "Отключить мост?",
            f"Контакт: {bold(item.title)}",
            "",
            "Сообщения перестанут ходить в обе стороны.",
            "",
            "<i>Бот останется в вашем аккаунте Telegram — удалить его через API "
            "нельзя, и слот он продолжит занимать. Мост можно включить снова, "
            "выбрав этот же диалог: бот и переписка сохранятся.</i>",
        ),
        _keyboard(
            [("Отключить мост", bridge_off_yes_callback(item.max_chat_id, revision))],
            [(f"{BACK_ARROW} Отмена", bridge_settings_callback(item.max_chat_id))],
        ),
    )


def disconnected(item: Any) -> Screen:
    """Done, and the one thing that is *not* done, with the way to finish it."""
    return (
        title(
            "Мост отключён",
            f"Контакт: {bold(item.title)}",
            "",
            "Чтобы вернуть мост, выберите этот диалог снова — "
            "бот и переписка на месте.",
            "",
            "Бот всё ещё занимает слот в вашем аккаунте.",
        ),
        _keyboard(
            [("Удалить мост…", bridge_free_callback(item.max_chat_id))],
            [("Мосты", BRIDGES), ("➕ Добавить", ADD_CONTACT)],
            [(BACK_HOME, MENU)],
        ),
    )


def repull_confirm(item: Any, revision: int, *, by_session: bool = False) -> Screen:
    """Says the things about this that are not obvious before it is tapped.

    Two different operations behind one button, and they are not described the
    same way. With the owner's session the chat is emptied *completely* — the
    owner's own messages included — and rebuilt from MAX. Without one, a bot
    deletes what it placed and nothing older than forty-eight hours.

    The caveat about the forty-eight hours is the bot's limit, and it belongs on
    the screen only while the bot is doing the deleting.
    """
    if by_session:
        body = [
            "Очищу чат в Telegram полностью — и то, что принёс мост, "
            "и ваши собственные сообщения, — и подтяну переписку из MAX заново.",
            "",
            "<i>Переписка в MAX не тронется: она и есть оригинал, "
            "а чат в Telegram — её отображение.</i>",
        ]
    else:
        body = [
            "Удалю всё, что мост уже принёс в этот чат, "
            "и подтяну переписку из MAX заново.",
            "",
            "<i>Telegram не даёт боту удалять сообщения старше 48 часов — "
            "что не удалится, останется на месте и заново не придёт. "
            "Ваши собственные сообщения, отправленные из Telegram, не тронутся.</i>",
        ]
    return (
        title(
            "Перезалить переписку?",
            f"Контакт: {bold(item.title)}",
            "",
            *body,
        ),
        _keyboard(
            [("Перезалить переписку", bridge_repull_yes_callback(item.max_chat_id, revision))],
            [(f"{BACK_ARROW} Отмена", bridge_history_callback(item.max_chat_id))],
        ),
    )


def repulling(item: Any) -> Screen:
    return (
        title("Перезаливаю переписку", f"{BUSY} Удаляю старые сообщения…", esc(item.title)),
        None,
    )


def repulled(item: Any, *, gone: int, kept: int) -> Screen:
    """Counts, because «готово» hides the one number that matters here."""
    lines = [f"Контакт: {bold(item.title)}", "", f"{DONE} Очищено сообщений: {gone}"]
    if kept:
        lines += [
            f"{ATTENTION} Осталось прежних: {kept}",
            "",
            "<i>Telegram не дал их удалить — они старше 48 часов. "
            "Заново эти сообщения не приходили, так что дублей нет.</i>",
        ]
    return (
        title("Переписка перезалита", *lines),
        _keyboard(
            [(_back_to(item.title), bridge_callback(item.max_chat_id))],
            [("Мосты", BRIDGES)],
            [(BACK_HOME, MENU)],
        ),
    )


#: What @BotFather asks for before it removes anything, and the only safety
#: interlock Telegram has on this. Quoted exactly: a paraphrase would not work.
BOTFATHER_CONFIRMATION = "Yes, I am totally sure."


def free_slot(
    item: Any,
    *,
    can_delete: bool = False,
    can_wipe: bool = False,
    revision: int = 0,
    free_slots_hint: str = "",
) -> Screen:
    """Delete the bot, or say how to — depending on who can.

    Bot API 10.2 has `getManagedBotToken`, `replaceManagedBotToken` and the two
    access-settings calls, and nothing that deletes a managed bot. So this used
    to be instructions and a link, never a button that pretends.

    The owner's own session can drive `/deletebot`, and when provisioning runs
    that way the button is real. The instructions stay for the install that has
    no session: the same screen, and neither version lies about which it is.
    """
    if can_delete:
        wiping = (
            ["— чат Telegram с этим ботом"]
            if can_wipe
            else ["<i>Чат Telegram останется: без вашей сессии его не стереть.</i>"]
        )
        return (
            title(
                f"Удалить мост «{esc(item.title)}»?",
                "Будут удалены:",
                "— Telegram-бот",
                *wiping,
                "— локальные данные этого моста",
                "",
                "Переписка в MAX останется.",
                "",
                f"<i>Это действие нельзя отменить.{esc(free_slots_hint)}</i>",
            ),
            _keyboard(
                # The button says what it does. «Да» under a heading the owner
                # has already stopped reading is how a bot gets deleted by
                # somebody who thought they were confirming something else.
                [("🗑 Удалить мост", bridge_free_yes_callback(item.max_chat_id, revision))],
                [(f"{BACK_ARROW} Отмена", bridge_settings_callback(item.max_chat_id))],
            ),
        )
    return (
        title(
            "Удаление бота",
            f"Бот моста «{esc(item.title)}» остаётся в аккаунте, пока вы его не удалите. "
            "Через Bot API это невозможно — только в @BotFather.",
            "",
            "1. Откройте @BotFather",
            "2. Отправьте <code>/deletebot</code>",
            f"3. Выберите <code>@{esc(item.username)}</code>",
            f"4. Пришлите <code>{esc(BOTFATHER_CONFIRMATION)}</code>",
            "",
            "<i>Слот освободится сразу. Мост уже отключён — "
            "удаление бота ничего больше не сломает.</i>",
        ),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Открыть @BotFather ↗", url="https://t.me/BotFather"
                    )
                ],
                [InlineKeyboardButton(text="Мосты", callback_data=BRIDGES)],
                [InlineKeyboardButton(text=BACK_HOME, callback_data=MENU)],
            ]
        ),
    )


def tearing_down(item: Any) -> Screen:
    """Shown while it runs. Several remote round trips, none of them instant."""
    return (
        title("Сношу мост…", f"Контакт: {bold(item.title)}", "", "Чищу чат, удаляю бота."),
        None,
    )


def teardown_stalled(item: Any, reason: str) -> Screen:
    """It stopped part-way, and the owner is owed the truth about where.

    The walk drops local rows only after the remote effects are confirmed, so a
    stall leaves the bridge recoverable and the retry safe. Saying "не удалось"
    and nothing else would hide which half happened.
    """
    return (
        title(
            "Снос не завершён",
            f"Контакт: {bold(item.title)}",
            "",
            esc(reason[:200]),
            "",
            "<i>Ничего локального не стёрто — мост и его записи на месте. "
            "Повтор продолжит с того же места.</i>",
        ),
        _keyboard(
            [("Повторить", bridge_free_callback(item.max_chat_id))],
            [("Мосты", BRIDGES)],
            # There was no route to home from here at all: a teardown that
            # stopped part-way left the owner on a screen with two buttons,
            # neither of which was the way out.
            [(BACK_HOME, MENU)],
        ),
    )


def slot_freed(item: Any, outcome: Any = None) -> Screen:
    """Done, and it really is done — the count on the picker moves.

    Reports each irreversible effect separately rather than as one word. They
    can succeed apart: a wipe that failed leaves a conversation the owner
    expected to be gone, and being told so is the difference between a tidy
    result and a quiet lie.
    """
    lines = [f"<code>@{esc(item.username)}</code> больше нет, слот свободен."]
    if outcome is not None:
        if getattr(outcome, "dialog_wiped", False):
            lines.append("Переписка в чате Telegram удалена, чат убран из списка.")
        rows = getattr(outcome, "row_count", 0)
        if rows:
            lines.append(f"Стёрто локальных записей: {rows}.")
    return (
        title(
            "Мост снесён",
            *lines,
            "",
            "Чтобы снова связаться с этим контактом, выберите его в списке диалогов — "
            "будет создан новый бот с тем же именем.",
        ),
        _keyboard(
            [("Мосты", BRIDGES), ("➕ Добавить", ADD_CONTACT)],
            [(BACK_HOME, MENU)],
        ),
    )

