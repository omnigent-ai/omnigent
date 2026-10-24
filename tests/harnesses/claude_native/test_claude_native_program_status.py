"""Concurrency coverage for the Claude OSC status adapter."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from omnigent.harnesses.claude_native.program_status import ClaudeProgramStatusPoller
from omnigent.harnesses.claude_native.status_file import SessionStatusPoller
from omnigent.terminals.program_status import ProgramStatusReader

# The production grace period, from ERROR_HOOK_GRACE_S.
_GRACE_S = 5.0
_ERROR_REPORT = {"state": "error", "app": "claude-code", "message": "API Error: 499"}


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
    clock: Callable[[], float] = time.monotonic,
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
        clock=clock,
    )


def test_error_report_without_a_hook_edge_fails_the_turn_once(tmp_path: Path) -> None:
    """A turn the runner still counts in flight fails once when its hook never lands."""
    path = tmp_path / "program-status.json"
    _write_report(path, _ERROR_REPORT)
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


def test_forwarded_hook_edge_cancels_the_grace_period(tmp_path: Path) -> None:
    """Once the hook's terminal edge clears the turn, the standing error report fails nothing."""
    path = tmp_path / "program-status.json"
    _write_report(path, _ERROR_REPORT)
    clock = _FakeClock()
    turn_active = [True]
    published: list[str] = []
    poller = _poller(tmp_path, path, published, turn_is_active=lambda: turn_active[0], clock=clock)
    poller.tick()
    turn_active[0] = False
    clock.now = _GRACE_S * 6
    poller.tick()
    assert published == []


def test_new_turn_on_a_standing_error_report_gets_its_own_grace_period(tmp_path: Path) -> None:
    """A turn dispatched after the previous hook landed is not failed by the old timer."""
    path = tmp_path / "program-status.json"
    _write_report(path, _ERROR_REPORT)
    clock = _FakeClock()
    turn_active = [True]
    published: list[str] = []
    poller = _poller(tmp_path, path, published, turn_is_active=lambda: turn_active[0], clock=clock)
    poller.tick()
    clock.now = 2.0
    turn_active[0] = False
    poller.tick()
    clock.now = 3.0
    turn_active[0] = True
    poller.tick()
    clock.now = 3.0 + _GRACE_S - 1  # The old timer would have expired at _GRACE_S.
    poller.tick()
    assert published == []
    clock.now = 3.0 + _GRACE_S + 1
    poller.tick()
    assert published == ["failed"]


def test_error_report_leaves_the_failed_edge_to_the_stop_failure_hook(tmp_path: Path) -> None:
    """An error report publishes nothing, so the hook's detailed failure is the turn's only one."""
    path = tmp_path / "program-status.json"
    path.write_text(
        json.dumps(
            {
                "relay_pid": os.getpid(),
                "status": {"state": "error", "app": "claude-code", "message": "API Error: 499"},
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
    )
    poller.tick()
    poller.resync()
    poller.tick()
    assert published == []

    path.write_text(json.dumps({"relay_pid": os.getpid(), "status": {"state": "working"}}))
    poller.tick()
    assert published == ["running"]


def test_resync_during_tick_survives_the_in_flight_read(tmp_path: Path, monkeypatch) -> None:
    """A forwarded idle edge cannot lose its resync to an in-flight OSC read."""
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

    path.write_text(json.dumps({"relay_pid": os.getpid(), "status": {"state": "working"}}))
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
        pending = executor.submit(poller.tick)
        try:
            assert entered.wait(5), "poller did not reach the snapshot read"
            poller.resync()
        finally:
            release.set()
        pending.result(timeout=5)

    assert published == ["idle", "running"]
    poller.tick()
    assert published == ["idle", "running", "running"]
    poller.tick()
    assert published == ["idle", "running", "running"]
