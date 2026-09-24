"""Evidence retained when terminal probes fail and cleanup removes the socket."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import shutil
import socket
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import omnigent.inner.terminal as terminal_mod
from omnigent.debug_logging import record_to_row
from omnigent.inner.terminal import TerminalInstance
from omnigent.process_logging import RedactingLogFormatter


async def _run_watcher(instance: TerminalInstance, threaded: bool, on_exit) -> None:
    if threaded:
        stop = threading.Event()
        task = asyncio.create_task(
            asyncio.to_thread(
                instance._idle_watch_loop_threaded,
                stop,
                on_exit=on_exit,
                poll_interval_s=0.001,
            )
        )
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=1)
        finally:
            stop.set()
            await task
    else:
        await asyncio.wait_for(instance._idle_watch_loop(lambda: None, on_exit=on_exit), timeout=1)


def _patch_tmux(monkeypatch: pytest.MonkeyPatch, run) -> None:
    async def create(*cmd, **kwargs):
        result = run(cmd, **kwargs)

        async def communicate():
            return result.stdout, result.stderr

        return SimpleNamespace(returncode=result.returncode, communicate=communicate)

    monkeypatch.setattr(terminal_mod.subprocess, "run", run)
    monkeypatch.setattr(
        terminal_mod,
        "asyncio",
        SimpleNamespace(**{**vars(asyncio), "create_subprocess_exec": create}),
    )
    monkeypatch.setattr(terminal_mod, "_IDLE_POLL_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(terminal_mod, "_TMUX_PROBE_START_FAILURE_BACKOFF_SECONDS", 0.001)


@pytest.mark.parametrize("threaded", [False, True], ids=["async", "threaded"])
async def test_unavailable_error_retains_both_probes_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    threaded: bool,
) -> None:
    instance = TerminalInstance(
        name="runtime",
        session_key="main",
        socket_path=tmp_path / "tmux.sock",
        private_dir=tmp_path,
        running=True,
    )
    instance._remember_pane_snapshot("private terminal output")
    commands = []

    def run(cmd, **kwargs):
        commands.append(cmd[5])
        if cmd[5] == "has-session":
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", "tmux")
        return subprocess.CompletedProcess(
            cmd,
            17,
            b"",
            f"error connecting to {instance.socket_path}: Connection refused".encode(),
        )

    _patch_tmux(monkeypatch, run)
    log_path = tmp_path.parent / f"{tmp_path.name}.log"
    log_at_cleanup: list[str] = []

    def cleanup():
        log_at_cleanup.append(log_path.read_text())
        shutil.rmtree(tmp_path)

    # Keep a real socket entry so the error must snapshot it before on_exit removes it.
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(instance.socket_path))
        handler = logging.FileHandler(log_path, mode="w")
        handler.setFormatter(RedactingLogFormatter(use_colors=False))
        terminal_mod.logger.addHandler(handler)
        try:
            with caplog.at_level(logging.WARNING, logger=terminal_mod.__name__):
                await _run_watcher(instance, threaded, cleanup)
        finally:
            terminal_mod.logger.removeHandler(handler)
            handler.close()

    assert not tmp_path.exists()
    assert not instance.running
    assert commands == ["capture-pane", "has-session"] * 3
    records = [
        r
        for r in caplog.records
        if r.name == terminal_mod.__name__ and "tmux unavailable after" in r.getMessage()
    ]
    assert len(records) == 1
    record = records[0]
    assert record.event_name == "terminal_unavailable"
    attrs = record.attributes
    assert attrs["socket_state"] == "socket"
    assert attrs["private_dir_state"] == "directory"
    assert attrs["consecutive_probe_failures"] == 3
    assert attrs["shutdown_requested"] is False
    failures = json.loads(attrs["probe_failures_json"])
    assert len(failures) == 6
    assert len(log_at_cleanup) == 1
    saved = json.loads(log_at_cleanup[0].split("diagnostics=", 1)[1])
    assert json.loads(saved["probe_failures_json"]) == failures
    for capture, session in zip(failures[::2], failures[1::2], strict=True):
        assert capture["command"] == "capture-pane"
        assert capture["returncode"] == 17
        assert capture["errno"] is None
        assert "Connection refused" in capture["error"]
        assert capture["elapsed_ms"] >= 0
        assert capture["failed_at_unix_ms"] > 0
        assert session["command"] == "has-session"
        assert session["returncode"] is None
        assert session["errno"] == errno.ENOENT

    # Both the local file and remote row must contain evidence with warnings filtered out.
    local_line = RedactingLogFormatter(use_colors=False).format(record)
    row = record_to_row(record, source="runner")
    assert "Connection refused" in local_line
    assert "No such file or directory" in local_line
    assert json.loads(row["attributes"]["probe_failures_json"]) == failures
    local_bundle = local_line.split("diagnostics=", 1)[1]
    for output in (local_bundle, str(row["attributes"])):
        assert str(tmp_path) not in output
        assert "private terminal output" not in output


@pytest.mark.parametrize("threaded", [False, True], ids=["async", "threaded"])
@pytest.mark.parametrize(
    "recovery", ["capture-pane", "has-session", "capture-spawn", "session-spawn"]
)
async def test_probe_history_resets_on_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    threaded: bool,
    recovery: str,
) -> None:
    instance = TerminalInstance(
        name="runtime",
        session_key="main",
        socket_path=tmp_path / "tmux.sock",
        private_dir=tmp_path,
        running=True,
    )
    tick = 0

    def run(cmd, **kwargs):
        nonlocal tick
        command = cmd[5]
        if command == "capture-pane":
            tick += 1
        if tick == 3 and (
            (command == "capture-pane" and recovery == "capture-spawn")
            or (command == "has-session" and recovery == "session-spawn")
        ):
            raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
        if command == "list-panes" or (tick == 3 and command == recovery):
            return subprocess.CompletedProcess(cmd, 0, b"0\n", b"")
        detail = (
            b"can't find session: before recovery"
            if tick <= 3
            else b"can't find session: after recovery"
        )
        return subprocess.CompletedProcess(cmd, 1, b"", detail)

    _patch_tmux(monkeypatch, run)
    await _run_watcher(instance, threaded, lambda: None)

    assert tick == 6
    record = next(r for r in caplog.records if "tmux unavailable after" in r.getMessage())
    failures = json.loads(record.attributes["probe_failures_json"])
    assert len(failures) == 6
    assert {failure["error"] for failure in failures} == {"can't find session: after recovery"}
    assert record.attributes["socket_state"] == "missing"
    assert record.attributes["socket_stat_errno"] == errno.ENOENT


def test_probe_evidence_is_bounded_and_redacted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    instance = TerminalInstance(
        name="runtime",
        session_key="main",
        socket_path=tmp_path / "tmux.sock",
        private_dir=tmp_path,
    )
    secret = "sensitive-test-value"
    for _ in range(100):
        error = terminal_mod._TmuxCommandError(
            ["tmux", "capture-pane"],
            returncode=1,
            stderr=f"token={secret} detail={'x' * 2000}".encode(),
        )
        instance._remember_probe_failure("capture-pane", error, terminal_mod.time.monotonic())

    instance._log_tmux_unavailable(3, exit_callback_present=False)
    record = next(r for r in caplog.records if "tmux unavailable after" in r.getMessage())
    failures = json.loads(record.attributes["probe_failures_json"])
    assert len(failures) == 6
    assert all(len(failure["error"]) <= 1024 for failure in failures)
    assert all(failure["error_truncated"] for failure in failures)
    assert secret not in record.getMessage()
    assert "[REDACTED]" in failures[0]["error"]


def test_socket_stat_failure_does_not_hide_probe_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    instance = TerminalInstance(
        name="runtime",
        session_key="main",
        socket_path=tmp_path / "tmux.sock",
        private_dir=tmp_path,
    )
    real_stat = Path.stat

    def stat(path, *args, **kwargs):
        if path == instance.socket_path:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    instance._log_tmux_unavailable(3, exit_callback_present=False)
    record = next(r for r in caplog.records if "tmux unavailable after" in r.getMessage())
    assert record.attributes["socket_state"] == "stat_failed"
    assert record.attributes["socket_stat_errno"] == errno.EACCES
