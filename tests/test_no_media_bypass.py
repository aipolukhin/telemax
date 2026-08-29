"""One rule, enforced by reading the source: a remote effect goes through the queue.

Every way a message reaches Telegram — the contact bot's `MaxMediaDelivery`, and
the owner's own session placing their own words — writes a dedup row *before* it
sends. So a send that skips the job skips the retry, the lease, the
FAILED/AMBIGUOUS accounting and the `source_key` with it, and a failure leaves a
claim with nothing behind it: the next MAX replay reads that claim as "already
delivered" and the message is gone for good.

That has been reintroduced three times now. Once as the original `on_max_message`
branch, once as an `except UnstorablePayloadError` fallback that looked like a
safety net, and once — for the whole of the owner's own voice — as an explicit
exemption in this very file, written when the claim still sat *below* the branch
and left in place after it moved above. Thirty orphaned rows in the live database
are what that exemption cost.

So the exemption is gone and the rule covers the owner session's three
effect-bearing methods too. It reads the code rather than exercising it because
the failure mode is a line that *looks* reasonable in review: a behavioural test
only catches a bypass on the paths it happens to drive, and this catches one the
moment it is written.
"""

from __future__ import annotations

import ast
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent.parent / "bridge"

#: Every call site that is not a bypass, and why. Kept short and argued: an
#: allowlist that grows without reasons is how the rule stops meaning anything.
ALLOWED = {
    # The queue's sender. Runs only for a job that already exists, and its
    # outcome settles that job. This is *the* delivery path.
    ("bridge/service/runtime.py", "send_job"),
    # The owner-side half of that sender, lifted out of it so a test can drive
    # the delivery itself rather than a reimplementation of it. Reached only
    # from `send_job`, which `test_the_owner_placement_is_only_reached_from_the_
    # queue` holds shut.
    ("bridge/service/runtime.py", "place_owner_message"),
    # `MaxMediaDelivery.deliver` calling itself: the clone that carries the
    # sending hook. Same class, same message, one extra frame.
    ("bridge/media/delivery.py", "deliver"),
    ("bridge/media/delivery.py", "deliver_receipt"),
    # The `self._pipe is None` branch in the router: unit tests construct a
    # router without a queue. Production never does — `test_production_always_
    # wires_the_queue` below is what keeps that true.
    ("bridge/routing/router.py", "_send_media_durably"),
}

#: The owner session's own effect-bearing methods, and the only functions that
#: may call each. Every one of them is a remote effect on the owner's account:
#: two put a message in a chat, the third takes messages out of one.
OWNER_SESSION_EFFECTS: dict[str, set[tuple[str, str]]] = {
    "send_own_message": {
        # The adapters the queue's sender reaches the session through. They hold
        # no policy of their own — they resolve the session, call it, and let the
        # exception travel so the queue can classify it.
        ("bridge/routing/owner_voice.py", "send_as_owner"),
        ("bridge/routing/owner_voice.py", "send_text"),
        # The port itself.
        ("bridge/telegram/user_session.py", "send_own_message"),
    },
    "send_own_file": {
        ("bridge/routing/owner_voice.py", "_place"),
        ("bridge/telegram/user_session.py", "send_own_file"),
    },
    "delete_own_messages": {
        # The album sweep, and nothing else on the Telegram→MAX side. It runs as
        # a durable job, and the ids it names come out of the album's own
        # aliases.
        ("bridge/service/runtime.py", "sweep_album_in_telegram"),
        # A MAX deletion carried the other way, which is the same shape: a
        # durable job, and ids re-derived from the mapping and its aliases rather
        # than trusted from the payload.
        ("bridge/routing/max_mutation.py", "resolve_max_delete"),
        ("bridge/telegram/user_session.py", "delete_own_messages"),
    },
    "edit_own_message": {
        # A MAX edit of a message the owner's own account placed. The bot cannot
        # do it — it may only edit its own messages — so the session that placed
        # it is the one that changes it, from inside the queue's sender.
        ("bridge/routing/max_mutation.py", "resolve_max_edit"),
        ("bridge/telegram/user_session.py", "edit_own_message"),
    },
}

