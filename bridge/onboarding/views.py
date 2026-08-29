"""What the guardian's screens are allowed to know.

The home screen used to be built by handing `menu_lines()` — a list of finished
sentences — to a renderer that could do nothing but escape them. Everything that
decided what the owner saw therefore lived inside the worker: the verdict, the
counts, the capacity caveat. Adding "🟡 требует внимания" meant teaching the
worker about attention, and the worker is the wrong place to be making a claim
about what deserves the owner's morning.

So there is a model in between. Facts come out of the services — connected,
failed, unfinished, how deep the queue is — and this module turns them into the
three or four things a screen actually renders. Nothing here talks to a
database, nothing here performs an action, and every function is pure, which is
what makes the states testable without a Telegram at all.

The one rule the whole file exists to enforce: **the first screen answers three
questions and no others.** Is Telemax working, are Telegram and MAX working, and
is there anything the owner has to do. Bot slots, queue internals, task names
and timezones are answers to questions nobody opened the bot to ask, and they
live behind a tap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import tzinfo
from enum import StrEnum

from bridge.telegram import humanise
from bridge.telegram.design import ATTENTION, BROKEN, DISABLED, RUNNING

__all__ = [
    "AMBIGUOUS_TITLE",
    "CATEGORY_LABELS",
    "CATEGORY_SLUGS",
    "FAILED_TITLE",
    "KIND_BY_SLUG",
    "PAGE_SIZE",
    "PER_ITEM_KINDS",
    "AttemptFacts",
    "AttentionFacts",
    "BridgeFacts",
    "BridgeUiState",
    "BridgeView",
    "HomeState",
    "HomeView",
    "JobFacts",
    "ProblemGroup",
    "ProblemKind",
    "Severity",
    "UserProblem",
    "bridge_view",
    "bridge_views",
    "group_problems",
    "home_view",
    "problems",
    "verdict",
]


class HomeState(StrEnum):
    """The three answers the first screen may give."""

    HEALTHY = "healthy"
    ATTENTION = "attention"
    BROKEN = "broken"


class BridgeUiState(StrEnum):
    """How one bridge reads on a list, which is not the same as its lifecycle.

    `PROVISIONING` and `BROKEN` were both invisible before: a bridge being made
    was absent from the list altogether and a bridge whose bot never came up was
    drawn exactly like a working one. The lifecycle states did not change — the
    presentation stopped throwing two of them away.
    """

    ACTIVE = "active"
    PROVISIONING = "provisioning"
    BROKEN = "broken"
    DISABLED = "disabled"


class Severity(StrEnum):
    BROKEN = "broken"
    ATTENTION = "attention"


class ProblemKind(StrEnum):
    """One kind per *decision the owner has to make*, not per internal event.

    A MAX outage raises `max-offline`, then `queue-depth`, then `queue-stalled`.
    That is three incidents and one problem: the owner does the same thing about
    all three, which is nothing. Failed and ambiguous stay apart, because
    "повторить" and "дошло ли оно" are different questions.
    """

    MAX_OFFLINE = "max-offline"
    DATABASE = "database"
    OWNER_SESSION = "owner-session"
    QUEUE_STALLED = "queue-stalled"
    DELIVERY_FAILED = "delivery-failed"
    DELIVERY_AMBIGUOUS = "delivery-ambiguous"
    PROVISIONING = "provisioning"
    BRIDGE_DOWN = "bridge-down"
    DEGRADED = "degraded"


GLYPHS = {
    Severity.BROKEN: BROKEN,
    Severity.ATTENTION: ATTENTION,
}

BRIDGE_GLYPHS = {
    BridgeUiState.ACTIVE: RUNNING,
    BridgeUiState.PROVISIONING: ATTENTION,
    BridgeUiState.BROKEN: BROKEN,
    BridgeUiState.DISABLED: DISABLED,
}


# --------------------------------------------------------------------- facts


@dataclass(frozen=True, slots=True)
class AttentionFacts:
    """Everything the verdict is decided from, as facts rather than sentences.

    Read-only, and every field already existed somewhere: the health snapshot,
    the bridge registry, the provisioning journal. Nothing here is a new
    measurement — the numbers were simply not reaching the first screen.
    """

    worker_running: bool = False
    max_connected: bool = False
    telegram_connected: bool = True
    degraded: bool = False
    database_healthy: bool = True
    owner_ingress_ready: bool = True

    bridges: int = 0
    bridges_broken: int = 0
    bridges_provisioning: int = 0
    bridges_disabled: int = 0

    queued: int = 0
    failed: int = 0
    ambiguous: int = 0
    expired: int = 0
    oldest_pending_ms: int | None = None
    max_offline_ms: int | None = None

    provisioning_unfinished: int = 0


@dataclass(frozen=True, slots=True)
class JobFacts:
    """One delivery job the owner has to decide about.

    Named by the contact rather than by `bridge_name`. The queue keys on the
    deterministic bridge name and every alert and listing printed it under the
    label «Контакт:» — `p6wyzx5zu7vv6pwcddx5` is not a person, and it is the
    only handle the owner was ever given for one.
    """

    job_id: int
    bridge_name: str
    contact: str
    #: `max_to_tg` or `tg_to_max` — which way this message was going.
    direction: str = "tg_to_max"
    kind: str = ""
    ambiguous: bool = False
    created_at: int | None = None


@dataclass(frozen=True, slots=True)
class AttemptFacts:
    """One bridge that was being made and is not finished."""

    max_chat_id: int
    contact: str
    username: str = ""
    #: The sanitised sentence the journal kept, never the enum it stopped at.
    reason: str | None = None
    retryable: bool = True


@dataclass(frozen=True, slots=True)
class BridgeFacts:
    """One bridge, as the services see it. No sentences, no formatting."""

    title: str
    username: str
    bridge_name: str
    max_chat_id: int
    bot_id: int | None = None
    state: BridgeUiState = BridgeUiState.ACTIVE
    queued: int = 0
    failed: int = 0
    ambiguous: int = 0
    last_delivery_at: int | None = None
    #: The stored exception, kept for Diagnostics and never for the card.
    detail: str | None = None


# ---------------------------------------------------------------- view models


@dataclass(frozen=True, slots=True)
class UserProblem:
    """One thing that is wrong, in the owner's terms, with its own way out."""

    kind: ProblemKind
    severity: Severity
    title: str
    lines: tuple[str, ...] = ()
    #: Which bridge it belongs to, when it belongs to one. The contact's real
    #: name — never `bridge_name`, which is a key and not a person.
    contact: str | None = None
    #: The outbox job behind it, for the screens that can act on one.
    job_id: int | None = None
    #: The provisioning attempt behind it, for the same reason.
    max_chat_id: int | None = None
    #: When it happened, already in words — the second half of a list row's
    #: label, where there is no space for a sentence.
    when: str | None = None

    @property
    def glyph(self) -> str:
        return GLYPHS[self.severity]

    @property
    def key(self) -> str:
        """What a button carries to get back to this exact problem.

        A kind alone for the ones there is only ever one of, and the job or the
        contact for the ones there can be several of. Never an operator's id on
        the screen itself — the number is in the callback, where nobody reads it.
        """
        if self.job_id is not None:
            return f"job:{self.job_id}"
        if self.max_chat_id is not None:
            return f"prov:{self.max_chat_id}"
        return f"kind:{self.kind.value}"


