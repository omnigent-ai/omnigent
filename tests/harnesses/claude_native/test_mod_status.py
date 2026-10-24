"""Tests for the mod turn-event status source and its hand-back to the other sources.

Covers :mod:`omnigent.harnesses.claude_native.mod_status`: the mod owning the
running and idle status once a turn event has been applied, the structured and
pane edges that it mutes, the heartbeat correcting a lost turn.complete or a lost
turn.start, the settle window that keeps a racy heartbeat from flipping the state,
the fallback when the heartbeat goes stale or names another server or lacks the
turn field, subagent filtering, the idle grace, and the pane-only hand-back.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native.mod_events import (
    MOD_EVENTS_FILE_NAME,
    MOD_HEARTBEAT_MAX_AGE_S,
    MOD_STATE_FILE_NAME,
    SERVER_STATE_FILE_NAME,
)
from omnigent.harnesses.claude_native.mod_status import (
    MOD_HEARTBEAT_SETTLE_S,
    MOD_TURN_IDLE_GRACE_S,
    ModTurnStatusPoller,
)
from tests.harnesses.claude_native.mod_fixtures import (
    append_event_record,
    append_event_text,
    turn_complete_record,
    turn_start_record,
    write_mod_heartbeat,
    write_raw_mod_heartbeat,
    write_server_state,
)

NOW = 1_785_480_000.0
SERVER_PID = 77
PANE_QUIET_S = 1.0


@dataclass
class _Clocks:
    """Wall clock (server time) and monotonic clock, advanced by each test."""

    wall: float = NOW
    monotonic: float = 1_000.0


@dataclass
class _Edges:
    """Every publish, recorded as ``(channel, status, blocked_on)``."""

    calls: list[tuple[str, str, str | None]] = field(default_factory=list)

    def structured(self, status: str, blocked_on: str | None = None) -> None:
        self.calls.append(("structured", status, blocked_on))

    def mod(self, status: str, blocked_on: str | None = None) -> None:
        self.calls.append(("mod", status, blocked_on))

    def pane(self, status: str, blocked_on: str | None = None) -> None:
        self.calls.append(("pane", status, blocked_on))


class _FakeStructuredSource:
    """Stands in for the OSC or status-file poller. ``emit`` drives its edges."""

    def __init__(self, on_status: Callable[[str, str | None], None]) -> None:
        self._on_status = on_status
        self.active = False
        self.blocked_on: str | None = None
        self.resyncs = 0
        self.retired = False

    def tick(self) -> None:
        return None

    def resync(self) -> None:
        self.resyncs += 1

    def retire(self) -> None:
        self.retired = True
        self.active = False

    def emit(self, status: str, blocked_on: str | None = None) -> None:
        """Simulate the structured source reporting a value."""
        self.blocked_on = blocked_on
        self._on_status(status, blocked_on)


@dataclass
class _Harness:
    bridge: Path
    clocks: _Clocks
    edges: _Edges
    poller: ModTurnStatusPoller
    source: _FakeStructuredSource


def _build(tmp_path: Path, *, mod_files: bool = True) -> _Harness:
    """Build a poller over a temp bridge dir, with a usable mod unless ``mod_files`` is off.

    The initial heartbeat is stamped at ``NOW`` and reports no running turn.
    """
    bridge = tmp_path / "bridge"
    if mod_files:
        write_server_state(bridge, pid=SERVER_PID)
        write_mod_heartbeat(bridge, server_pid=SERVER_PID, updated_at=NOW, main_turn_id=None)
    clocks = _Clocks()
    edges = _Edges()
    sources: list[_FakeStructuredSource] = []

    def build_structured(on_status: Callable[[str, str | None], None]) -> _FakeStructuredSource:
        source = _FakeStructuredSource(on_status)
        sources.append(source)
        return source

    poller = ModTurnStatusPoller(
        bridge_dir=bridge,
        build_structured=build_structured,
        on_structured_status=edges.structured,
        on_mod_status=edges.mod,
        on_pane_status=edges.pane,
        pane_quiet_s=PANE_QUIET_S,
        wall_clock=lambda: clocks.wall,
        monotonic=lambda: clocks.monotonic,
    )
    return _Harness(bridge=bridge, clocks=clocks, edges=edges, poller=poller, source=sources[0])


def _publish_heartbeat(h: _Harness, main_turn_id: str | None) -> None:
    """Write a heartbeat stamped with the current server time, as the server does."""
    write_mod_heartbeat(
        h.bridge,
        server_pid=SERVER_PID,
        updated_at=h.clocks.wall,
        main_turn_id=main_turn_id,
    )


def _settle(h: _Harness) -> None:
    """Advance past the idle grace and poll once, as a later tick would."""
    h.clocks.monotonic += MOD_TURN_IDLE_GRACE_S
    h.poller.tick()


def test_turn_events_own_running_and_idle_and_mute_the_structured_source(tmp_path: Path) -> None:
    """Once a turn event is applied, the mod decides and the structured edges are muted."""
    h = _build(tmp_path)
    append_event_record(h.bridge, turn_start_record("t1", at=NOW))
    h.poller.tick()
    assert h.edges.calls == [("mod", "running", None)]
    assert h.poller.active

    h.source.emit("idle")  # the status file lags the turn
    h.poller.tick()
    assert h.edges.calls == [("mod", "running", None)]

    append_event_record(h.bridge, turn_complete_record("t1", at=NOW + 5))
    h.poller.tick()
    assert h.edges.calls == [("mod", "running", None)]  # idle waits out the grace

    _settle(h)
    assert h.edges.calls == [("mod", "running", None), ("mod", "idle", None)]

    h.source.emit("running")  # a stale busy value after the turn ended
    h.poller.tick()
    assert h.edges.calls == [("mod", "running", None), ("mod", "idle", None)]


def test_structured_source_decides_until_the_mod_reports_a_turn(tmp_path: Path) -> None:
    """A usable mod with no applied turn event leaves the structured source in charge."""
    h = _build(tmp_path)
    h.poller.tick()
    assert not h.poller.active
    h.source.emit("running")
    h.source.emit("idle")
    assert h.edges.calls == [("structured", "running", None), ("structured", "idle", None)]


def test_a_heartbeat_alone_does_not_take_ownership(tmp_path: Path) -> None:
    """A heartbeat that reports a running turn, with no turn event applied, claims nothing."""
    h = _build(tmp_path)
    _publish_heartbeat(h, "t1")
    h.poller.tick()
    assert not h.poller.active
    h.source.emit("running")
    assert h.edges.calls == [("structured", "running", None)]


def test_without_mod_files_the_structured_source_is_untouched(tmp_path: Path) -> None:
    """With no mod at all, every structured edge publishes as it did before."""
    h = _build(tmp_path, mod_files=False)
    append_event_record(h.bridge, turn_start_record("t1", at=NOW))
    h.poller.tick()
    h.source.emit("running", "permission prompt")
    assert h.edges.calls == [("structured", "running", "permission prompt")]
    assert h.poller.blocked_on == "permission prompt"


@pytest.mark.parametrize(
    "breakage",
    [
        "heartbeat-stale",
        "server-pid-changed",
        "heartbeat-removed",
        "server-removed",
        "heartbeat-without-turn-field",
    ],
)
def test_unusable_mod_files_hand_the_status_back_to_the_structured_source(
    tmp_path: Path, breakage: str
) -> None:
    """Staleness, a pid mismatch, a removed file, or a heartbeat without the turn field."""
    h = _build(tmp_path)
    append_event_record(h.bridge, turn_start_record("t1", at=NOW))
    h.poller.tick()
    assert h.edges.calls == [("mod", "running", None)]
    # The pane was busy when the mod stopped, so the hand-back leaves it to its own edges.
    h.poller.note_pane_activity()

    if breakage == "heartbeat-stale":
        h.clocks.wall = NOW + MOD_HEARTBEAT_MAX_AGE_S + 1
    elif breakage == "server-pid-changed":
        write_server_state(h.bridge, pid=SERVER_PID + 1)
    elif breakage == "heartbeat-removed":
        (h.bridge / MOD_STATE_FILE_NAME).unlink()
    elif breakage == "server-removed":
        (h.bridge / SERVER_STATE_FILE_NAME).unlink()
    else:
        write_raw_mod_heartbeat(h.bridge, {"v": 1, "server_pid": SERVER_PID, "updated_at": NOW})

    h.poller.tick()
    assert h.source.resyncs == 1
    assert not h.poller.active
    h.source.emit("idle")
    assert h.edges.calls[-1] == ("structured", "idle", None)


def test_recovered_mod_reclaims_the_status_it_still_reports(tmp_path: Path) -> None:
    """A mod that comes back still reporting the turn takes the status again."""
    h = _build(tmp_path)
    append_event_record(h.bridge, turn_start_record("t1", at=NOW))
    h.poller.tick()
    h.poller.note_pane_activity()
    h.clocks.wall = NOW + MOD_HEARTBEAT_MAX_AGE_S + 1
    h.poller.tick()  # heartbeat stale: hand back

    _publish_heartbeat(h, "t1")
    h.poller.tick()  # usable again, and the heartbeat still names the running turn
    h.source.emit("idle")  # muted again, because the mod owns the status once more
    assert h.edges.calls == [("mod", "running", None), ("mod", "running", None)]
    assert h.source.resyncs == 1


def test_subagent_turns_do_not_move_the_main_loop_status(tmp_path: Path) -> None:
    """Records with a non-null agent id never change the session's status."""
    h = _build(tmp_path)
    append_event_record(h.bridge, turn_start_record("sub-1", at=NOW, agent_id="agent-7"))
    h.poller.tick()
    assert h.edges.calls == []

    append_event_record(h.bridge, turn_start_record("t1", at=NOW))
    append_event_record(h.bridge, turn_complete_record("sub-1", at=NOW + 1, agent_id="agent-7"))
    h.poller.tick()
    assert h.edges.calls == [("mod", "running", None)]

    append_event_record(h.bridge, turn_complete_record("t1", at=NOW + 2))
    h.poller.tick()
    _settle(h)
    assert h.edges.calls == [("mod", "running", None), ("mod", "idle", None)]


