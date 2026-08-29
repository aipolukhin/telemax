"""Screens the owner could be left standing on with nothing to tap.

The guardian keeps one message. That is its best property and its sharpest edge:
when a final frame is drawn with `reply_markup=None`, the owner's entire control
surface becomes a sentence, and the only way out is `/menu` — a command that
nothing on the screen mentions and that a person who has been using buttons for
three months has no reason to know.

Eight refusals, the history summary, the picker, the result screen and the
«Несколько номеров» list were all in that state. The last of them was worse than
a dead end: every button on it reached the wrong handler and answered «Не понял
кнопку.»
"""

from __future__ import annotations

import pathlib
import re

from bridge.onboarding import screens
from bridge.provisioning import byphone, guardian
from bridge.provisioning import history as history_screens
from bridge.provisioning import selection as ui

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _has_a_way_out(markup: object) -> bool:
    rows = getattr(markup, "inline_keyboard", None)
    if not rows:
        return False
    return any(button.callback_data or button.url for row in rows for button in row)


# ------------------------------------------------------------ the routing bug


def test_the_several_numbers_screen_reaches_its_own_handler() -> None:
    """`prov:add:pick:<epoch>:<index>` was swallowed by the `prov:` catch-all.

    `parse_decision` cannot read `"pick"` as a chat id, so every row of the
    screen shown when a shared contact card holds several MAX numbers answered
    the toast «Не понял кнопку.» and did nothing. Only «Отмена» worked.
    """
    source = (ROOT / "bridge/provisioning/guardian.py").read_text(encoding="utf-8")
    order = re.findall(r'F\.data\.startswith\((?:f?)"(?:\{byphone\.(\w+)\}|(prov):)', source)
    names = [first or second for first, second in order]

    assert "PICK" in names, "the handler is registered at all"
    assert names.index("PICK") < names.index("prov"), (
        "prov:add:pick must be registered before the prov: catch-all, or every"
        " row of «Несколько номеров» reaches _decide and answers «Не понял кнопку.»"
    )


def test_every_prov_child_prefix_is_registered_before_its_parent() -> None:
    """The `onb:` namespace has had this guard for months. `prov:` had none.

    Only a *prefix* filter can swallow anything: `F.data == "prov:add"` never
    matches `prov:add:phone`, but `F.data.startswith("prov:")` matches both — and
    a handler that runs stops the routing.
    """
    source = (ROOT / "bridge/provisioning/guardian.py").read_text(encoding="utf-8")
    #: (display name, callback value, whether it is a prefix filter)
    registered: list[tuple[str, str, bool]] = []
    for match in re.finditer(
        r'F\.data(?:\.startswith\(f?"(?:\{(?:byphone|ui)\.(\w+)\}:|(prov:))"\)'
        r'|\s*==\s*(?:byphone|ui)\.(\w+))',
        source,
    ):
        prefix_name, catch_all, equality = match.groups()
        if catch_all:
            registered.append(("the prov: catch-all", "prov", True))
            continue
        name = prefix_name or equality
        value = getattr(byphone, name, None) or getattr(ui, name, None)
        if isinstance(value, str) and value.startswith("prov:"):
            registered.append((name, value, bool(prefix_name)))

    assert any(is_prefix for _, _, is_prefix in registered), "the hazard is real"

    for index, (parent_name, parent, is_prefix) in enumerate(registered):
        if not is_prefix:
            continue
        for later, (child_name, child, _) in enumerate(registered):
            if later <= index or not child.startswith(f"{parent}:"):
                continue
            raise AssertionError(
                f"{child_name} ({child}) is a child of {parent_name} ({parent}) and"
                " is registered after it — every tap on it reaches the wrong"
                " handler, silently"
            )


def test_the_catch_all_also_refuses_the_shapes_it_must_not_answer() -> None:
    """Belt and braces: registration order is easy to break by moving code."""
    for data in (
        byphone.pick_callback(1, epoch=3),
        byphone.confirm_callback(epoch=3),
        byphone.import_callback(epoch=3),
    ):
        assert guardian.parse_decision(data) is None, data


def test_no_callback_constant_is_defined_and_never_drawn() -> None:
    """`byphone.CANCEL = "prov:add:no"` existed, was rendered nowhere, and
    would have answered «Не понял кнопку.» if it ever had been."""
    assert not hasattr(byphone, "CANCEL")


