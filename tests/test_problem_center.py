"""What the owner does about a stuck message, without typing anything.

Before this, the whole of it was: an alert with no keyboard saying «Не
доставлено сообщений: 3 · Контакт: p6wyzx5zu7vv6pwcddx5 · Нужен повтор или
решение владельца», then `/failed`, then reading a list of `#41`, then
`/retry 41` — four persistent messages beside the anchor for one decision, and
the only name the owner was ever given for the person on the other side was the
deterministic bridge key.

The operations underneath are unchanged and that is the point of most of these
tests: «Повторить» is `retry_now`, «Дошло» is `resolve`, «Не отправлять» is
`archive`. AMBIGUOUS still never retries by itself — a duplicate lands in a real
person's chat (ADR 0002).
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
    AttemptFacts,
    AttentionFacts,
    JobFacts,
    ProblemKind,
    UserProblem,
    group_problems,
    home_view,
    problems,
)
from bridge.telegram import OwnerOnlyMiddleware
from tests.fake_telegram import OWNER_ID, FakeBot, make_callback

NOW = 1_786_192_400_000


@dataclass
class Control:
    """Only the surface the problem centre uses."""

    facts: AttentionFacts = field(
        default_factory=lambda: AttentionFacts(worker_running=True, max_connected=True)
    )
    jobs: list[JobFacts] = field(default_factory=list)
    attempts: list[AttemptFacts] = field(default_factory=list)
    retried: list[int] = field(default_factory=list)
    settled: list[int] = field(default_factory=list)
    archived: list[int] = field(default_factory=list)
    resumed: list[int] = field(default_factory=list)
    abandoned: list[int] = field(default_factory=list)
    refuse: bool = False

    async def status_lines(self) -> list[str]:
        return ["MAX подключён"]

    async def restart_bridge(self) -> bool:
        return True

    async def home_view(self) -> Any:
        return home_view(self.facts)

    async def problems(self) -> list[UserProblem]:
        return problems(
            self.facts, jobs=self.jobs, attempts=self.attempts, now_ms=NOW
        )

    async def retry_job(self, job_id: int) -> bool:
        self.retried.append(job_id)
        return not self.refuse

    async def settle_job(self, job_id: int) -> bool:
        self.settled.append(job_id)
        return not self.refuse

    async def archive_job(self, job_id: int) -> bool:
        self.archived.append(job_id)
        return not self.refuse

    async def resume_attempt(self, max_chat_id: int) -> str | None:
        self.resumed.append(max_chat_id)
        return "готово"

    async def abandon_attempt(self, max_chat_id: int) -> bool:
        self.abandoned.append(max_chat_id)
        return True

    def clear(self) -> None:
        """What acting on a job would do to the next read of the list."""
        self.jobs = []
        self.facts = replace(self.facts, failed=0, ambiguous=0)


def wire(tmp_path: Path, control: Control) -> tuple[Dispatcher, FakeBot]:
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
            control=control,  # type: ignore[arg-type]
            is_guardian=lambda _: True,
            show=board.show,
        )
    )
    return dispatcher, bot


def said(bot: FakeBot) -> str:
    return "\n".join(str(call.kwargs.get("text", "")) for call in bot.calls)


def last_text(bot: FakeBot) -> str:
    for call in reversed(bot.calls):
        if call.method in {"send_message", "edit_message_text"}:
            return str(call.kwargs.get("text", ""))
    raise AssertionError("nothing was drawn")


def last_markup(bot: FakeBot) -> str:
    for call in reversed(bot.calls):
        if "reply_markup" in call.kwargs:
            return str(call.kwargs["reply_markup"])
    raise AssertionError("no keyboard was drawn")


def failed_job(job_id: int = 41, contact: str = "Мама") -> JobFacts:
    return JobFacts(
        job_id=job_id,
        bridge_name="p6wyzx5zu7vv6pwcddx5",
        contact=contact,
        direction="tg_to_max",
        kind="tg_to_max_text",
        created_at=NOW - 90 * 60_000,
    )


def ambiguous_job(job_id: int = 42, contact: str = "Мама") -> JobFacts:
    return replace(failed_job(job_id, contact), ambiguous=True)


# ------------------------------------------------------------------ the list


def test_one_outage_is_one_problem_not_three() -> None:
    """`max-offline`, `queue-depth` and `queue-stalled` are one answer: wait."""
    facts = AttentionFacts(
        worker_running=True,
        max_connected=False,
        queued=28,
        oldest_pending_ms=20 * 60_000,
        max_offline_ms=16 * 60_000,
    )

    found = problems(facts, jobs=[], attempts=[])

    assert [item.kind for item in found] == [ProblemKind.MAX_OFFLINE]
    assert "28 сообщений ожидают отправки" in found[0].lines[0]


def test_failed_and_ambiguous_never_merge() -> None:
    """They ask different questions: «повторить?» and «дошло ли оно?»."""
    facts = AttentionFacts(
        worker_running=True, max_connected=True, failed=1, ambiguous=1
    )

    found = problems(facts, jobs=[failed_job(), ambiguous_job()], attempts=[])

    assert [item.kind for item in found] == [
        ProblemKind.DELIVERY_FAILED,
        ProblemKind.DELIVERY_AMBIGUOUS,
    ]


def test_the_badge_agrees_with_the_list() -> None:
    """Home counts before it can expand; the two must not disagree."""
    facts = AttentionFacts(
        worker_running=True,
        max_connected=True,
        failed=2,
        ambiguous=1,
        provisioning_unfinished=1,
    )

    assert home_view(facts).problems == len(
        problems(
            facts,
            jobs=[failed_job(1), failed_job(2), ambiguous_job(3)],
            attempts=[AttemptFacts(max_chat_id=-1, contact="Папа")],
        )
    )


async def test_the_list_names_people_not_keys(tmp_path: Path) -> None:
    """`Контакт: p6wyzx5zu7vv6pwcddx5` was the owner's only handle."""
    control = Control(
        facts=AttentionFacts(worker_running=True, max_connected=True, failed=1),
        jobs=[failed_job()],
    )
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(bot, make_callback(1, screens.PROBLEMS))  # type: ignore[arg-type]

    # The index is an index: one row per family, with a count.
    text, rendered = last_text(bot), last_markup(bot)
    assert "1 сообщение требует решения" in text
    assert "Не отправлено · 1" in rendered
    assert "p6wyzx5zu7vv6pwcddx5" not in text + rendered

    # The contact's name is on the category page, one tap in.
    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(2, screens.category_callback(ProblemKind.DELIVERY_FAILED))
    )
    text, rendered = last_text(bot), last_markup(bot)
    assert "Мама" in rendered
    assert "p6wyzx5zu7vv6pwcddx5" not in text + rendered
    assert "#41" not in text, "a job id is a handle for an operator, not a name"


