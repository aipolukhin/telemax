"""AU-5A's rules, read out of the source rather than remembered.

Every assertion here stands for a defect that was measured. They are structural
on purpose: a comment saying "the lock must live on the service" is a comment,
and the lock that did not was accompanied by one for months.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "bridge"


def source(relative: str) -> str:
    return (BRIDGE / relative).read_text(encoding="utf-8")


def tree(relative: str) -> ast.Module:
    return ast.parse(source(relative))


def function(module: ast.Module, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in ast.walk(module):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no function named {name}")


def calls_in(node: ast.AST) -> list[str]:
    names: list[str] = []
    for item in ast.walk(node):
        if isinstance(item, ast.Call):
            target = item.func
            if isinstance(target, ast.Attribute):
                names.append(target.attr)
            elif isinstance(target, ast.Name):
                names.append(target.id)
    return names


# ------------------------------------------------------------ 1-3 coordinator


def test_no_lock_is_created_inside_the_batch() -> None:
    """The defect: a per-tap object holding the lock that was meant to exclude taps."""
    assert "asyncio.Lock()" not in source("provisioning/batch.py")
    assert "import asyncio" not in source("provisioning/batch.py")


def test_every_walk_goes_through_the_coordinator() -> None:
    module = tree("provisioning/batch.py")
    assert "claim" in calls_in(function(module, "run"))


def test_the_flow_holds_one_coordinator_for_its_lifetime() -> None:
    module = tree("provisioning/flow.py")
    assert "coordinator" in [
        argument.arg for argument in function(module, "__init__").args.kwonlyargs
    ]
    assert "coordinator=self._coordinator" in source("provisioning/flow.py")


def test_the_service_owns_the_coordinator_and_the_secret_store() -> None:
    text = source("service/runtime.py")
    assert "self._coordinator = ProvisioningCoordinator()" in text
    assert "self._secrets = ContactBotSecretStore(" in text


# ----------------------------------------------------------------- 4-5 stores


def test_only_the_secret_store_writes_the_token_file() -> None:
    """One writer, one lock, one atomic replacement."""
    writers = {
        path.relative_to(BRIDGE).as_posix()
        for path in BRIDGE.rglob("*.py")
        if "os.replace" in path.read_text(encoding="utf-8")
    }
    assert writers == {
        "config/writer.py",
        "max_client/state_files.py",
        "provisioning/secrets.py",
    }, sorted(writers)

    store = source("provisioning/secrets.py")
    assert "os.replace" in store
    assert "tempfile.mkstemp" in store
    assert "fsync" in store


def test_the_secret_store_exports_only_after_the_disk_commit() -> None:
    module = tree("provisioning/secrets.py")
    body = calls_in(function(module, "_mutate"))
    assert body.index("_replace") < len(body)
    assert "os.environ" in source("provisioning/secrets.py")
    text = source("provisioning/secrets.py")
    assert text.index("self._replace(values)") < text.index("os.environ[name] = value")


def test_the_journal_never_replaces_its_whole_set() -> None:
    """`self._entries = fresh` is what threw away a bot that existed."""
    module = tree("provisioning/journal.py")
    begin = function(module, "begin")
    assigned = [
        ast.unparse(target)
        for node in ast.walk(begin)
        if isinstance(node, ast.Assign)
        for target in node.targets
    ]
    assert "self._entries" in assigned, "it re-reads the file"
    assert not any(
        isinstance(node, ast.Assign)
        and any(ast.unparse(t) == "self._entries" for t in node.targets)
        and ast.unparse(node.value) == "fresh"
        for node in ast.walk(begin)
    ), "the whole set is never replaced by the selection"
    assert "_merged" in calls_in(function(module, "begin"))


def test_journal_mutations_are_read_modify_write_under_a_lock() -> None:
    module = tree("provisioning/journal.py")
    for name in ("begin", "note", "forget", "prune_finished"):
        body = calls_in(function(module, name))
        assert "_read" in body, name


# ------------------------------------------------------------- 6-7 confirming


def test_the_confirmation_is_registered_before_the_link_is_drawn() -> None:
    text = source("provisioning/batch.py")
    prepare = text.index("await self._prepare(username)")
    report = text.index("await self._report()", prepare)
    assert prepare < report


def test_a_creation_timeout_asks_telegram_before_it_answers() -> None:
    module = tree("provisioning/managed.py")
    body = calls_in(function(module, "_after_timeout"))
    assert "bot_id_for" in body
    assert "check_username" in body


# ------------------------------------------------------------ 8-9 reconciling


def test_startup_reads_the_unfinished_journal() -> None:
    module = tree("service/runtime.py")
    assert "_reconcile_provisioning" in calls_in(function(module, "start"))
    assert "ProvisioningReconciler" in source("provisioning/reconcile.py")


def test_reconciliation_creates_nothing_that_is_not_already_ours() -> None:
    """A restart spending one of twenty slots is the thing this must never do."""
    text = source("provisioning/reconcile.py")
    assert "create_bot" not in text
    assert "if state is not UsernameState.OWNED:" in text


# --------------------------------------------------------- 10-11 activation


def test_the_row_is_written_before_the_transport_starts() -> None:
    text = source("service/runtime.py")
    start = text.index("async def start_worker(")
    end = text.index("async def is_healthy(", start)
    body = text[start:end]
    assert body.index("BridgeState.PROVISIONING") < body.index("self._registry.add(")


def test_active_is_only_set_after_the_health_check() -> None:
    module = tree("provisioning/batch.py")
    body = calls_in(function(module, "_verify"))
    assert body.index("is_healthy") < body.index("mark_active")


def test_the_legacy_paste_path_writes_its_row_first_too() -> None:
    text = source("provisioning/service.py")
    start = text.index("async def activate(")
    body = text[start : text.index("async def _unique_name(", start)]
    assert body.index("BridgeState.PROVISIONING") < body.index("self._activator.add(")


# --------------------------------------------------------------- 12 disabled


def test_ownership_is_read_from_every_row_not_only_the_live_ones() -> None:
    assert "RepositoryKnownBots(bridges.all)" in source("service/runtime.py")
    assert "self._rows()" in source("provisioning/owned.py")


# ------------------------------------------------------------- 13-15 identity


def test_the_registry_checks_the_bot_it_is_given() -> None:
    module = tree("telegram/registry.py")
    assert "_mismatch" in calls_in(function(module, "add"))


def test_the_username_comparison_folds_case() -> None:
    text = source("telegram/registry.py")
    start = text.index("def _mismatch(")
    assert ".lower()" in text[start:]


def test_the_guardian_token_is_refused_as_a_contact_bot() -> None:
    assert "guardian_bot_id" in source("telegram/registry.py")
    assert "guardian_bot_id=self._guardian.bot_id" in source("service/runtime.py")


# --------------------------------------------------------------- 16-17 shape


def test_no_guardian_handler_reaches_a_remote_effect_directly() -> None:
    """Handlers draw and delegate. Everything decided lives in the flow."""
    text = source("provisioning/guardian.py")
    for forbidden in ("create_bot(", "getManagedBotToken", "save_token(", "registry.add"):
        assert forbidden not in text, forbidden


def test_provisioning_failure_never_stops_the_delivery_plane() -> None:
    module = tree("service/runtime.py")
    text = source("service/runtime.py")
    start = text.index("async def _reconcile_provisioning(")
    body = text[start : text.index("async def _provisioning_stuck(", start)]
    assert "except Exception:" in body, "reconciliation may not take start-up with it"
    assert "_reconcile_provisioning" in calls_in(function(module, "start"))


# --------------------------------------------------------------- 18-19 naming


def test_an_empty_naming_secret_is_never_regenerated() -> None:
    text = source("provisioning/naming.py")
    assert "NamingSecretUnusableError" in text
    assert "generating a new one" not in text


def test_v2_naming_takes_no_secret_and_no_machine() -> None:
    module = tree("provisioning/naming_v2.py")
    imported = {
        node.module
        for node in ast.walk(module)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(module)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "hmac" not in imported and "secrets" not in imported
    assert "socket" not in imported and "uuid" not in imported


def test_a_group_max_chat_is_not_a_provisioning_candidate() -> None:
    module = tree("provisioning/service.py")
    assert "is_personal_chat" in calls_in(function(module, "on_unbridged_message"))


def test_the_dead_second_naming_system_is_gone() -> None:
    assert not (BRIDGE / "provisioning" / "identity.py").exists()
    assert "contact_slug" not in source("provisioning/__init__.py")


# ------------------------------------------------------------------- 20 AU-4


def test_the_delivery_architecture_is_unchanged() -> None:
    """One queue, one worker pool, one supervisor. AU-1…AU-4 are not touched."""
    text = source("service/runtime.py")
    assert text.count("WorkerPool()") == 1
    assert text.count("Supervisor()") == 1
    assert "OutboxWorker(" in text


@pytest.mark.parametrize(
    "path",
    ["provisioning/secrets.py", "provisioning/journal.py", "provisioning/reconcile.py"],
)
def test_no_new_module_logs_a_token(path: str) -> None:
    text = source(path)
    assert "token=" not in text.replace("token_env=", "")
    assert 'logger.info("%s", token' not in text
