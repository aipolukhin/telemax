"""The guardian says nothing. That is the feature.

A bad afternoon in production — eighty-two ambiguous sends, sixteen failed ones,
two provisioning attempts stuck behind a flood wait, a queue that grew and then
stopped moving, and every one of those recovering an hour later — used to arrive
as a run of standalone messages:

    🟡 16 сообщений не отправлено
    🟡 2 моста не созданы
    ⚠️ Очередь выросла: 27 сообщений ждут отправки
    ⚠️ Сообщение ждёт отправки 16 мин
    ✅ Доставка восстановлена

None of them carried a button, all of them pushed the guardian's anchor up out
of reach, and not one of them told the owner anything they could not have read
on HOME → Проблемы at a time of their own choosing.

So this file asserts a number, over and over, and the number is zero. Detection
is untouched: every incident below is still raised, still deduplicated, still
recorded durably, and still counted into the home badge. What it may no longer
do is interrupt anybody.

The one exception has its own section. A sustained MAX outage or a database that
cannot commit a write earns exactly one message with one button, edited while it
lasts and edited again when it is over — never a second send, and never a green
line for a problem nobody was told about.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest_asyncio

from bridge.onboarding.state import StateStore
from bridge.onboarding.views import AttentionFacts, group_problems, problems
from bridge.service.health import (
    AlertDispatcher,
    HealthService,
    HealthSnapshot,
)
from bridge.service.notifications import (
    ATTENTION_TEXT,
    PROBLEMS_CALLBACK,
    PUSH_KEY,
    RESOLVED_TEXT,
    RETIRED_TEXT,
    SUSTAINED_OUTAGE_MS,
    NotificationCentre,
    critical,
)
from bridge.storage import (
    AlertRepository,
    Database,
    Direction,
    HealthStateRepository,
    OutboxRepository,
    TelegramInboxRepository,
)

BRIDGE = "dad"


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


@dataclass
class Chat:
    """A Telegram chat that remembers what was put in it and what was edited."""

    sent: list[tuple[int, str]] = field(default_factory=list)
    edits: list[tuple[int, str]] = field(default_factory=list)
    remembered: dict[str, int] = field(default_factory=dict)
    facts: Any = field(default_factory=lambda: AttentionFacts(worker_running=True))
    #: Messages Telegram will refuse to edit — older than 48 hours, or deleted.
    uneditable: set[int] = field(default_factory=set)
    #: Raised by `read_facts` when the state cannot be read at all.
    unreadable: bool = False
    _next: int = 100

    async def send(self, text: str, markup: Any) -> int:
        self._next += 1
        self.sent.append((self._next, text))
        return self._next

    async def edit(self, message_id: int, text: str, markup: Any) -> bool:
        if message_id in self.uneditable:
            return False
        self.edits.append((message_id, text))
        return True

    async def read_facts(self) -> Any:
        if self.unreadable:
            raise RuntimeError("the snapshot could not be taken")
        return self.facts

    def centre(self) -> NotificationCentre:
        def write(mapping: dict[str, int]) -> None:
            self.remembered = mapping

        return NotificationCentre(
            send=self.send,
            edit=self.edit,
            read=lambda: dict(self.remembered),
            write=write,
            facts=self.read_facts,
        )

    @property
    def texts(self) -> list[str]:
        return [text for _, text in self.sent] + [text for _, text in self.edits]


def alert(key: str, *, recovery: bool = False) -> dict[str, Any]:
    return {
        "id": 1,
        "incident_key": key,
        "severity": "recovery" if recovery else "warning",
        "text": "whatever the health service queued",
    }


def _busy(**over: Any) -> AttentionFacts:
    """Everything that used to be a message, all at once, and nothing critical.

    MAX is connected, the database commits, and the owner has ninety-eight
    decisions waiting for them on a screen they have not opened yet.
    """
    return replace(
        AttentionFacts(
            worker_running=True,
            max_connected=True,
            failed=16,
            ambiguous=82,
            provisioning_unfinished=2,
            bridges_broken=2,
            queued=41,
            oldest_pending_ms=282 * 60_000,
        ),
        **over,
    )


def _quiet(**over: Any) -> AttentionFacts:
    return replace(
        AttentionFacts(worker_running=True, max_connected=True), **over
    )


def _outage(**over: Any) -> AttentionFacts:
    """MAX gone long enough that this is an outage rather than a reconnect."""
    return replace(
        AttentionFacts(
            worker_running=True,
            max_connected=False,
            queued=40,
            max_offline_ms=SUSTAINED_OUTAGE_MS + 60_000,
        ),
        **over,
    )


# ------------------------------------------------------------------- the rule


def test_no_ordinary_trouble_is_ever_critical() -> None:
    """The whole list from the brief, asserted one condition at a time."""
    assert critical(_quiet()) is False
    assert critical(_busy()) is False, "ninety-eight decisions are still not an emergency"
    assert critical(_quiet(failed=16)) is False
    assert critical(_quiet(ambiguous=82)) is False
    assert critical(_quiet(expired=9)) is False
    assert critical(_quiet(provisioning_unfinished=2)) is False
    assert critical(_quiet(bridges_broken=4)) is False
    assert critical(_quiet(queued=250)) is False
    assert critical(_quiet(oldest_pending_ms=6 * 60 * 60_000)) is False
    assert critical(_quiet(degraded=True)) is False
    assert critical(_quiet(owner_ingress_ready=False)) is False


def test_an_ordinary_reconnect_is_not_an_outage() -> None:
    """MAX drops and comes back most days. Nobody is woken for it."""
    assert critical(_outage(max_offline_ms=1_000)) is False
    assert critical(_outage(max_offline_ms=SUSTAINED_OUTAGE_MS - 1)) is False
    assert critical(_outage(max_offline_ms=SUSTAINED_OUTAGE_MS)) is True


def test_a_database_that_cannot_write_is_catastrophic() -> None:
    """The one condition the owner cannot find out about by looking."""
    assert critical(_quiet(database_healthy=False)) is True


def test_facts_that_cannot_be_read_are_never_critical() -> None:
    """A push decided from nothing is a guess."""
    assert critical(None) is False


# ------------------------------------------------------ zero standalone sends


async def test_a_bad_afternoon_produces_no_standalone_message() -> None:
    """The strong acceptance test, at the layer that would do the sending.

    Eighty-two ambiguous, sixteen failed, two provisioning attempts, a queue
    that grew and stalled, and then all of it recovering. Every one of those is
    a real incident and every one of them used to be a message.
    """
    chat = Chat(facts=_busy())
    centre = chat.centre()

    for key in (
        "delivery-ambiguous",
        "delivery-failed",
        "provisioning-stuck:-400000004",
        "provisioning-stuck:-682404227",
        "bridge-not-started:dad",
        "bridge-not-started:mum",
        "queue-depth",
        "queue-stalled",
        "owner-session-unavailable",
        "owner-update-stuck",
        "native-media-degraded",
        "delivery-expired",
    ):
        await centre.publish(alert(key))

    chat.facts = _quiet()
    for key in (
        "delivery-ambiguous",
        "delivery-failed",
        "provisioning-stuck:-400000004",
        "queue-depth",
        "queue-stalled",
        "owner-session-unavailable",
        "native-media-degraded",
    ):
        await centre.publish(alert(key, recovery=True))

    assert chat.sent == [], "not one standalone message"
    assert chat.edits == [], "and nothing to edit either, because nothing was posted"
    assert chat.remembered == {}


async def test_a_hundred_simultaneous_problems_produce_no_standalone_message() -> None:
    chat = Chat(facts=_busy(ambiguous=100))
    centre = chat.centre()

    for index in range(100):
        await centre.publish(alert(f"provisioning-stuck:{-index}"))

    assert chat.sent == []


async def test_counts_that_keep_changing_produce_no_standalone_message() -> None:
    """The six-hourly reminder, which used to re-send its identical text."""
    chat = Chat(facts=_busy(ambiguous=3))
    centre = chat.centre()

    for count in (3, 17, 41, 82, 40, 2):
        chat.facts = _busy(ambiguous=count)
        await centre.publish(alert("delivery-ambiguous"))

    assert chat.sent == []


async def test_a_recovery_with_no_push_says_nothing_at_all() -> None:
    """«✅ Доставка восстановлена» at three in the morning, for a problem the
    owner was never told about in the first place."""
    chat = Chat(facts=_quiet())
    centre = chat.centre()

    await centre.publish(alert("max-offline", recovery=True))
    await centre.publish(alert("queue-depth", recovery=True))
    await centre.publish(alert("delivery-failed", recovery=True))

    assert chat.sent == []
    assert chat.edits == []


async def test_a_bridge_that_never_started_is_not_a_message() -> None:
    chat = Chat(facts=_quiet(bridges_broken=4))
    centre = chat.centre()

    for name in ("dad", "mum", "sister", "boss"):
        await centre.publish(alert(f"bridge-not-started:{name}"))

    assert chat.sent == []


# --------------------------------------------- end to end, through the queue


def _service(database: Database, *, ready: bool = True) -> HealthService:
    return HealthService(
        health=HealthStateRepository(database),
        outbox=OutboxRepository(database),
        inbox=TelegramInboxRepository(database),
        alerts=AlertRepository(database),
        db_path=Path(database.path),
        bridges=lambda: [(BRIDGE, True)],
        max_is_ready=lambda: ready,
    )


async def _wedge(outbox: OutboxRepository, *, failed: int, ambiguous: int) -> None:
    for index in range(failed + ambiguous):
        job = await outbox.enqueue(
            bridge_name=BRIDGE,
            direction=Direction.MAX_TO_TG,
            kind="max_to_tg_text",
            payload={},
            source_key=f"max:1:{index}",
        )
        if index < failed:
            await outbox.mark_failed(job, error="permanent")
        else:
            await outbox.mark_ambiguous(job, error="died mid-send")


async def test_the_real_pipeline_drains_a_bad_afternoon_in_silence(
    database: Database,
) -> None:
    """Health decides, the queue carries, the dispatcher drains — and nothing
    reaches the chat.

    The same collaborators the service wires in production, so this fails if
    somebody reconnects a text-only sender to the alert queue.
    """
    outbox = OutboxRepository(database)
    await _wedge(outbox, failed=16, ambiguous=82)

    alerts = AlertRepository(database)
    # What the provisioning reconciler writes when an attempt stalls, verbatim.
    for chat_id in (-400000004, -682404227):
        await alerts.open_incident(
            incident_key=f"provisioning-stuck:{chat_id}", text="⚠️ Мост не создан"
        )

    service = _service(database)
    snapshot = await service.snapshot()
    assert snapshot.outbox_failed == 16
    assert snapshot.outbox_ambiguous == 82
    raised = await service.evaluate(snapshot)
    assert "delivery-failed" in raised and "delivery-ambiguous" in raised

    chat = Chat(facts=_busy())
    centre = chat.centre()
    drained = await AlertDispatcher(alerts=alerts, deliver=centre.publish).drain_once()

    assert drained >= 4, "every incident was still detected, recorded and claimed"
    assert chat.sent == [], "and not one of them became a message"
    assert await alerts.pending_count() == 0, "the queue is drained, not stuck"


async def test_the_anchor_is_the_only_message_the_guardian_creates(
    tmp_path: Path,
) -> None:
    """One bot, one chat, one state file: the anchor and the notification layer
    share everything they would share in production.

    The count that matters is `sends outside the anchor`, and it is zero.
    """
    from bridge.onboarding.board import StatusBoard

    posted: list[str] = []

    class Bot:
        async def send_message(self, chat_id: int, text: str, reply_markup: Any = None) -> Any:
            posted.append(text)
            return type("Sent", (), {"message_id": 500 + len(posted)})()

        async def edit_message_text(self, **kwargs: Any) -> None:
            return None

    bot = Bot()
    store = StateStore.for_data_dir(tmp_path)
    board = StatusBoard(bot=bot, chat_id=7, store=store)
    await board.show(("🟡 Требует внимания", None))
    anchor = len(posted)
    assert anchor == 1, "the anchor is drawn once"

    chat = Chat(facts=_busy())

    def write(mapping: dict[str, int]) -> None:
        store.update(notifications=mapping)

    centre = NotificationCentre(
        send=chat.send,
        edit=chat.edit,
        read=lambda: dict(store.load().notifications),
        write=write,
        facts=chat.read_facts,
    )
    for key in ("delivery-failed", "delivery-ambiguous", "queue-depth", "queue-stalled"):
        await centre.publish(alert(key))
        await centre.publish(alert(key, recovery=True))

    assert chat.sent == [], "zero sends outside the canonical anchor"
    assert len(posted) == anchor, "and the anchor posted nothing extra"


# ------------------------------------------------------------------ the push


async def test_a_sustained_outage_pushes_exactly_once() -> None:
    chat = Chat(facts=_outage())
    centre = chat.centre()

    await centre.publish(alert("max-offline"))

    assert len(chat.sent) == 1
    assert chat.sent[0][1] == ATTENTION_TEXT
    assert chat.remembered[PUSH_KEY] == chat.sent[0][0]


async def test_the_push_carries_the_one_button_that_opens_the_problems() -> None:
    markups: list[Any] = []

    async def send(text: str, markup: Any) -> int:
        markups.append(markup)
        return 1

    async def edit(message_id: int, text: str, markup: Any) -> bool:
        return True

    centre = NotificationCentre(
        send=send,
        edit=edit,
        read=dict,
        write=lambda mapping: None,
        facts=Chat(facts=_outage()).read_facts,
    )
    await centre.publish(alert("max-offline"))

    keyboard = markups[0].inline_keyboard
    assert len(keyboard) == 1 and len(keyboard[0]) == 1, "one button, not a menu"
    assert keyboard[0][0].callback_data == PROBLEMS_CALLBACK


async def test_worsening_edits_the_same_push_rather_than_sending_another() -> None:
    chat = Chat(facts=_outage())
    centre = chat.centre()

    await centre.publish(alert("max-offline"))
    for facts in (
        _outage(queued=140),
        _outage(queued=300, max_offline_ms=6 * 60 * 60_000),
        _outage(queued=300, failed=16, ambiguous=82),
    ):
        chat.facts = facts
        await centre.publish(alert("queue-depth"))

    assert len(chat.sent) == 1, "still one push"
    assert len(chat.edits) == 3
    assert {message_id for message_id, _ in chat.edits} == {chat.sent[0][0]}


async def test_a_second_critical_condition_does_not_add_a_second_push() -> None:
    """One aggregate push, never one per category."""
    chat = Chat(facts=_outage())
    centre = chat.centre()

    await centre.publish(alert("max-offline"))
    chat.facts = _outage(database_healthy=False)
    await centre.publish(alert("database-write-unavailable"))

    assert len(chat.sent) == 1


async def test_recovery_edits_the_push_green_and_sends_nothing() -> None:
    chat = Chat(facts=_outage())
    centre = chat.centre()

    await centre.publish(alert("max-offline"))
    chat.facts = _quiet()
    await centre.publish(alert("max-offline", recovery=True))

    assert len(chat.sent) == 1, "no additional send"
    assert chat.edits[-1] == (chat.sent[0][0], RESOLVED_TEXT)
    assert PUSH_KEY not in chat.remembered


async def test_a_push_closes_even_though_ordinary_problems_remain() -> None:
    """The outage is over. The eighty-two decisions are not, and they belong on
    HOME rather than in a message that keeps a red glyph alive."""
    chat = Chat(facts=_outage())
    centre = chat.centre()

    await centre.publish(alert("max-offline"))
    chat.facts = _busy()
    await centre.publish(alert("max-offline", recovery=True))

    assert chat.edits[-1][1] == RESOLVED_TEXT
    assert len(chat.sent) == 1


async def test_a_closed_push_can_be_raised_again() -> None:
    """Closing must not make the guardian mute for the rest of the run."""
    chat = Chat(facts=_outage())
    centre = chat.centre()

    await centre.publish(alert("max-offline"))
    chat.facts = _quiet()
    await centre.publish(alert("max-offline", recovery=True))

    chat.facts = _outage()
    await centre.publish(alert("max-offline"))
    assert len(chat.sent) == 2, "a later outage is a new push"
    assert chat.remembered[PUSH_KEY] == chat.sent[-1][0]


async def test_a_push_too_old_to_edit_becomes_a_new_one() -> None:
    """A bot may edit its own message for forty-eight hours only, and an outage
    that outlives that window is when silence is worst."""
    chat = Chat(facts=_outage())
    centre = chat.centre()

    await centre.publish(alert("max-offline"))
    first = chat.sent[0][0]
    chat.uneditable.add(first)
    await centre.publish(alert("max-offline"))

    assert len(chat.sent) == 2
    assert chat.remembered[PUSH_KEY] != first


async def test_an_unreadable_state_neither_pushes_nor_closes() -> None:
    """Closing a push on a guess is worse than pushing on one."""
    chat = Chat(facts=_outage())
    centre = chat.centre()
    await centre.publish(alert("max-offline"))

    chat.unreadable = True
    await centre.publish(alert("max-offline", recovery=True))

    assert len(chat.sent) == 1
    assert chat.edits == [], "nothing was said either way"
    assert PUSH_KEY in chat.remembered, "and the push is still owned"


async def test_a_centre_with_no_facts_is_permanently_silent() -> None:
    chat = Chat()
    centre = NotificationCentre(
        send=chat.send,
        edit=chat.edit,
        read=lambda: dict(chat.remembered),
        write=lambda mapping: None,
    )
    await centre.publish(alert("database-write-unavailable"))

    assert chat.sent == []


# ------------------------------------------------ the previous design's debris


async def test_the_previous_designs_messages_are_retired_once_and_never_re_sent() -> None:
    """An upgrade must not strand «🟡 16 сообщений не отправлено» for ever."""
    chat = Chat(facts=_busy())
    chat.remembered = {"connectivity": 401, "delivery-failed": 402, "bridges": 403}
    centre = chat.centre()

    await centre.publish(alert("delivery-failed"))

    assert chat.sent == [], "retiring is editing, never sending"
    assert {message_id for message_id, _ in chat.edits} == {401, 402, 403}
    assert {text for _, text in chat.edits} == {RETIRED_TEXT}
    assert chat.remembered == {}

    await centre.publish(alert("delivery-failed"))
    assert len(chat.edits) == 3, "once per process, not once per alert"


async def test_retiring_leaves_a_live_push_alone() -> None:
    chat = Chat(facts=_outage())
    chat.remembered = {"connectivity": 401, PUSH_KEY: 999}
    centre = chat.centre()

    await centre.publish(alert("max-offline"))

    assert (401, RETIRED_TEXT) in chat.edits
    assert (999, ATTENTION_TEXT) in chat.edits
    assert chat.remembered == {PUSH_KEY: 999}
    assert chat.sent == []


# ------------------------------------------------ the state stays reachable


def test_every_silenced_problem_is_still_on_the_problem_screen() -> None:
    """Silence is not disappearance. The Problem Center is where all of it went.

    The counts are the same ones the notifications used to announce, and the
    index is bounded by *kinds* rather than by job count — which is what makes
    a hundred problems a screen rather than a message Telegram refuses.
    """
    found = problems(_busy())
    counts = {group.kind.value: group.count for group in group_problems(found)}

    assert counts["delivery-failed"] == 16
    assert counts["delivery-ambiguous"] == 82
    assert counts["provisioning"] == 2
    assert counts["bridge-down"] == 1, "two dead bridges, one decision"
    assert counts["queue-stalled"] == 1
    assert len(found) == 102
    assert len(counts) <= 9, "the index is bounded by kinds, never by job count"


def test_the_home_badge_still_counts_what_is_no_longer_announced() -> None:
    from bridge.onboarding.views import HomeState, home_view

    view = home_view(_busy())

    assert view.state is HomeState.ATTENTION
    assert view.problems == 102
    assert view.action_required == 98


# ------------------------------------------------------------- what it may say


def test_the_guardian_has_exactly_three_sentences() -> None:
    """A push, its green line, and the note on a retired message. Nothing built
    from a count, a key, an enum or an id — the three strings are constants, so
    there is nothing to interpolate a `p6wyzx5zu7vv6pwcddx5` into."""
    for text in (ATTENTION_TEXT, RESOLVED_TEXT, RETIRED_TEXT):
        for banned in (
            "p6wyzx5zu7vv6pwcddx5",
            "_max_bot",
            "awaiting_confirmation",
            "failed_retryable",
            "max-poller",
            "owner-inbox",
            "бот 8",
            "Контакт:",
            "{",
            "%s",
        ):
            assert banned not in text, f"«{banned}» in: {text}"

    assert ATTENTION_TEXT.startswith("⚠️ Telemax требует внимания")
    assert RESOLVED_TEXT.startswith("✅")


def test_this_layer_cannot_touch_a_delivery_job() -> None:
    """It decides and it sends. It has no way to settle anything.

    Checked structurally rather than by reading, because the next person to
    reach for "…and also mark the jobs done while we are here" would be adding
    a second settlement path beside the outbox's own.
    """
    from bridge.service import notifications

    source = inspect.getsource(notifications)

    for forbidden in (
        "retry_now",
        "resolve(",
        "archive(",
        "OutboxRepository",
        "OutboxState",
        "AlertRepository",
    ):
        assert forbidden not in source, f"the notification layer reaches for {forbidden}"

    accepted = inspect.signature(notifications.NotificationCentre.__init__).parameters
    assert set(accepted) == {"self", "send", "edit", "read", "write", "facts"}


def test_the_alert_queue_can_no_longer_be_handed_a_bare_sender() -> None:
    """`AlertDispatcher(send=...)` posted `alert["text"]` straight into the chat,
    one incident one message. Its last production caller went with the previous
    design, and a slot like that is a cascade one wiring mistake away."""
    accepted = inspect.signature(AlertDispatcher.__init__).parameters

    assert "send" not in accepted
    assert accepted["deliver"].default is inspect.Parameter.empty


def test_the_state_file_round_trips_the_push(tmp_path: Path) -> None:
    """Durable across a restart, and a damaged entry costs one extra message."""
    store = StateStore.for_data_dir(tmp_path)
    store.update(notifications={PUSH_KEY: 4242})

    fresh = StateStore.for_data_dir(tmp_path)
    assert fresh.load().notifications == {PUSH_KEY: 4242}

    store.path.write_text('{"notifications": {"attention": "oops"}}', encoding="utf-8")
    assert StateStore.for_data_dir(tmp_path).load().notifications == {}


# ------------------------------------------------------------- the inventory


#: Every `send_message` in `bridge/`, by module and by the function it sits in,
#: with what it is allowed to be. The point of pinning it is that a new one
#: cannot appear without somebody writing down which of these it is.
#:
#: ANCHOR       the guardian's one canonical message, edited in place
#: CRITICAL     the single attention push, gated by `notifications.critical`
#: OWNER-EVENT  a decision about a person, not an operational event
#: BOOTSTRAP    setup only, once per install, from the console flow
#: REACTIVE     an answer to something the user just did in that chat
#: DELIVERY     a contact's bot carrying a message; not the guardian at all
#: NOT-A-BOT    MAX, or the owner's own Telethon session
SEND_SITES = {
    ("bridge/onboarding/board.py", "_post"): "ANCHOR",
    ("bridge/service/runtime.py", "send"): "CRITICAL",
    ("bridge/provisioning/runtime.py", "announce"): "OWNER-EVENT",
    ("bridge/bootstrap/handoff.py", "_bot_invites"): "BOOTSTRAP",
    ("bridge/telegram/app.py", "_opened"): "REACTIVE",
    ("bridge/telegram/owner.py", "_brush_off"): "REACTIVE",
    ("bridge/provisioning/runtime.py", "replay"): "DELIVERY",
    ("bridge/routing/adapters.py", "send_text"): "DELIVERY",
    ("bridge/routing/media_adapter.py", "send_text"): "DELIVERY",
    ("bridge/routing/upload_router.py", "say"): "DELIVERY",
    ("bridge/presence/adapters.py", "send_status"): "DELIVERY",
    ("bridge/max_client/client.py", "send_text"): "NOT-A-BOT",
    ("bridge/max_client/client.py", "send_media"): "NOT-A-BOT",
    ("bridge/provisioning/mtproto.py", "send_to_saved"): "NOT-A-BOT",
    ("bridge/provisioning/mtproto.py", "send_start"): "NOT-A-BOT",
    ("bridge/provisioning/mtproto.py", "_send"): "NOT-A-BOT",
    ("bridge/telegram/user_session.py", "send_own_message"): "NOT-A-BOT",
}

#: The ones that can put a *new* standalone message in the guardian's chat
#: without the owner having just done something. This is the number the whole
#: increment is about, and it is four: the anchor when it has to be re-posted,
#: the single push, a new contact asking to be bridged, and the setup link.
PROACTIVE = {"ANCHOR", "CRITICAL", "OWNER-EVENT", "BOOTSTRAP"}


def _send_sites() -> dict[tuple[str, str], int]:
    """`send_message` calls in `bridge/`, keyed by module and enclosing function."""
    found: dict[tuple[str, str], int] = {}
    for path in sorted(Path("bridge").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr != "send_message":
                continue
            innermost = min(
                (
                    function
                    for function in functions
                    if function.lineno <= node.lineno <= (function.end_lineno or 0)
                ),
                key=lambda function: (function.end_lineno or 0) - function.lineno,
                default=None,
            )
            key = (path.as_posix(), innermost.name if innermost else "<module>")
            found[key] = found.get(key, 0) + 1
    return found


def test_every_message_the_guardian_can_create_is_on_the_inventory() -> None:
    """The audit, kept honest by the test suite rather than by a document.

    A new `send_message` anywhere in `bridge/` fails this until somebody says
    which of the seven kinds it is — which is the whole point, because every
    message this increment removed was added by somebody who had a good local
    reason and no view of the chat as a whole.
    """
    found = set(_send_sites())

    assert found == set(SEND_SITES), (
        f"new: {sorted(found - set(SEND_SITES))}, gone: {sorted(set(SEND_SITES) - found)}"
    )


def test_only_four_paths_can_speak_first() -> None:
    proactive = {site for site, kind in SEND_SITES.items() if kind in PROACTIVE}

    assert len(proactive) == 4
    assert proactive == {
        ("bridge/onboarding/board.py", "_post"),
        ("bridge/service/runtime.py", "send"),
        ("bridge/provisioning/runtime.py", "announce"),
        ("bridge/bootstrap/handoff.py", "_bot_invites"),
    }


def test_the_only_operational_sender_is_the_gated_push() -> None:
    """Health, delivery, provisioning and the queue reach the chat through one
    function, and that function is the one `critical()` guards."""
    runtime = Path("bridge/service/runtime.py").read_text(encoding="utf-8")
    tree = ast.parse(runtime)

    senders = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "send_message"
    ]
    assert len(senders) == 1, "the worker has exactly one way to post to the guardian"

    # And it sits inside the notification centre it is built for, so there is no
    # second caller that could reach it without going through `critical()`.
    builders = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == "_notification_centre"
    ]
    assert len(builders) == 1
    assert builders[0].lineno <= senders[0].lineno <= (builders[0].end_lineno or 0)


def test_the_snapshot_still_carries_everything_the_screens_read() -> None:
    """Nothing about detection moved: the fields the Problem Center reads are
    the fields the alerts were decided from, and they are all still here."""
    fields = {field.name for field in dataclasses.fields(HealthSnapshot)}

    for name in (
        "outbox_failed",
        "outbox_ambiguous",
        "outbox_expired",
        "outbox_pending",
        "oldest_pending_ms",
        "max_offline_ms",
        "database_write",
        "owner_ingress",
    ):
        assert name in fields