async def test_no_message_content_reaches_the_screen(tmp_path: Path) -> None:
    control = Control(
        facts=AttentionFacts(worker_running=True, max_connected=True, failed=1),
        jobs=[failed_job()],
    )
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(bot, make_callback(1, screens.PROBLEMS))  # type: ignore[arg-type]
    problem = (await control.problems())[0]
    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(2, screens.problem_callback(problem))
    )

    # `JobFacts` carries no payload at all — there is nothing to leak, and this
    # is the assertion that keeps it that way.
    assert not hasattr(control.jobs[0], "payload_json")
    assert "payload" not in said(bot)


# ---------------------------------------------------------------- the actions


async def test_retry_calls_the_existing_retry_exactly_once(tmp_path: Path) -> None:
    control = Control(
        facts=AttentionFacts(worker_running=True, max_connected=True, failed=1),
        jobs=[failed_job()],
    )
    dispatcher, bot = wire(tmp_path, control)
    problem = (await control.problems())[0]

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(1, screens.problem_callback(problem))
    )
    assert "Telemax точно знает" in last_text(bot)

    control.clear()
    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(2, screens.job_callback(screens.JOB_RETRY, 41))
    )

    assert control.retried == [41]
    assert control.settled == [] and control.archived == []
    assert "Проблем нет" in last_text(bot), "and the list is redrawn"


