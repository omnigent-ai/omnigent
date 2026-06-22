"""Tests for the running-sub-agents bottom-toolbar segment.

Covers the pure helpers behind the toolbar feature: the compact elapsed
formatter, the width-budgeted segment formatter (singular/plural, oldest-first,
``+K`` overflow, the ``⚠`` blocked-on-input flag, and graceful degradation),
and the reducer that folds the runner's PARTIAL
``session.child_session.updated`` deltas into the active-children registry.
"""

from __future__ import annotations

from omnigent_ui_sdk.terminal._host import (
    _format_elapsed_short,
    _format_subagent_segment,
    _reduce_subagent_event,
)

# ── elapsed formatter ────────────────────────────────


def test_elapsed_seconds_minutes_hours() -> None:
    """Scales s → m → h, flooring fractional seconds."""
    assert _format_elapsed_short(0) == "0s"
    assert _format_elapsed_short(8.9) == "8s"
    assert _format_elapsed_short(65) == "1m"
    assert _format_elapsed_short(3661) == "1h"


def test_elapsed_never_negative() -> None:
    """A negative delta (clock jitter) clamps to ``0s``, never ``-5s``."""
    assert _format_elapsed_short(-5) == "0s"


# ── segment formatter ────────────────────────────────


def _active(
    *entries: tuple[str, str, float, bool],
) -> dict[str, tuple[str, float, bool]]:
    """Build an active-children dict from ``(id, label, started, awaiting)``."""
    return {cid: (label, started, awaiting) for cid, label, started, awaiting in entries}


def test_segment_empty_is_blank() -> None:
    """No active children → no segment."""
    assert _format_subagent_segment({}, budget=80, now=100.0) == ""


def test_segment_singular() -> None:
    """One child uses the singular noun and shows its elapsed time."""
    seg = _format_subagent_segment(
        _active(("c1", "researcher", 90.0, False)), budget=80, now=100.0
    )
    assert seg == " ⇡1 sub-agent · researcher 10s "


def test_segment_two_children_oldest_first() -> None:
    """Two children are listed oldest-first (longest-running leads)."""
    active = _active(("c1", "coder", 92.0, False), ("c2", "researcher", 88.0, False))
    seg = _format_subagent_segment(active, budget=80, now=100.0)
    assert seg == " ⇡2 sub-agents · researcher 12s · coder 8s "


def test_segment_collapses_extra_with_plus_k() -> None:
    """Beyond two names, the rest collapse into ``+K``."""
    active = _active(
        ("c1", "a", 90.0, False),
        ("c2", "b", 91.0, False),
        ("c3", "c", 92.0, False),
        ("c4", "d", 93.0, False),
    )
    seg = _format_subagent_segment(active, budget=80, now=100.0)
    assert seg == " ⇡4 sub-agents · a 10s · b 9s +2 "


def test_segment_warn_prefix_when_awaiting_input() -> None:
    """A child blocked on input (pending elicitation) flags the segment with ⚠."""
    seg = _format_subagent_segment(_active(("c1", "researcher", 90.0, True)), budget=80, now=100.0)
    assert seg == " ⚠ ⇡1 sub-agent · researcher 10s "


def test_segment_degrades_to_compact_when_tight() -> None:
    """When the full form won't fit, it drops the per-child names."""
    active = _active(("c1", "researcher", 90.0, False), ("c2", "coder", 88.0, False))
    compact = " ⇡2 sub-agents "
    seg = _format_subagent_segment(active, budget=len(compact), now=100.0)
    assert seg == compact


def test_segment_disappears_when_no_room() -> None:
    """With essentially no room, the segment vanishes rather than wrap."""
    assert (
        _format_subagent_segment(_active(("c1", "researcher", 90.0, False)), budget=3, now=100.0)
        == ""
    )


# ── reducer ──────────────────────────────────────────


def test_reduce_adds_running_child() -> None:
    """A first ``busy`` delta adds the child with its label and start time."""
    state: dict[str, dict[str, object]] = {}
    active: dict[str, tuple[str, float, bool]] = {}
    _reduce_subagent_event(
        state,
        active,
        child_id="c1",
        child={"busy": True, "current_task_status": "in_progress", "tool": "researcher"},
        now=100.0,
    )
    assert active == {"c1": ("researcher", 100.0, False)}


def test_reduce_merges_partial_and_preserves_start() -> None:
    """A later partial delta (only the elicitation flag) merges without
    losing the label or resetting the start time."""
    state: dict[str, dict[str, object]] = {}
    active: dict[str, tuple[str, float, bool]] = {}
    _reduce_subagent_event(
        state,
        active,
        child_id="c1",
        child={"busy": True, "current_task_status": "in_progress", "tool": "researcher"},
        now=100.0,
    )
    _reduce_subagent_event(
        state, active, child_id="c1", child={"pending_elicitations_count": 1}, now=130.0
    )
    label, started, awaiting = active["c1"]
    assert label == "researcher"  # preserved across the partial delta
    assert started == 100.0  # first-seen start preserved (not bumped to 130)
    assert awaiting is True


def test_reduce_drops_on_terminal() -> None:
    """A terminal delta removes the child and cleans the merge cache."""
    state: dict[str, dict[str, object]] = {}
    active: dict[str, tuple[str, float, bool]] = {}
    _reduce_subagent_event(
        state,
        active,
        child_id="c1",
        child={"busy": True, "current_task_status": "in_progress", "tool": "x"},
        now=100.0,
    )
    _reduce_subagent_event(
        state,
        active,
        child_id="c1",
        child={"busy": False, "current_task_status": "completed"},
        now=140.0,
    )
    assert active == {}
    assert state == {}


def test_reduce_active_when_launching_without_busy() -> None:
    """A ``launching`` task status counts as active even before ``busy``."""
    state: dict[str, dict[str, object]] = {}
    active: dict[str, tuple[str, float, bool]] = {}
    _reduce_subagent_event(
        state,
        active,
        child_id="c1",
        child={"current_task_status": "launching", "session_name": "explorer"},
        now=100.0,
    )
    assert "c1" in active
    assert active["c1"][0] == "explorer"


def test_reduce_label_fallback() -> None:
    """With no tool/session_name/title, the label falls back to 'sub-agent'."""
    state: dict[str, dict[str, object]] = {}
    active: dict[str, tuple[str, float, bool]] = {}
    _reduce_subagent_event(state, active, child_id="c1", child={"busy": True}, now=100.0)
    assert active["c1"][0] == "sub-agent"
