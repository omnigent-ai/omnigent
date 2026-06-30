"""Tests for host CLI commands and auto-launch."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import psutil
import pytest
from click.testing import CliRunner

from omnigent.cli import _ensure_host_daemon, _host_daemon_alive, cli
from omnigent.host.local_server import LocalServerStartup


@dataclass(frozen=True)
class _SpawnedDaemon:
    """
    Minimal subprocess handle returned by patched daemon spawns.

    :param pid: Fake process id, e.g. ``4242``.
    """

    pid: int


@dataclass(frozen=True)
class _HostRun:
    """
    One captured call to the (patched) foreground daemon loop.

    :param server_url: Omnigent server URL the daemon was told to connect
        to, e.g. ``"https://from-arg.example.com"``.
    """

    server_url: str


def test_host_command_registered() -> None:
    """
    Verify that ``host`` is a registered subcommand.

    If the command is missing, the CLI wiring in cli.py is broken
    and users can't run ``omnigent host``.
    """
    runner = CliRunner()
    result = runner.invoke(cli, ["host", "--help"])
    # --help should succeed (exit code 0) and show usage.
    assert result.exit_code == 0, (
        f"Expected exit code 0 for --help, got {result.exit_code}. Output: {result.output}"
    )
    assert "server" in result.output.lower(), "Help text should mention the --server option"


def test_host_no_server_starts_local_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verify that ``host`` with no --server starts a local Omnigent server.

    Under the daemon model, ``omnigent host`` (no URL, no config) is
    valid: it starts (or reuses) a persistent local Omnigent server and connects
    the foreground daemon to it — it no longer errors. We mock the local
    server spawn and the (blocking) daemon loop so the command returns.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid")
    captured_url: list[str] = []

    def _fake_run(server_url: str, **kwargs: object) -> None:
        captured_url.append(server_url)

    with (
        # spawned=False: this test only checks URL resolution; reused keeps
        # the Ctrl-C stop-server prompt out of the picture (it has its own tests).
        patch(
            "omnigent.cli.ensure_local_omnigent_server",
            lambda: LocalServerStartup(url="http://127.0.0.1:8123", spawned=False),
        ),
        patch("omnigent.host.connect.run_host_process", _fake_run),
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["host"])

    assert result.exit_code == 0, result.output
    # The foreground daemon connects to the spawned local server's URL.
    assert captured_url == ["http://127.0.0.1:8123"]


def test_host_reads_server_from_global_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verify that ``host`` reads the server URL from global config
    when --server is not passed on the CLI.

    If it doesn't, users must always pass --server even when
    ``~/.omnigent/config.yaml`` has a ``server:`` key.
    """
    (tmp_path / "config.yaml").write_text("server: https://from-config.example.com\n")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid")

    captured_url: list[str] = []

    def _fake_run(server_url: str, **kwargs: object) -> None:
        captured_url.append(server_url)

    with patch("omnigent.host.connect.run_host_process", _fake_run):
        runner = CliRunner()
        result = runner.invoke(cli, ["host"])

    assert result.exit_code == 0, (
        f"Expected success, got {result.exit_code}. Output: {result.output}"
    )
    assert captured_url == ["https://from-config.example.com"]


def test_host_accepts_server_as_positional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verify ``host <url>`` routes the positional URL to the daemon.

    This is the new default: the server may be given positionally
    instead of via ``--server``. If the ``_HostGroup`` argument
    handling regresses, Click treats the URL as an unknown subcommand
    and the command exits non-zero — so this test fails loud.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid")
    runs: list[_HostRun] = []

    def _fake_run(server_url: str, **kwargs: object) -> None:
        runs.append(_HostRun(server_url=server_url))

    with patch("omnigent.host.connect.run_host_process", _fake_run):
        runner = CliRunner()
        result = runner.invoke(cli, ["host", "https://from-arg.example.com"])

    assert result.exit_code == 0, (
        f"Expected success, got {result.exit_code}. Output: {result.output}"
    )
    # The positional URL — not config, not local — drove the daemon loop.
    # A failure here means the positional argument was not redirected into
    # the --server param (e.g. it was dispatched as a subcommand instead).
    assert runs == [_HostRun(server_url="https://from-arg.example.com")]


