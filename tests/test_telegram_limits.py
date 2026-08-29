"""No stable guardian screen may exceed what Telegram will accept.

This is not a style rule. A message over 4096 characters, or a keyboard over a
hundred rows, is rejected outright — `editMessageText` fails, `StatusBoard`
falls back to `sendMessage`, that fails for the same reason, and the owner taps
a button and nothing happens at all. The failure is completely silent from the
chat.

It shipped exactly once, and in the state that most needed the screen: eighty-two
ambiguous sends and sixteen failed ones rendered the problem centre at 8173
characters and 101 rows, so «⚠️ Проблемы · 100» opened nothing.

The fixture that caught it is not enough on its own — a hundred was simply the
number production happened to have that afternoon. What has to hold is the
*shape*: the index is bounded by how many kinds of problem exist, and a category
page is bounded by its page size, whatever the job count is.
"""

from __future__ import annotations

import pytest

from bridge.onboarding import screens
from bridge.onboarding.views import (
    PAGE_SIZE,
    AttemptFacts,
    AttentionFacts,
    JobFacts,
    ProblemKind,
    group_problems,
    home_view,
    problems,
)

#: Telegram's own limits, and the reason each one matters here.
TEXT_LIMIT = 4096
ROW_LIMIT = 100
BUTTON_LIMIT = 100
CALLBACK_LIMIT = 64

NOW = 1_786_192_400_000


def _jobs(failed: int, ambiguous: int) -> list[JobFacts]:
    made = [
        JobFacts(
            job_id=index,
            bridge_name="p6wyzx5zu7vv6pwcddx5",
            contact="Иван Петров",
            direction="tg_to_max",
            created_at=NOW - 90 * 60_000,
        )
        for index in range(failed)
    ]
    made += [
        JobFacts(
            job_id=failed + index,
            bridge_name="cgw3ihd3tqcr4yrqnwqw",
            contact="Мама",
            direction="max_to_tg",
            ambiguous=True,
            created_at=NOW - 91 * 60_000,
        )
        for index in range(ambiguous)
    ]
    return made


def _groups(failed: int, ambiguous: int, attempts: int = 0):  # type: ignore[no-untyped-def]
    facts = AttentionFacts(
        worker_running=True,
        max_connected=True,
        bridges=4,
        failed=failed,
        ambiguous=ambiguous,
        provisioning_unfinished=attempts,
    )
    found = problems(
        facts,
        jobs=_jobs(failed, ambiguous),
        attempts=[
            AttemptFacts(max_chat_id=-index, contact=f"Контакт {index}")
            for index in range(attempts)
        ],
        now_ms=NOW,
    )
    return facts, group_problems(found)


def _fits(screen: tuple[str, object], where: str) -> None:
    text, markup = screen
    assert len(text) <= TEXT_LIMIT, f"{where}: {len(text)} chars > {TEXT_LIMIT}"
    rows = getattr(markup, "inline_keyboard", [])
    assert len(rows) <= ROW_LIMIT, f"{where}: {len(rows)} rows > {ROW_LIMIT}"
    buttons = [button for row in rows for button in row]
    assert len(buttons) <= BUTTON_LIMIT, f"{where}: {len(buttons)} buttons"
    for button in buttons:
        data = button.callback_data
        if data is not None:
            assert len(data.encode()) <= CALLBACK_LIMIT, f"{where}: callback {data!r}"


# ------------------------------------------------------------------- the shape


