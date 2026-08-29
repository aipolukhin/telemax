"""AU-3 — the shape of the code, asserted rather than remembered.

Every finding AU-3 fixed was a *structural* one: the same decision made in five
places that had drifted apart, a settlement that existed on one path and not the
other, a classification called from two of twenty-one sites. None of them was a
wrong line. All of them were a second copy that stopped agreeing with the first.

So the repairs are held by shape, not only by behaviour. Each guard below is one
sentence about the codebase that has to keep being true, and each one names the
finding it came from — a guard nobody can trace back to a real failure is a guard
that gets deleted the first time it is inconvenient.

These parse `bridge/` with `ast` and read nothing at runtime.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

BRIDGE = Path("bridge")


def modules() -> list[tuple[Path, ast.Module]]:
    return [(path, ast.parse(path.read_text(encoding="utf-8"))) for path in sorted(
        BRIDGE.rglob("*.py")
    )]


def calls(tree: ast.Module) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def enclosing_names(tree: ast.Module, node: ast.AST) -> set[str]:
    return {
        parent.name
        for parent in ast.walk(tree)
        if isinstance(parent, ast.AsyncFunctionDef | ast.FunctionDef)
        and parent.lineno <= node.lineno <= (parent.end_lineno or 0)
    }


# ------------------------------------------------------- G1: one creating path


def test_every_message_creating_call_goes_through_the_shared_boundary() -> None:
    """G1. `MaxClient` may reach MAX in exactly one way for a message that
    creates something, and that way decides what a failure means."""
    tree = ast.parse((BRIDGE / "max_client" / "client.py").read_text(encoding="utf-8"))
    creating = {"send_text", "send_contact", "send_sticker", "send_media", "_send_native_media"}

    for function in ast.walk(tree):
        if not isinstance(function, ast.AsyncFunctionDef) or function.name not in creating:
            continue
        if function.name == "send_media":
            # Routes to the sticker, native and plain paths; each is checked on
            # its own line, and its own plain branch is checked by the dump below.
            pass
        body = ast.dump(function)
        # `_invoke_native_send` is the native specialisation of the boundary, not
        # a way around it — it is nothing but a call to `_run_creating_call` with
        # the breaker hook attached.
        assert "_run_creating_call" in body or "_invoke_native_send" in body, (
            f"{function.name} reaches MAX without the shared creating boundary"
        )


def test_the_creating_boundary_is_the_only_reader_of_a_sent_message_id() -> None:
    """G7. Five copies of "read the id out of the answer" had drifted far enough
    that a contact and a sticker disagreed about the same reply."""
    tree = ast.parse((BRIDGE / "max_client" / "client.py").read_text(encoding="utf-8"))
    # Passed as `extract_message_id=`, so it is a reference and not a call.
    readers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "_message_id_of"
    ]
    assert len(readers) > 1, "the shared reader is defined and not used"

    # The shared reader reads it by hand by definition — that is what it is for.
    # Found by name rather than by line number: this used to exclude "line > 270"
    # and started failing the moment an import was added above it, which is a
    # test that measures the wrong thing.
    own = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_message_id_of"
    )

    # `sticker.get("id")` is a *sticker* id off op193 — a different thing that is
    # not a sent message. What must not come back is `message.get("id")` outside
    # the shared reader, which is the copy that drifted.
    handmade = [
        f"line {node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and ast.unparse(node.func.value) == "message"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "id"
        and not (own.lineno <= node.lineno <= (own.end_lineno or own.lineno))
    ]
    assert handmade == [], f"a message id is read by hand outside the shared reader: {handmade}"


# ------------------------------------------------- G1: no direct MAX from above


def test_routing_and_ingress_never_send_to_max_by_hand() -> None:
    """G1/G4. A creating send outside the durable path is a message with no job
    behind it — which is exactly what made a replayed reaction send twice."""
    allowed_files = {
        Path("bridge/max_client/client.py"),  # the client itself
        Path("bridge/routing/adapters.py"),  # and its adapters, which only
        Path("bridge/reactions/adapters.py"),  # forward one call each
    }
    allowed_functions = {
        "_deliver_to_max",  # the durable entry point, and its no-queue fallback
        "carry_emoji_note",  # builds a job, then hands the lambda to the above
        "on_telegram_text",
        "on_telegram_contact",
        "on_telegram_media",
        "_carry_emoji_note",  # the reaction fallback when no queue is wired
        "send_job",  # the durable branch both senders run
        "_creating_send",
        "_carry_media_into_max",
    }
    creating = {"send_text", "send_contact", "send_sticker", "send_media"}

    offenders: list[str] = []
    for path, tree in modules():
        if path in allowed_files:
            continue
        for node in calls(tree):
            if not isinstance(node.func, ast.Attribute) or node.func.attr not in creating:
                continue
            owner = ast.unparse(node.func.value)
            if not any(marker in owner for marker in ("_max", "max_sender", "_client")):
                continue  # a Telegram-side sender of the same name
            if enclosing_names(tree, node) & allowed_functions:
                continue
            offenders.append(f"{path}:{node.lineno} {owner}.{node.func.attr}")
    assert offenders == [], f"MAX sends outside the durable path: {offenders}"


# ------------------------------------------------------- G3: one settlement


def test_the_mapping_is_settled_in_one_place() -> None:
    """G3. The attach lived in the inline sender only, so anything the worker
    carried never got its MAX id."""
    offenders: list[str] = []
    for path, tree in modules():
        if path.name in ("settlement.py", "repositories.py"):
            continue
        for node in calls(tree):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "attach_max_message":
                offenders.append(f"{path}:{node.lineno}")
    assert offenders == [], f"attach_max_message outside the shared settlement: {offenders}"


# --------------------------------------------------- G2: one failure settlement


def test_both_senders_ask_the_same_policy() -> None:
    """G2. Crash recovery read `send_started_at` and the live paths did not, so a
    process that died was handled more carefully than one that timed out."""
    for module in (BRIDGE / "routing" / "delivery.py", BRIDGE / "retry" / "worker.py"):
        body = module.read_text(encoding="utf-8")
        assert "settle(" in body, f"{module} does not consult the shared settlement policy"


def test_nothing_marks_a_job_ambiguous_without_the_policy() -> None:
    """The other half: the policy is not advisory."""
    offenders: list[str] = []
    for path, tree in modules():
        if path.name in ("delivery.py", "worker.py", "repositories.py"):
            continue
        for node in calls(tree):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "mark_ambiguous":
                offenders.append(f"{path}:{node.lineno}")
    assert offenders == [], f"a job is marked ambiguous outside the two senders: {offenders}"


def test_creating_kinds_are_declared_once() -> None:
    """G2. "Would a retry duplicate this?" has one answer per kind, in one file."""
    from bridge.routing.settlement import CREATING_KINDS

    assert CREATING_KINDS
    offenders: list[str] = []
    for path, tree in modules():
        if path.name == "settlement.py":
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and "CREATING" in ast.unparse(node):
                offenders.append(f"{path}:{node.lineno}")
    assert offenders == [], f"creating-kind membership tested outside settlement: {offenders}"


# ------------------------------------------------ G5: one error classification


def test_max_error_markers_live_in_one_module() -> None:
    """G5. Three mechanisms classified MAX errors and none saw the whole set."""
    markers = (
        "chat.control",
        "restriction to input",
        "no access",
        "proto.payload",
        "invalid media wave",
        "errors.process.attachment.video.not.supported",
    )
    offenders: list[str] = []
    for path, tree in modules():
        if path.name == "max_errors.py":
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in markers:
                offenders.append(f"{path}:{node.lineno} {node.value!r}")
    assert offenders == [], f"MAX error markers outside max_errors.py: {offenders}"


# ------------------------------------------------------- G6: no direct mutation


def test_max_is_edited_and_deleted_only_through_the_mutation_path() -> None:
    """G6. `/del` and the Bot API edit called MAX from the router, so they had no
    job, no dedup and no coalescing with the owner-side entrance."""
    allowed_files = {
        Path("bridge/routing/owner_mutation.py"),
        Path("bridge/routing/adapters.py"),
        Path("bridge/max_client/client.py"),
    }
    offenders: list[str] = []
    for path, tree in modules():
        if path in allowed_files:
            continue
        for node in calls(tree):
            if not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in ("edit_text", "delete_messages"):
                continue
            owner = ast.unparse(node.func.value)
            if not any(marker in owner for marker in ("_max", "max_sender", "_client")):
                continue
            if "_apply_mutation_directly" in enclosing_names(tree, node):
                continue  # the documented no-queue fallback
            offenders.append(f"{path}:{node.lineno} {owner}.{node.func.attr}")
    assert offenders == [], f"direct MAX edit/delete outside the mutation path: {offenders}"


# -------------------------------------------------------- one queue, one worker


def test_there_is_one_queue_and_one_worker() -> None:
    """A second queue is how the two halves of a lifecycle stop agreeing, which
    is the shape of every finding above."""
    worker_classes = [
        node.name
        for _, tree in modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name.endswith("Worker")
    ]
    assert worker_classes == ["OutboxWorker"], worker_classes


def test_no_send_has_a_retry_loop_of_its_own() -> None:
    """The backoff policy is the worker's. A feature that grows its own loop
    stops being covered by the settlement above."""
    # Scoped to the delivery machinery. Provisioning polls BotFather for a
    # released username, which is a different kind of waiting and not a delivery.
    watched = ("routing", "retry", "max_client", "media")
    offenders: list[str] = []
    for path, tree in modules():
        if path.name in ("worker.py", "http.py"):  # the worker, and HTTP downloads
            continue
        if not any(part in path.parts for part in watched):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.For):
                continue
            if "attempt" not in ast.unparse(node.target).lower():
                continue
            offenders.append(f"{path}:{node.lineno}")
    assert offenders == [], f"a feature-specific retry loop: {offenders}"


# ---------------------------------------------- deletion: one gesture, one door


def test_del_is_not_registered_anywhere() -> None:
    """The product decision: deletion has one gesture — deleting the message —
    and `/del` was a second door to the same room, left over from before the
    owner's own session reported deletions at all."""
    offenders: list[str] = []
    for path, tree in modules():
        for node in calls(tree):
            if not isinstance(node.func, ast.Name) or node.func.id != "Command":
                continue
            for argument in node.args:
                if isinstance(argument, ast.Constant) and argument.value == "del":
                    offenders.append(f"{path}:{node.lineno}")
    assert offenders == [], f"/del is registered again: {offenders}"


