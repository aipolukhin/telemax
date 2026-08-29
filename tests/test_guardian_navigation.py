"""Rules about the shape of the guardian's screens, not about their wording.

Wording is a judgement call and it moves. These are the four things that were
each individually true in production, each individually invisible, and each
individually the kind of thing that comes back the moment somebody writes a
screen without thinking about it:

* a stable screen with no way off it — the owner's only exit was typing `/menu`;
* a raw epoch-millisecond integer and up to five hundred characters of stored
  exception text on the card an owner opens to see whether a contact is fine;
* an irreversible action one row below a reversible one, on the same card;
* a first screen that answered five questions, three of which nobody asked.

Every test here is written against the renderers rather than the router, so a
failure names the screen rather than a callback chain.
"""

from __future__ import annotations

import re
from typing import Any

from aiogram.types import InlineKeyboardMarkup

from bridge.onboarding import screens
from bridge.onboarding.views import (
    AttentionFacts,
    BridgeFacts,
    BridgeUiState,
    HomeState,
    ProblemKind,
    bridge_view,
    group_problems,
    home_view,
    problems,
)

Screen = tuple[str, InlineKeyboardMarkup | None]


def _card(**kwargs: Any) -> Any:
    facts = BridgeFacts(
        title="Мама",
        username="def_max_bot",
        bridge_name="def",
        max_chat_id=22,
        bot_id=42,
        **kwargs,
    )
    return bridge_view(facts, now_ms=1_786_192_400_000)


def _ambiguous_group():  # type: ignore[no-untyped-def]
    """A family big enough to paginate, for the screens that list one."""
    from bridge.onboarding.views import JobFacts

    facts = AttentionFacts(worker_running=True, max_connected=True, ambiguous=20)
    jobs = [
        JobFacts(job_id=index, bridge_name="abc", contact="Мама", ambiguous=True,
                 created_at=1_786_192_000_000)
        for index in range(20)
    ]
    found = problems(facts, jobs=jobs, attempts=[], now_ms=1_786_192_400_000)
    return next(
        one for one in group_problems(found)
        if one.kind is ProblemKind.DELIVERY_AMBIGUOUS
    )


def _healthy() -> AttentionFacts:
    return AttentionFacts(worker_running=True, max_connected=True, bridges=5)