@dataclass(frozen=True, slots=True)
class HomeView:
    """The first screen, and nothing that belongs on a later one."""

    state: HomeState
    headline: str
    telegram_connected: bool
    max_connected: bool
    bridges: int
    queued: int = 0
    action_required: int = 0
    problems: int = 0

    @property
    def glyph(self) -> str:
        return {
            HomeState.HEALTHY: RUNNING,
            HomeState.ATTENTION: ATTENTION,
            HomeState.BROKEN: BROKEN,
        }[self.state]


@dataclass(frozen=True, slots=True)
class BridgeView:
    """One bridge card: a verdict, a moment, and a queue — already in words."""

    title: str
    username: str
    max_chat_id: int
    bot_id: int | None
    state: BridgeUiState
    headline: str
    cause: str | None = None
    last_delivery: str | None = None
    queue: str = "пусто"
    held: int = 0
    detail: str | None = None
    lines: tuple[str, ...] = field(default_factory=tuple)

    @property
    def glyph(self) -> str:
        return BRIDGE_GLYPHS[self.state]

    @property
    def running(self) -> bool:
        return self.state is BridgeUiState.ACTIVE


# ------------------------------------------------------------------ verdicts


#: The oldest a queued message may get before the owner is told the queue has
#: stopped moving. The same number the durable incident uses — this is a second
#: reading of one threshold, not a second threshold.
STALLED_MS = 900_000