def test_partial_and_unknown_records_do_not_move_the_status_early(tmp_path: Path) -> None:
    """An unfinished line waits for its newline, and an unknown kind changes nothing."""
    h = _build(tmp_path)
    append_event_record(h.bridge, turn_start_record("t1", at=NOW))
    append_event_record(
        h.bridge,
        {"v": 1, "kind": "turn.paused", "at": NOW, "turn_id": "t1", "agent_id": None},
    )
    complete_line = json.dumps(turn_complete_record("t1", at=NOW + 1))
    append_event_text(h.bridge, complete_line[:15])
    h.poller.tick()
    assert h.edges.calls == [("mod", "running", None)]

    append_event_text(h.bridge, complete_line[15:] + "\n")
    h.poller.tick()
    _settle(h)
    assert h.edges.calls == [("mod", "running", None), ("mod", "idle", None)]


def test_recreated_events_file_is_applied_from_its_start(tmp_path: Path) -> None:
    """A removed and recreated events file is read from its beginning."""
    h = _build(tmp_path)
    append_event_record(h.bridge, turn_start_record("t1", at=NOW))
    h.poller.tick()
    (h.bridge / MOD_EVENTS_FILE_NAME).unlink()
    h.poller.tick()
    assert h.edges.calls == [("mod", "running", None)]

    append_event_record(h.bridge, turn_start_record("t2", at=NOW + 1))
    append_event_record(h.bridge, turn_complete_record("t2", at=NOW + 2))
    h.poller.tick()
    _settle(h)
    assert h.edges.calls == [("mod", "running", None), ("mod", "idle", None)]