def _every_stable_screen() -> dict[str, Screen]:
    """Every screen an owner can be left sitting on.

    Transient ones are deliberately absent: «⏳ Перезапускаю…» has no keyboard
    by design and is replaced within the same second. What must never happen is
    a *final* frame with nothing to tap.
    """
    card = _card()
    return {
        "HOME": screens.home(home_view(_healthy())),
        "HOME/attention": screens.home(
            home_view(AttentionFacts(worker_running=True, max_connected=True, failed=2))
        ),
        "PROBLEMS": screens.problems_screen(
            group_problems(
                problems(
                    AttentionFacts(worker_running=True, max_connected=False, queued=28)
                )
            )
        ),
        "PROBLEMS/empty": screens.problems_screen([]),
        "PROBLEMS/category": screens.category_screen(_ambiguous_group(), 0),
        "PROBLEMS/category last page": screens.category_screen(
            _ambiguous_group(), _ambiguous_group().pages - 1
        ),
        "SETTINGS": screens.settings(mirror_own=False, timezone="Москва · UTC+03:00"),
        "OWN_MESSAGES": screens.own_messages(mirror_own=True),
        "TIMEZONE": screens.timezone_list(
            [("Москва · UTC+03:00", "Europe/Moscow")],
            back=screens.SETTINGS,
            current="Москва · UTC+03:00",
            callback=screens.settings_timezone_callback,
        ),
        "DIAGNOSTICS": screens.diagnostics(
            telegram=True, max_connected=True, delivery=True, database=True
        ),
        "DIAGNOSTICS/section": screens.diagnostics_section("Связь", ["MAX подключён"]),
        "TECHNICAL": screens.technical_details(["MAX подключён"]),
        "BRIDGES": screens.bridges_screen([card]),
        "BRIDGES/empty": screens.bridges_screen([]),
        "BRIDGE": screens.bridge_screen(card),
        "BRIDGE/broken": screens.bridge_screen(_card(state=BridgeUiState.BROKEN)),
        "BRIDGE/disabled": screens.bridge_screen(_card(state=BridgeUiState.DISABLED)),
        "BRIDGE/provisioning": screens.bridge_screen(
            _card(state=BridgeUiState.PROVISIONING)
        ),
        "BRIDGE_SETTINGS": screens.bridge_settings_screen(card, can_delete=True),
        "BRIDGE_HISTORY": screens.bridge_history_screen(card),
        "BRIDGE_TECH": screens.bridge_tech_screen(card, ["Очередь: 0"]),
        "DISCONNECT_CONFIRM": screens.disconnect_confirm(card, 1),
        "DISCONNECTED": screens.disconnected(card),
        "REPULL_CONFIRM": screens.repull_confirm(card, 1),
        "REPULLED": screens.repulled(card, gone=12, kept=0),
        "DELETE_CONFIRM": screens.free_slot(card, can_delete=True, can_wipe=True, revision=1),
        "DELETE_RECIPE": screens.free_slot(card, can_delete=False),
        "TEARDOWN_STALLED": screens.teardown_stalled(card, "boom"),
        "SLOT_FREED": screens.slot_freed(card),
        "RESTART_CONFIRM": screens.restart_confirm(1),
        "READY": screens.ready(timezone="Europe/Moscow"),
        "LAUNCH_FAILED": screens.launch_failed("не поднялось"),
    }


def test_every_stable_screen_has_a_way_off_it() -> None:
    """The one navigation rule. There is no history stack to fall back on."""
    stranded = []
    for name, (_, markup) in _every_stable_screen().items():
        if markup is None:
            stranded.append(name)
            continue
        if not any(button.callback_data or button.url for row in markup.inline_keyboard
                   for button in row):
            stranded.append(name)
    assert stranded == [], f"no way out of: {', '.join(stranded)}"


def test_the_home_screen_answers_three_questions_and_no_others() -> None:
    """Everything below is still computed, still reachable, and not here."""
    text, markup = screens.home(home_view(_healthy()))
    rendered = str(markup)

    for banned in ("Боты", "слот", "Свободно", "лимит", "Часовой пояс", "Europe/"):
        assert banned not in text, f"«{banned}» is not one of the three questions"
    assert screens.OWN_MSGS not in rendered, "the own-message toggle moved to settings"
    assert screens.TZ_LIST not in rendered, "so did the timezone"
    assert "очередь" in text or "мост" in text


def test_the_home_verdict_notices_what_needs_a_decision() -> None:
    """Failed, ambiguous, stuck provisioning and dead bridges were all silent.

    The glyph came from `max_connected` and «is a supervised task dead», so an
    install could say «🟢 Всё работает» over three undelivered messages.
    """
    healthy = _healthy()

    assert home_view(healthy).state is HomeState.HEALTHY
    for one in (
        {"failed": 1},
        {"ambiguous": 1},
        {"provisioning_unfinished": 1},
        {"bridges_broken": 1},
        {"degraded": True},
        {"owner_ingress_ready": False},
        {"oldest_pending_ms": 20 * 60_000},
    ):
        from dataclasses import replace

        view = home_view(replace(healthy, **one))
        assert view.state is HomeState.ATTENTION, one
        assert view.problems >= 1, one

    for one in ({"max_connected": False}, {"worker_running": False}, {"database_healthy": False}):
        from dataclasses import replace

        assert home_view(replace(healthy, **one)).state is HomeState.BROKEN, one