def verdict(facts: AttentionFacts) -> HomeState:
    """Which of the three answers this install owes the owner.

    BROKEN is reserved for "a thing you use does not work at all". Everything
    that still carries messages while wanting a decision is ATTENTION, because
    an owner who is shown red for a single ambiguous send stops reading red.
    """
    if not facts.worker_running or not facts.max_connected or not facts.database_healthy:
        return HomeState.BROKEN
    if (
        facts.failed
        or facts.ambiguous
        or facts.provisioning_unfinished
        or facts.bridges_broken
        or facts.degraded
        or not facts.owner_ingress_ready
        or (facts.oldest_pending_ms or 0) >= STALLED_MS
    ):
        return HomeState.ATTENTION
    return HomeState.HEALTHY


def _broken_headline(facts: AttentionFacts) -> str:
    """Why it is red, in a sentence rather than a component name."""
    if not facts.worker_running:
        return "Telemax не работает"
    if not facts.max_connected:
        return "Нет связи с MAX"
    return "Не могу сохранять сообщения"


#: Which way a message was going, in words. The queue's own kind strings
#: (`max_to_tg_media` and friends) name a code path, not a direction anybody
#: would describe out loud.
_DIRECTIONS = {
    "max_to_tg": "MAX → Telegram",
    "tg_to_max": "Telegram → MAX",
}


def problems(
    facts: AttentionFacts,
    *,
    jobs: list[JobFacts] | None = None,
    attempts: list[AttemptFacts] | None = None,
    now_ms: int = 0,
    tz: tzinfo | None = None,
) -> list[UserProblem]:
    """Every open problem, once each, in the order they deserve attention.

    Aggregation is the whole point. A MAX outage that has also grown the queue
    and stalled its oldest item is one problem with one answer — waiting — and
    it appears here once. What does *not* aggregate is anything asking a
    different question of the owner: a failed send and an ambiguous send need
    two different decisions and stay two problems.
    """
    found: list[UserProblem] = []

    if not facts.max_connected and facts.worker_running:
        lines = []
        span = humanise.duration(facts.max_offline_ms)
        if facts.queued:
            lines.append(
                f"{humanise.count(facts.queued, 'сообщение', 'сообщения', 'сообщений')}"
                " ожидают отправки"
            )
        found.append(
            UserProblem(
                kind=ProblemKind.MAX_OFFLINE,
                severity=Severity.BROKEN,
                title="Нет связи с MAX" + (f" · {span}" if span else ""),
                lines=tuple(lines),
            )
        )
    if not facts.database_healthy:
        found.append(
            UserProblem(
                kind=ProblemKind.DATABASE,
                severity=Severity.BROKEN,
                title="Telemax не сохраняет события",
                lines=("Ваши действия в Telegram приостановлены.",),
            )
        )
    if not facts.owner_ingress_ready and facts.worker_running:
        found.append(
            UserProblem(
                kind=ProblemKind.OWNER_SESSION,
                severity=Severity.ATTENTION,
                title="Ваш Telegram не подключён",
                lines=("То, что вы пишете из Telegram, пока не уходит в MAX.",),
            )
        )
    if (
        facts.max_connected
        and (facts.oldest_pending_ms or 0) >= STALLED_MS
    ):
        span = humanise.duration(facts.oldest_pending_ms)
        found.append(
            UserProblem(
                kind=ProblemKind.QUEUE_STALLED,
                severity=Severity.ATTENTION,
                title="Очередь не движется" + (f" · {span}" if span else ""),
                lines=(
                    f"{humanise.count(facts.queued, 'сообщение', 'сообщения', 'сообщений')}"
                    " ждут отправки",
                ),
            )
        )
    if facts.bridges_broken:
        verb = humanise.plural(
            facts.bridges_broken, "не работает", "не работают", "не работают"
        )
        found.append(
            UserProblem(
                kind=ProblemKind.BRIDGE_DOWN,
                severity=Severity.ATTENTION,
                title=f"{humanise.count(facts.bridges_broken, 'мост', 'моста', 'мостов')}"
                f" {verb}",
                lines=("Сообщения этих контактов не ходят.",),
            )
        )
    if facts.degraded:
        found.append(
            UserProblem(
                kind=ProblemKind.DEGRADED,
                severity=Severity.ATTENTION,
                title="Часть Telemax остановилась",
                lines=("Сообщения могут задерживаться. Помогает перезапуск.",),
            )
        )
    if attempts is not None:
        found.extend(
            UserProblem(
                kind=ProblemKind.PROVISIONING,
                severity=Severity.ATTENTION,
                title="Мост не создан",
                contact=attempt.contact,
                lines=(attempt.reason,) if attempt.reason else (),
                max_chat_id=attempt.max_chat_id,
            )
            for attempt in attempts
        )
    elif facts.provisioning_unfinished:
        found.extend(
            UserProblem(
                kind=ProblemKind.PROVISIONING,
                severity=Severity.ATTENTION,
                title="Мост не создан",
                lines=("Создание остановилось на полпути.",),
            )
            for _ in range(facts.provisioning_unfinished)
        )

    # The two that ask the owner a question, and they ask different ones:
    # «повторить?» and «дошло ли оно?». They never aggregate into each other.
    #
    # With the jobs to hand each becomes one entry naming a contact and a
    # moment; without them — the home screen, which counts before it renders —
    # the same number of placeholders, so the badge agrees with the list.
    if jobs is not None:
        found.extend(
            _job_problem(job, now_ms=now_ms, tz=tz)
            for job in jobs
            if not job.ambiguous
        )
        found.extend(
            _job_problem(job, now_ms=now_ms, tz=tz) for job in jobs if job.ambiguous
        )
        return found

    found.extend(
        UserProblem(
            kind=ProblemKind.DELIVERY_FAILED,
            severity=Severity.ATTENTION,
            title=FAILED_TITLE,
        )
        for _ in range(facts.failed)
    )
    found.extend(
        UserProblem(
            kind=ProblemKind.DELIVERY_AMBIGUOUS,
            severity=Severity.ATTENTION,
            title=AMBIGUOUS_TITLE,
        )
        for _ in range(facts.ambiguous)
    )
    return found


