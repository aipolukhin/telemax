"""The message the owner is looking at, before and after provisioning.

One message, edited. That is the whole design, and it is not cosmetic: a picker
that answers with a *second* message leaves two live keyboards in the chat, and
the older one refers to an account state that has moved on. Tapping it is how
somebody re-provisions a contact they already connected.

So when the run finishes, the selection buttons do not sit there greyed out —
they are gone, replaced by links to the bots that now exist. And the history
offer only appears once something is actually carrying messages.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bridge.provisioning import selection as ui
from bridge.provisioning.capacity import Capacity, PeerBotStatus, PeerPlan
from bridge.provisioning.flow import DialogFlow
from bridge.provisioning.journal import ItemState, JournalEntry, ProvisioningJournal
from bridge.provisioning.naming_v2 import contact_bot_username_v2
from bridge.provisioning.picker import DialogOption
from bridge.provisioning.provisioner import BotLimit
from tests.fake_provisioning import (
    FakeBridgeRepository,
    FakeDialogPicker,
    FakeGateway,
    FakeProvisioner,
)

#: The owner's Telegram id is half of every V2 username; the contact's
#: MAX id is the other half. No secret is involved any more.
OWNER = 100000001


def done(chat_id: int, title: str, *, needs_open: bool = False) -> JournalEntry:
    """A finished bridge. `needs_open` is the managed path, where a bot cannot
    open a chat and the owner's tap is the `/start`."""
    return JournalEntry(
        max_chat_id=chat_id,
        expected_username=f"c{chat_id}_max_bot",
        title=title,
        state=ItemState.HEALTHY,
        bridge_name=f"c{chat_id}",
        needs_open=needs_open,
    )


def failed(chat_id: int, title: str, *, permanent: bool = False) -> JournalEntry:
    return JournalEntry(
        max_chat_id=chat_id,
        expected_username=f"c{chat_id}_max_bot",
        title=title,
        state=ItemState.FAILED_PERMANENT if permanent else ItemState.FAILED_RETRYABLE,
        error="создание не завершено",
    )


# ------------------------------------------------------------------- the result


def test_a_bot_nobody_could_start_asks_the_owner_to() -> None:
    """The managed path: a bot cannot open a chat, so the owner's tap is it.

    `?start=` opens the chat *on its Start button* rather than on an empty
    conversation the owner has to work out what to do with.
    """
    rendered = str(
        ui.result_markup(
            [done(1, "Иван Петров", needs_open=True), done(2, "Мама", needs_open=True)],
            epoch=3,
        )
    )

    assert "Открыть и нажать Старт · Иван Петров" in rendered
    assert "https://t.me/c1_max_bot?start=" in rendered
    assert "https://t.me/c2_max_bot?start=" in rendered


def test_a_start_already_sent_is_never_asked_for_twice() -> None:
    """The owner's session sends `/start` itself, and then this button sent
    another: the chat read Start, the imported history, and Start again."""
    rendered = str(ui.result_markup([done(1, "Наталья")], epoch=3))

    assert "Открыть чат · Наталья" in rendered
    assert "Нажать Старт" not in rendered
    assert "https://t.me/c1_max_bot" in rendered
    assert "?start=" not in rendered


def test_the_url_points_at_the_deterministic_username() -> None:
    entry = done(1, "Мама")
    assert ui.bot_link(entry.expected_username) == "https://t.me/c1_max_bot"
    assert entry.expected_username in str(ui.result_markup([entry], epoch=0))


def test_the_old_selection_buttons_are_gone() -> None:
    """Nothing tappable may still refer to the list that has been acted on."""
    rendered = str(ui.result_markup([done(1, "Мама")], epoch=7))

    assert ui.SELECT not in rendered
    assert ui.GO not in rendered
    assert ui.PAGE not in rendered


def test_a_full_success_says_so_only_when_it_is_one() -> None:
    text = ui.result_text([done(1, "Иван"), done(2, "Мама")])

    assert "<b>Готово 🎉</b>" in text
    assert "Создано мостов: 2" in text
    assert "Сообщения будут передаваться автоматически." in text