def test_host_accepts_option_after_positional_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verify ``host <url> --non-interactive`` parses the trailing option.

    The positional-URL shorthand must not swallow options that follow the
    URL: ``omnigent host https://… --non-interactive`` is the scripted/CI
    form (#1428). A regression here — the URL rewrite misclassifying the
    trailing option as an extra positional — makes the command exit
    non-zero with "Unexpected extra argument(s)".
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid")
    runs: list[_HostRun] = []

    def _fake_run(server_url: str, **kwargs: object) -> None:
        runs.append(_HostRun(server_url=server_url))

    with patch("omnigent.host.connect.run_host_process", _fake_run):
        runner = CliRunner()
        result = runner.invoke(cli, ["host", "https://from-arg.example.com", "--non-interactive"])

    assert result.exit_code == 0, (
        f"Expected success, got {result.exit_code}. Output: {result.output}"
    )
    assert runs == [_HostRun(server_url="https://from-arg.example.com")]


def test_host_accepts_empty_positional_as_local_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verify ``host ""`` keeps selecting local-mode connection.

    The empty string is the only non-URL positional token allowed by the
    shorthand. It must bind as an explicit empty ``server`` value so it
    overrides configured remote defaults and starts the local Omnigent server.
    """
    (tmp_path / "config.yaml").write_text("server: https://from-config.example.com\n")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid")
    runs: list[_HostRun] = []

    def _fake_run(server_url: str, **kwargs: object) -> None:
        runs.append(_HostRun(server_url=server_url))

    with (
        # spawned=False: this test only checks the empty-string local-mode URL
        # resolution; reused keeps the Ctrl-C stop-server prompt out of scope.
        patch(
            "omnigent.cli.ensure_local_omnigent_server",
            lambda: LocalServerStartup(url="http://127.0.0.1:8123", spawned=False),
        ),
        patch("omnigent.host.connect.run_host_process", _fake_run),
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["host", ""])

    assert result.exit_code == 0, (
        f"Expected success, got {result.exit_code}. Output: {result.output}"
    )
    assert runs == [_HostRun(server_url="http://127.0.0.1:8123")]


def test_host_status_subcommand_still_dispatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verify ``host status`` still runs the status subcommand.

    The positional-URL feature must not swallow management subcommands.
    If ``status`` were treated as a server URL, the daemon loop would be
    invoked with ``server_url="status"``; this test asserts the daemon
    loop is never called and the status path is taken instead.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid")
    runs: list[_HostRun] = []
    selected_calls: list[dict[str, object]] = []

    def _fake_run(server_url: str, **kwargs: object) -> None:
        runs.append(_HostRun(server_url=server_url))

    def _fake_selected(**kwargs: object) -> list[object]:
        selected_calls.append(kwargs)
        return []

    with (
        patch("omnigent.host.connect.run_host_process", _fake_run),
        patch("omnigent.cli._selected_daemon_records", _fake_selected),
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["host", "status"])

    assert result.exit_code == 0, (
        f"Expected success, got {result.exit_code}. Output: {result.output}"
    )
    # The status subcommand ran (its record selector was invoked)...
    assert len(selected_calls) == 1, (
        f"status should call _selected_daemon_records once, got {len(selected_calls)}. "
        f"If 0, 'status' was misrouted as a server URL instead of a subcommand."
    )
    # ...and the daemon loop was NOT invoked with 'status' as a URL.
    assert runs == [], (
        f"run_host_process must not run for 'host status', but got {runs}. "
        f"A non-empty list means 'status' was treated as a positional server URL."
    )


def test_host_rejects_unknown_plain_token_as_subcommand(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verify plain unknown tokens are not accepted as positional URLs.

    The positional server shorthand is only for URL-like values. A token
    such as ``"sessions"`` is more likely a removed or misspelled
    subcommand, so Click must report it as an unknown command instead of
    starting the foreground daemon with ``server_url="sessions"``.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid")
    runs: list[_HostRun] = []

    def _fake_run(server_url: str, **kwargs: object) -> None:
        runs.append(_HostRun(server_url=server_url))

    with patch("omnigent.host.connect.run_host_process", _fake_run):
        runner = CliRunner()
        result = runner.invoke(cli, ["host", "sessions"])

    assert result.exit_code != 0, f"Expected an unknown command error. Output: {result.output}"
    assert "No such command 'sessions'" in result.output
    assert runs == [], "run_host_process must not run for an unknown plain token"


def test_host_rejects_positional_and_server_option_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verify passing both a positional URL and ``--server`` is an error.

    Supplying the server two ways is ambiguous; ``_HostGroup`` raises
    a usage error rather than silently picking one. If the guard
    regresses, one value would silently win and this test fails.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid")
    runs: list[_HostRun] = []

    def _fake_run(server_url: str, **kwargs: object) -> None:
        runs.append(_HostRun(server_url=server_url))

    with patch("omnigent.host.connect.run_host_process", _fake_run):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["host", "--server", "https://a.example.com", "https://b.example.com"],
        )

    # Usage errors exit non-zero; the daemon loop must never start.
    assert result.exit_code != 0, f"Expected a usage error, got success. Output: {result.output}"
    assert "not both" in result.output, (
        f"Expected the conflict message mentioning 'not both', got: {result.output}"
    )
    assert runs == [], "run_host_process must not run when the args are rejected"


def test_host_daemon_alive_returns_false_when_no_pid_file(
    tmp_path: Path,
) -> None:
    """
    Verify _host_daemon_alive returns False when there's no PID file.

    If it returns True, the auto-launch would skip spawning a
    daemon even on a fresh machine.
    """
    with patch("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid"):
        assert _host_daemon_alive() is False


def test_host_daemon_alive_returns_false_for_dead_pid(
    tmp_path: Path,
) -> None:
    """
    Verify _host_daemon_alive returns False when the PID file
    points to a dead process.

    If it returns True for a stale PID, the daemon would never
    restart after a crash.
    """
    pid_path = tmp_path / "host.pid"
    # PID 99999999 almost certainly doesn't exist.
    pid_path.write_text("99999999\nhttp://localhost:8000\n")
    with patch("omnigent.cli._HOST_PID_PATH", pid_path):
        assert _host_daemon_alive() is False


def test_ensure_host_daemon_writes_pid_file(
    tmp_path: Path,
) -> None:
    """
    Verify _ensure_host_daemon spawns a subprocess and writes its
    PID to the PID file.

    If the PID file is empty after the call, the subprocess spawn
    or PID write is broken.
    """
    pid_path = tmp_path / "host.pid"

    import subprocess

    spawned_pids: list[int] = []
    original_popen = subprocess.Popen

    def _fake_popen(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        """Spawn a no-op process and record its PID.

        :param args: Command args (ignored).
        :param kwargs: Popen kwargs.
        :returns: A real subprocess handle.
        """
        proc = original_popen(
            ["sleep", "10"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        spawned_pids.append(proc.pid)
        return proc

    with (
        patch("omnigent.cli._HOST_PID_PATH", pid_path),
        patch("omnigent.cli.subprocess.Popen", side_effect=_fake_popen),
    ):
        _ensure_host_daemon("http://localhost:8000")

    assert pid_path.exists(), "PID file should be created"
    lines = pid_path.read_text().strip().splitlines()
    # Two lines: PID and server URL.
    assert len(lines) == 2, f"PID file should have 2 lines (PID + server URL), got {len(lines)}"
    pid = int(lines[0])
    assert pid in spawned_pids, (
        f"PID file should contain the spawned process PID, "
        f"got {pid}, expected one of {spawned_pids}"
    )
    assert lines[1] == "http://localhost:8000", "PID file should contain the server URL on line 2"

    # Clean up the spawned sleep process.
    import contextlib
    import os
    import signal

    for p in spawned_pids:
        with contextlib.suppress(OSError):
            os.kill(p, signal.SIGTERM)


def test_ensure_host_daemon_keeps_old_for_different_server(
    tmp_path: Path,
) -> None:
    """
    Verify _ensure_host_daemon keeps an existing daemon for another server.

    Multi-target daemon management allows one daemon per server target.
    Starting a new target must not kill or overwrite the existing
    per-target registry record.
    """
    pid_path = tmp_path / "host.pid"
    spawned_pids = [4242, 4243]
    killed: list[int] = []

    def _fake_popen(args: list[str], **kwargs: object) -> _SpawnedDaemon:
        """Return a fake daemon process for the requested target.

        :param args: Command args.
        :param kwargs: Popen kwargs.
        :returns: Fake daemon process handle.
        """
        del args, kwargs
        return _SpawnedDaemon(pid=spawned_pids.pop(0))

    with (
        patch("omnigent.cli._HOST_PID_PATH", pid_path),
        patch("omnigent.cli._pid_alive", lambda pid: pid in {4242, 4243}),
        patch("omnigent.cli.os.kill", lambda pid, sig: killed.append(pid)),
        patch("omnigent.cli.subprocess.Popen", side_effect=_fake_popen),
    ):
        _ensure_host_daemon("http://old-server:8000")
        _ensure_host_daemon("http://new-server:9000")

    registry_files = list((tmp_path / "daemons").glob("*.json"))
    registry_text = "\n".join(path.read_text() for path in registry_files)

    assert killed == []
    assert len(registry_files) == 2
    assert "http://old-server:8000" in registry_text
    assert "http://new-server:9000" in registry_text

    # The legacy pidfile points at the most recently spawned daemon, while
    # per-target JSON records keep both targets inspectable.
    lines = pid_path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert lines[1] == "http://new-server:9000"


def test_ensure_host_daemon_skips_if_alive(
    tmp_path: Path,
) -> None:
    """
    Verify _ensure_host_daemon does not spawn a second daemon when
    one is already running for the same server.

    If a second process is spawned, the user would end up with
    duplicate host registrations.
    """
    import os

    pid_path = tmp_path / "host.pid"
    # Use our own PID (known to be alive) with the same server.
    pid_path.write_text(f"{os.getpid()}\nhttp://localhost:8000\n")

    spawn_count = 0
    original_popen = subprocess.Popen

    def _counting_popen(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        """Count spawn attempts.

        :param args: Command args.
        :param kwargs: Popen kwargs.
        :returns: A real subprocess handle.
        """
        nonlocal spawn_count
        spawn_count += 1
        return original_popen(args, **kwargs)

    with (
        patch("omnigent.cli._HOST_PID_PATH", pid_path),
        patch("omnigent.cli.subprocess.Popen", side_effect=_counting_popen),
    ):
        _ensure_host_daemon("http://localhost:8000")

    # No process should have been spawned.
    assert spawn_count == 0, "Should not spawn a daemon when one is already alive"


def test_host_stop_treats_zombie_daemon_as_dead(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verify ``host stop`` succeeds when the recorded daemon pid is a zombie.

    A daemon process that dies while its parent never reaps it stays in
    the process table as a zombie. Zombies cannot be signalled, so a
    liveness check that treats them as alive makes ``host stop`` (even
    with ``--force``) fail forever with "did not exit" and blocks every
    subsequent ``host`` start with "already running".
    """
    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid")

    zombie_pid = os.fork()
    if zombie_pid == 0:
        os._exit(0)
    # Wait for the child to exit WITHOUT reaping it, so it stays in the
    # process table as a zombie. os.waitid(..., WNOWAIT) would block until
    # exactly that state but is unavailable on macOS Python builds, so poll
    # the process status with a deadline — the child runs no code, so the
    # transition happens within microseconds.
    deadline = time.monotonic() + 30.0
    while psutil.Process(zombie_pid).status() != psutil.STATUS_ZOMBIE:
        if time.monotonic() > deadline:
            pytest.fail("forked child never became a zombie — test setup is broken")
    try:
        daemons_dir = tmp_path / "daemons"
        daemons_dir.mkdir()
        # The file name must match _daemon_record_path's derivation
        # (sha256 of the target, truncated) or stop's record cleanup
        # would unlink a different path than the one written here.
        record_path = daemons_dir / (hashlib.sha256(b"local").hexdigest()[:16] + ".json")
        record_path.write_text(
            json.dumps(
                {
                    "pid": zombie_pid,
                    "target": "local",
                    "mode": "local",
                    "server_url": None,
                    "log_path": None,
                    "started_at": 1781200000,
                    "host_id": "host_zombie_test",
                    "resolved_server_url": None,
                    "config_sig": None,
                }
            )
        )

        runner = CliRunner()
        # --daemon-only skips the session-stop HTTP calls; the daemon
        # process termination path is what the zombie bug breaks.
        result = runner.invoke(cli, ["host", "stop", "--all", "--daemon-only"])
    finally:
        # Reap the zombie so it doesn't outlive the test.
        os.waitpid(zombie_pid, 0)

    # Exit code 0 proves the zombie was treated as dead. A nonzero exit
    # ("Daemon ... did not exit; retry with --force.") means the
    # liveness check saw the unkillable zombie as a live daemon.
    assert result.exit_code == 0, f"host stop failed for a zombie daemon pid: {result.output}"
    # The stale registry record must be deleted, otherwise the next
    # ``host`` start still sees a conflicting "running" daemon.
    assert not record_path.exists(), (
        "stale daemon record survived stop — a subsequent host start "
        "would be blocked by an 'already running' conflict"
    )