@pytest.mark.parametrize("count", [0, 1, 10, 100, 1_000, 10_000])
def test_the_problem_index_stays_within_telegram_at_any_volume(count: int) -> None:
    """The regression, generalised. Ten thousand is not a realistic number; the
    point is that nothing in this screen is proportional to the job count."""
    _, groups = _groups(failed=count // 2, ambiguous=count - count // 2, attempts=2)

    _fits(screens.problems_screen(groups), f"index at {count}")


@pytest.mark.parametrize("count", [0, 1, 10, 100, 1_000, 10_000])
def test_the_index_is_bounded_by_kinds_not_by_jobs(count: int) -> None:
    _, groups = _groups(failed=count // 2, ambiguous=count - count // 2, attempts=2)
    _, markup = screens.problems_screen(groups)
    rows = len(markup.inline_keyboard)  # type: ignore[union-attr]

    assert rows <= len(ProblemKind) + 1, f"{rows} rows for {count} problems"


@pytest.mark.parametrize("count", [1, 10, 100, 1_000, 10_000])
def test_every_category_page_stays_within_telegram(count: int) -> None:
    """First, middle and last page of the biggest family."""
    _, groups = _groups(failed=0, ambiguous=count)
    group = next(one for one in groups if one.kind is ProblemKind.DELIVERY_AMBIGUOUS)

    for page in {0, group.pages // 2, group.pages - 1}:
        _fits(screens.category_screen(group, page), f"page {page} of {group.pages}")


@pytest.mark.parametrize("count", [1, 10, 100, 1_000, 10_000])
def test_a_category_page_is_bounded_by_its_page_size(count: int) -> None:
    _, groups = _groups(failed=0, ambiguous=count)
    group = next(one for one in groups if one.kind is ProblemKind.DELIVERY_AMBIGUOUS)
    _, markup = screens.category_screen(group, 0)
    rows = markup.inline_keyboard  # type: ignore[union-attr]

    # items, at most one pager row, one way back
    assert len(rows) <= PAGE_SIZE + 2
    assert len(group.page(0)) <= PAGE_SIZE


def test_every_page_of_a_big_category_is_reachable_and_whole() -> None:
    """Nothing is hidden. «Show the first ten» would strand the other seventy."""
    _, groups = _groups(failed=0, ambiguous=82)
    group = next(one for one in groups if one.kind is ProblemKind.DELIVERY_AMBIGUOUS)

    assert group.pages == -(-82 // PAGE_SIZE) == 11
    seen = [item.key for page in range(group.pages) for item in group.page(page)]
    assert len(seen) == 82, "every job is on exactly one page"
    assert len(set(seen)) == 82


def test_a_stale_page_number_is_clamped_not_an_error() -> None:
    _, groups = _groups(failed=0, ambiguous=10)
    group = next(one for one in groups if one.kind is ProblemKind.DELIVERY_AMBIGUOUS)

    assert group.page(999) == group.page(group.pages - 1)
    assert group.page(-5) == group.page(0)
    _fits(screens.category_screen(group, 999), "clamped page")


# --------------------------------------------------------------- the numbers


def test_the_badge_the_index_and_the_categories_all_agree() -> None:
    """Production's own shape: 82 ambiguous, 16 failed, 2 provisioning."""
    facts, groups = _groups(failed=16, ambiguous=82, attempts=2)

    assert home_view(facts).problems == 100
    assert sum(group.count for group in groups) == 100

    counts = {group.kind: group.count for group in groups}
    assert counts[ProblemKind.DELIVERY_AMBIGUOUS] == 82
    assert counts[ProblemKind.DELIVERY_FAILED] == 16
    assert counts[ProblemKind.PROVISIONING] == 2

    text, markup = screens.problems_screen(groups)
    rendered = str(markup)
    assert "Неясно, дошло ли · 82" in rendered
    assert "Не отправлено · 16" in rendered
    assert "Создание мостов · 2" in rendered
    # The body may summarise; it may not contradict.
    assert "98 сообщений требуют решения" in text
    assert "2 моста не созданы" in text


def test_the_index_offers_no_bulk_action() -> None:
    """Especially not for AMBIGUOUS: each one is a message to a real person, and
    «resolve all» is a way to mark eighty-two of them delivered without looking."""
    _, groups = _groups(failed=16, ambiguous=82, attempts=2)
    screens_to_check = [screens.problems_screen(groups)]
    for group in groups:
        if group.paged:
            screens_to_check.append(screens.category_screen(group, 0))

    for text, markup in screens_to_check:
        labels = [b.text for row in markup.inline_keyboard for b in row]  # type: ignore[union-attr]
        for label in labels:
            lowered = label.lower()
            assert "все" not in lowered, label
            assert "всё" not in lowered, label
        assert "все" not in text.lower()


def test_no_category_row_carries_a_job_id() -> None:
    _, groups = _groups(failed=0, ambiguous=20)
    group = next(one for one in groups if one.kind is ProblemKind.DELIVERY_AMBIGUOUS)
    text, markup = screens.category_screen(group, 0)
    labels = [b.text for row in markup.inline_keyboard for b in row]  # type: ignore[union-attr]

    for label in labels:
        assert "#" not in label
        assert "p6wyzx" not in label and "cgw3ihd" not in label
    assert "#" not in text


def test_a_category_row_names_a_person_and_a_moment() -> None:
    _, groups = _groups(failed=0, ambiguous=3)
    group = next(one for one in groups if one.kind is ProblemKind.DELIVERY_AMBIGUOUS)
    _, markup = screens.category_screen(group, 0)
    labels = [b.text for row in markup.inline_keyboard for b in row]  # type: ignore[union-attr]

    assert any(label.startswith("Мама · ") for label in labels), labels
