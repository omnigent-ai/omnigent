"""Regression guards: every native harness must be wired into the runner's
terminal dispatch and the interrupt/stop maps.

A native harness absent from the terminal lock dispatch raised ``KeyError`` mid
terminal-ensure (surfaced as "malformed runner response (HTTP 500)") — the class
of bug that broke devin-native's "start a chat". Absence from the interrupt/stop
maps instead made the web Stop button a silent no-op. These pin both so a
future native harness that is not fully wired fails a test rather than a user.
"""

from __future__ import annotations

import pytest

from omnigent.native.native_coding_agents import NATIVE_CODING_AGENTS
from omnigent.runner.app import _require_full_native_lock_coverage
from omnigent.runner.native.interrupt import _UNIFORM_INTERRUPT, _UNIFORM_STOP

# Native coding agents are dispatched by ``agent.key`` (e.g. "devin", "claude").
_NATIVE_KEYS = frozenset(agent.key for agent in NATIVE_CODING_AGENTS)


def _full_dispatch() -> dict[str, dict]:
    return {key: {} for key in _NATIVE_KEYS}


def test_devin_is_a_native_coding_agent() -> None:
    # The guards below are only meaningful if devin is registered as a native
    # coding agent in the first place.
    assert "devin" in _NATIVE_KEYS


def test_full_lock_dispatch_passes() -> None:
    dispatch = _full_dispatch()
    assert _require_full_native_lock_coverage(dispatch) is dispatch


@pytest.mark.parametrize("missing", sorted(_NATIVE_KEYS))
def test_missing_harness_from_lock_dispatch_raises(missing: str) -> None:
    dispatch = _full_dispatch()
    del dispatch[missing]
    with pytest.raises(RuntimeError, match=missing):
        _require_full_native_lock_coverage(dispatch)


def test_devin_is_wired_into_interrupt_and_stop() -> None:
    # devin's Stop/interrupt route through the uniform bridge-inject maps; absent
    # entries make the web Stop button a silent no-op (`_UNIFORM_*.get` -> None).
    # Not every native harness uses these maps (claude/codex special-case
    # interrupt; claude/codex/pi have no uniform stop; antigravity/opencode are
    # handled elsewhere), so this asserts devin specifically rather than blanket
    # coverage.
    assert _UNIFORM_INTERRUPT["devin"].module == "omnigent.harnesses.devin_native.bridge"
    assert _UNIFORM_INTERRUPT["devin"].inject_fn == "inject_interrupt"
    assert _UNIFORM_STOP["devin"].module == "omnigent.harnesses.devin_native.bridge"
