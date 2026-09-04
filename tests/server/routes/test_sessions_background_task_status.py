"""Background-shell tally: cache stickiness and the sidebar-list rollup.

A claude-native turn can settle to ``idle`` while background shells keep
running. ``_publish_status`` keeps a sticky per-session tally so the
in-chat "N background tasks still running" indicator survives the trailing
PTY-activity ``idle`` (which carries no count) and a reload.

The tally must clear on an authoritative ``Stop``-hook ``0`` (the shell
finished) and on a failure — but NOT on the countless trailing ``idle`` edges
that carry no count, and NOT on a new ``running`` turn (background shells
outlive turn boundaries, so the composer pill stays lit alongside the working
shimmer until the next authoritative count). It must never make
``_session_status_with_child_rollup`` report ``running``: the turn is over
and the session takes a new message immediately.
"""

from __future__ import annotations

import types

import pytest

from omnigent.server.routes import sessions as _sessions_mod
from omnigent.server.routes.sessions import (
    _CLAUDE_NATIVE_WRAPPER_LABEL_KEY,
    _CODEX_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE,
    _background_task_delivery_status,
    _publish_status,
    _session_status_with_child_rollup,
)
from omnigent.server.schemas import BackgroundTaskInfo

_SID = "conv_bg_test"


def _tasks(*descriptions: str) -> list[BackgroundTaskInfo]:
    """Detail entries for the given shell descriptions."""
    return [BackgroundTaskInfo(status="running", description=d) for d in descriptions]


def _conv(kind: str, *, labels: dict[str, str] | None = None) -> object:
    """Minimal conversation stand-in for status-mapping helpers."""
    return types.SimpleNamespace(kind=kind, labels=labels or {})


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    """Isolate each case from leaked module-level cache state."""
    _sessions_mod._session_status_cache.pop(_SID, None)
    _sessions_mod._session_background_task_count_cache.pop(_SID, None)
    _sessions_mod._session_background_tasks_cache.pop(_SID, None)
    _sessions_mod._session_active_response_cache.pop(_SID, None)
    yield
    _sessions_mod._session_status_cache.pop(_SID, None)
    _sessions_mod._session_background_task_count_cache.pop(_SID, None)
    _sessions_mod._session_background_tasks_cache.pop(_SID, None)
    _sessions_mod._session_active_response_cache.pop(_SID, None)


def test_background_task_turn_end_closes_the_active_response() -> None:
    # The composer bug's mechanism: `waiting` KEEPS the in-flight response id,
    # and the snapshot projects `waiting` as `running` — so a reconnect reopened
    # the settled turn as "streaming" and queued every send behind "Steer".
    # Delivering the turn-end as `idle` closes the response, so the snapshot has
    # nothing to reopen. The tally still reports the shells.
    _publish_status(_SID, "running", response_id="resp_1")
    assert _sessions_mod._session_active_response_cache.get(_SID) == "resp_1"
    _publish_status(_SID, "idle", response_id="resp_1", background_task_count=2)
    assert _SID not in _sessions_mod._session_active_response_cache
    assert _sessions_mod._session_background_task_count_cache.get(_SID) == 2


def test_idle_with_positive_count_sets_sticky_tally_but_list_stays_idle() -> None:
    # Stop hook: idle turn-end but a shell is still running. The tally sticks
    # (it feeds the in-chat indicator) while the list row stays idle — the turn
    # is over, so the sidebar must not spin and the composer must not queue.
    _publish_status(_SID, "idle", background_task_count=2)
    assert _sessions_mod._session_background_task_count_cache.get(_SID) == 2
    assert _session_status_with_child_rollup(_SID, []) == "idle"


def test_trailing_idle_without_count_leaves_tally_sticky() -> None:
    # PTY-activity idle (no count) must NOT wipe the count the Stop hook set.
    _publish_status(_SID, "idle", background_task_count=2)
    _publish_status(_SID, "idle", background_task_count=None)
    assert _sessions_mod._session_background_task_count_cache.get(_SID) == 2


def test_authoritative_zero_clears_tally_and_list_drops_to_idle() -> None:
    # The shell finished: the next Stop hook reports an explicit 0, which must
    # clear the tally so the spinner goes out.
    _publish_status(_SID, "idle", background_task_count=2)
    _publish_status(_SID, "idle", background_task_count=0)
    assert _SID not in _sessions_mod._session_background_task_count_cache
    assert _session_status_with_child_rollup(_SID, []) == "idle"


def test_new_turn_running_keeps_tally() -> None:
    # A new ``running`` turn does NOT clear the tally: background shells outlive
    # turn boundaries, so the composer pill stays lit alongside the working
    # shimmer until the next authoritative count (the trailing ``running`` edge
    # carries none).
    _publish_status(_SID, "idle", background_task_count=2)
    _publish_status(_SID, "running")
    assert _sessions_mod._session_background_task_count_cache.get(_SID) == 2


