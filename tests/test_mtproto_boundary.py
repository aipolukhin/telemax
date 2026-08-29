"""One rule, enforced by reading the source: MTProto never touches PyMax.

The whole point of routing owner-side Telegram events through the existing
durable pipeline is that no transport reaches MAX directly — a send that skips
the queue skips the lease, the retry, the FAILED/AMBIGUOUS accounting and the
source_key dedup with it. The MTProto user session is a *second* Telegram intake,
so it is exactly the place a well-meaning shortcut would appear:

    MTProto update → pymax.send/edit/delete

This reads the module's imports and forbids that shortcut before it can be
written. It is a Stage-1 guard for Stage-2 code: today `user_session.py` has no
update handlers at all, so the check is trivially satisfied — which is the point,
because the handlers that will call the *existing* `BridgeRouter` methods must
never import the MAX client to call it themselves.
"""

from __future__ import annotations

import ast
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent.parent / "bridge"

#: Modules that carry MTProto intake. As Stage 2 adds handlers, new files join
#: this list — and inherit the same boundary.
MTPROTO_MODULES = (
    "telegram/user_session.py",
    "telegram/mtproto_intake.py",
    "telegram/mtproto_media.py",
)

#: Import prefixes that mean "the MAX side directly". An MTProto module reaching
#: any of these is the bypass this test exists to stop.
FORBIDDEN_IMPORT_PREFIXES = ("pymax", "bridge.max_client")


def _imported_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_mtproto_modules_do_not_import_the_max_client() -> None:
    offenders: list[tuple[str, str]] = []
    for relative in MTPROTO_MODULES:
        source = (BRIDGE / relative).read_text(encoding="utf-8")
        for module in _imported_modules(source):
            if module.startswith(FORBIDDEN_IMPORT_PREFIXES):
                offenders.append((relative, module))
    assert not offenders, (
        "an MTProto intake module reached the MAX side directly, bypassing the "
        f"durable pipeline: {offenders}"
    )