def test_the_problems_button_appears_only_when_there_are_problems() -> None:
    _, healthy = screens.home(home_view(_healthy()))
    from dataclasses import replace

    _, wanting = screens.home(home_view(replace(_healthy(), failed=2)))

    assert screens.PROBLEMS not in str(healthy)
    assert screens.PROBLEMS in str(wanting)
    assert "Проблемы · 2" in str(wanting), "the badge agrees with the list"


def test_a_normal_bridge_card_never_renders_a_raw_epoch() -> None:
    """`Последняя доставка: 1700000003000` shipped and stayed for months."""
    text, _ = screens.bridge_screen(_card(last_delivery_at=1_786_192_333_412))

    assert not re.search(r"\b\d{10,}\b", text), text
    assert "назад" in text or "только что" in text or "сегодня" in text


def test_a_normal_bridge_card_never_renders_a_stored_exception() -> None:
    """Up to five hundred characters of it, straight from `bridge_state`."""
    boom = "TelegramUnauthorizedError: bot token is invalid"
    text, markup = screens.bridge_screen(
        _card(state=BridgeUiState.BROKEN, detail=boom)
    )

    assert boom not in text
    assert "Мост не работает" in text
    # It is not lost. It is one tap away, on a screen that says it is technical.
    assert screens.bridge_tech_callback(22) in str(markup)
    assert boom in screens.bridge_tech_screen(_card(), [boom])[0]


def test_the_destructive_actions_are_absent_from_the_primary_card() -> None:
    """«Отключить мост» and «Снести мост целиком 🗑» were adjacent rows."""
    _, markup = screens.bridge_screen(_card(), can_delete=True)
    rendered = str(markup)

    assert screens.bridge_off_callback(22) not in rendered
    assert screens.bridge_free_callback(22) not in rendered
    assert screens.bridge_settings_callback(22) in rendered


def test_the_delete_confirmation_button_names_the_action() -> None:
    """«Да» under a heading somebody has stopped reading is not a confirmation."""
    _, markup = screens.free_slot(_card(), can_delete=True, can_wipe=True, revision=1)
    labels = [b.text for row in markup.inline_keyboard for b in row]

    assert any("Удалить мост" in label for label in labels)
    assert not any(label.startswith("Да") for label in labels)


def test_the_settings_screen_exists_and_holds_the_preferences() -> None:
    _, markup = screens.settings(mirror_own=True, timezone="Москва · UTC+03:00")
    rendered = str(markup)

    assert screens.SETTINGS_OWN in rendered
    assert screens.SETTINGS_TZ in rendered
    assert screens.DIAGNOSTICS in rendered
    assert screens.MENU in rendered, "and a way back to the panel"


def test_the_bridge_list_shows_all_four_states() -> None:
    """A provisioning bridge was invisible; a broken one looked like a live one."""
    rows = [
        _card(),
        _card(state=BridgeUiState.PROVISIONING),
        _card(state=BridgeUiState.BROKEN),
        _card(state=BridgeUiState.DISABLED),
    ]
    text, markup = screens.bridges_screen(rows)
    labels = [b.text for row in markup.inline_keyboard for b in row]

    assert len({label[0] for label in labels if label[0] in "🟢🟡🔴⚪"}) == 4
    assert "1 работает · 1 не работает · 1 создаётся · 1 отключён" in text


def test_no_ordinary_screen_carries_queue_vocabulary() -> None:
    """`failed_retryable`, `awaiting_owner`, `max-poller` and friends.

    They belong in Diagnostics and in Technical Details, which are not in this
    list — everything here is a screen an owner reaches without asking for
    anything technical.
    """
    ordinary = {
        name: screen
        for name, screen in _every_stable_screen().items()
        if "TECHNICAL" not in name and "DIAGNOSTICS" not in name
    }
    banned = (
        "failed_retryable",
        "awaiting_owner",
        "awaiting_confirmation",
        "max-poller",
        "owner-inbox",
        "ItemState",
        "outbox",
        "worker",
        "Traceback",
    )
    for name, (text, _) in ordinary.items():
        for word in banned:
            assert word not in text, f"{name} leaks «{word}»"