def test_failure_clears_tally_and_wins_over_count() -> None:
    _publish_status(_SID, "idle", background_task_count=2)
    _publish_status(_SID, "failed")
    assert _SID not in _sessions_mod._session_background_task_count_cache
    # ``failed`` is authoritative for the list row, never masked by a tally.
    assert _session_status_with_child_rollup(_SID, []) == "failed"


# ── background-task detail rides with the tally ─────────────────────────────


def test_background_tasks_detail_stored_with_positive_count() -> None:
    # The per-shell detail is cached in lockstep with the count so a
    # reload/reconnect can restore the individual shells.
    tasks = _tasks("Wait for CI", "Build check")
    _publish_status(_SID, "idle", background_task_count=2, background_tasks=tasks)
    assert _sessions_mod._session_background_task_count_cache.get(_SID) == 2
    assert _sessions_mod._session_background_tasks_cache.get(_SID) == tasks


def test_background_tasks_detail_empty_when_count_only() -> None:
    # An older runner posts the count with no detail: the tally sticks and the
    # detail cache holds [] (pill shows the number, no per-shell detail).
    _publish_status(_SID, "idle", background_task_count=1, background_tasks=None)
    assert _sessions_mod._session_background_task_count_cache.get(_SID) == 1
    assert _sessions_mod._session_background_tasks_cache.get(_SID) == []


def test_background_tasks_detail_sticky_on_trailing_idle() -> None:
    # The trailing PTY-activity idle carries no count: it must not wipe the
    # detail the Stop hook just set (same rule as the count).
    tasks = _tasks("Wait for CI")
    _publish_status(_SID, "idle", background_task_count=1, background_tasks=tasks)
    _publish_status(_SID, "idle", background_task_count=None)
    assert _sessions_mod._session_background_tasks_cache.get(_SID) == tasks


def test_background_tasks_detail_cleared_on_authoritative_zero() -> None:
    _publish_status(_SID, "idle", background_task_count=1, background_tasks=_tasks("Wait for CI"))
    _publish_status(_SID, "idle", background_task_count=0)
    assert _SID not in _sessions_mod._session_background_tasks_cache


def test_background_tasks_detail_cleared_on_failure() -> None:
    _publish_status(_SID, "idle", background_task_count=1, background_tasks=_tasks("Wait for CI"))
    _publish_status(_SID, "failed")
    assert _SID not in _sessions_mod._session_background_tasks_cache


# ── background-task delivery status (ended-turn collapse) ───────────────────


def test_subagent_background_waiting_delivers_as_idle() -> None:
    # Regression: a claude-native sub-agent relabels its Stop turn-end to
    # `waiting` for background shells, but the parent's terminal-delivery gate
    # keys off idle/failed — `waiting` would hang the orchestrator. The turn
    # ended, so deliver `idle` (the tally still drives the child spinner).
    assert _background_task_delivery_status("waiting", 1, _conv("sub_agent")) == "idle"


def test_top_level_background_waiting_delivers_as_idle() -> None:
    # A top-level session's turn has ended too: `waiting` would spin the
    # sidebar, gate the composer into queue/"Steer", and let a reconnect reopen
    # the settled turn's streaming bubble. The tally carries the shells instead.
    assert _background_task_delivery_status("waiting", 1, _conv("session")) == "idle"


def test_top_level_waiting_without_background_count_unchanged() -> None:
    # A genuine async-work park (the runtime's own `waiting`, no tally) is a
    # real busy state and must survive the collapse.
    assert _background_task_delivery_status("waiting", None, _conv("session")) == "waiting"
    assert _background_task_delivery_status("waiting", 0, _conv("session")) == "waiting"


def test_subagent_waiting_without_background_count_unchanged() -> None:
    # A genuine async-park `waiting` (no background tally) is a real waiting
    # state, not a relabeled turn-end — it must not be collapsed.
    assert _background_task_delivery_status("waiting", None, _conv("sub_agent")) == "waiting"
    assert _background_task_delivery_status("waiting", 0, _conv("sub_agent")) == "waiting"


def test_codex_native_subagent_waiting_unchanged() -> None:
    # Codex-internal children are excluded from the native delivery branch, so
    # the collapse must not touch them either.
    codex_child = _conv(
        "sub_agent",
        labels={_CLAUDE_NATIVE_WRAPPER_LABEL_KEY: _CODEX_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE},
    )
    assert _background_task_delivery_status("waiting", 1, codex_child) == "waiting"


def test_non_waiting_status_passes_through() -> None:
    assert _background_task_delivery_status("idle", 1, _conv("sub_agent")) == "idle"
    assert _background_task_delivery_status("failed", 1, _conv("sub_agent")) == "failed"