async def test_settle_calls_the_existing_settlement_exactly_once(tmp_path: Path) -> None:
    control = Control(
        facts=AttentionFacts(worker_running=True, max_connected=True, ambiguous=1),
        jobs=[ambiguous_job()],
    )
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(1, screens.job_callback(screens.JOB_SETTLE, 42))
    )

    assert control.settled == [42]
    assert control.retried == []


async def test_archive_uses_the_existing_archive_path(tmp_path: Path) -> None:
    control = Control(
        facts=AttentionFacts(worker_running=True, max_connected=True, failed=1),
        jobs=[failed_job()],
    )
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(1, screens.job_callback(screens.JOB_ARCHIVE, 41))
    )

    assert control.archived == [41]


async def test_an_ambiguous_job_is_never_retried_by_opening_it(tmp_path: Path) -> None:
    """Opening the screen must not settle anything. Only a tap decides.

    A duplicate lands in a real person's chat, so the safety here is that
    reading about the problem does nothing at all to the job.
    """
    control = Control(
        facts=AttentionFacts(worker_running=True, max_connected=True, ambiguous=1),
        jobs=[ambiguous_job()],
    )
    dispatcher, bot = wire(tmp_path, control)
    problem = (await control.problems())[0]

    await dispatcher.feed_update(bot, make_callback(1, screens.PROBLEMS))  # type: ignore[arg-type]
    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(2, screens.problem_callback(problem))
    )

    assert control.retried == [] and control.settled == [] and control.archived == []
    text = last_text(bot)
    assert "Повтор может создать дубликат" in text
    # And «Дошло» is offered first, because it is the answer that costs nothing.
    rendered = last_markup(bot)
    assert rendered.index("Дошло") < rendered.index("Повторить")


async def test_a_job_that_has_gone_says_so_instead_of_pretending(tmp_path: Path) -> None:
    control = Control(
        facts=AttentionFacts(worker_running=True, max_connected=True, failed=1),
        jobs=[failed_job()],
        refuse=True,
    )
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(1, screens.job_callback(screens.JOB_RETRY, 41))
    )

    toasts = [
        str(call.kwargs.get("text") or "")
        for call in bot.calls
        if call.method == "answer_callback_query"
    ]
    assert any("уже нет в очереди" in toast for toast in toasts)


# --------------------------------------------------------------- provisioning


async def test_a_stalled_attempt_offers_the_two_things_that_exist(
    tmp_path: Path,
) -> None:
    """`шаг awaiting_confirmation · timeout · бот 8123456789` and no buttons."""
    control = Control(
        facts=AttentionFacts(
            worker_running=True, max_connected=True, provisioning_unfinished=1
        ),
        attempts=[
            AttemptFacts(
                max_chat_id=-400000004,
                contact="Папа",
                username="example_contact_max_bot",
                reason="Telegram попросил подождать",
            )
        ],
    )
    dispatcher, bot = wire(tmp_path, control)
    problem = (await control.problems())[0]

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(1, screens.problem_callback(problem))
    )

    text = last_text(bot)
    assert "Папа" in text
    assert "awaiting_confirmation" not in text and "timeout" not in text
    assert "example_contact_max_bot" not in text, "a username is for the deletion screen"

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(2, f"{screens.ATTEMPT_RETRY}:-400000004")
    )
    assert control.resumed == [-400000004]

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(3, f"{screens.ATTEMPT_DROP}:-400000004")
    )
    assert control.abandoned == [-400000004]


# ------------------------------------------------------------------ the shape