def test_no_router_entry_answers_a_del_command() -> None:
    """Registration is one half; the router entry it called is the other.

    Named by the exact symbol rather than by shape: `guardian._delete` removes
    the message that carried a bot token, which is a different thing that
    happens to read alike.
    """
    offenders: list[str] = []
    for path, tree in modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and (
                "delete_request" in node.name
            ):
                offenders.append(f"{path}:{node.lineno} {node.name}")
    assert offenders == [], f"a /del router entry is back: {offenders}"


def test_del_is_not_promised_in_any_user_facing_string() -> None:
    """Help, menus and command descriptions. Historical prose that explains the
    removal is fine and is what the `gone`/`used to` check allows for."""
    offenders: list[str] = []
    for path, tree in modules():
        # Docstrings are prose about the code, and the prose that records *why*
        # `/del` is gone is worth keeping. What must not survive is a literal a
        # person could be shown.
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings:
                continue
            # `/del` as a command, not as a fragment of "edits/deletes" or of
            # BotFather's own `/deletebot`.
            if not re.search(r"/del\b", node.value) or "/deletebot" in node.value:
                continue
            offenders.append(f"{path}:{node.lineno}")
    assert offenders == [], f"a user-facing string still offers /del: {offenders}"


def test_owner_mtproto_is_the_only_deletion_ingress() -> None:
    """`on_owner_delete` on the router is reached from exactly one place: the
    MTProto intake, which is reached from the one `UpdateDeleteMessages` handler.

    A second caller would be a second ingress for one user action, which is the
    whole thing this removal was for."""
    callers = {
        str(path)
        for path, tree in modules()
        for node in calls(tree)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "on_owner_delete"
    }
    # One chain, now three links: the session's update handler calls the intake,
    # the intake writes the update down and hands it to the dispatch, and the
    # dispatch carries it to the router. Any fourth file is a second door.
    assert callers == {
        "bridge/telegram/user_session.py",
        "bridge/telegram/mtproto_intake.py",
        "bridge/routing/owner_updates.py",
    }, callers

    handlers = [
        f"{path}:{node.lineno}"
        for path, tree in modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "UpdateDeleteMessages"
    ]
    assert len(handlers) == 1, f"more than one UpdateDeleteMessages handler: {handlers}"