#: Where a remote effect must never appear, whatever it is called on. These are
#: the ingress surfaces: a Telethon update handler, the Bot API routers, the
#: guardian's dispatcher. Each of them runs with no job on the queue and nothing
#: to record the outcome against.
INGRESS_MODULES = (
    "routing/adapters.py",
    "routing/upload_router.py",
    "provisioning/guardian.py",
    "onboarding/router.py",
)


def _enclosing_functions(tree: ast.AST) -> dict[ast.AST, str]:
    """Map every node to the function it sits in, innermost first."""
    owner: dict[ast.AST, str] = {}

    def walk(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = current
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = child.name
            owner[child] = name
            walk(child, name)

    walk(tree, "<module>")
    return owner


def _calls_named(path: Path, names: set[str]) -> list[tuple[str, int, str]]:
    """Every `<something>.<name>(...)` call, with the function containing it."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner = _enclosing_functions(tree)
    found: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in names:
            continue
        found.append((owner.get(node, "<module>"), node.lineno, ast.unparse(func)))
    return found


def _deliver_calls(path: Path) -> list[tuple[str, int, str]]:
    """Every `<something>.deliver(...)` / `.deliver_receipt(...)` call.

    Both, because they are the same act: `deliver` is `deliver_receipt` with the
    per-part ids thrown away, and a rule that named only one of them would be a
    rule with a documented way around it.
    """
    return _calls_named(path, {"deliver", "deliver_receipt"})


def test_media_delivery_is_only_called_by_the_queues_sender() -> None:
    offenders: list[str] = []
    for path in sorted(BRIDGE.rglob("*.py")):
        relative = path.relative_to(BRIDGE.parent).as_posix()
        for function, line, expression in _deliver_calls(path):
            if (relative, function) in ALLOWED:
                continue
            offenders.append(f"{relative}:{line} in {function}() — {expression}")

    assert not offenders, (
        "media may only be sent from the queue's sender. These calls bypass the "
        "durable job, so a failure leaves a dedup row with nothing behind it:\n  "
        + "\n  ".join(offenders)
        + "\n\nIf a new call site is genuinely part of the pipeline, add it to "
        "ALLOWED in this test and say why."
    )


def test_the_router_has_no_media_fallback() -> None:
    """The specific shape that came back once already.

    `except ...: await self._media.deliver(...)` reads like prudence and is the
    opposite: it is reached exactly when the durable record could not be made,
    which is when the guarantee matters most.
    """
    router = BRIDGE / "routing" / "router.py"
    tree = ast.parse(router.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "deliver"
            ):
                raise AssertionError(
                    f"router.py:{inner.lineno} sends media from an exception handler. "
                    "A payload that cannot be stored becomes a FAILED job, not a "
                    "send that skips the queue."
                )


def test_the_allowlist_still_points_at_real_code() -> None:
    """A stale allowlist silently permits everything it no longer describes."""
    for relative, function in ALLOWED:
        path = BRIDGE.parent / relative
        assert path.exists(), f"{relative} is in ALLOWED and does not exist"
        names = {
            node.name
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        assert function in names, f"{relative} no longer defines {function}()"


def test_production_always_wires_the_queue() -> None:
    """The router's no-queue branch must stay a test affordance.

    `_send_media_durably` falls back to a direct send when `self._pipe is None`,
    which is how unit tests drive routing without a database. That is only
    harmless while the running service never takes it — so the composition root
    is checked to pass a pipe every time it builds a router.
    """
    runtime = BRIDGE / "service" / "runtime.py"
    tree = ast.parse(runtime.read_text(encoding="utf-8"))

    built = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else None
        if name != "BridgeRouter":
            continue
        built += 1
        keywords = {keyword.arg for keyword in node.keywords}
        assert "pipe" in keywords, (
            f"runtime.py:{node.lineno} builds a BridgeRouter without a pipe — "
            "production would then send media past the queue"
        )

    assert built == 1, f"expected one BridgeRouter in the composition root, found {built}"


def test_the_no_queue_branch_is_guarded_by_the_pipe_check() -> None:
    """And that branch must remain exactly that: a check, not a fallback."""
    router = BRIDGE / "routing" / "router.py"
    tree = ast.parse(router.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name != "_send_media_durably":
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "deliver"
            ):
                # Walk outwards: the call must sit under `if self._pipe is None`.
                guarded = any(
                    isinstance(parent, ast.If)
                    and "self._pipe is None" in ast.unparse(parent.test)
                    and inner in list(ast.walk(parent))
                    for parent in ast.walk(node)
                )
                assert guarded, (
                    f"router.py:{inner.lineno} sends media outside the "
                    "`if self._pipe is None` guard"
                )
        return
    raise AssertionError("_send_media_durably is gone; this test needs updating")


# ------------------------------------------- the owner session's own effects


def test_the_owner_session_is_only_reached_from_the_queues_lifecycle() -> None:
    """Placing and deleting as the owner are remote effects like any other.

    They act on the *owner's account* — one puts words in a chat under their
    name, the other takes messages out of it — so a call site with no durable job
    behind it is worse than a media bypass, not better. This is the rule that
    was missing while `own_media` sat in an exemption list.
    """
    offenders: list[str] = []
    for path in sorted(BRIDGE.rglob("*.py")):
        relative = path.relative_to(BRIDGE.parent).as_posix()
        for function, line, expression in _calls_named(path, set(OWNER_SESSION_EFFECTS)):
            method = expression.rsplit(".", 1)[-1]
            if (relative, function) in OWNER_SESSION_EFFECTS[method]:
                continue
            offenders.append(f"{relative}:{line} in {function}() — {expression}")

    assert not offenders, (
        "the owner's session may only be used from the queue's own lifecycle. "
        "These calls place or delete without a durable job behind them:\n  "
        + "\n  ".join(offenders)
        + "\n\nIf a new call site is genuinely part of the pipeline, add it to "
        "OWNER_SESSION_EFFECTS in this test and say why."
    )


def test_no_ingress_handler_performs_a_remote_effect() -> None:
    """A Telethon update, a Bot API message, a guardian callback: all handlers.

    None of them has a job on the queue when it runs, and none of them has
    anywhere to record what happened. Whatever they decide has to become a row
    before it becomes a request.
    """
    forbidden = set(OWNER_SESSION_EFFECTS) | {"deliver", "deliver_receipt"}
    offenders: list[str] = []
    for relative in INGRESS_MODULES:
        path = BRIDGE / relative
        assert path.exists(), f"{relative} is named in INGRESS_MODULES and does not exist"
        for function, line, expression in _calls_named(path, forbidden):
            offenders.append(f"bridge/{relative}:{line} in {function}() — {expression}")

    assert not offenders, (
        "an ingress handler reached a remote transport directly:\n  " + "\n  ".join(offenders)
    )


def test_the_router_decides_and_never_places() -> None:
    """`_deliver_as_owner` is a decision. The placement belongs to the sender.

    It used to call the session here, with the claim already written, and both of
    the defects that cost real messages followed from that one line.
    """
    router = BRIDGE / "routing" / "router.py"
    tree = ast.parse(router.read_text(encoding="utf-8"))
    owner = _enclosing_functions(tree)

    forbidden = set(OWNER_SESSION_EFFECTS) | {
        "deliver",
        "deliver_receipt",
        "send_as_owner",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in forbidden:
            continue
        holder = owner.get(node, "<module>")
        assert holder != "_deliver_as_owner", (
            f"router.py:{node.lineno} places a message from the decision that a "
            "message should be placed. The job comes first."
        )


def test_the_owner_placement_is_only_reached_from_the_queue() -> None:
    """`place_owner_message` is the sender's own step, not a callable shortcut."""
    runtime = BRIDGE / "service" / "runtime.py"
    tree = ast.parse(runtime.read_text(encoding="utf-8"))
    owner = _enclosing_functions(tree)

    callers = {
        owner.get(node, "<module>")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "place_owner_message"
    }
    assert callers == {"send_job"}, f"reached from {sorted(callers)}, expected only send_job"


def test_the_owner_placement_holds_no_bot_api_fallback() -> None:
    """Past the remote boundary there is exactly one transport.

    The duplicate this whole change exists for was a fallback: the session
    answered None on a timeout that arrived after Telegram had taken the message,
    and the Bot API branch placed it again. So the sender's owner path may not
    even mention the bot's transports.
    """
    runtime = BRIDGE / "service" / "runtime.py"
    tree = ast.parse(runtime.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name != "place_owner_message":
            continue
        source = ast.unparse(node)
        for forbidden in ("telegram_sender", "RegistrySender", "max_media", "send_text("):
            assert forbidden not in source, (
                f"place_owner_message mentions {forbidden!r}; the owner path must "
                "not be able to reach the contact bot at all"
            )
        return
    raise AssertionError("place_owner_message is gone; this test needs updating")


def test_the_owner_kind_settles_before_it_can_be_marked_done() -> None:
    """The payload holds `link_id`, and `mark_done` clears the payload.

    So the owner-side id has to be written from inside the sender, while the job
    is still in flight — which is what `settle_owner_delivery_mapping` is, and
    why the sender returns its result rather than settling beside it.
    """
    runtime = BRIDGE / "service" / "runtime.py"
    tree = ast.parse(runtime.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name != "place_owner_message":
            continue
        returns = [
            ast.unparse(inner.value)
            for inner in ast.walk(node)
            if isinstance(inner, ast.Return) and inner.value is not None
        ]
        assert returns, "the sender must return the settled id"
        assert all("settle_owner_delivery_mapping" in text for text in returns), (
            "every way out of the owner placement settles the mapping first: "
            f"{returns}"
        )
        return
    raise AssertionError("place_owner_message is gone; this test needs updating")


def test_the_owner_kind_has_no_retry_loop_of_its_own() -> None:
    """One backoff policy, in the worker. A second one is how they diverge."""
    for relative in ("routing/owner_voice.py", "telegram/user_session.py"):
        source = (BRIDGE / relative).read_text(encoding="utf-8")
        for forbidden in ("BackoffPolicy", "for attempt in", "while True:\n            try"):
            assert forbidden not in source, (
                f"bridge/{relative} looks like it retries on its own; the queue owns that"
            )


# ------------------------------------------------ one owner-delete entry point


def test_owner_deletions_have_one_production_entry_point() -> None:
    """Two transports used to carry the same event, and only one was durable.

    `deleted_business_messages` reached the guardian and deleted in MAX straight
    from the handler: no job, no source_key, an exception swallowed, and — the
    part that made it more than a style question — no album sweep, so what the
    owner saw depended on which transport happened to deliver the news.

    The owner's own MTProto session carries it now, into the durable path, and
    the Business route is gone rather than merely unused: an update type still
    named in `allowed_updates` is a standing subscription, and a handler still
    registered is a path a future reader will assume is live.
    """
    registry = (BRIDGE / "telegram" / "registry.py").read_text(encoding="utf-8")
    assert "deleted_business_messages" not in registry, (
        "still subscribed: Telegram will keep sending it and something will grow "
        "a handler for it again"
    )

    guardian = (BRIDGE / "provisioning" / "guardian.py").read_text(encoding="utf-8")
    assert "deleted_business_messages" not in guardian
    assert "on_owner_deleted" not in guardian

    router = (BRIDGE / "routing" / "router.py").read_text(encoding="utf-8")
    assert "async def on_owner_deleted" not in router, (
        "the direct MAX delete is back; owner deletions belong on the queue"
    )

    # And the durable one is still the only one: exactly one place turns an
    # owner-side deletion into a job.
    tree = ast.parse(router)
    entries = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("on_owner_delete")
    }
    assert entries == {"on_owner_delete"}, f"owner-delete entry points: {sorted(entries)}"


def test_the_business_connection_is_still_recorded() -> None:
    """Removing the delete path is not removing Secretary Mode's leftovers.

    The connection id arrives exactly once and cannot be asked for again, so it
    is still written down. What went is the one thing that acted on it behind the
    queue's back.
    """
    registry = (BRIDGE / "telegram" / "registry.py").read_text(encoding="utf-8")
    guardian = (BRIDGE / "provisioning" / "guardian.py").read_text(encoding="utf-8")
    assert '"business_connection"' in registry
    assert "@router.business_connection()" in guardian
    assert (BRIDGE / "provisioning" / "business.py").exists()