async def test_back_always_returns_to_problems_or_home(tmp_path: Path) -> None:
    control = Control(
        facts=AttentionFacts(
            worker_running=True, max_connected=False, failed=1, ambiguous=1, queued=3
        ),
        jobs=[failed_job(), ambiguous_job()],
    )
    dispatcher, bot = wire(tmp_path, control)

    for index, problem in enumerate(await control.problems()):
        await dispatcher.feed_update(  # type: ignore[arg-type]
            bot, make_callback(index + 1, screens.problem_callback(problem))
        )
        assert screens.PROBLEMS in last_markup(bot), problem.kind

    await dispatcher.feed_update(bot, make_callback(90, screens.PROBLEMS))  # type: ignore[arg-type]
    assert screens.MENU in last_markup(bot)


async def test_the_whole_walk_is_one_message(tmp_path: Path) -> None:
    """The problem centre is the anchor, not a second conversation beside it."""
    control = Control(
        facts=AttentionFacts(worker_running=True, max_connected=True, failed=1),
        jobs=[failed_job()],
    )
    dispatcher, bot = wire(tmp_path, control)
    problem = (await control.problems())[0]

    await dispatcher.feed_update(bot, make_callback(1, screens.PROBLEMS))  # type: ignore[arg-type]
    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(2, screens.problem_callback(problem))
    )
    control.clear()
    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(3, screens.job_callback(screens.JOB_RETRY, 41))
    )

    assert len(bot.method_calls("send_message")) == 1
    assert bot.method_calls("edit_message_text"), "everything after is an edit"


def test_no_problem_screen_carries_an_enum_or_a_key() -> None:
    facts = AttentionFacts(
        worker_running=True,
        max_connected=False,
        failed=1,
        ambiguous=1,
        provisioning_unfinished=1,
        queued=3,
        max_offline_ms=60_000,
    )
    found = problems(
        facts,
        jobs=[failed_job(), ambiguous_job()],
        attempts=[
            AttemptFacts(max_chat_id=-1, contact="Папа", username="example_contact_max_bot")
        ],
        now_ms=NOW,
    )

    drawn = [screens.problems_screen(group_problems(found))[0]]
    for problem in found:
        for builder in (
            screens.problem_screen,
            screens.failed_job_screen,
            screens.ambiguous_job_screen,
            screens.attempt_screen,
        ):
            drawn.append(builder(problem)[0])

    for text in drawn:
        for banned in (
            "p6wyzx5zu7vv6pwcddx5",
            "example_contact_max_bot",
            "failed_retryable",
            "awaiting_owner",
            "tg_to_max_text",
            "outbox",
            "#41",
        ):
            assert banned not in text, f"«{banned}» in: {text}"


# ------------------------------------------------------- categories and pages


def many(failed: int = 0, ambiguous: int = 0) -> list[JobFacts]:
    made = [failed_job(index, f"Контакт {index}") for index in range(failed)]
    made += [
        ambiguous_job(1000 + index, f"Человек {index}") for index in range(ambiguous)
    ]
    return made


async def test_the_index_opens_a_category_and_the_category_opens_a_job(
    tmp_path: Path,
) -> None:
    """The whole point of the index: nothing is hidden, it is one tap further."""
    control = Control(
        facts=AttentionFacts(
            worker_running=True, max_connected=True, failed=16, ambiguous=82
        ),
        jobs=many(failed=16, ambiguous=82),
    )
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(bot, make_callback(1, screens.PROBLEMS))  # type: ignore[arg-type]
    assert "Неясно, дошло ли · 82" in last_markup(bot)
    assert "Не отправлено · 16" in last_markup(bot)

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(2, screens.category_callback(ProblemKind.DELIVERY_AMBIGUOUS))
    )
    assert "1 / 11" in last_markup(bot), "82 at eight a page"
    assert "Человек 0" in last_markup(bot)

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot,
        make_callback(3, screens.category_callback(ProblemKind.DELIVERY_AMBIGUOUS, 10)),
    )
    assert "11 / 11" in last_markup(bot)
    assert "Человек 81" in last_markup(bot), "the last page carries the last job"


