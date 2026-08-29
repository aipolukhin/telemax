"""AU-2 F12 — the stands have to accept exactly what production accepts.

A fake that takes a parameter the real function does not is a fake that keeps
passing after the real one changes. `_Recorder.fake_post` had a keyword
`content_type` left over from a change that did not survive, and collected the
values into a list nothing ever asserted on — so for several commits the rig was
describing a `_post_media` that had not existed since.

Cheap to check, and it catches the class of mistake rather than the one instance.
"""

from __future__ import annotations

import inspect
import tempfile
from pathlib import Path
from typing import Any

import test_native_media
from test_native_media_faults import Rig

from bridge.max_client.client import MaxClient


def parameters_of(function: Any) -> list[str]:
    return [name for name in inspect.signature(function).parameters if name != "self"]


def test_the_recorder_post_matches_production() -> None:
    with tempfile.TemporaryDirectory() as raw:
        rig = test_native_media._Recorder(Path(raw), slot=test_native_media._SLOT)
        assert parameters_of(rig.client._post_media) == parameters_of(MaxClient._post_media)


def test_the_fault_rig_post_matches_production() -> None:
    with tempfile.TemporaryDirectory() as raw:
        rig = Rig(Path(raw))
        assert parameters_of(rig.client._post_media) == parameters_of(MaxClient._post_media)


def test_the_fault_rig_invoke_takes_what_production_takes() -> None:
    """`_invoke` has a keyword-only `timeout`; a stand that rejected it would hide
    a caller that started passing one."""
    with tempfile.TemporaryDirectory() as raw:
        rig = Rig(Path(raw))
        names = inspect.signature(rig.client._invoke).parameters

    assert list(names)[:2] == ["opcode", "payload"]
    assert any(kind.kind is inspect.Parameter.VAR_KEYWORD for kind in names.values())


def test_no_stand_collects_a_field_production_never_sets() -> None:
    """The tell that started this: a list on the rig that nothing reads."""
    with tempfile.TemporaryDirectory() as raw:
        rig = test_native_media._Recorder(Path(raw), slot=test_native_media._SLOT)

    assert not hasattr(rig, "content_types")
