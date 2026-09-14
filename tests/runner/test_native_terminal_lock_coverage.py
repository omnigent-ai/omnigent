"""Regression guards: every built-in native harness must be wired into the
runner's terminal dispatch, and devin into the interrupt/stop maps.

A native harness absent from the terminal lock dispatch raised ``KeyError`` mid
terminal-ensure (surfaced as "malformed runner response (HTTP 500)") — the class
of bug that broke devin-native's "start a chat". Absence from the interrupt/stop
maps instead made the web Stop button a silent no-op. These pin both so a future
built-in native harness that is not fully wired fails a test rather than a user.

The lock-coverage guard is scoped to the BUILT-IN native providers, not the
merged registry: a community-contributed native harness wires its own launcher
and must not be forced into the built-in dispatch.
"""

from __future__ import annotations

import pytest

from omnigent.harness_plugins import _BUILTIN_NATIVE_PROVIDERS
from omnigent.runner.app import _require_full_native_lock_coverage
from omnigent.runner.native.interrupt import _UNIFORM_INTERRUPT, _UNIFORM_STOP

# Built-in native harnesses are dispatched by ``agent.key`` (e.g. "devin").
_BUILTIN_NATIVE_KEYS = frozenset(provider.key for provider in _BUILTIN_NATIVE_PROVIDERS)


def _full_dispatch() -> dict[str, dict]:
    return {key: {} for key in _BUILTIN_NATIVE_KEYS}


def test_devin_is_a_builtin_native_harness() -> None:
    # The guards below are only meaningful if devin is a built-in native harness.
    assert "devin" in _BUILTIN_NATIVE_KEYS


def test_full_lock_dispatch_passes() -> None:
    dispatch = _full_dispatch()
    assert _require_full_native_lock_coverage(dispatch) is dispatch


@pytest.mark.parametrize("missing", sorted(_BUILTIN_NATIVE_KEYS))
def test_missing_harness_from_lock_dispatch_raises(missing: str) -> None:
    dispatch = _full_dispatch()
    del dispatch[missing]
    with pytest.raises(RuntimeError, match=missing):
        _require_full_native_lock_coverage(dispatch)


def test_extra_community_key_does_not_trip_the_guard() -> None:
    # A community-contributed native agent (present in the merged registry but
    # not a built-in provider) must not force the built-in dispatch to cover it.
    dispatch = _full_dispatch()
    dispatch["some-community-harness"] = {}
    assert _require_full_native_lock_coverage(dispatch) is dispatch


def test_devin_drives_web_status_from_the_pty_watcher() -> None:
    # devin-native's run_turn injects and returns, so the runner's PTY-activity
    # watcher is its only top-level running/idle status source. Absent from this
    # set, the web "Working…" spinner never clears and the turn times out (the
    # class of bug that left devin turns stuck on "Starting up…").
    from omnigent.runner.resource_registry import (
        _STATUS_EMITTING_TERMINAL_ROLES,
        DEVIN_NATIVE_TERMINAL_ROLE,
    )

    assert DEVIN_NATIVE_TERMINAL_ROLE in _STATUS_EMITTING_TERMINAL_ROLES


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
