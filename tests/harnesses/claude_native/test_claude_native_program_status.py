"""Concurrency coverage for the Claude OSC status adapter."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from omnigent.debug_logging import record_to_row
from omnigent.harnesses.claude_native.program_status import (
    ERROR_HOOK_GRACE_S as _GRACE_S,
)
from omnigent.harnesses.claude_native.program_status import ClaudeProgramStatusPoller
from omnigent.harnesses.claude_native.status_file import SessionStatusPoller
from omnigent.terminals.program_status import ProgramStatusReader

_ERROR_REPORT = {"state": "error", "app": "claude-code", "message": "API Error: 499"}
# What Claude reports after a turn fails authentication, until the user signs in.
_AUTH_BLOCKED_REPORT = {
    "state": "blocked",
    "app": "claude-code",
    "kind": "auth",
    "message": "Invalid API key · Please run /login",
}


class _FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _write_report(path: Path, status: dict[str, object]) -> None:
    path.write_text(json.dumps({"relay_pid": os.getpid(), "status": status}))


def _poller(
    tmp_path: Path,
    path: Path,
    published: list[str],
    *,
    turn_is_active: Callable[[], bool] | None = None,
    turn_activity_epoch: Callable[[], int] | None = None,
    failures: list[str] | None = None,
    clock: Callable[[], float] = time.monotonic,
    background_agents_after_turn: Callable[[], bool] | None = None,
) -> ClaudeProgramStatusPoller:
    def on_status(status: str, _reason: str | None) -> None:
        published.append(status)

    return ClaudeProgramStatusPoller(
        ProgramStatusReader(path),
        SessionStatusPoller(
            on_status=on_status,
            pane_pid_getter=lambda: None,
            session_id_getter=lambda: None,
            config_dir=tmp_path,
        ),
        on_status,
        turn_is_active=turn_is_active,
        turn_activity_epoch=turn_activity_epoch,
        on_failure=failures.append if failures is not None else None,
        clock=clock,
        background_agents_after_turn=background_agents_after_turn,
    )


def test_working_report_reads_waiting_while_a_settled_turn_runs_a_background_agent(
    tmp_path: Path,
) -> None:
    """Claude reports working for a background agent that outlives its turn."""
    path = tmp_path / "program-status.json"
    _write_report(path, {"state": "working", "app": "claude-code"})
    agent_after_turn = [False]
    published: list[str] = []
    poller = _poller(
        tmp_path, path, published, background_agents_after_turn=lambda: agent_after_turn[0]
    )
    poller.tick()
    assert published == ["running"]

    agent_after_turn[0] = True
    poller.tick()
    poller.tick()
    assert published == ["running", "waiting"]
    assert poller.holding_background_agents is True

    _write_report(path, {"state": "blocked", "kind": "permission", "message": "allow?"})
    poller.tick()
    assert published == ["running", "waiting", "running"]
    assert poller.holding_background_agents is False
    assert poller.blocked_on == "allow?"

    _write_report(path, {"state": "working", "app": "claude-code"})
    poller.tick()
    agent_after_turn[0] = False
    poller.tick()
    assert published == ["running", "waiting", "running", "waiting", "running"]
    assert poller.holding_background_agents is False

    agent_after_turn[0] = True
    poller.tick()
    _write_report(path, {"state": "idle", "app": "claude-code"})
    poller.tick()
    assert published[-2:] == ["waiting", "idle"]
    assert poller.holding_background_agents is False


def test_file_fallback_keeps_a_background_agent_hold_after_osc_reports_stop(
    tmp_path: Path,
) -> None:
    """Once OSC reports stop, the busy file still reads waiting until the hold clears."""
    path = tmp_path / "program-status.json"
    _write_report(path, {"state": "working", "app": "claude-code"})
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "1.json").write_text(
        json.dumps({"pid": 1, "sessionId": "s", "kind": "interactive", "status": "busy"})
    )
    agent_after_turn = [True]
    published: list[str] = []

    def on_status(status: str, _reason: str | None) -> None:
        published.append(status)

    poller = ClaudeProgramStatusPoller(
        ProgramStatusReader(path),
        SessionStatusPoller(
            on_status=on_status,
            pane_pid_getter=lambda: 1,
            session_id_getter=lambda: "s",
            config_dir=tmp_path,
            background_agents_after_turn=lambda: agent_after_turn[0],
        ),
        on_status,
        background_agents_after_turn=lambda: agent_after_turn[0],
    )
    poller.tick()
    assert published == ["waiting"]
    assert poller.holding_background_agents is True

    path.unlink()
    poller.tick()
    assert published == ["waiting", "waiting"]
    assert poller.holding_background_agents is True

    agent_after_turn[0] = False
    poller.tick()
    assert published == ["waiting", "waiting", "running"]
    assert poller.holding_background_agents is False

    agent_after_turn[0] = True
    poller.tick()
    assert poller.holding_background_agents is True
    poller.retire()
    assert poller.holding_background_agents is False


@pytest.mark.parametrize("report", [_ERROR_REPORT, _AUTH_BLOCKED_REPORT])
def test_error_report_without_a_hook_edge_fails_the_turn_once(
    tmp_path: Path, report: dict[str, object]
) -> None:
    """A turn the runner still counts in flight fails once when its hook never lands."""
    path = tmp_path / "program-status.json"
    _write_report(path, report)
    clock = _FakeClock()
    published: list[str] = []
    poller = _poller(tmp_path, path, published, turn_is_active=lambda: True, clock=clock)
    poller.tick()
    clock.now = _GRACE_S - 1
    poller.resync()
    poller.tick()
    assert published == []
    clock.now = _GRACE_S * 6
    poller.tick()
    poller.tick()
    assert published == ["failed"]


@pytest.mark.parametrize("message", [None, "", "  ", "API Error: 499"])
def test_error_fallback_preserves_available_detail(tmp_path: Path, message: str | None) -> None:
    """A missing hook does not discard the reason already in the OSC report."""
    path = tmp_path / "program-status.json"
    _write_report(path, {**_ERROR_REPORT, "message": message})
    clock = _FakeClock()
    published: list[str] = []
    failures: list[str] = []
    poller = _poller(
        tmp_path, path, published, turn_is_active=lambda: True, failures=failures, clock=clock
    )
    poller.tick()
    assert published == failures == []
    clock.now = 10.0
    poller.tick()
    poller.resync()
    poller.tick()
    assert failures == ([message] if message and message.strip() else [])
    assert published == ([] if failures else ["failed"])


@pytest.mark.parametrize(
    "report",
    [
        _ERROR_REPORT,
        {**_ERROR_REPORT, "message": None},
        _AUTH_BLOCKED_REPORT,
        {**_AUTH_BLOCKED_REPORT, "message": None},
    ],
)
def test_delayed_failure_hook_can_settle_before_fallback(
    tmp_path: Path, report: dict[str, object]
) -> None:
    """A delayed detailed hook must not be preceded by a synthetic failure."""
    path = tmp_path / "program-status.json"
    _write_report(path, report)
    clock = _FakeClock()
    published: list[str] = []
    failures: list[str] = []
    turn_active = True
    poller = _poller(
        tmp_path,
        path,
        published,
        turn_is_active=lambda: turn_active,
        failures=failures,
        clock=clock,
    )

    poller.tick()
    clock.now = 7.5
    poller.resync()
    poller.tick()
    assert published == failures == []

    turn_active = False
    clock.now = 30.0
    poller.tick()
    assert published == failures == []


def test_auth_block_is_a_failed_turn_while_a_prompt_block_is_work(tmp_path: Path) -> None:
    """Only a dialog block keeps the turn running; an auth block, even resynced, publishes none."""
    path = tmp_path / "program-status.json"
    reasons: list[tuple[str, str | None]] = []

    def on_status(status: str, reason: str | None) -> None:
        reasons.append((status, reason))

    poller = ClaudeProgramStatusPoller(
        ProgramStatusReader(path),
        SessionStatusPoller(
            on_status=on_status,
            pane_pid_getter=lambda: None,
            session_id_getter=lambda: None,
            config_dir=tmp_path,
        ),
        on_status,
    )
    _write_report(path, {"state": "blocked", "app": "claude-code", "kind": "permission"})
    poller.tick()
    _write_report(path, _AUTH_BLOCKED_REPORT)
    poller.tick()
    poller.resync()
    poller.tick()
    assert reasons == [("running", "permission prompt")]
    _write_report(path, {"state": "idle", "app": "claude-code"})
    poller.tick()
    assert reasons == [("running", "permission prompt"), ("idle", None)]


def test_reports_idle_follows_the_osc_report(tmp_path: Path) -> None:
    """Pane polling may back off only while OSC reads idle, never once retired."""
    path = tmp_path / "program-status.json"
    poller = _poller(tmp_path, path, [])
    # No OSC report and no status file read yet: unknown is not idle.
    assert poller.reports_idle is False
    for state, idle in [("working", False), ("blocked", False), ("done", True), ("idle", True)]:
        _write_report(path, {"state": state, "app": "claude-code"})
        assert poller.reports_idle is idle, state
    poller.retire()
    assert poller.reports_idle is False


def test_osc_owns_status_while_the_file_reports_background_shells(tmp_path: Path) -> None:
    """OSC says ``done`` once a turn ends with a shell still running; only the file says so."""
    path = tmp_path / "program-status.json"
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    status_file = sessions / "4242.json"
    statuses: list[str] = []
    background: list[bool] = []
    # Each replay with the observation version current when it was issued.
    replays: list[tuple[str, int]] = []

    def on_status(status: str, _reason: str | None) -> None:
        statuses.append(status)

    def write_file(status: str) -> None:
        previous = status_file.stat().st_mtime if status_file.exists() else None
        status_file.write_text(
            json.dumps({"pid": 4242, "sessionId": "s", "kind": "interactive", "status": status})
        )
        if previous is not None:
            # Consecutive writes must stay observable on coarse-mtime filesystems.
            mtime = max(status_file.stat().st_mtime, previous + 1)
            os.utime(status_file, (mtime, mtime))

    poller = ClaudeProgramStatusPoller(
        ProgramStatusReader(path),
        SessionStatusPoller(
            on_status=on_status,
            pane_pid_getter=lambda: 4242,
            session_id_getter=lambda: "s",
            config_dir=tmp_path,
            on_background=background.append,
        ),
        on_status,
        on_replay=lambda status, _reason: replays.append((status, poller.observation_version)),
    )
    _write_report(path, {"state": "working", "app": "claude-code"})
    write_file("busy")
    poller.tick()
    _write_report(path, {"state": "done", "app": "claude-code"})
    write_file("shell")
    poller.tick()
    assert statuses == ["running", "idle"]
    assert background == [True]

    # The shell's completion starts a follow-up turn; the file's busy and idle
    # publish no status edge of their own while OSC is live.
    write_file("busy")
    poller.tick()
    write_file("idle")
    poller.tick()
    assert statuses == ["running", "idle"]
    assert background == [True, False]

    # A resync replays OSC's status. The file vanishing in the same tick is not a
    # status observation, so it cannot make that replay look stale.
    poller.resync()
    status_file.unlink()
    poller.tick()
    assert replays == [("idle", poller.observation_version)]
    assert background == [True, False, False]


def test_forwarded_hook_edge_cancels_the_grace_period(tmp_path: Path) -> None:
    """Once the hook's terminal edge clears the turn, the standing error report fails nothing."""
    path = tmp_path / "program-status.json"
    _write_report(path, _ERROR_REPORT)
    clock = _FakeClock()
    turn_active = [True]
    published: list[str] = []
    failures: list[str] = []
    poller = _poller(
        tmp_path,
        path,
        published,
        turn_is_active=lambda: turn_active[0],
        failures=failures,
        clock=clock,
    )
    poller.tick()
    turn_active[0] = False
    clock.now = _GRACE_S * 6
    poller.tick()
    assert published == []
    assert failures == []