def test_only_the_owner_delete_intake_creates_a_delete_job() -> None:
    """No Bot API handler may enqueue `tg_to_max_delete`."""
    offenders: list[str] = []
    for path, tree in modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Name) or node.id != "KIND_TG_TO_MAX_DELETE":
                continue
            enclosing = enclosing_names(tree, node)
            # The producer, the resolver's own dispatch, and the key builder.
            allowed = {
                "on_owner_delete",  # the producer
                "send_job",  # the branch both senders run
                "_send",
                "resolve_delete",
                "_apply_mutation_directly",  # the documented no-queue fallback
            }
            if enclosing & allowed or not enclosing:
                continue
            offenders.append(f"{path}:{node.lineno} in {sorted(enclosing)}")
    assert offenders == [], f"a delete job is created outside the owner intake: {offenders}"


def test_no_bot_side_delete_namespace_is_produced() -> None:
    """`tg-owner-delete:0:bot<id>:<msg>` existed only for `/del`. `_bot_side_target`
    survives for the Bot API *edit* fallback — a real caller with a real
    scenario — so the account-0 branch stays; what must not come back is a
    *delete* built from it."""
    tree = ast.parse(Path("bridge/routing/router.py").read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in calls(tree):
        if not isinstance(node.func, ast.Name) or node.func.id != "delete_source_key":
            continue
        if "_bot_side_target" in ast.unparse(node):
            offenders.append(f"line {node.lineno}")
        enclosing = enclosing_names(tree, node)
        if enclosing and not (enclosing & {"on_owner_delete"}):
            offenders.append(f"line {node.lineno} in {sorted(enclosing)}")
    assert offenders == [], f"a bot-side delete key is built again: {offenders}"


def test_there_is_no_bot_side_owner_target_at_all() -> None:
    """It existed to translate a bot-side id into the owner-side identity a
    mutation keys on, for two Bot API entrances. `/del` went in the previous
    commit and the edit fallback in this one, so it has no callers — and a
    resurrection would mean Bot API is authoring owner events again."""
    source = Path("bridge/routing/router.py").read_text(encoding="utf-8")
    assert "_bot_side_target" not in source
    assert "on_telegram_edit" not in source


# ------------------------------------------------- owner ingress: one transport


#: Everything the owner does that the bridge carries. Named as one list because
#: the invariant is the same for all of them: the puppet session sees it, and
#: nothing else may author it.
OWNER_EVENTS = ("text", "media", "album", "reply", "edit", "delete", "reaction")


def _builder_body(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is gone from {path}")


def test_no_bot_api_handler_creates_an_owner_job() -> None:
    """G-owner. The contact bot is the chat the owner types into, so its handlers
    see every one of their messages. They may recognise an echo of something the
    bridge placed, and they may count the hand-off. They may not carry it.

    Scoped to the two builder functions, because the same modules also hold the
    machinery the *MTProto* intake drives — `MediaUploader._deliver` calls
    `on_telegram_media` and must keep doing so."""
    creating = {
        "on_telegram_text",
        "on_telegram_media",
        "on_telegram_contact",
        "on_telegram_edit",
        "on_telegram_delete_request",
        "handle",  # MediaUploader.handle — the Bot API upload entry
    }
    offenders: list[str] = []
    for path, builder in (
        (Path("bridge/routing/adapters.py"), "build_forwarding_router"),
        (Path("bridge/routing/upload_router.py"), "build_upload_router"),
    ):
        for node in ast.walk(_builder_body(path, builder)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in creating
            ):
                offenders.append(f"{path}:{node.lineno} {node.func.attr}")
    assert offenders == [], f"a Bot API handler authors an owner event: {offenders}"


def test_the_owner_intake_has_no_configuration_to_turn_off() -> None:
    """There is no flag, so there is no state in which Bot API becomes the owner
    ingress — not on a disconnect, not on a fresh install, not by editing a file."""
    import inspect

    from bridge.onboarding.state import OnboardingRecord
    from bridge.routing.adapters import build_forwarding_router
    from bridge.routing.upload_router import build_upload_router

    assert not hasattr(OnboardingRecord(), "owner_mtproto_intake_enabled")
    for builder in (build_forwarding_router, build_upload_router):
        assert "owner_mtproto_intake" not in inspect.signature(builder).parameters

    offenders = [
        f"{path}:{node.lineno}"
        for path, tree in modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "owner_mtproto_intake"
    ]
    assert offenders == [], f"the gate is back: {offenders}"


def test_every_carried_owner_event_enters_through_the_intake() -> None:
    """The owner side has one entrance, and these are its doors.

    Two modules, because an edit stopped being readable off the update: the
    intake decides the dialog is ours, and `owner_updates` decides — against
    durable state — whether what arrived was an edit, a reaction, or both."""
    reached: set[str] = set()
    for path in (
        Path("bridge/telegram/mtproto_intake.py"),
        Path("bridge/routing/owner_updates.py"),
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        reached |= {
            node.func.attr for node in calls(tree) if isinstance(node.func, ast.Attribute)
        }
    for entry in (
        "on_telegram_text",
        "on_telegram_media",
        "on_owner_edit",
        "on_owner_delete",
        "apply_owner_reaction",
    ):
        assert entry in reached, f"{entry} is not reached from the MTProto intake"


def test_no_reaction_reaches_max_from_a_bot_api_router() -> None:
    """The exception that used to live here is gone with the path it named. The
    owner's reactions enter on the puppet session, like everything else they do
    in Telegram, and no aiogram router may author one."""
    offenders: list[str] = []
    for path, tree in modules():
        if path.parts[:2] != ("bridge", "routing"):
            continue
        if path.name == "owner_updates.py":
            continue  # the one reader, and it is fed by the MTProto intake
        for node in calls(tree):
            if isinstance(node.func, ast.Attribute) and node.func.attr in (
                "apply_owner_reaction",
                "on_owner_reaction",
            ):
                offenders.append(f"{path}:{node.lineno}")
    assert offenders == [], f"a router authors an owner reaction: {offenders}"


def test_the_picker_is_gone_from_every_layer() -> None:
    """It was authoring UX for a transport that no longer exists. Left behind in
    config it would read as a feature that had simply stopped working."""
    import bridge.config as config_module
    import bridge.reactions as reactions_module
    from bridge.config import ReactionsConfig

    assert not hasattr(config_module, "ReactionPicker")
    assert not hasattr(reactions_module, "default_picker")
    for field in ("picker", "picker_emoji"):
        assert field not in ReactionsConfig.model_fields


def test_one_update_dispatcher_computes_both_diffs() -> None:
    """Content and reactions are read from one snapshot in one place. Two
    readers of a *live update* would be two opinions about what the message was
    before; the bootstrap is excluded by name because it reads a message that no
    update is being derived from — it writes the baseline, and derives nothing."""
    offenders = [
        f"{path}:{node.lineno}"
        for path, tree in modules()
        if path.name not in ("owner_updates.py", "owner_bootstrap.py")
        for node in calls(tree)
        if isinstance(node.func, ast.Name) and node.func.id == "content_fingerprint_of"
    ]
    assert offenders == [], f"a second reader of the snapshot: {offenders}"


def test_the_state_only_moves_through_the_compare_and_set() -> None:
    """`advance` is the only writer of a live version, and it is one statement."""
    source = Path("bridge/storage/repositories.py").read_text(encoding="utf-8")
    assert "WHERE excluded.pts > owner_message_state.pts" in source
    offenders = [
        f"{path}:{node.lineno}"
        for path, tree in modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "UPDATE owner_message_state" in node.value
    ]
    assert offenders == [], f"owner_message_state is written outside the CAS: {offenders}"


async def test_the_state_key_is_the_updates_own_identity(tmp_path: Path) -> None:
    """Account, peer, message — exactly what `UpdateEditMessage` carries. An
    owner-side id means nothing outside the account that issued it, and the peer
    is on the update, so a key that mirrors the event cannot name the wrong row."""
    from bridge.storage import Database

    database = await Database.connect(tmp_path / "bridge.db")
    try:
        rows = await database.query("PRAGMA table_info(owner_message_state)")
        key = [row["name"] for row in rows if row["pk"]]
        assert key == [
            "telegram_owner_account_id",
            "telegram_bot_id",
            "telegram_owner_message_id",
        ]
    finally:
        await database.close()


def test_an_emoji_note_never_goes_straight_at_max() -> None:
    """It is a message somebody receives, and a second one is a second message.
    Sending it outside the queue is what put the emoji in the chat twice."""
    tree = ast.parse(Path("bridge/reactions/sync.py").read_text(encoding="utf-8"))
    offenders = [
        f"sync.py:{node.lineno}"
        for node in calls(tree)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "send_text"
    ]
    assert offenders == [], f"an emoji note bypasses the outbox: {offenders}"


def test_the_state_advances_after_every_effect_it_stands_for() -> None:
    """The whole crash contract in one ordering. A row that moved before its
    effects landed says an update was applied that was not, and nothing would
    ever re-derive it."""
    tree = ast.parse(Path("bridge/routing/owner_updates.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        lines = {
            name: [
                call.lineno
                for call in calls(ast.Module(body=node.body, type_ignores=[]))
                if isinstance(call.func, ast.Attribute) and call.func.attr == name
            ]
            for name in ("advance", "on_owner_edit", "apply_owner_reaction", "_carry_reaction")
        }
        effects = lines["on_owner_edit"] + lines["apply_owner_reaction"] + lines["_carry_reaction"]
        for advanced in lines["advance"]:
            assert all(effect < advanced for effect in effects), (
                f"{node.name}: the state moves before an effect it stands for"
            )


def test_one_update_is_read_under_one_lock() -> None:
    """The CAS is not the concurrency protection and must not be mistaken for
    it. Read, staleness, both diffs, both effects and the commit are one section
    keyed by the identity the update carries."""
    tree = ast.parse(Path("bridge/routing/owner_updates.py").read_text(encoding="utf-8"))
    # `_apply` is the entry to the critical section for both paths — a live
    # update and a drain of what was written down and never finished.
    entry = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_apply"
    )
    holds = [
        node
        for node in ast.walk(entry)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "hold"
    ]
    assert holds, "the entry point does not take the per-message lock"

    #: Nothing that reads or writes the state may sit outside it.
    for name in ("get", "advance", "seed"):
        outside = [
            call.lineno
            for call in calls(ast.Module(body=entry.body, type_ignores=[]))
            if isinstance(call.func, ast.Attribute)
            and call.func.attr == name
            and isinstance(call.func.value, ast.Attribute)
            and call.func.value.attr == "_state"
        ]
        assert outside == [], f"{name} runs outside the lock: {outside}"


def test_a_reaction_reaches_max_only_from_the_worker() -> None:
    """Durable accounting, not idempotence, is what makes the effect survive. A
    direct call from the dispatch would be an effect with nothing behind it."""
    offenders: list[str] = []
    for path, tree in modules():
        if path.name in ("owner_mutation.py", "adapters.py", "client.py"):
            # The resolver *is* the worker; the adapter and the client are the
            # wire underneath it, not a second caller.
            continue
        for node in calls(tree):
            if isinstance(node.func, ast.Attribute) and node.func.attr in (
                "add_reaction",
                "remove_reaction",
            ):
                offenders.append(f"{path}:{node.lineno}")
    assert offenders == [], f"a reaction bypasses the queue: {offenders}"


def test_the_reaction_job_carries_the_version_it_was_made_for() -> None:
    """Without it a job that waited out a MAX outage would put back a reaction
    the owner has replaced since."""
    source = Path("bridge/routing/owner_mutation.py").read_text(encoding="utf-8")
    assert "current.pts > pts" in source, "the generation check is gone"

    tree = ast.parse(Path("bridge/routing/router.py").read_text(encoding="utf-8"))
    enqueues = [
        node
        for node in calls(tree)
        if isinstance(node.func, ast.Name) and node.func.id == "owner_reaction_source_key"
    ]
    assert enqueues, "the reaction job is not keyed by its update"


def test_the_bootstrap_writes_through_the_same_lock() -> None:
    """A seed landing in the middle of a reading would be a third opinion about
    what the message was before."""
    tree = ast.parse(Path("bridge/telegram/owner_bootstrap.py").read_text(encoding="utf-8"))
    direct = [
        f"{node.lineno}"
        for node in calls(tree)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "seed"
    ]
    assert direct == [], f"the bootstrap writes the state directly: {direct}"

def test_the_baseline_compares_projections_before_calling_them_applied() -> None:
    """Recording the fetched Telegram state as though MAX already showed it is
    what loses an owner action made during the fetch."""
    source = Path("bridge/telegram/owner_bootstrap.py").read_text(encoding="utf-8")
    assert "projected == already" in source, "the baseline no longer compares projections"
    assert "to_max(" in source


def test_the_bot_side_binding_creates_no_owner_event() -> None:
    """It is the first code since the removal to touch an owner-authored message
    at all, so the line it must not cross is worth holding explicitly."""
    tree = ast.parse(Path("bridge/routing/owner_binding.py").read_text(encoding="utf-8"))
    forbidden = {
        "on_telegram_text", "on_telegram_media", "on_telegram_contact",
        "on_owner_edit", "on_owner_delete", "apply_owner_reaction",
        "carry_owner_reaction", "carry_emoji_note", "send_text",
    }
    offenders = [
        f"{node.lineno} {node.func.attr}"
        for node in calls(tree)
        if isinstance(node.func, ast.Attribute) and node.func.attr in forbidden
    ]
    assert offenders == [], f"the observer authors an owner event: {offenders}"


def test_the_binding_never_takes_a_row_without_counting_the_candidates() -> None:
    """"Newest unattached row" is the rule this must not be. Two messages in
    flight have no evidence saying which is which."""
    source = Path("bridge/routing/owner_binding.py").read_text(encoding="utf-8")
    assert "len(waiting) > 1" in source, "the ambiguity check is gone"
    assert "attach_bot_message_if_unset" in source, "the attach is no longer conditional"
    assert "since_ms=" in source, "the candidate set is unbounded again"

    #: Without the bound the set is every owner message ever written before the
    #: observer existed — 79 of them on the live smoke, so nothing ever bound.
    repository = Path("bridge/storage/repositories.py").read_text(encoding="utf-8")
    assert "AND created_at >= ?" in repository


def test_an_owner_row_never_stores_the_peer_as_its_chat() -> None:
    """A bot posts into the owner's chat, never into its own id. The MTProto
    intake speaks in peers and the mapping row must not."""
    source = Path("bridge/routing/router.py").read_text(encoding="utf-8")
    marker = "telegram_owner_account_id=owner_account_id,"
    owner_branch = source[: source.index(marker)]
    tail = owner_branch[owner_branch.rindex("if owner_account_id is not None:") :]
    assert "telegram_chat_id=self._owner_chat_id" in tail


def test_the_conditional_attach_cannot_overwrite() -> None:
    source = Path("bridge/storage/repositories.py").read_text(encoding="utf-8")
    assert "WHERE id = ? AND telegram_message_id IS NULL" in source

def test_the_update_is_written_down_before_anything_is_derived() -> None:
    """`remember` first, in the entry point. Anything before it is an effect
    derived from an event nothing has written down."""
    tree = ast.parse(Path("bridge/routing/owner_updates.py").read_text(encoding="utf-8"))
    entry = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_owner_update"
    )
    body = ast.Module(body=entry.body, type_ignores=[])
    remembered = [
        call.lineno
        for call in calls(body)
        if isinstance(call.func, ast.Attribute) and call.func.attr == "remember"
    ]
    settled = [
        call.lineno
        for call in calls(body)
        if isinstance(call.func, ast.Attribute) and call.func.attr in ("_settle", "_apply")
    ]
    assert remembered, "the update is not written down at all"
    assert min(remembered) < max(settled), "an effect is derived before the insert"


def test_live_and_recovery_share_one_processing_function() -> None:
    """Recovery that is a second implementation only runs on the day it is
    needed, which is the day it is discovered not to work."""
    tree = ast.parse(Path("bridge/routing/owner_updates.py").read_text(encoding="utf-8"))
    for name in ("on_owner_update", "drain"):
        entry = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name
        )
        reached = {
            call.func.attr
            for call in calls(ast.Module(body=entry.body, type_ignores=[]))
            if isinstance(call.func, ast.Attribute)
        }
        assert "_settle" in reached or "_apply" in reached, f"{name} has its own path"


def test_an_unfinished_owner_update_cannot_be_deleted_by_age() -> None:
    """An old row that is not accounted is the problem, not the litter."""
    source = Path("bridge/storage/repositories.py").read_text(encoding="utf-8")
    sweep = source[source.index("async def sweep_accounted") :][:700]
    assert "state = 'accounted'" in sweep
    assert "created_at" not in sweep, "the sweep matches on age"


def test_the_inbox_state_advances_only_after_the_effects() -> None:
    tree = ast.parse(Path("bridge/routing/owner_updates.py").read_text(encoding="utf-8"))
    entry = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_settle"
    )
    body = calls(ast.Module(body=entry.body, type_ignores=[]))
    applied = [
        c.lineno for c in body if isinstance(c.func, ast.Attribute) and c.func.attr == "_apply"
    ]
    accounted = [
        c.lineno for c in body if isinstance(c.func, ast.Attribute) and c.func.attr == "account"
    ]
    assert applied and accounted
    assert max(applied) < min(accounted), "the row is accounted before its effects"