FAILED_TITLE = "Сообщение не отправлено"
AMBIGUOUS_TITLE = "Неясно, дошло ли сообщение"


def _job_problem(
    job: JobFacts, *, now_ms: int, tz: tzinfo | None
) -> UserProblem:
    when = humanise.when(job.created_at, now_ms=now_ms, tz=tz) if now_ms else None
    route = _DIRECTIONS.get(job.direction, "")
    detail = " · ".join(part for part in (route, when) if part)
    return UserProblem(
        kind=(
            ProblemKind.DELIVERY_AMBIGUOUS if job.ambiguous else ProblemKind.DELIVERY_FAILED
        ),
        severity=Severity.ATTENTION,
        title=AMBIGUOUS_TITLE if job.ambiguous else FAILED_TITLE,
        contact=job.contact,
        lines=(detail,) if detail else (),
        job_id=job.job_id,
        when=when,
    )


def problem_count(facts: AttentionFacts) -> int:
    """How many problems the owner would see, without rendering any of them."""
    return len(problems(facts))


#: The kinds there can be arbitrarily many of, because each one is a separate
#: decision about a separate message or bridge. They get a category row and a
#: paginated list; everything else is one row that opens the problem itself.
#:
#: This is the whole of the scale fix. One row per job on the first screen is
#: fine at three problems and a message Telegram refuses to send at a hundred —
#: eighty-two ambiguous sends is not an unusual number after a bad afternoon,
#: and that is exactly when the owner needs the screen to open.
#: `delivery-expired` is deliberately absent: expired jobs are reported but have
#: no GUI decision behind them yet, so there is no category to open. Adding the
#: kind here is all it will take when there is.
PER_ITEM_KINDS = (
    ProblemKind.DELIVERY_AMBIGUOUS,
    ProblemKind.DELIVERY_FAILED,
    ProblemKind.PROVISIONING,
)

#: What a category row says, and what its own screen is called.
CATEGORY_LABELS = {
    ProblemKind.DELIVERY_AMBIGUOUS: ("Неясно, дошло ли", "Неясные отправки"),
    ProblemKind.DELIVERY_FAILED: ("Не отправлено", "Не отправленные"),
    ProblemKind.PROVISIONING: ("Создание мостов", "Создание мостов"),
}

#: Short, stable slugs for the callbacks. `callback_data` is capped at 64 bytes
#: and a kind's own value would spend a third of it on the word "delivery".
CATEGORY_SLUGS = {
    ProblemKind.DELIVERY_AMBIGUOUS: "amb",
    ProblemKind.DELIVERY_FAILED: "fail",
    ProblemKind.PROVISIONING: "prov",
}
KIND_BY_SLUG = {slug: kind for kind, slug in CATEGORY_SLUGS.items()}

#: How many rows a category page carries. Small enough that the page plus its
#: pager and its way back is nowhere near Telegram's hundred-row ceiling, and
#: large enough that eighty-two ambiguous sends is eleven pages rather than
#: twenty-eight.
PAGE_SIZE = 8