@pytest.mark.parametrize("poll_between_turns", [True, False])
def test_new_turn_on_a_standing_error_report_gets_its_own_grace_period(
    tmp_path: Path, poll_between_turns: bool
) -> None:
    """A turn dispatched after the previous hook landed is not failed by the old timer."""
    path = tmp_path / "program-status.json"
    _write_report(path, _ERROR_REPORT)
    clock = _FakeClock()
    turn_active = [True]
    activity_epoch = [1]
    published: list[str] = []
    poller = _poller(
        tmp_path,
        path,
        published,
        turn_is_active=lambda: turn_active[0],
        turn_activity_epoch=lambda: activity_epoch[0],
        clock=clock,
    )
    poller.tick()
    clock.now = 2.0
    turn_active[0] = False
    if poll_between_turns:
        poller.tick()
    clock.now = 3.0
    turn_active[0] = True
    activity_epoch[0] += 1
    poller.tick()
    clock.now = 3.0 + _GRACE_S - 1  # The old timer would have expired at _GRACE_S.
    poller.tick()
    assert published == []
    clock.now = 3.0 + _GRACE_S + 1
    poller.tick()
    assert published == ["failed"]


def test_error_report_leaves_the_failed_edge_to_the_stop_failure_hook(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An error report publishes nothing, so the hook's detailed failure is the turn's only one."""
    caplog.set_level(logging.INFO)
    path = tmp_path / "program-status.json"
    path.write_text(
        json.dumps(
            {
                "relay_pid": os.getpid(),
                "status": {
                    "state": "error",
                    "app": "claude-code",
                    "message": "private-synthetic-error-text",
                },
            }
        )
    )
    published: list[str] = []

    def on_status(status: str, _reason: str | None) -> None:
        published.append(status)

    poller = ClaudeProgramStatusPoller(
        ProgramStatusReader(path),
        SessionStatusPoller(
            on_status=on_status,
            pane_pid_getter=lambda: None,
            session_id_getter=lambda: None,
            config_dir=tmp_path,
        ),
        on_status,
        omnigent_session_id="synthetic-session",
        terminal_instance_id="synthetic-terminal",
        terminal_launch_id="synthetic-launch",
    )
    poller.tick()
    poller.resync()
    poller.tick()
    assert published == []
    observations = [
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "claude_program_error_observed"
    ]
    assert len(observations) == 1
    assert observations[0].levelno == logging.INFO
    row = record_to_row(observations[0], source="runner")
    assert row["session_id"] == "synthetic-session"
    assert row["attributes"]["terminal_instance_id"] == "synthetic-terminal"
    assert row["attributes"]["terminal_launch_id"] == "synthetic-launch"
    assert row["attributes"]["status_publication"] == "deferred_to_stop_failure"
    assert "private-synthetic-error-text" not in json.dumps(row)

    path.write_text(json.dumps({"relay_pid": os.getpid(), "status": {"state": "working"}}))
    poller.tick()
    assert published == ["running"]
    path.write_text(json.dumps({"relay_pid": os.getpid(), "status": {"state": "error"}}))
    poller.tick()
    poller.tick()
    poller.retire()
    poller.tick()
    assert published == ["running"]
    assert (
        sum(
            getattr(record, "event_name", None) == "claude_program_error_observed"
            for record in caplog.records
        )
        == 2
    )


@pytest.mark.parametrize(
    ("operation", "transition"),
    [("tick", "resync"), ("tick", "retire"), ("active", "retire"), ("blocked_on", "retire")],
)
def test_lifecycle_change_during_snapshot_read(
    tmp_path: Path, monkeypatch, operation: str, transition: str
) -> None:
    """Reconnect survives an in-flight read; retirement prevents its publication."""
    path = tmp_path / "program-status.json"
    path.write_text(json.dumps({"relay_pid": os.getpid(), "status": {"state": "idle"}}))
    published: list[str] = []

    def on_status(status: str, _reason: str | None) -> None:
        published.append(status)

    poller = ClaudeProgramStatusPoller(
        ProgramStatusReader(path),
        SessionStatusPoller(
            on_status=on_status,
            pane_pid_getter=lambda: None,
            session_id_getter=lambda: None,
            config_dir=tmp_path,
        ),
        on_status,
    )
    poller.tick()
    assert published == ["idle"]

    _write_report(path, {"state": "blocked", "kind": "question", "message": "input needed"})
    entered = threading.Event()
    release = threading.Event()
    original_read = Path.read_text

    def pause_read(file: Path, *args, **kwargs):
        result = original_read(file, *args, **kwargs)
        if file == path and threading.current_thread() is not threading.main_thread():
            entered.set()
            assert release.wait(5), "poller did not resume"
        return result

    monkeypatch.setattr(Path, "read_text", pause_read)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            lambda: poller.tick() if operation == "tick" else getattr(poller, operation)
        )
        try:
            assert entered.wait(5), "poller did not reach the snapshot read"
            getattr(poller, transition)()
        finally:
            release.set()
        result = pending.result(timeout=5)

    if transition == "retire":
        assert result is (False if operation == "active" else None)
        poller.resync()
        poller.tick()
        assert poller.active is False
        assert poller.blocked_on is None
        assert published == ["idle"]
        return
    assert published == ["idle", "running"]
    poller.tick()
    assert published == ["idle", "running", "running"]
    poller.tick()
    assert published == ["idle", "running", "running"]