def test_a_partial_failure_is_shown_line_by_line() -> None:
    text = ui.result_text([done(1, "Иван"), done(2, "Мама"), failed(3, "Папа")])

    assert "Создано 2 из 3 мостов" in text
    assert "✅ Иван" in text
    assert "🔴 Папа — создание не завершено" in text
    assert "Готово 🎉" not in text, "not a success"


def test_a_retryable_failure_gets_its_own_button() -> None:
    markup = ui.result_markup([done(1, "Мама"), failed(3, "Папа")], epoch=5)
    rendered = str(markup)

    assert "Повторить · Папа" in rendered
    assert f"{ui.RETRY}:5:3" in rendered
    assert "Открыть чат · Мама" in rendered


def test_a_permanent_failure_offers_no_retry() -> None:
    """Nothing to retry: the username belongs to somebody else."""
    rendered = str(ui.result_markup([failed(3, "Папа", permanent=True)], epoch=5))

    assert ui.RETRY not in rendered


def test_history_is_offered_only_once_something_works() -> None:
    with_bridge = str(ui.result_markup([done(1, "Мама")], epoch=1))
    without = str(ui.result_markup([failed(3, "Папа")], epoch=1))

    assert ui.HISTORY_ALL in with_bridge
    assert ui.HISTORY_ALL not in without


def test_progress_redraws_the_same_lines() -> None:
    entries = [done(1, "Иван"), failed(2, "Мама"), JournalEntry(3, "c3_max_bot", "Папа")]
    text = ui.progress_text(entries)

    assert "✅ Иван" in text
    assert "🔴 Мама" in text
    assert "○ Папа" in text
    assert "1 из 3" in text, "the count leads, not the list"


# ------------------------------------------------------------------ the picker


def plans(count: int) -> tuple[PeerPlan, ...]:
    return tuple(
        PeerPlan(
            max_chat_id=1000 + index,
            max_peer_id=2000 + index,
            title=f"Контакт {index}",
            expected_username=f"c{index}_max_bot",
            status=PeerBotStatus.NOT_CREATED,
        )
        for index in range(count)
    )


def selection(count: int = 3, *, limit: int | None = 20, owned: int = 13) -> ui.Selection:
    made = ui.Selection()
    made.reset(
        Capacity(limit=BotLimit(value=limit), owned_bot_count=owned, plans=plans(count))
    )
    return made


def test_the_done_button_counts_what_is_chosen() -> None:
    chooser = selection()
    assert "Создать · 0" in str(ui.picker_markup(chooser))

    chooser.toggle(1000)
    chooser.toggle(1001)
    assert "Создать · 2" in str(ui.picker_markup(chooser))


def test_a_chosen_dialog_is_ticked() -> None:
    chooser = selection()
    chooser.toggle(1000)

    rendered = str(ui.picker_markup(chooser))
    assert "✅ Контакт 0" in rendered
    assert "✅ Контакт 1" not in rendered
    assert "○ Контакт 1" in rendered, "unchosen rows are toggles too"


def test_a_refused_tap_leaves_the_selection_exactly_as_it_was() -> None:
    chooser = selection(count=3, limit=14, owned=13)
    chooser.toggle(1000)
    before = set(chooser.chosen)

    changed, alert = chooser.toggle(1001)

    assert not changed
    assert alert == ui.NO_SLOTS_ALERT
    assert chooser.chosen == before


def test_a_locked_selection_refuses_every_change() -> None:
    chooser = selection()
    chooser.toggle(1000)
    chooser.locked = True

    changed, alert = chooser.toggle(1001)

    assert not changed
    assert alert == ui.LOCKED_ALERT
    assert chooser.chosen == {1000}


