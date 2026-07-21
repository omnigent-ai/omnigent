"""Tests for ``omni integration slack`` and the integration daemon manager."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.integration_daemon import DaemonRecord, IntegrationDaemon


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate daemon state under a temp OMNIGENT_DATA_DIR."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    return tmp_path


# ── IntegrationDaemon (unit) ──────────────────────────────────────


def test_record_round_trip_and_prune(tmp_path: Path) -> None:
    d = IntegrationDaemon("slack", tmp_path)
    assert d.read_record() is None
    d._write_record(DaemonRecord(pid=4242, log_path="/tmp/x.log", started_at=1))
    rec = d.read_record()
    assert rec is not None and rec.pid == 4242 and rec.log_path == "/tmp/x.log"

    # A dead PID is pruned by running_record().
    with mock.patch.object(IntegrationDaemon, "_pid_alive", return_value=False):
        assert d.running_record() is None
    assert d.read_record() is None  # pruned from disk


def test_start_writes_record_detached(tmp_path: Path) -> None:
    d = IntegrationDaemon("slack", tmp_path)
    with mock.patch("omnigent.integration_daemon.subprocess.Popen") as popen:
        popen.return_value.pid = 777
        record = d.start(["python", "-m", "omnigent_slack"], {"A": "b"})
    assert record.pid == 777
    assert d.read_record() == record
    # Spawned detached (own session/process group) with stdin closed.
    _, kwargs = popen.call_args
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert "start_new_session" in kwargs or "creationflags" in kwargs


def test_stop_signals_and_clears(tmp_path: Path) -> None:
    d = IntegrationDaemon("slack", tmp_path)
    d._write_record(DaemonRecord(pid=4242, log_path="/tmp/x.log", started_at=1))
    calls: list[int] = []
    # Alive for the first liveness check (running_record), then dead so stop()
    # doesn't spin.
    alive = iter([True, False, False])
    with (
        mock.patch.object(IntegrationDaemon, "_pid_alive", side_effect=lambda _pid: next(alive)),
        mock.patch.object(
            IntegrationDaemon, "_signal", side_effect=lambda pid, sig: calls.append(sig)
        ),
    ):
        stopped = d.stop(grace_seconds=0.0)
    assert stopped is not None and stopped.pid == 4242
    assert calls  # at least a SIGTERM was sent
    assert d.read_record() is None


def test_stop_when_not_running_is_noop(tmp_path: Path) -> None:
    d = IntegrationDaemon("slack", tmp_path)
    assert d.stop() is None


def test_confirm_alive_prunes_dead_record(tmp_path: Path) -> None:
    d = IntegrationDaemon("slack", tmp_path)
    record = DaemonRecord(pid=4242, log_path="/tmp/x.log", started_at=1)
    d._write_record(record)
    with mock.patch.object(IntegrationDaemon, "_pid_alive", return_value=False):
        assert d.confirm_alive(record, grace_seconds=0.0) is False
    assert d.read_record() is None  # pruned
    with mock.patch.object(IntegrationDaemon, "_pid_alive", return_value=True):
        d._write_record(record)
        assert d.confirm_alive(record, grace_seconds=0.0) is True


# ── CLI wiring ────────────────────────────────────────────────────


def test_slack_start_hint_when_not_installed(data_dir: Path) -> None:
    runner = CliRunner()
    with mock.patch("omnigent.cli._slack_installed", return_value=False):
        result = runner.invoke(cli, ["integration", "slack", "start"])
    assert result.exit_code != 0
    assert "isn't installed" in result.output
    assert "omnigent-slack" in result.output


def test_slack_foreground_hint_when_not_installed(data_dir: Path) -> None:
    runner = CliRunner()
    with mock.patch("omnigent.cli._slack_installed", return_value=False):
        result = runner.invoke(cli, ["integration", "slack"])
    assert result.exit_code != 0
    assert "isn't installed" in result.output


def test_slack_status_reports_not_running(data_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["integration", "slack", "status"])
    assert result.exit_code == 0
    assert "not running" in result.output


def test_slack_start_status_stop_lifecycle(data_dir: Path) -> None:
    runner = CliRunner()
    with (
        mock.patch("omnigent.cli._slack_installed", return_value=True),
        mock.patch("omnigent.integration_daemon.subprocess.Popen") as popen,
        # The spawned pid is a mock, not a real process — force liveness true so
        # status reports running. confirm_alive (startup-crash detection) has
        # its own tests; short-circuit it here so the happy path doesn't wait
        # out the grace period.
        mock.patch.object(IntegrationDaemon, "_pid_alive", return_value=True),
        mock.patch.object(IntegrationDaemon, "confirm_alive", return_value=True),
    ):
        popen.return_value.pid = 9911
        start = runner.invoke(cli, ["integration", "slack", "start"])
        assert start.exit_code == 0, start.output
        assert "9911" in start.output
        # Argv targets the slack package in the current interpreter.
        argv = popen.call_args.args[0]
        assert argv[1:] == ["-m", "omnigent_slack"]

        status = runner.invoke(cli, ["integration", "slack", "status"])
        assert "running" in status.output and "9911" in status.output

        # start again is idempotent — reports the existing pid, no 2nd spawn.
        popen.reset_mock()
        again = runner.invoke(cli, ["integration", "slack", "start"])
        assert "already running" in again.output
        popen.assert_not_called()

    # stop terminates and clears.
    with (
        mock.patch.object(IntegrationDaemon, "_pid_alive", side_effect=[True, False, False]),
        mock.patch.object(IntegrationDaemon, "_signal"),
    ):
        stop = runner.invoke(cli, ["integration", "slack", "stop"])
        assert stop.exit_code == 0
        assert "Stopped" in stop.output

    # After stop, status is clean again.
    assert "not running" in runner.invoke(cli, ["integration", "slack", "status"]).output


def test_slack_start_reports_immediate_exit(data_dir: Path) -> None:
    """A daemon that dies on startup fails loudly with a log tail, not a lie."""
    runner = CliRunner()
    with (
        mock.patch("omnigent.cli._slack_installed", return_value=True),
        mock.patch("omnigent.integration_daemon.subprocess.Popen") as popen,
        # Process is gone by the time confirm_alive checks.
        mock.patch.object(IntegrationDaemon, "_pid_alive", return_value=False),
        mock.patch.object(IntegrationDaemon, "read_log_tail", return_value="Traceback: boom"),
    ):
        popen.return_value.pid = 5150
        result = runner.invoke(cli, ["integration", "slack", "start"])
    assert result.exit_code != 0
    assert "exited immediately" in result.output
    assert "boom" in result.output
    # The dead record was pruned, so status is clean.
    assert "not running" in runner.invoke(cli, ["integration", "slack", "status"]).output


def test_slack_foreground_runs_subprocess(data_dir: Path) -> None:
    runner = CliRunner()
    with (
        mock.patch("omnigent.cli._slack_installed", return_value=True),
        mock.patch("omnigent.cli.subprocess.run") as run,
    ):
        run.return_value = mock.Mock(returncode=0)
        result = runner.invoke(cli, ["integration", "slack"])
    assert result.exit_code == 0
    argv = run.call_args.args[0]
    assert argv[1:] == ["-m", "omnigent_slack"]


def test_slack_foreground_refuses_when_daemon_running(data_dir: Path) -> None:
    """Bare (foreground) run refuses if a background daemon holds the socket."""
    IntegrationDaemon("slack", data_dir)._write_record(
        DaemonRecord(pid=4242, log_path="/tmp/x.log", started_at=1)
    )
    runner = CliRunner()
    with (
        mock.patch("omnigent.cli._slack_installed", return_value=True),
        mock.patch.object(IntegrationDaemon, "_pid_alive", return_value=True),
        mock.patch("omnigent.cli.subprocess.run") as run,
    ):
        result = runner.invoke(cli, ["integration", "slack"])
    assert result.exit_code != 0
    assert "already running" in result.output
    run.assert_not_called()  # never spawned a second bot


def test_slack_logs_prints_path(data_dir: Path) -> None:
    runner = CliRunner()
    # No daemon yet.
    none = runner.invoke(cli, ["integration", "slack", "logs"])
    assert "No Slack daemon" in none.output
    # With a record, prints the path.
    IntegrationDaemon("slack", data_dir)._write_record(
        DaemonRecord(pid=1, log_path="/tmp/slack.log", started_at=1)
    )
    result = runner.invoke(cli, ["integration", "slack", "logs"])
    assert result.exit_code == 0
    assert "/tmp/slack.log" in result.output


def test_integration_group_bare_shows_help(data_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["integration"])
    assert result.exit_code == 0
    assert "slack" in result.output.lower()
