"""AU-4's rules, enforced by reading the source rather than by remembering them.

Every one of these is a defect that shipped. A behavioural test proves the fix;
these prove the *shape* that made the fix possible, so the next change that
reaches for a bare `except Exception` beside a creating call, or edits from an
ingress handler, fails here rather than in somebody's chat.
"""

from __future__ import annotations

import ast
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent.parent / "bridge"

ADAPTER = BRIDGE / "routing" / "media_adapter.py"
ROUTER = BRIDGE / "routing" / "router.py"
MUTATION = BRIDGE / "routing" / "max_mutation.py"
SETTLEMENT = BRIDGE / "routing" / "settlement.py"
SYNC = BRIDGE / "reactions" / "sync.py"
OWNER_VOICE = BRIDGE / "routing" / "owner_voice.py"
WORKER = BRIDGE / "retry" / "worker.py"
PIPE = BRIDGE / "routing" / "delivery.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _function(name: str, tree: ast.AST) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is gone — has it been renamed?")


def _called(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if isinstance(child.func, ast.Attribute):
            names.add(child.func.attr)
        elif isinstance(child.func, ast.Name):
            names.add(child.func.id)
    return names


def _handlers(tree: ast.AST) -> list[ast.ExceptHandler]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]


# ------------------------------------------------- 1. one list of creating kinds


def test_every_max_to_tg_send_is_declared_creating_in_one_place() -> None:
    from bridge.routing.delivery import (
        KIND_MAX_TO_TG_MEDIA,
        KIND_MAX_TO_TG_OWNER,
        KIND_MAX_TO_TG_TEXT,
    )
    from bridge.routing.settlement import CREATING_KINDS

    for kind in (KIND_MAX_TO_TG_TEXT, KIND_MAX_TO_TG_MEDIA, KIND_MAX_TO_TG_OWNER):
        assert kind in CREATING_KINDS, kind


def test_the_mutation_kinds_are_not_creating() -> None:
    """Widening the set must never sweep the idempotent kinds in with it."""
    from bridge.routing.delivery import KIND_MAX_TO_TG_DELETE, KIND_MAX_TO_TG_EDIT
    from bridge.routing.settlement import CREATING_KINDS

    assert KIND_MAX_TO_TG_EDIT not in CREATING_KINDS
    assert KIND_MAX_TO_TG_DELETE not in CREATING_KINDS


# --------------------------------------- 2 & 3. no fallback without an answer


def test_the_media_adapter_catches_nothing_blindly() -> None:
    """`except Exception` beside a creating call is the whole defect, in one line.

    It reads a timeout exactly the same way it reads a refusal, and the branch
    underneath sends the same content a second way.
    """
    offenders = [
        f"{ADAPTER.name}:{handler.lineno}"
        for handler in _handlers(_tree(ADAPTER))
        if handler.type is None
        or (
            isinstance(handler.type, ast.Name)
            and handler.type.id in {"Exception", "BaseException"}
        )
    ]
    assert offenders == [], f"a blind except sits beside a creating call: {offenders}"


def test_every_media_fallback_is_gated_on_a_confirmed_refusal() -> None:
    """Each handler either re-raises or asks the classifier first."""
    for handler in _handlers(_tree(ADAPTER)):
        called = _called(handler)
        reraises = any(
            isinstance(node, ast.Raise) and node.exc is None for node in ast.walk(handler)
        )
        assert reraises, f"{ADAPTER.name}:{handler.lineno} swallows a Telegram failure"
        assert called & {"refused_representation", "refused_group"}, (
            f"{ADAPTER.name}:{handler.lineno} decides a fallback without the classifier"
        )


def test_the_album_only_decomposes_under_a_confirmed_group_refusal() -> None:
    """One request became six messages because this branch was unguarded."""
    send_album = _function("send_album", _tree(ADAPTER))
    handlers = _handlers(send_album)
    assert len(handlers) == 1
    assert "refused_group" in _called(handlers[0])
    assert "_dispatch_single" in _called(handlers[0])
    # And nowhere else: sending the parts one at a time is that branch's job.
    others = [
        node.name
        for node in ast.walk(_tree(ADAPTER))
        if isinstance(node, ast.AsyncFunctionDef) and "_dispatch_single" in _called(node)
    ]
    assert others == ["send_album"]


# ------------------------------------- 4, 5 & 6. no remote effect from an ingress


def test_the_max_edit_handler_calls_no_transport() -> None:
    called = _called(_function("on_max_edit", _tree(ROUTER)))
    assert not called & {"edit_text", "edit_caption", "edit_message_text", "delete"}
    assert "_enqueue_max_mutation" in called


def test_the_max_delete_handler_calls_no_transport() -> None:
    called = _called(_function("on_max_delete", _tree(ROUTER)))
    assert not called & {"delete", "delete_message", "edit_text", "delete_own_messages"}
    assert "_enqueue_max_mutation" in called


def test_the_reaction_module_never_creates_a_message_itself() -> None:
    """A note is a message somebody receives; it belongs on the queue."""
    called = _called(_tree(SYNC))
    assert not called & {"send_message", "send_note", "send_text"}
    assert "carry_reaction_note" in called


def test_the_reaction_adapter_has_no_sender_left() -> None:
    from bridge.reactions.adapters import TelegramReactionAdapter

    assert not hasattr(TelegramReactionAdapter, "send_note")


# --------------------------------------------- 7. the two id spaces never mix