def test_the_header_is_quiet_while_the_capacity_changes_nothing() -> None:
    """It used to carry five lines of accounting above the list of names.

    Bots owned, the limit, free slots, the sentence explaining that Telegram
    publishes neither, how many are chosen, how many of those are replacements
    — read by somebody who opened this screen to tap their mother's name.
    """
    chooser = selection(limit=20, owned=13)
    chooser.toggle(1000)

    text = ui.picker_text(chooser.priced())
    assert "Можно выбрать несколько." in text
    assert "Выбрано: 1" in text
    assert "Боты этой установки" not in text
    assert ui.NOT_THE_WHOLE_ACCOUNT not in text


def test_the_capacity_speaks_up_when_it_changes_what_is_possible() -> None:
    """Three cases earn a line: no room, an unknown limit, or nearly no room."""
    nearly = ui.picker_text(selection(limit=15, owned=13).priced())
    assert "Можно добавить ещё 2 контакта." in nearly

    full = ui.picker_text(selection(limit=13, owned=13).priced())
    assert ui.AT_LIMIT_TEXT in full

    unknown = ui.picker_text(selection(limit=None, owned=13).priced())
    assert "Лимит Telegram неизвестен" in unknown


def test_the_exact_numbers_are_still_computed() -> None:
    """They moved off the header; they did not stop being right.

    Bot API cannot list an account's bots and cannot read its limit, so on that
    path the count is this install's own and the limit comes from configuration
    — which is what `NOT_THE_WHOLE_ACCOUNT` exists to say, wherever it is said.
    """
    ours = selection(limit=20, owned=13).priced()

    assert ours.counts_only_ours
    assert ui.capacity_lines(ours, selection=False) == [
        "Боты этой установки: 13 из 20",
        "Свободно новых слотов: 7",
        ui.NOT_THE_WHOLE_ACCOUNT,
    ]


# -------------------------------------------------------------- the whole flow


@pytest.fixture
def flow(tmp_path: Path) -> DialogFlow:
    options = [
        DialogOption(
            max_chat_id=1000 + index,
            title=f"Контакт {index}",
            last_activity=index,
            max_user_id=2000 + index,
        )
        for index in range(3)
    ]
    return DialogFlow(
        picker=FakeDialogPicker(options),  # type: ignore[arg-type]
        provisioner=FakeProvisioner(),
        gateway=FakeGateway(),
        journal=ProvisioningJournal.for_data_dir(tmp_path),
        bridges=FakeBridgeRepository(),
        telegram_owner_user_id=OWNER,
    )


async def test_a_stale_epoch_changes_nothing(flow: DialogFlow) -> None:
    await flow.open()
    epoch = flow.selection.epoch
    await flow.open()  # the list is rebuilt; the old keyboard is a generation behind

    screen, alert = await flow.toggle(epoch, 1000)

    assert screen is None
    assert alert == ui.STALE_ALERT
    assert flow.selection.chosen == set()


async def test_committing_freezes_the_selection(flow: DialogFlow) -> None:
    await flow.open()
    epoch = flow.selection.epoch
    await flow.toggle(epoch, 1000)

    drawn: list[str] = []

    async def draw(text: str, markup: object) -> None:
        drawn.append(text)

    assert await flow.commit(epoch, draw) is None
    assert flow.selection.locked

    screen, alert = await flow.toggle(epoch, 1001)
    assert screen is None
    assert alert == ui.LOCKED_ALERT
    assert "Готово 🎉" in drawn[-1]


async def test_the_final_message_links_to_the_expected_username(
    flow: DialogFlow,
) -> None:
    await flow.open()
    epoch = flow.selection.epoch
    await flow.toggle(epoch, 1000)

    drawn: list[tuple[str, object]] = []

    async def draw(text: str, markup: object) -> None:
        drawn.append((text, markup))

    await flow.commit(epoch, draw)

    expected = contact_bot_username_v2(OWNER, 2000)
    assert f"https://t.me/{expected}" in str(drawn[-1][1])


async def test_nothing_selected_is_refused_rather_than_run(flow: DialogFlow) -> None:
    await flow.open()

    async def draw(text: str, markup: object) -> None:
        raise AssertionError("nothing should have been drawn")

    assert await flow.commit(flow.selection.epoch, draw) == ui.NOTHING_SELECTED_ALERT