def test_there_is_one_owner_session_supervisor() -> None:
    """A second watcher of the same session is two opinions about whether it is
    up, and two sets of reconnect attempts against one account."""
    starts = [
        f"{path}:{node.lineno}"
        for path, tree in modules()
        for node in calls(tree)
        if isinstance(node.func, ast.Attribute)
        and node.func.attr == "start"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "telegram-user-session"
    ]
    assert len(starts) == 1, starts


def test_readiness_is_computed_in_one_place() -> None:
    """`/status`, the incident and any future reader ask the same function, so
    they cannot drift into three different answers about the same session."""
    offenders: list[str] = []
    for path, tree in modules():
        if path.name == "health.py":
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in (
                "OwnerIngress",
                "OwnerIngressState",
            ):
                offenders.append(f"{path}:{node.lineno}")
    assert offenders == [], f"readiness is defined twice: {offenders}"


# ------------------------------------------------------------------ cid is not


def test_nothing_treats_cid_as_an_idempotency_key() -> None:
    """AU-3 §4 measured it: `cid` is regenerated on every attempt, never stored,
    and no server-side dedup has been demonstrated. Treating it as a key would
    quietly turn AMBIGUOUS into a guess."""
    offenders: list[str] = []
    for path, tree in modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            text = ast.unparse(node)
            if '"cid"' in text or "'cid'" in text or ".cid" in text:
                offenders.append(f"{path}:{node.lineno} {text}")
    assert offenders == [], f"cid is being compared as an identity: {offenders}"


def test_cid_is_never_stored_in_a_durable_payload() -> None:
    """The other half of the same claim: nothing writes it down, so nothing could
    reuse it on a retry even if the server did dedup."""
    offenders: list[str] = []
    for path, tree in modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key in node.keys:
                if isinstance(key, ast.Constant) and key.value == "cid":
                    enclosing = enclosing_names(tree, node)
                    if enclosing & {"_storable", "enqueue", "submit"}:
                        offenders.append(f"{path}:{node.lineno}")
    assert offenders == [], f"cid written into a durable payload: {offenders}"