async def test_a_job_opened_from_page_seven_goes_back_to_page_seven(
    tmp_path: Path,
) -> None:
    """Returning to page one every time is how a long list becomes unusable."""
    control = Control(
        facts=AttentionFacts(worker_running=True, max_connected=True, ambiguous=82),
        jobs=many(ambiguous=82),
    )
    dispatcher, bot = wire(tmp_path, control)
    found = await control.problems()
    # job index 50 lives on page 6 (zero-based), at eight a page.
    problem = next(p for p in found if p.job_id == 1050)

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(1, screens.problem_callback(problem))
    )
    rendered = last_markup(bot)

    assert "Человек 50" in last_text(bot)
    assert "← Неясные отправки" in rendered
    assert screens.category_callback(ProblemKind.DELIVERY_AMBIGUOUS, 6) in rendered


async def test_acting_on_a_job_redraws_its_page(tmp_path: Path) -> None:
    control = Control(
        facts=AttentionFacts(worker_running=True, max_connected=True, ambiguous=20),
        jobs=many(ambiguous=20),
    )
    dispatcher, bot = wire(tmp_path, control)

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(1, screens.job_callback(screens.JOB_SETTLE, 1009))
    )

    assert control.settled == [1009]
    # Back on the page that job was on, not on the index.
    assert "Неясные отправки" in last_text(bot)
    assert "2 / 3" in last_markup(bot)


async def test_emptying_the_last_page_falls_back_rather_than_stranding(
    tmp_path: Path,
) -> None:
    """Settling the only item on the last page must not leave a page 3 of 2."""
    control = Control(
        facts=AttentionFacts(worker_running=True, max_connected=True, ambiguous=17),
        jobs=many(ambiguous=17),
    )
    dispatcher, bot = wire(tmp_path, control)

    async def settle(job_id: int) -> bool:
        control.jobs = [job for job in control.jobs if job.job_id != job_id]
        control.facts = replace(control.facts, ambiguous=len(control.jobs))
        control.settled.append(job_id)
        return True

    control.settle_job = settle  # type: ignore[method-assign]

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(1, screens.job_callback(screens.JOB_SETTLE, 1016))
    )

    assert "2 / 2" in last_markup(bot), "clamped onto the last page that exists"
    assert screens.PROBLEMS in last_markup(bot)


async def test_emptying_a_category_returns_to_problems(tmp_path: Path) -> None:
    control = Control(
        facts=AttentionFacts(worker_running=True, max_connected=True, ambiguous=1),
        jobs=many(ambiguous=1),
    )
    dispatcher, bot = wire(tmp_path, control)

    async def settle(job_id: int) -> bool:
        control.clear()
        control.settled.append(job_id)
        return True

    control.settle_job = settle  # type: ignore[method-assign]

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(1, screens.job_callback(screens.JOB_SETTLE, 1000))
    )

    assert "Проблем нет" in last_text(bot)
    assert screens.MENU in last_markup(bot), "and a way home"


async def test_opening_a_category_or_a_page_mutates_nothing(tmp_path: Path) -> None:
    control = Control(
        facts=AttentionFacts(worker_running=True, max_connected=True, ambiguous=30),
        jobs=many(ambiguous=30),
    )
    dispatcher, bot = wire(tmp_path, control)

    for index, data in enumerate(
        [
            screens.PROBLEMS,
            screens.category_callback(ProblemKind.DELIVERY_AMBIGUOUS),
            screens.category_callback(ProblemKind.DELIVERY_AMBIGUOUS, 2),
            screens.category_callback(ProblemKind.DELIVERY_AMBIGUOUS, 999),
        ]
    ):
        await dispatcher.feed_update(bot, make_callback(index + 1, data))  # type: ignore[arg-type]

    assert control.retried == []
    assert control.settled == []
    assert control.archived == []
