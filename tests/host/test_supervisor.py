"""Tests for the per-user host service supervisor."""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import psutil
import pytest

from omnigent.host import supervisor


def _at(hour: int, minute: int = 0, *, day: int = 9) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc)


class _FakeProcess:
    _next_pid = 1000

    def __init__(self, returncode: int | None = None) -> None:
        self.pid = self._next_pid
        type(self)._next_pid += 1
        self.returncode = returncode
        self.signals: list[int] = []

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(["fake"], timeout)
        return self.returncode

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)
        if signum in {
            signal.SIGTERM,
            signal.SIGINT,
            getattr(signal, "SIGKILL", signal.SIGTERM),
        }:
            self.returncode = -signum


def _fake_process_groups(monkeypatch: pytest.MonkeyPatch, processes: list[_FakeProcess]) -> None:
    monkeypatch.setattr(
        supervisor.os,
        "killpg",
        lambda pid, signum: next(
            process for process in processes if process.pid == pid
        ).send_signal(signum),
    )


def test_schedule_stays_inside_local_maintenance_window() -> None:
    assert not supervisor._maintenance_due(_at(3, 59), None)
    assert supervisor._maintenance_due(_at(4), None)
    assert supervisor._maintenance_due(_at(5, 59), None)
    assert not supervisor._maintenance_due(_at(6), None)
    assert not supervisor._maintenance_due(_at(4), date(2026, 10, 9))
    assert not supervisor._maintenance_due(_at(4), date(2026, 10, 10))
    assert supervisor._maintenance_due(_at(4, day=10), date(2026, 10, 9))


def test_schedule_skips_a_window_missed_by_sleep() -> None:
    now = _at(6)

    assert not supervisor._maintenance_due(now, None)


def test_upgrade_attempt_date_is_persisted_before_running_upgrade(tmp_path: Path) -> None:
    state_path = tmp_path / "upgrade-date"
    current = date(2026, 10, 9)
    first = supervisor.HostSupervisor("", state_path=state_path)

    assert first._claim_date(current)
    assert state_path.read_text() == "2026-10-09"
    second = supervisor.HostSupervisor("", state_path=state_path)
    assert not second._claim_date(current)
    assert second._claim_date(date(2026, 10, 10))


def test_commands_use_fresh_interpreter_and_skip_draining_stopped_sessions(tmp_path: Path) -> None:
    host = supervisor.HostSupervisor("https://example.com", state_path=tmp_path / "state")

    assert host.child_command == [
        sys.executable,
        "-P",
        "-m",
        "omnigent",
        "host",
        "--server",
        "https://example.com",
        "--non-interactive",
        "--no-open",
    ]
    assert host.upgrade_command == [sys.executable, "-P", "-m", "omnigent", "upgrade", "--force"]


def test_successful_upgrade_restarts_child_with_new_on_disk_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    version_path = tmp_path / "version"
    version_path.write_text("old")
    commands: list[list[str]] = []
    host_versions: list[str] = []
    processes: list[_FakeProcess] = []

    def fake_popen(args: list[str], **_kwargs: object) -> _FakeProcess:
        commands.append(args)
        if "upgrade" in args:
            version_path.write_text("new")
            process = _FakeProcess(0)
        else:
            host_versions.append(version_path.read_text())
            process = _FakeProcess(None if not host_versions[:-1] else 0)
        processes.append(process)
        return process

    _fake_process_groups(monkeypatch, processes)
    host = supervisor.HostSupervisor(
        "https://example.com",
        state_path=tmp_path / "date",
        now=lambda: _at(4),
        popen=fake_popen,
    )

    assert host.run() == 0
    assert host_versions == ["old", "new"]
    assert commands[1][-2:] == ["upgrade", "--force"]


@pytest.mark.parametrize("upgrade_code", [3])
def test_failed_upgrade_still_recovers_host_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, upgrade_code: int
) -> None:
    processes: list[_FakeProcess] = []
    host_count = 0

    def fake_popen(args: list[str], **_kwargs: object) -> _FakeProcess:
        nonlocal host_count
        if "upgrade" in args:
            process = _FakeProcess(upgrade_code)
        else:
            host_count += 1
            process = _FakeProcess(None if host_count == 1 else 0)
        processes.append(process)
        return process

    _fake_process_groups(monkeypatch, processes)
    host = supervisor.HostSupervisor(
        "",
        state_path=tmp_path / "date",
        now=lambda: _at(4),
        popen=fake_popen,
    )

    assert host.run() == 0
    assert host_count == 2


def test_timed_out_upgrade_is_killed_and_child_is_restarted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supervisor, "UPGRADE_TIMEOUT_SECONDS", 0.0)
    processes: list[_FakeProcess] = []
    host_count = 0

    def fake_popen(args: list[str], **_kwargs: object) -> _FakeProcess:
        nonlocal host_count
        if "upgrade" in args:
            process = _FakeProcess(None)
        else:
            host_count += 1
            process = _FakeProcess(None if host_count == 1 else 0)
        processes.append(process)
        return process

    _fake_process_groups(monkeypatch, processes)
    host = supervisor.HostSupervisor(
        "",
        state_path=tmp_path / "date",
        now=lambda: _at(4),
        popen=fake_popen,
    )

    assert host.run() == 0
    assert host_count == 2
    assert processes[1].signals == [signal.SIGTERM, signal.SIGKILL]


@pytest.mark.parametrize("exit_code", [0, 78, 143, 130])
def test_deliberate_or_fatal_child_exit_is_not_resurrected(tmp_path: Path, exit_code: int) -> None:
    starts = 0

    def fake_popen(_args: list[str], **_kwargs: object) -> _FakeProcess:
        nonlocal starts
        starts += 1
        return _FakeProcess(exit_code)

    host = supervisor.HostSupervisor("", state_path=tmp_path / "date", popen=fake_popen)

    assert host.run() == exit_code
    assert starts == 1


def test_shutdown_signal_forwards_to_host_and_updater(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _FakeProcess()
    updater = _FakeProcess()
    processes = [child, updater]
    _fake_process_groups(monkeypatch, processes)
    host = supervisor.HostSupervisor("", state_path=tmp_path / "date")
    host._child = child  # type: ignore[assignment]
    host._updater = updater  # type: ignore[assignment]

    host._handle_signal(signal.SIGTERM, None)

    assert child.signals == [signal.SIGTERM]
    assert updater.signals == [signal.SIGTERM]
    assert host._stop_requested.is_set()


@pytest.mark.posix_only
def test_stop_kills_installer_even_when_its_parent_exits(tmp_path: Path) -> None:
    installer = (
        "import os, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print(os.getpid(), flush=True); time.sleep(60)"
    )
    parent = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', sys.argv[1]]); time.sleep(60)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", parent, installer],
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    host = supervisor.HostSupervisor("", state_path=tmp_path / "date")
    try:
        assert process.stdout is not None
        installer_pid = int(process.stdout.readline())
        host._stop_process(process)

        deadline = time.monotonic() + 5
        while psutil.pid_exists(installer_pid):
            try:
                if psutil.Process(installer_pid).status() == psutil.STATUS_ZOMBIE:
                    break
            except psutil.NoSuchProcess:
                break
            assert time.monotonic() < deadline, "installer survived supervisor shutdown"
            time.sleep(0.01)
        assert process.returncode is not None
    finally:
        host._stop_process(process)
        if process.stdout is not None:
            process.stdout.close()