def test_long_turn_stays_owned_and_running_while_its_heartbeat_names_it(tmp_path: Path) -> None:
    """A turn far past any elapsed-time bound stays with the mod while the heartbeat names it."""
    h = _build(tmp_path)
    h.source.active = True
    append_event_record(h.bridge, turn_start_record("t1", at=NOW))
    h.poller.tick()

    for elapsed_s in (60.0, 300.0, 900.0):
        h.clocks.wall = NOW + elapsed_s
        _publish_heartbeat(h, "t1")
        h.poller.tick()

    h.source.emit("idle")  # the structured source must stay muted the whole time
    h.poller.tick()
    assert h.edges.calls == [("mod", "running", None)]
    assert h.source.resyncs == 0


def test_lost_completion_is_healed_by_the_next_heartbeat(tmp_path: Path) -> None:
    """A turn.complete that never arrives is corrected by the next heartbeat that reports idle."""
    h = _build(tmp_path)
    append_event_record(h.bridge, turn_start_record("t1", at=NOW))
    h.poller.tick()
    assert h.edges.calls == [("mod", "running", None)]

    h.clocks.wall = NOW + 40
    _publish_heartbeat(h, None)  # the mod has finished the turn; its completion was lost
    h.poller.tick()
    assert h.edges.calls == [("mod", "running", None)]  # adopted idle waits out the grace

    _settle(h)
    assert h.edges.calls == [("mod", "running", None), ("mod", "idle", None)]


