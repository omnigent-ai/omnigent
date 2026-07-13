"""Background-shell tally: cache stickiness and the sidebar-list rollup.

A claude-native turn can settle to ``idle`` while background shells keep
running. ``_publish_status`` keeps a sticky per-session tally so the
sidebar spinner and the in-chat indicator survive the trailing
PTY-activity ``idle`` (which carries no count), and
``_session_status_with_child_rollup`` reads that tally so a settled-idle
session with live shells still reads as ``running`` in the session list.

The tally must clear on an authoritative ``Stop``-hook ``0`` (the shell
finished), on a new turn (``running``), and on a failure — but NOT on the
countless trailing ``idle`` edges that carry no count.
"""

from __future__ import annotations

import types

import pytest

from omnigent.server.routes import sessions as _sessions_mod
from omnigent.server.routes.sessions import (
    _CLAUDE_NATIVE_WRAPPER_LABEL_KEY,
    _CODEX_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE,
    _publish_status,
    _session_status_with_child_rollup,
    _subagent_delivery_status,
)

_SID = "conv_bg_test"


def _conv(kind: str, *, labels: dict[str, str] | None = None) -> object:
    """Minimal conversation stand-in for status-mapping helpers."""
    return types.SimpleNamespace(kind=kind, labels=labels or {})


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    """Isolate each case from leaked module-level cache state."""
    _sessions_mod._session_status_cache.pop(_SID, None)
    _sessions_mod._session_background_task_count_cache.pop(_SID, None)
    yield
    _sessions_mod._session_status_cache.pop(_SID, None)
    _sessions_mod._session_background_task_count_cache.pop(_SID, None)


def test_idle_with_positive_count_sets_sticky_tally_and_list_reads_running() -> None:
    # Stop hook: idle turn-end but a shell is still running. The list row must
    # read "running" so the sidebar spinner stays lit.
    _publish_status(_SID, "idle", background_task_count=2)
    assert _sessions_mod._session_background_task_count_cache.get(_SID) == 2
    assert _session_status_with_child_rollup(_SID, []) == "running"


def test_trailing_idle_without_count_leaves_tally_sticky() -> None:
    # PTY-activity idle (no count) must NOT wipe the count the Stop hook set.
    _publish_status(_SID, "idle", background_task_count=2)
    _publish_status(_SID, "idle", background_task_count=None)
    assert _sessions_mod._session_background_task_count_cache.get(_SID) == 2
    assert _session_status_with_child_rollup(_SID, []) == "running"


def test_authoritative_zero_clears_tally_and_list_drops_to_idle() -> None:
    # The shell finished: the next Stop hook reports an explicit 0, which must
    # clear the tally so the spinner goes out.
    _publish_status(_SID, "idle", background_task_count=2)
    _publish_status(_SID, "idle", background_task_count=0)
    assert _SID not in _sessions_mod._session_background_task_count_cache
    assert _session_status_with_child_rollup(_SID, []) == "idle"


def test_new_turn_running_clears_tally() -> None:
    _publish_status(_SID, "idle", background_task_count=2)
    _publish_status(_SID, "running")
    assert _SID not in _sessions_mod._session_background_task_count_cache


def test_failure_clears_tally_and_wins_over_count() -> None:
    _publish_status(_SID, "idle", background_task_count=2)
    _publish_status(_SID, "failed")
    assert _SID not in _sessions_mod._session_background_task_count_cache
    # ``failed`` is authoritative for the list row, never masked by a tally.
    assert _session_status_with_child_rollup(_SID, []) == "failed"


# ── sub-agent delivery status (parent-hang guard) ───────────────────────────


def test_subagent_background_waiting_delivers_as_idle() -> None:
    # Regression: a claude-native sub-agent relabels its Stop turn-end to
    # `waiting` for background shells, but the parent's terminal-delivery gate
    # keys off idle/failed — `waiting` would hang the orchestrator. The turn
    # ended, so deliver `idle` (the tally still drives the child spinner).
    assert _subagent_delivery_status("waiting", 1, _conv("sub_agent")) == "idle"


def test_top_level_background_waiting_keeps_waiting() -> None:
    # Only sub-agents need the collapse; a top-level session keeps `waiting`
    # so the web UI shows its shimmer.
    assert _subagent_delivery_status("waiting", 1, _conv("session")) == "waiting"


def test_subagent_waiting_without_background_count_unchanged() -> None:
    # A genuine async-park `waiting` (no background tally) is a real waiting
    # state, not a relabeled turn-end — it must not be collapsed.
    assert _subagent_delivery_status("waiting", None, _conv("sub_agent")) == "waiting"
    assert _subagent_delivery_status("waiting", 0, _conv("sub_agent")) == "waiting"


def test_codex_native_subagent_waiting_unchanged() -> None:
    # Codex-internal children are excluded from the native delivery branch, so
    # the collapse must not touch them either.
    codex_child = _conv(
        "sub_agent",
        labels={_CLAUDE_NATIVE_WRAPPER_LABEL_KEY: _CODEX_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE},
    )
    assert _subagent_delivery_status("waiting", 1, codex_child) == "waiting"


def test_non_waiting_status_passes_through() -> None:
    assert _subagent_delivery_status("idle", 1, _conv("sub_agent")) == "idle"
    assert _subagent_delivery_status("failed", 1, _conv("sub_agent")) == "failed"