def test_an_owner_mutation_never_reaches_the_bot_and_the_reverse() -> None:
    """A bot may only edit and delete its own messages, and the id the mapping
    holds for an owner-authored placement is the bot's view of somebody else's."""
    tree = _tree(MUTATION)
    for name in ("resolve_max_delete", "resolve_max_edit"):
        function = _function(name, tree)
        owner_branches = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.If) and "Authorship.OWNER" in ast.unparse(node.test)
        ]
        assert owner_branches, f"{name} no longer decides by authorship"
        for branch in owner_branches:
            body = "\n".join(ast.unparse(node) for node in branch.body)
            assert "bot." not in body, f"{name} reaches the bot for an owner-authored message"


def test_the_owner_transport_is_required_rather_than_assumed() -> None:
    tree = _tree(MUTATION)
    for name in ("resolve_max_delete", "resolve_max_edit"):
        assert "_require_session" in _called(_function(name, tree))


# ------------------------------------------- 8 & 9. one atomic album settlement


def test_the_album_settlement_is_one_transaction_aware_method() -> None:
    import inspect

    from bridge.storage.repositories import AlbumSettlementRepository

    for method in (
        AlbumSettlementRepository.settle_bot_album,
        AlbumSettlementRepository.settle_owner_album,
    ):
        source = inspect.getsource(method)
        assert source.count("self._db.transaction()") == 1, method.__name__


def test_no_receipt_is_ever_sorted_or_guessed() -> None:
    """Repairing the order would bind the third photo to the second, silently."""
    import inspect

    from bridge.media.delivery import check_album_receipt
    from bridge.storage.repositories import AlbumSettlementRepository

    for source in (
        inspect.getsource(check_album_receipt),
        inspect.getsource(AlbumSettlementRepository._check_receipt),
    ):
        assert "sorted(message_ids" not in source
        assert ".sort(" not in source
    # The one `sorted` in the checker is over positions, to prove coverage.
    positions = inspect.getsource(AlbumSettlementRepository._check_receipt)
    assert "sorted(indexes)" in positions


# ------------------------------------ 10. the snapshot advances after the effect


def test_the_reaction_snapshot_is_written_after_the_effect() -> None:
    """It used to be the first statement, which disabled the poll's whole retry."""
    tree = _tree(SYNC)
    for name in ("on_max_reaction", "_reconcile"):
        function = _function(name, tree)
        put_at = [
            node.lineno
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "put"
        ]
        effect_at = [
            node.lineno
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"set_reaction", "_draw"}
        ]
        assert put_at and effect_at, name
        assert min(put_at) > max(effect_at), (
            f"{name} records the reaction before it has been applied"
        )


def test_drawing_reports_whether_it_worked() -> None:
    from bridge.reactions.sync import ReactionSync

    annotation = ReactionSync._draw.__annotations__["return"]
    assert annotation is bool or annotation == "bool"


# ------------------------------------------- 11. a rate limit costs no attempt


def test_both_senders_carry_the_forced_delay_contract() -> None:
    for path, name in ((PIPE, "attempt"), (WORKER, "_handle_failure")):
        source = ast.unparse(_function(name, _tree(path)))
        assert "costs_attempt" in source, f"{path.name}:{name} prices every failure alike"
        assert "delay_ms" in source


def test_a_rate_limit_is_free_and_waited_out() -> None:
    from aiogram.exceptions import TelegramRetryAfter

    from bridge.routing.delivery import KIND_MAX_TO_TG_TEXT
    from bridge.routing.settlement import settle

    verdict = settle(
        KIND_MAX_TO_TG_TEXT,
        TelegramRetryAfter(method=None, message="flood", retry_after=42),  # type: ignore[arg-type]
        remote_marked=True,
    )
    assert not verdict.costs_attempt
    assert verdict.delay_ms == 42_000


# -------------------------------------- 12. one grouped-upload abstraction


def test_the_owner_album_goes_through_one_named_upload() -> None:
    tree = _tree(OWNER_VOICE)
    assert "send_own_album" in _called(_function("_grouped", tree))
    # And the ungrouped branch exists for one measured reason, named in the code.
    assert "hachoir" in OWNER_VOICE.read_text(encoding="utf-8")


def test_both_owner_album_paths_check_the_receipt() -> None:
    tree = _tree(OWNER_VOICE)
    for name in ("_grouped", "_one_at_a_time"):
        assert "check_album_receipt" in _called(_function(name, tree))


# ------------------------------------------- 13. still one queue and one worker


def test_no_second_queue_worker_or_supervisor_appeared() -> None:
    workers = 0
    supervisors = 0
    for path in BRIDGE.rglob("*.py"):
        tree = _tree(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name.endswith("Worker"):
                workers += 1
            if node.name.endswith("Supervisor"):
                supervisors += 1
    assert workers == 1, "a second worker class appeared"
    assert supervisors <= 1, "a second supervisor class appeared"


def test_every_max_to_tg_kind_is_served_by_the_one_sender() -> None:
    """One `send_job`, one queue. A new kind must join it rather than grow a path."""
    from bridge.service import runtime

    source = Path(runtime.__file__).read_text(encoding="utf-8")
    sender = _function("send_job", ast.parse(source))
    branches = ast.unparse(sender)
    for kind in (
        "KIND_MAX_TO_TG_TEXT",
        "KIND_MAX_TO_TG_MEDIA",
        "KIND_MAX_TO_TG_OWNER",
        "KIND_MAX_TO_TG_EDIT",
        "KIND_MAX_TO_TG_DELETE",
    ):
        assert kind in branches, f"{kind} is not served by the shared sender"
