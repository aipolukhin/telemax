"""Where onboarding ends up: one screen that says what the deployment is doing.

Everything here is read from live state rather than remembered — the bot count
comes from Telegram, the bridge list from the database — because the one thing a
status screen must never do is describe a system that no longer exists.

The bridges are links, not callbacks. A bot is opened, not operated, and a
callback button pointing at a chat is a button that does nothing when tapped
from a forwarded message.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from aiogram import Dispatcher

from bridge.onboarding import screens
from bridge.onboarding.board import StatusBoard
from bridge.onboarding.fsm import MaxOnboarding
from bridge.onboarding.router import build_onboarding_router
from bridge.onboarding.state import StateStore
from bridge.onboarding.views import (
    AttentionFacts,
    BridgeFacts,
    BridgeUiState,
    BridgeView,
    HomeView,
    UserProblem,
    bridge_view,
    home_view,
    problems,
)
from bridge.provisioning.flow import BridgeSummary, DialogFlow
from bridge.provisioning.journal import ProvisioningJournal
from bridge.provisioning.naming_v2 import contact_bot_username_v2
from bridge.storage.models import BridgeRecord
from bridge.telegram import OwnerOnlyMiddleware
from tests.fake_provisioning import (
    FakeBridgeRepository,
    FakeDialogPicker,
    FakeGateway,
    FakeProvisioner,
)
from tests.fake_telegram import OWNER_ID, FakeBot, make_callback, make_message

#: Half of every V2 username; the contact's MAX id is the other half.
OWNER = 100000001


def summary(title: str, username: str, max_chat_id: int) -> BridgeSummary:
    return BridgeSummary(
        title=title,
        username=username,
        bridge_name=username.removesuffix("_max_bot"),
        max_chat_id=max_chat_id,
    )


@dataclass
class Control:
    lines: list[str]
    links: list[BridgeSummary]
    disconnected: list[int] = field(default_factory=list)
    #: Bridges that were switched off — still in the database, still bots.
    gone: list[BridgeSummary] = field(default_factory=list)
    repulled: list[int] = field(default_factory=list)
    #: `(deleted, kept)` — kept is what Telegram would not let the bot remove.
    repull_result: tuple[int, int] = (12, 0)
    #: Whether the owner's own MAX messages are being carried. The setting
    #: screen reads and sets this.
    mirror_own: bool = False
    #: What the home verdict is decided from. A healthy install by default, so
    #: a test that cares about one fact only has to state that one.
    facts: AttentionFacts = field(
        default_factory=lambda: AttentionFacts(worker_running=True, max_connected=True)
    )
    #: Per-bridge presentation state, for the rows that are not simply green.
    states: dict[int, BridgeUiState] = field(default_factory=dict)

    async def status_lines(self) -> list[str]:
        return self.lines or ["MAX  подключён"]

    async def diagnostic_sections(self) -> dict[str, list[str]]:
        return {"link": await self.status_lines()}

    async def diagnostic_lights(self) -> dict[str, bool]:
        return {"telegram": True, "max": True, "delivery": True, "database": True}

    async def own_messages_mirror(self) -> bool:
        return self.mirror_own

    async def set_own_messages_mirror(self, value: bool) -> None:
        self.mirror_own = value

    async def timezone_label(self) -> str | None:
        return "Москва · UTC+03:00"

    async def restart_bridge(self) -> bool:
        return True

    async def home_view(self) -> HomeView:
        return home_view(replace(self.facts, bridges=len(self.links)))

    async def problems(self) -> list[UserProblem]:
        return problems(self.facts)

    def _view(self, item: BridgeSummary, *, disabled: bool = False) -> BridgeView:
        return bridge_view(
            BridgeFacts(
                title=item.title,
                username=item.username,
                bridge_name=item.bridge_name,
                max_chat_id=item.max_chat_id,
                bot_id=item.bot_id,
                state=(
                    BridgeUiState.DISABLED
                    if disabled
                    else self.states.get(item.max_chat_id, BridgeUiState.ACTIVE)
                ),
            ),
            now_ms=0,
        )

    async def bridge_views(self) -> list[BridgeView]:
        return [self._view(item) for item in self.links]

    async def bridge_view(self, max_chat_id: int) -> BridgeView | None:
        # Disabled rows are still found: the screen after disconnecting has to
        # name the bot the owner now has to delete by hand.
        for item in self.links:
            if item.max_chat_id == max_chat_id:
                return self._view(item)
        for item in self.gone:
            if item.max_chat_id == max_chat_id:
                return self._view(item, disabled=True)
        return None

    async def bridges(self) -> list[BridgeSummary]:
        return self.links

    async def bridge_card(self, max_chat_id: int) -> tuple[BridgeSummary | None, list[str]]:
        item = next(
            (one for one in self.links + self.gone if one.max_chat_id == max_chat_id), None
        )
        return item, ["Очередь: 0", "Последняя доставка: 1700000003000"]

    async def repull_history(self, max_chat_id: int) -> tuple[int, int] | None:
        if not any(one.max_chat_id == max_chat_id for one in self.links):
            return None
        self.repulled.append(max_chat_id)
        return self.repull_result

    async def disconnect_bridge(self, max_chat_id: int) -> bool:
        item = next((one for one in self.links if one.max_chat_id == max_chat_id), None)
        if item is None:
            return False
        self.disconnected.append(max_chat_id)
        self.links = [one for one in self.links if one.max_chat_id != max_chat_id]
        self.gone.append(item)
        return True


def wire(tmp_path: Path, control: Any) -> tuple[Dispatcher, FakeBot]:
    store = StateStore.for_data_dir(tmp_path)
    bot = FakeBot("1:aaa")
    board = StatusBoard(bot=bot, chat_id=OWNER_ID, store=store)

    async def nothing(*_: object, **__: object) -> None:
        return None

    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(OwnerOnlyMiddleware(OWNER_ID))
    dispatcher.include_router(
        build_onboarding_router(
            owner_user_id=OWNER_ID,
            store=store,
            onboarding=MaxOnboarding(
                store=store, show=board.show, connect=nothing, persist=nothing, launch=nothing
            ),
            control=control,
            is_guardian=lambda _: True,
            show=board.show,
        )
    )
    return dispatcher, bot


def said(bot: FakeBot) -> str:
    return "\n".join(str(call.kwargs.get("text", "")) for call in bot.calls)


def markup_of(bot: FakeBot) -> str:
    for call in reversed(bot.calls):
        if "reply_markup" in call.kwargs:
            return str(call.kwargs["reply_markup"])
    raise AssertionError("no keyboard was sent")


async def test_the_menu_answers_three_questions_and_nothing_else(tmp_path: Path) -> None:
    """Is Telemax working, are Telegram and MAX working, is anything owed.

    Everything that used to share this screen is still computed and still
    reachable; none of it is an answer to a question somebody opens the bot to
    ask, and each of them pushed the three that are further down the message.
    """
    control = Control(lines=[], links=[summary("Мама", "def_max_bot", 22)])
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(bot, make_message(1, "/menu"))  # type: ignore[arg-type]

    text = said(bot)
    assert "🟢 Всё работает" in text
    assert "Telegram ● подключён" in text
    assert "MAX ● подключён" in text
    assert "1 мост · очередь пуста" in text
    for banned in ("Боты Telegram", "Свободно", "Часовой пояс", "Europe/Moscow"):
        assert banned not in text, f"{banned} is not a question anybody opens this bot to ask"
    for banned in ("|", "╭", "─", "```"):
        assert banned not in text, "a phone renders a table as rubble"


async def test_the_menu_offers_bridges_adding_and_settings(tmp_path: Path) -> None:
    dispatcher, bot = wire(tmp_path, Control(lines=[], links=[]))

    await dispatcher.feed_update(bot, make_message(1, "/menu"))  # type: ignore[arg-type]

    rendered = markup_of(bot)
    assert screens.BRIDGES in rendered
    assert screens.ADD_CONTACT in rendered
    assert screens.SETTINGS in rendered
    assert screens.OWN_MSGS not in rendered, "a preference is not a first-screen control"
    assert screens.TZ_LIST not in rendered


async def test_a_healthy_home_offers_no_problems_button(tmp_path: Path) -> None:
    """A permanent «Проблемы · 0» teaches the owner to stop looking there."""
    dispatcher, bot = wire(tmp_path, Control(lines=[], links=[]))

    await dispatcher.feed_update(bot, make_message(1, "/menu"))  # type: ignore[arg-type]

    assert screens.PROBLEMS not in markup_of(bot)


async def test_the_home_screen_asks_for_attention_when_something_needs_it(
    tmp_path: Path,
) -> None:
    """Failed and ambiguous jobs used to be invisible here entirely.

    The glyph was chosen from `max_connected` and "is a background task dead",
    so an install with three undelivered messages said «🟢 Всё работает».
    """
    control = Control(
        lines=[],
        links=[summary("Мама", "def_max_bot", 22)],
        facts=AttentionFacts(
            worker_running=True, max_connected=True, queued=28, failed=1, ambiguous=1
        ),
    )
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(bot, make_message(1, "/menu"))  # type: ignore[arg-type]

    text = said(bot)
    assert "🟡 Требует внимания" in text
    assert "28 сообщений ждут отправки" in text
    assert "2 сообщения требуют решения" in text
    assert screens.PROBLEMS in markup_of(bot)


async def test_the_home_screen_says_what_is_broken_in_a_sentence(tmp_path: Path) -> None:
    control = Control(
        lines=[], links=[], facts=AttentionFacts(worker_running=True, max_connected=False)
    )
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(bot, make_message(1, "/menu"))  # type: ignore[arg-type]

    text = said(bot)
    assert "🔴 Нет связи с MAX" in text
    assert "MAX ○ нет связи" in text


async def test_the_own_message_setting_lives_in_settings_now(tmp_path: Path) -> None:
    """It was a toggle whose label showed the state and whose tap flipped it.

    That control is read by half of everybody as a promise about what pressing
    it will do. Two radio rows say the same thing without the ambiguity — and
    they are behind «Настройки», not on the screen that answers "is it working".
    """
    control = Control(lines=[], links=[])
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(bot, make_callback(1, screens.SETTINGS))  # type: ignore[arg-type]
    assert screens.SETTINGS_OWN in markup_of(bot)

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(2, screens.SETTINGS_OWN)
    )
    rendered = markup_of(bot)
    assert screens.own_set_callback(True) in rendered
    assert screens.own_set_callback(False) in rendered
    assert f"✓ {screens.OWN_MSGS_OFF}" in rendered, "off by default, and it says which"

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(3, screens.own_set_callback(True))
    )
    assert control.mirror_own is True
    assert f"✓ {screens.OWN_MSGS_ON}" in markup_of(bot)

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(4, screens.own_set_callback(False))
    )
    assert control.mirror_own is False


async def test_the_own_message_setting_is_owner_only(tmp_path: Path) -> None:
    control = Control(lines=[], links=[])
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(1, screens.own_set_callback(True), user_id=999)
    )

    assert control.mirror_own is False, "a stranger cannot flip it"


async def test_settings_reaches_the_timezone_and_has_a_way_back(tmp_path: Path) -> None:
    """Reached from home, the timezone list had no back button at all.

    The only ways off it were picking a city — which rewrites the config and
    restarts the worker — or typing `/menu`.
    """
    dispatcher, bot = wire(tmp_path, Control(lines=[], links=[]))

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(1, screens.SETTINGS_TZ)
    )

    rendered = markup_of(bot)
    assert "Москва · UTC+03:00" in rendered
    assert screens.SETTINGS in rendered, "and a way back to where it came from"


async def test_every_bridge_row_opens_a_card(tmp_path: Path) -> None:
    """One row per contact. The `↗` column is gone.

    It was a second button on every row for the second most common thing to do
    with a bridge, and it made a list of people read as a table of controls.
    """
    control = Control(
        lines=[],
        links=[
            summary("Иван Петров", "abc_max_bot", 11),
            summary("Мама", "def_max_bot", 22),
        ],
    )
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(bot, make_callback(1, screens.BRIDGES))  # type: ignore[arg-type]

    rendered = markup_of(bot)
    assert "Иван Петров" in rendered
    assert screens.bridge_callback(11) in rendered, "the name opens the card"
    assert "https://t.me/abc_max_bot" not in rendered, "the link lives on the card"

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(2, screens.bridge_callback(11))
    )
    assert "https://t.me/abc_max_bot" in markup_of(bot)


async def test_the_list_distinguishes_the_four_states(tmp_path: Path) -> None:
    """A provisioning bridge was absent and a broken one looked healthy."""
    control = Control(
        lines=[],
        links=[
            summary("Мама", "abc_max_bot", 11),
            summary("Папа", "def_max_bot", 22),
            summary("Друг", "ghi_max_bot", 33),
            summary("Илья", "jkl_max_bot", 44),
        ],
        states={
            22: BridgeUiState.PROVISIONING,
            33: BridgeUiState.DISABLED,
            44: BridgeUiState.BROKEN,
        },
    )
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(bot, make_callback(1, screens.BRIDGES))  # type: ignore[arg-type]

    rendered = markup_of(bot)
    assert "🟢 Мама" in rendered
    assert "🟡 Папа · создаётся" in rendered
    assert "⚪ Друг · отключён" in rendered
    assert "🔴 Илья · не работает" in rendered
    assert "1 работает · 1 не работает · 1 создаётся · 1 отключён" in said(bot)


async def test_the_card_hides_the_dangerous_action_behind_itself(tmp_path: Path) -> None:
    """Disconnecting is never a neighbour of «открыть чат».

    It used to be exactly that: «Отключить мост» and «Снести мост целиком 🗑»
    were adjacent rows on the card, separated by an emoji, one reversible and
    one not.
    """
    control = Control(lines=[], links=[summary("Мама", "def_max_bot", 22)])
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(bot, make_callback(1, screens.BRIDGES))  # type: ignore[arg-type]
    assert screens.BRIDGE_OFF not in markup_of(bot), "not on the list screen"

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(2, screens.bridge_callback(22))
    )
    rendered = markup_of(bot)
    assert "🟢 Мост работает" in said(bot)
    assert screens.bridge_off_callback(22) not in rendered, "nor on the card"
    assert screens.bridge_free_callback(22) not in rendered
    assert screens.bridge_settings_callback(22) in rendered

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(3, screens.bridge_settings_callback(22))
    )
    assert screens.bridge_off_callback(22) in markup_of(bot)


async def test_a_normal_card_shows_no_epoch_and_no_exception(tmp_path: Path) -> None:
    """`Последняя доставка: 1700000003000` was on this screen for months."""
    control = Control(lines=[], links=[summary("Мама", "def_max_bot", 22)])
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(1, screens.bridge_callback(22))
    )

    text = said(bot)
    assert "1700000003000" not in text
    assert "Очередь · пусто" in text


async def test_disconnecting_asks_first_and_says_the_bot_survives(tmp_path: Path) -> None:
    control = Control(lines=[], links=[summary("Мама", "def_max_bot", 22)])
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(1, screens.bridge_off_callback(22))
    )

    assert control.disconnected == [], "asking is not doing"
    text = said(bot)
    assert "Отключить мост?" in text
    # The surprising part, said before the tap rather than after it.
    assert "Бот останется в вашем аккаунте Telegram" in text


async def test_a_confirmed_disconnect_stops_one_bridge(tmp_path: Path) -> None:
    store = StateStore.for_data_dir(tmp_path)
    control = Control(
        lines=[],
        links=[summary("Мама", "def_max_bot", 22), summary("Папа", "ghi_max_bot", 33)],
    )
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(1, screens.bridge_off_callback(22))
    )
    revision = store.load().screen_revision
    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(2, screens.bridge_off_yes_callback(22, revision))
    )

    assert control.disconnected == [22], "only the one that was confirmed"
    assert "Мост отключён" in said(bot)


async def test_the_same_confirmation_disconnects_only_once(tmp_path: Path) -> None:
    store = StateStore.for_data_dir(tmp_path)
    control = Control(lines=[], links=[summary("Мама", "def_max_bot", 22)])
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(1, screens.bridge_off_callback(22))
    )
    stale = screens.bridge_off_yes_callback(22, store.load().screen_revision)
    await dispatcher.feed_update(bot, make_callback(2, stale))  # type: ignore[arg-type]
    await dispatcher.feed_update(bot, make_callback(3, stale))  # type: ignore[arg-type]

    assert control.disconnected == [22]
    toasts = [
        str(call.kwargs.get("text") or "")
        for call in bot.calls
        if call.method == "answer_callback_query"
    ]
    assert screens.STALE_SCREEN in toasts


async def test_with_no_bridges_the_screen_points_at_the_picker(tmp_path: Path) -> None:
    dispatcher, bot = wire(tmp_path, Control(lines=[], links=[]))

    await dispatcher.feed_update(bot, make_callback(1, screens.BRIDGES))  # type: ignore[arg-type]

    assert "Мостов пока нет" in said(bot)
    assert screens.ADD_CONTACT in markup_of(bot)


async def test_a_stranger_gets_nothing(tmp_path: Path) -> None:
    dispatcher, bot = wire(tmp_path, Control(lines=["секрет"], links=[]))

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(1, screens.MENU, user_id=999)
    )

    assert "секрет" not in said(bot)


async def test_the_bridge_list_comes_from_the_database(tmp_path: Path) -> None:
    """Not from the journal: bridges outlive the run that provisioned them."""
    bridges = FakeBridgeRepository()
    bridges.records[236856064] = BridgeRecord(
        bridge_name="c1",
        max_chat_id=236856064,
        token_env="TELEMAX_BOT_C1",
        max_user_id=200000002,
        title="Иван Петров",
        expected_username=None,
    )
    flow = DialogFlow(
        picker=FakeDialogPicker([]),  # type: ignore[arg-type]
        provisioner=FakeProvisioner(),
        gateway=FakeGateway(),
        journal=ProvisioningJournal.for_data_dir(tmp_path),
        bridges=bridges,
        telegram_owner_user_id=100000001,
    )

    summaries = await flow.summaries()

    assert [item.title for item in summaries] == ["Иван Петров"]
    # No stored username: it is re-derived, and lands on the same name as ever.
    assert summaries[0].username == contact_bot_username_v2(OWNER, 200000002)


def test_the_glyph_says_what_the_owner_came_to_find_out() -> None:
    """One symbol per state, and never two symbols for the same one."""
    from bridge.telegram.design import ATTENTION, BROKEN, RUNNING

    healthy = AttentionFacts(worker_running=True, max_connected=True)

    assert home_view(healthy).glyph == RUNNING
    assert home_view(replace(healthy, failed=1)).glyph == ATTENTION
    assert home_view(replace(healthy, max_connected=False)).glyph == BROKEN


async def test_the_home_screen_leads_with_a_verdict(tmp_path: Path) -> None:
    control = Control(lines=[], links=[summary("Мама", "def_max_bot", 22)])
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(bot, make_message(1, "/menu"))  # type: ignore[arg-type]

    text = said(bot)
    assert "<b>Telemax</b>" in text
    assert "🟢 Всё работает" in text
    assert text.index("🟢") < text.index("Telegram ●")


async def test_disconnecting_offers_the_way_to_free_the_slot(tmp_path: Path) -> None:
    """The bot outlives the bridge, and only @BotFather can remove it.

    Bot API 10.2 has four managed-bot methods and none of them deletes one, so
    the honest end of this flow is instructions plus the exact username — not a
    button that pretends to do it.
    """
    store = StateStore.for_data_dir(tmp_path)
    control = Control(lines=[], links=[summary("Мама", "def_max_bot", 22)])
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(1, screens.bridge_off_callback(22))
    )
    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(2, screens.bridge_off_yes_callback(22, store.load().screen_revision))
    )
    assert screens.bridge_free_callback(22) in markup_of(bot)

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(3, screens.bridge_free_callback(22))
    )

    text = said(bot)
    assert "Удаление бота" in text
    assert "/deletebot" in text
    assert "@def_max_bot" in text, "the exact bot, not a list to guess from"
    assert screens.BOTFATHER_CONFIRMATION in text
    assert "https://t.me/BotFather" in markup_of(bot)


async def test_a_repull_asks_first_and_names_the_48_hour_limit(tmp_path: Path) -> None:
    """The two surprising parts, said before the tap rather than discovered after.

    A bot may not delete a message older than 48 hours, so a wipe is partial by
    nature — and the owner's own messages are not the bridge's to remove.
    """
    control = Control(lines=[], links=[summary("Мама", "def_max_bot", 22)])
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(1, screens.bridge_repull_callback(22))
    )

    assert control.repulled == [], "asking is not doing"
    text = said(bot)
    assert "Перезалить переписку?" in text
    assert "48 часов" in text
    assert "Ваши собственные сообщения" in text


async def test_a_confirmed_repull_wipes_and_reports_the_counts(tmp_path: Path) -> None:
    store = StateStore.for_data_dir(tmp_path)
    control = Control(lines=[], links=[summary("Мама", "def_max_bot", 22)])
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(1, screens.bridge_repull_callback(22))
    )
    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot,
        make_callback(2, screens.bridge_repull_yes_callback(22, store.load().screen_revision)),
    )

    assert control.repulled == [22]
    text = said(bot)
    assert "Очищено сообщений: 12" in text
    assert "Осталось прежних" not in text, "nothing survived, so nothing to warn about"


async def test_what_could_not_be_deleted_is_reported_not_hidden(tmp_path: Path) -> None:
    """A partial wipe is the normal case for an old dialog. Say the number."""
    store = StateStore.for_data_dir(tmp_path)
    control = Control(lines=[], links=[summary("Мама", "def_max_bot", 22)])
    control.repull_result = (5, 7)
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(1, screens.bridge_repull_callback(22))
    )
    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot,
        make_callback(2, screens.bridge_repull_yes_callback(22, store.load().screen_revision)),
    )

    text = said(bot)
    assert "Очищено сообщений: 5" in text
    assert "Осталось прежних: 7" in text
    assert "старше 48 часов" in text
    assert "дублей нет" in text


async def test_the_same_repull_confirmation_runs_once(tmp_path: Path) -> None:
    """A wipe is not idempotent: the second run would delete the fresh copy."""
    store = StateStore.for_data_dir(tmp_path)
    control = Control(lines=[], links=[summary("Мама", "def_max_bot", 22)])
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(1, screens.bridge_repull_callback(22))
    )
    stale = screens.bridge_repull_yes_callback(22, store.load().screen_revision)
    await dispatcher.feed_update(bot, make_callback(2, stale))  # type: ignore[arg-type]
    await dispatcher.feed_update(bot, make_callback(3, stale))  # type: ignore[arg-type]

    assert control.repulled == [22]


async def test_the_repull_lives_under_the_history_screen(tmp_path: Path) -> None:
    """One tap in, away from the settings screen that ends the bridge."""
    control = Control(lines=[], links=[summary("Мама", "def_max_bot", 22)])
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(1, screens.bridge_callback(22))
    )
    assert screens.bridge_history_callback(22) in markup_of(bot)

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(2, screens.bridge_history_callback(22))
    )
    assert screens.bridge_repull_callback(22) in markup_of(bot)
    assert screens.bridge_callback(22) in markup_of(bot), "and a way back to the card"
