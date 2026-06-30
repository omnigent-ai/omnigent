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

import pytest

from omnigent.server.routes import sessions as _sessions_mod
from omnigent.server.routes.sessions import (
    _publish_status,
    _session_status_with_child_rollup,
)

_SID = "conv_bg_test"


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