def test_lost_start_is_healed_by_the_next_heartbeat(tmp_path: Path) -> None:
    """A turn.start that never arrives is corrected by a heartbeat that reports a running turn."""
    h = _build(tmp_path)
    append_event_record(h.bridge, turn_start_record("t0", at=NOW - 20))
    append_event_record(h.bridge, turn_complete_record("t0", at=NOW - 10))
    h.poller.tick()
    _settle(h)
    assert h.edges.calls == [("mod", "idle", None)]

    _publish_heartbeat(h, "t2")  # the mod started t2 and its start event was lost
    h.poller.tick()
    assert h.edges.calls == [("mod", "idle", None), ("mod", "running", None)]


def test_heartbeat_inside_the_settle_window_is_ignored(tmp_path: Path) -> None:
    """A heartbeat that disagrees but is too close to the last event cannot flip the state."""
    h = _build(tmp_path)
    append_event_record(h.bridge, turn_start_record("t1", at=NOW))
    h.poller.tick()
    assert h.edges.calls == [("mod", "running", None)]

    h.clocks.wall = NOW + MOD_HEARTBEAT_SETTLE_S - 0.5
    _publish_heartbeat(h, None)  # computed before the start may have been seen; too soon
    h.poller.tick()
    _settle(h)  # the racy view must not take effect even after the grace has passed
    assert h.edges.calls == [("mod", "running", None)]

    h.clocks.wall = NOW + MOD_HEARTBEAT_SETTLE_S
    _publish_heartbeat(h, None)  # settled: the heartbeat's view is adopted now
    h.poller.tick()
    _settle(h)
    assert h.edges.calls == [("mod", "running", None), ("mod", "idle", None)]


def test_heartbeat_older_than_the_last_applied_event_does_not_override_it(tmp_path: Path) -> None:
    """A heartbeat written before the newest event can never undo that event."""
    h = _build(tmp_path)
    h.clocks.wall = NOW + 10
    append_event_record(h.bridge, turn_start_record("t1", at=NOW + 10))
    h.poller.tick()
    assert h.edges.calls == [("mod", "running", None)]

    h.clocks.wall = NOW + 30  # the heartbeat stamped at NOW still says no turn is running
    h.poller.tick()
    _settle(h)  # an older view must not take effect even after the grace has passed
    assert h.edges.calls == [("mod", "running", None)]
    assert h.poller.active


def test_quick_completion_then_a_new_turn_publishes_no_idle(tmp_path: Path) -> None:
    """A follow-up turn that starts within the grace never shows the session as idle."""
    h = _build(tmp_path)
    append_event_record(h.bridge, turn_start_record("t1", at=NOW))
    h.poller.tick()
    append_event_record(h.bridge, turn_complete_record("t1", at=NOW + 1))
    h.poller.tick()
    h.clocks.monotonic += 0.5
    append_event_record(h.bridge, turn_start_record("t2", at=NOW + 1.3))
    h.poller.tick()
    _settle(h)
    assert h.edges.calls == [("mod", "running", None)]


def test_completion_and_a_new_turn_read_together_publish_no_idle(tmp_path: Path) -> None:
    """Both records arriving in one read leave the status running, with no idle edge."""
    h = _build(tmp_path)
    append_event_record(h.bridge, turn_start_record("t1", at=NOW))
    h.poller.tick()
    append_event_record(h.bridge, turn_complete_record("t1", at=NOW + 1))
    append_event_record(h.bridge, turn_start_record("t2", at=NOW + 1.3))
    h.poller.tick()
    _settle(h)
    assert h.edges.calls == [("mod", "running", None)]


def test_idle_is_published_once_after_the_grace(tmp_path: Path) -> None:
    """The idle waits out the full grace, then publishes exactly once."""
    h = _build(tmp_path)
    append_event_record(h.bridge, turn_start_record("t1", at=NOW))
    h.poller.tick()
    append_event_record(h.bridge, turn_complete_record("t1", at=NOW + 1))
    h.poller.tick()
    h.clocks.monotonic += MOD_TURN_IDLE_GRACE_S / 2
    h.poller.tick()
    assert h.edges.calls == [("mod", "running", None)]

    h.clocks.monotonic += MOD_TURN_IDLE_GRACE_S / 2
    h.poller.tick()
    h.poller.tick()
    assert h.edges.calls == [("mod", "running", None), ("mod", "idle", None)]


