"""Concurrency coverage for the Claude OSC status adapter."""

from __future__ import annotations

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from omnigent.debug_logging import record_to_row
from omnigent.harnesses.claude_native.program_status import ClaudeProgramStatusPoller
from omnigent.harnesses.claude_native.status_file import SessionStatusPoller
from omnigent.terminals.program_status import ProgramStatusReader


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