# --------------------------------------------------------------- the dead ends


def test_the_history_summary_has_a_way_on() -> None:
    """It was the guardian's most reliable dead end: a list of counts and
    `reply_markup=None`, reached from three different buttons."""
    assert _has_a_way_out(history_screens.summary_markup())


def test_the_picker_has_a_way_back() -> None:
    """There was no Back and no Cancel: the only exits were «Готово», which
    creates bots, and a slash command nothing on the screen mentioned."""
    from tests.test_selection_ui import selection

    markup = ui.picker_markup(selection(limit=20, owned=1))
    labels = [button.callback_data for row in markup.inline_keyboard for button in row]

    assert byphone.ADD in labels


def test_the_result_screen_leads_somewhere() -> None:
    """It ended the most common successful flow in the guardian with links into
    the new chats, «Ещё контакт», and no route to «Мосты» or to the panel."""
    from bridge.provisioning.journal import ItemState, JournalEntry

    entry = JournalEntry(
        max_chat_id=1,
        expected_username="example_contact_max_bot",
        title="Мама",
        state=ItemState.HEALTHY,
        telegram_bot_id=42,
    )
    markup = ui.result_markup([entry], epoch=1)
    labels = [button.callback_data for row in markup.inline_keyboard for button in row]

    assert ui.BRIDGES in labels
    assert ui.HOME in labels


def test_declining_the_history_actually_closes_something() -> None:
    """«Закрыть» and «Не сейчас» answered a toast and left the screen alone."""
    _, markup = ui.history_declined()

    assert _has_a_way_out(markup)


def test_every_refusal_carries_a_keyboard() -> None:
    """Eight of them replaced the whole control surface with one sentence."""
    for text in (
        guardian.NOT_RUNNING,
        guardian.NO_AUTOMATION,
        byphone.NO_DIRECTORY,
        byphone.NOT_RUNNING,
    ):
        _, markup = byphone.refusal(text)
        assert _has_a_way_out(markup), text


def test_the_callback_constants_copied_by_value_still_agree() -> None:
    """Three modules name onboarding's callbacks by value to avoid a cycle."""
    assert byphone.HOME == screens.MENU
    assert ui.HOME == screens.MENU
    assert ui.BRIDGES == screens.BRIDGES
    assert history_screens.HOME == screens.MENU
    assert history_screens.BRIDGES == screens.BRIDGES


# ------------------------------------------------------------- the leaked bits


def test_the_published_command_menu_offers_no_operator_tools() -> None:
    """Nine of the fourteen commands are operator-shaped and stay unpublished.

    `/restart` interrupts delivery and used to sit in the same four-item list as
    «Панель», one mis-tap away. It still works and it is a button under
    Diagnostics.
    """
    from bridge.provisioning.runtime import COMMANDS

    published = {name for name, _ in COMMANDS}

    assert published == {"menu", "bridges", "add", "status"}
    for operator in (
        "failed",
        "ambiguous",
        "retry",
        "resolved",
        "archive",
        "archived",
        "attempts",
        "provretry",
        "provabandon",
        "restart",
    ):
        assert operator not in published


def test_the_operator_commands_still_answer() -> None:
    """Unpublished is not removed. They are the recovery path for the case where
    a screen is the thing that is broken."""
    from bridge.service.incidents import build_incidents_router

    router = build_incidents_router(None)

    assert len(router.message.handlers) == 9, "all nine still registered"


def test_only_transient_frames_are_drawn_without_a_keyboard() -> None:
    """A source-level sweep for the shape that produced every dead end.

    `draw(text, None)` is legitimate while an operation is running and for
    nothing else, so the survivors are listed by name — adding a tenth has to be
    a deliberate act rather than a habit.
    """
    permitted = {
        # Progress frames, replaced by the next frame of the same run and
        # finally by a summary that does carry buttons.
        ("bridge/provisioning/flow.py", "history_progress(reports)"),
        ("bridge/provisioning/flow.py", "ui.progress_text(current)"),
    }
    offenders: list[str] = []
    for path in sorted((ROOT / "bridge/provisioning").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            found = re.search(r"draw\(\s*([^,]+?)\s*,\s*None\s*\)", line)
            if found and (relative, found.group(1)) not in permitted:
                offenders.append(f"{relative}:{number}: {line.strip()}")

    assert offenders == [], "a frame with no keyboard: " + "; ".join(offenders)
