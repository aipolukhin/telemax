"""A callback prefix that is a parent of another must be registered after it.

«Да, снести» did nothing. Not an error, not a log line — the screen quietly
redrew the bridge list. `onb:bridge:free:yes:…` starts with `onb:bridge:free:`,
the parent's handler was registered first, its parser did not recognise the
child's shape and returned None, and the "unknown bridge" branch ran.

Three pairs in this file share that shape and two of them happened to be
registered in the right order. That is not a property to leave to happenstance:
the failure is invisible, and a tap that does nothing looks exactly like a tap
nobody registered.
"""

from __future__ import annotations

from bridge.onboarding import screens


def _prefixes() -> list[str]:
    """Every callback namespace the onboarding screens define."""
    return sorted(
        value
        for name in screens.__all__
        if name.isupper() and isinstance(value := getattr(screens, name, None), str)
        and value.startswith("onb:")
    )


def test_the_namespaces_do_have_parents_and_children() -> None:
    """If this ever fails the test below has stopped testing anything."""
    pairs = [
        (parent, child)
        for parent in _prefixes()
        for child in _prefixes()
        if child != parent and child.startswith(f"{parent}:")
    ]
    assert pairs, "no parent/child prefixes left — has the naming changed?"


def test_a_child_prefix_is_always_registered_before_its_parent() -> None:
    """Read out of the source, because that is where the order lives.

    aiogram matches handlers in registration order. A parent registered first
    swallows every child, and swallows it *silently*.
    """
    import pathlib
    import re

    source = (
        pathlib.Path(__file__).resolve().parent.parent / "bridge/onboarding/router.py"
    ).read_text(encoding="utf-8")

    order: list[str] = re.findall(
        r'F\.data\.startswith\(f"\{screens\.([A-Z_]+)\}:"\)', source
    )
    position = {name: index for index, name in enumerate(order)}

    for parent_name, parent in ((name, getattr(screens, name)) for name in order):
        for child_name in order:
            child = getattr(screens, child_name)
            if child_name == parent_name or not child.startswith(f"{parent}:"):
                continue
            assert position[child_name] < position[parent_name], (
                f"{child_name} ({child}) is a child of {parent_name} ({parent}) and is"
                f" registered after it — every tap on it reaches the wrong handler"
            )


def test_a_prefix_handler_never_precedes_a_literal_child() -> None:
    """The other half of the hazard, and the one the file did not cover.

    `F.data == "onb:settings:own"` cannot swallow anything, but
    `F.data.startswith("onb:settings:")` registered above it swallows exactly
    that — and the failure looks identical: a tap that does nothing.
    """
    import pathlib
    import re

    source = (
        pathlib.Path(__file__).resolve().parent.parent / "bridge/onboarding/router.py"
    ).read_text(encoding="utf-8")

    order: list[tuple[str, str]] = re.findall(
        r'F\.data(?:\.startswith\(f"\{screens\.([A-Z_]+)\}:"\)|\s*==\s*screens\.([A-Z_]+))',
        source,
    )
    registered = [(prefix or literal, bool(prefix)) for prefix, literal in order]

    for index, (name, is_prefix) in enumerate(registered):
        if not is_prefix:
            continue
        parent = getattr(screens, name)
        for later, (child_name, _) in enumerate(registered):
            if later <= index:
                continue
            if getattr(screens, child_name).startswith(f"{parent}:"):
                raise AssertionError(
                    f"{child_name} is a child of {name} and is registered after it"
                )


def test_the_parent_handler_also_refuses_the_child_shape() -> None:
    """Belt and braces. Order is easy to break by moving a function."""
    free_yes = screens.bridge_free_yes_callback(1, 5)

    assert free_yes.startswith(f"{screens.BRIDGE_FREE}:"), "the collision is real"
    assert screens.parse_bridge_free(free_yes) is None
    assert screens.parse_bridge_free_yes(free_yes) == (1, 5)


def test_the_same_holds_for_the_other_two_pairs() -> None:
    off_yes = screens.bridge_off_yes_callback(1, 5)
    repull_yes = screens.bridge_repull_yes_callback(1, 5)

    assert screens.parse_bridge_off(off_yes) is None
    assert screens.parse_bridge_repull(repull_yes) is None