@dataclass(frozen=True, slots=True)
class ProblemGroup:
    """One row of the problem index: a kind, how many, and what is behind it."""

    kind: ProblemKind
    severity: Severity
    #: The row's own words — «Неясно, дошло ли», «Связь с MAX».
    label: str
    #: What the screen behind it is called, for the row's «← …» button.
    heading: str
    items: tuple[UserProblem, ...]

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def paged(self) -> bool:
        """Whether the row opens a list, or the one problem it stands for."""
        return self.kind in PER_ITEM_KINDS

    @property
    def glyph(self) -> str:
        return GLYPHS[self.severity]

    @property
    def pages(self) -> int:
        return max(1, -(-self.count // PAGE_SIZE))

    def page(self, number: int) -> tuple[UserProblem, ...]:
        """One page of items, clamped — a stale page number is not an error."""
        first = max(0, min(number, self.pages - 1)) * PAGE_SIZE
        return self.items[first : first + PAGE_SIZE]

    def page_of(self, problem: UserProblem) -> int:
        """Which page an item is on, so «назад» lands where it was opened."""
        for index, item in enumerate(self.items):
            if item.key == problem.key:
                return index // PAGE_SIZE
        return 0


def group_problems(found: list[UserProblem]) -> list[ProblemGroup]:
    """The problem list as an index. Bounded by kinds, never by job count.

    Order is preserved from `problems()`: the worst thing first, and the two
    that ask the owner a question last, because they are the ones with numbers
    on them.
    """
    order: list[ProblemKind] = []
    buckets: dict[ProblemKind, list[UserProblem]] = {}
    for problem in found:
        if problem.kind not in buckets:
            buckets[problem.kind] = []
            order.append(problem.kind)
        buckets[problem.kind].append(problem)

    groups: list[ProblemGroup] = []
    for kind in order:
        items = buckets[kind]
        label, heading = CATEGORY_LABELS.get(kind, (items[0].title, items[0].title))
        groups.append(
            ProblemGroup(
                kind=kind,
                severity=items[0].severity,
                label=label,
                heading=heading,
                items=tuple(items),
            )
        )
    return groups


# ------------------------------------------------------------------ builders


def home_view(facts: AttentionFacts) -> HomeView:
    """Facts in, first screen out."""
    state = verdict(facts)
    headline = {
        HomeState.HEALTHY: "Всё работает",
        HomeState.ATTENTION: "Требует внимания",
        HomeState.BROKEN: _broken_headline(facts),
    }[state]
    return HomeView(
        state=state,
        headline=headline,
        telegram_connected=facts.telegram_connected,
        max_connected=facts.max_connected,
        bridges=facts.bridges,
        queued=facts.queued,
        action_required=facts.failed + facts.ambiguous,
        problems=problem_count(facts),
    )


_BRIDGE_HEADLINES = {
    BridgeUiState.ACTIVE: "Мост работает",
    BridgeUiState.PROVISIONING: "Мост создаётся",
    BridgeUiState.BROKEN: "Мост не работает",
    BridgeUiState.DISABLED: "Мост отключён",
}

_BRIDGE_CAUSES = {
    BridgeUiState.PROVISIONING: "Telegram ещё не подтвердил создание бота.",
    BridgeUiState.BROKEN: "Бот этого моста не отвечает.",
    BridgeUiState.DISABLED: "Сообщения не ходят в обе стороны.",
}


def bridge_view(
    facts: BridgeFacts, *, now_ms: int, tz: tzinfo | None = None
) -> BridgeView:
    """One card. Every number that reaches the screen is already a word."""
    return BridgeView(
        title=facts.title,
        username=facts.username,
        max_chat_id=facts.max_chat_id,
        bot_id=facts.bot_id,
        state=facts.state,
        headline=_BRIDGE_HEADLINES[facts.state],
        cause=_BRIDGE_CAUSES.get(facts.state),
        last_delivery=humanise.when(facts.last_delivery_at, now_ms=now_ms, tz=tz),
        queue=humanise.queue_words(facts.queued),
        held=facts.queued + facts.failed + facts.ambiguous,
        detail=facts.detail,
    )


def bridge_views(
    facts: list[BridgeFacts], *, now_ms: int, tz: tzinfo | None = None
) -> list[BridgeView]:
    return [bridge_view(item, now_ms=now_ms, tz=tz) for item in facts]