def test_hand_back_during_the_grace_leaves_the_idle_to_an_active_structured_source(
    tmp_path: Path,
) -> None:
    """A mod that goes stale mid-grace publishes no idle; the resynced file source settles it."""
    h = _build(tmp_path)
    h.source.active = True
    append_event_record(h.bridge, turn_start_record("t1", at=NOW))
    h.poller.tick()
    append_event_record(h.bridge, turn_complete_record("t1", at=NOW + 1))
    h.poller.tick()
    h.clocks.wall = NOW + MOD_HEARTBEAT_MAX_AGE_S + 1
    _settle(h)  # the grace has passed, but the mod no longer owns the status
    assert h.edges.calls == [("mod", "running", None)]
    assert h.source.resyncs == 1

    h.source.emit("idle")  # the active source re-asserts its value after the resync
    assert h.edges.calls == [("mod", "running", None), ("structured", "idle", None)]


@pytest.mark.parametrize("phase", ["mid-turn", "mid-grace", "mid-grace-with-resync"])
@pytest.mark.parametrize(
    ("pane_last_change", "expected_pane_edges"),
    [
        (1_000.0, [("pane", "idle", None)]),
        (1_001.5, []),
    ],
    ids=["pane-quiet-at-hand-back", "pane-still-changing"],
)
def test_pane_only_hand_back_publishes_idle_only_when_the_pane_is_quiet(
    tmp_path: Path,
    phase: str,
    pane_last_change: float,
    expected_pane_edges: list[tuple[str, str, str | None]],
) -> None:
    """Without a structured source, a muted pane idle edge is replaced by a quiet check.

    The mod still shows running when it goes stale, either mid-turn or while it
    holds a completed turn's idle for the grace, so only the pane can settle it.
    """
    h = _build(tmp_path)
    append_event_record(h.bridge, turn_start_record("t1", at=NOW))
    h.poller.tick()
    if phase != "mid-turn":
        h.clocks.monotonic = 1_001.5
        append_event_record(h.bridge, turn_complete_record("t1", at=NOW + 1))
        h.poller.tick()  # the idle is held until 1_002.5, so running still shows
    if phase == "mid-grace-with-resync":
        h.poller.resync()  # a reconnect lands on the same tick as the hand-back
    h.clocks.monotonic = pane_last_change
    h.poller.note_pane_activity()
    h.clocks.monotonic = 1_002.0
    h.clocks.wall = NOW + MOD_HEARTBEAT_MAX_AGE_S + 1  # the mod goes stale

    h.poller.tick()
    assert h.edges.calls == [("mod", "running", None), *expected_pane_edges]


def test_resync_republishes_the_owned_status(tmp_path: Path) -> None:
    """After a reconnect the owned status is published again, even though unchanged."""
    h = _build(tmp_path)
    append_event_record(h.bridge, turn_start_record("t1", at=NOW))
    h.poller.tick()
    h.poller.resync()
    h.poller.tick()
    assert h.edges.calls == [("mod", "running", None), ("mod", "running", None)]
    assert h.source.resyncs == 1


def test_dialog_reason_from_the_structured_source_annotates_the_running_turn(
    tmp_path: Path,
) -> None:
    """A permission prompt the file reports still shows while the mod owns the turn."""
    h = _build(tmp_path)
    append_event_record(h.bridge, turn_start_record("t1", at=NOW))
    h.poller.tick()

    h.source.emit("running", "permission prompt")
    h.poller.tick()
    assert h.edges.calls[-1] == ("mod", "running", "permission prompt")
    assert h.poller.blocked_on == "permission prompt"

    h.source.emit("running", None)
    h.poller.tick()
    assert h.edges.calls[-1] == ("mod", "running", None)


def test_retire_stops_publishing_and_retires_the_structured_source(tmp_path: Path) -> None:
    """Once the terminal has exited, no further status is published."""
    h = _build(tmp_path)
    append_event_record(h.bridge, turn_start_record("t1", at=NOW))
    h.poller.tick()
    h.poller.retire()

    append_event_record(h.bridge, turn_complete_record("t1", at=NOW + 1))
    h.poller.tick()
    h.source.emit("idle")
    assert h.source.retired
    assert not h.poller.active
    assert h.edges.calls == [("mod", "running", None)]
