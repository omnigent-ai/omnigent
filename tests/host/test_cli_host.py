"""Tests for host CLI commands and auto-launch."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import click
import psutil
import pytest
from click.testing import CliRunner

from omnigent import cli as cli_module
from omnigent.cli import _add_daemon_host_status, _ensure_host_daemon, _host_daemon_alive, cli
from omnigent.cli_diagnostics import suppresses_recovery_hint
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


def _persist_fake_daemon_claim(
    target: str,
    spawned: object,
    **_kwargs: object,
) -> object:
    """Simulate the child-side registry claim for patched daemon spawns."""
    assert isinstance(spawned, cli_module._SpawnedDaemonProcess)
    mode = "local" if target == "local" else "server"
    cli_module._write_daemon_record(
        cli_module._HostDaemonRecord(
            pid=spawned.pid,
            target=target,
            mode=mode,
            server_url=None if mode == "local" else target,
            log_path=spawned.log_path,
            started_at=int(time.time()),
            host_id=cli_module._load_existing_host_id(),
            config_sig="test-config-signature",
        )
    )
    cli_module._HOST_PID_PATH.write_text(f"{spawned.pid}\n{target}\n")
    return cli_module._find_daemon_record(target)


def test_host_pid_path_honors_data_dir_at_import(tmp_path: Path) -> None:
    env = {**os.environ, "OMNIGENT_DATA_DIR": str(tmp_path / "data")}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from omnigent.cli import _HOST_PID_PATH; print(_HOST_PID_PATH)",
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == str(tmp_path / "data" / "host.pid")


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


def test_host_enable_subcommand_installs_user_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify ``host enable`` resolves the target and installs its service."""
    from omnigent.host.service import HostService

    captured: list[tuple[str | None, dict[str, str]]] = []
    service_path = tmp_path / "omnigent-host.service"

    def _enable(
        server_url: str | None,
        *,
        environment: dict[str, str],
    ) -> HostService:
        captured.append((server_url, environment))
        return HostService(kind="systemd_user", path=service_path, label=service_path.name)

    monkeypatch.setattr("omnigent.cli._find_daemon_record", lambda target: None)
    monkeypatch.setattr(
        "omnigent.cli._build_host_daemon_env",
        lambda *, server_url: {"HOME": str(tmp_path)},
    )
    monkeypatch.setattr("omnigent.host.service.enable_user_host_service", _enable)

    result = CliRunner().invoke(cli, ["host", "enable", "--server", ""])

    assert result.exit_code == 0, result.output
    assert captured == [(None, {"HOME": str(tmp_path)})]
    assert "Enabled the Omnigent host user service for local" in result.output


def _record_for(target: str) -> cli_module._HostDaemonRecord:
    """Build a live-looking daemon record for ``target``."""
    return cli_module._HostDaemonRecord(
        pid=4242,
        target=target,
        mode="server",
        server_url=target,
        log_path=None,
        started_at=1,
    )


@pytest.mark.parametrize(
    ("args", "listings", "waits"),
    [
        pytest.param([], [2, 1, 0], True, id="waits-for-running-turns"),
        pytest.param(["--force"], [2], False, id="force-replaces-now"),
        pytest.param([], [None], False, id="unreadable-listing-does-not-block"),
    ],
)
def test_host_enable_lets_running_turns_finish_before_replacing_the_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    listings: list[int | None],
    waits: bool,
) -> None:
    """Re-enabling the service replaces a live daemon only once its turns finish."""
    from omnigent.host.service import HostService

    target = "https://server.example.com"
    events: list[str] = []
    remaining = list(listings)

    def _sessions(
        record: cli_module._HostDaemonRecord, *, connected_only: bool = False
    ) -> cli_module._DaemonSessionsResult:
        assert record.target == target
        assert connected_only is True
        running = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        events.append(f"list:{running}")
        if running is None:
            return cli_module._DaemonSessionsResult(base_url=target, sessions=[], error="boom")
        sessions = [{"id": f"conv_{index}", "status": "running"} for index in range(running)]
        return cli_module._DaemonSessionsResult(
            base_url=target,
            sessions=[*sessions, {"id": "conv_idle", "status": "idle"}],
            error=None,
        )

    def _terminate(record: cli_module._HostDaemonRecord, *, force: bool) -> str:
        assert force is False
        events.append("terminate")
        return cli_module._STOP_TERMINATED

    def _enable(server_url: str | None, *, environment: dict[str, str]) -> HostService:
        del environment
        events.append("enable")
        service_path = tmp_path / "omnigent-host.service"
        return HostService(kind="systemd_user", path=service_path, label=service_path.name)

    monkeypatch.setattr("omnigent.cli._resolve_host_server", lambda server: target)
    monkeypatch.setattr("omnigent.cli._ensure_databricks_server_auth", lambda *a, **k: None)
    monkeypatch.setattr("omnigent.cli._find_daemon_record", _record_for)
    monkeypatch.setattr("omnigent.cli._daemon_owner_is_live", lambda record: True)
    monkeypatch.setattr("omnigent.cli._sessions_for_daemon", _sessions)
    monkeypatch.setattr("omnigent.cli._terminate_daemon", _terminate)
    monkeypatch.setattr("omnigent.cli._UPGRADE_DRAIN_POLL_S", 0.0)
    monkeypatch.setattr(
        "omnigent.cli._build_host_daemon_env",
        lambda *, server_url: {"HOME": str(tmp_path)},
    )
    monkeypatch.setattr("omnigent.host.service.enable_user_host_service", _enable)

    result = CliRunner().invoke(cli, ["host", "enable", "--server", target, *args])

    assert result.exit_code == 0, result.output
    if waits:
        assert events == ["list:2", "list:1", "list:0", "terminate", "enable"]
        assert "Waiting for 2 running session(s) to finish" in result.output
        assert "--force to replace the host now" in result.output
        assert "1 session(s) still running" in result.output
    elif args == ["--force"]:
        assert events == ["terminate", "enable"]
    else:
        assert events == ["list:None", "terminate", "enable"]
        assert "Waiting for" not in result.output


def test_host_enable_does_not_wait_on_a_dead_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead daemon's stale running rows can't finish, so enable replaces it at once."""
    from omnigent.host.service import HostService

    target = "https://server.example.com"
    terminated: list[str] = []
    monkeypatch.setattr("omnigent.cli._resolve_host_server", lambda server: target)
    monkeypatch.setattr("omnigent.cli._ensure_databricks_server_auth", lambda *a, **k: None)
    monkeypatch.setattr("omnigent.cli._find_daemon_record", _record_for)
    monkeypatch.setattr("omnigent.cli._daemon_owner_is_live", lambda record: False)
    monkeypatch.setattr(
        "omnigent.cli._sessions_for_daemon",
        lambda record, *, connected_only=False: pytest.fail("listed a dead daemon's sessions"),
    )
    monkeypatch.setattr(
        "omnigent.cli._terminate_daemon",
        lambda record, *, force: terminated.append(record.target) or cli_module._STOP_TERMINATED,
    )
    monkeypatch.setattr(
        "omnigent.cli._build_host_daemon_env",
        lambda *, server_url: {"HOME": str(tmp_path)},
    )
    service_path = tmp_path / "omnigent-host.service"
    monkeypatch.setattr(
        "omnigent.host.service.enable_user_host_service",
        lambda server_url, *, environment: HostService(
            kind="systemd_user", path=service_path, label=service_path.name
        ),
    )

    result = CliRunner().invoke(cli, ["host", "enable", "--server", target])

    assert result.exit_code == 0, result.output
    assert terminated == [target]


def test_host_disable_subcommand_removes_user_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify ``host disable`` dispatches to the service remover."""
    from omnigent.host.service import HostService

    service_path = tmp_path / "omnigent-host.service"
    removed: list[bool] = []

    def _disable() -> HostService:
        removed.append(True)
        return HostService(kind="systemd_user", path=service_path, label=service_path.name)

    monkeypatch.setattr("omnigent.host.service.disable_user_host_service", _disable)
    monkeypatch.setattr("omnigent.cli._list_daemon_records", list)

    result = CliRunner().invoke(cli, ["host", "disable"])

    assert result.exit_code == 0, result.output
    assert removed == [True]
    assert "Disabled the Omnigent host user service" in result.output


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
        patch("omnigent.cli._wait_for_daemon_claim", side_effect=_persist_fake_daemon_claim),
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
        patch("omnigent.cli._pid_is_recorded_daemon", lambda record: True),
        patch("omnigent.cli._pid_alive", lambda pid: pid in {4242, 4243}),
        patch("omnigent.cli.os.kill", lambda pid, sig: killed.append(pid)),
        patch("omnigent.cli.subprocess.Popen", side_effect=_fake_popen),
        patch("omnigent.cli._wait_for_daemon_claim", side_effect=_persist_fake_daemon_claim),
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


def test_host_stop_drops_stale_foreign_daemon_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verify ``host stop`` drops a daemon record whose pid it cannot signal.

    A registry record can outlive its daemon: the pid may be reused by
    another user's process, or the daemon was started under a different
    account. ``_pid_alive`` reports such a pid as alive (psutil maps the
    EPERM to ``AccessDenied``), so stop falls through to ``os.kill``, which
    raises ``PermissionError``. Stop must treat the record as stale — warn
    and delete it — instead of crashing on the EPERM, even with ``--force``.
    """
    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid")

    daemons_dir = tmp_path / "daemons"
    daemons_dir.mkdir()
    # The file name must match _daemon_record_path's derivation (sha256 of
    # the target, truncated) or stop's record cleanup would unlink a
    # different path than the one written here.
    record_path = daemons_dir / (hashlib.sha256(b"local").hexdigest()[:16] + ".json")
    record_path.write_text(
        json.dumps(
            {
                "pid": 4242,
                "target": "local",
                "mode": "local",
                "server_url": None,
                "log_path": None,
                "started_at": 1781200000,
                "host_id": "host_foreign_test",
                "resolved_server_url": None,
                "config_sig": None,
            }
        )
    )

    def _eperm_kill(pid: int, sig: int) -> None:
        """Simulate signalling a pid owned by another user (EPERM)."""
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr("omnigent.cli._pid_alive", lambda pid: True)
    # Identity is not what this test exercises — force the os.kill EPERM path.
    monkeypatch.setattr("omnigent.cli._pid_is_recorded_daemon", lambda record: True)
    monkeypatch.setattr("omnigent.cli.os.kill", _eperm_kill)

    runner = CliRunner()
    result = runner.invoke(cli, ["host", "stop", "--all", "--daemon-only", "--force"])

    # Exit code 0 proves the EPERM was treated as a stale record. A nonzero
    # exit means the PermissionError propagated out of os.kill and crashed
    # the CLI before the SIGKILL path --force is supposed to reach.
    assert result.exit_code == 0, f"host stop crashed on a foreign daemon pid: {result.output}"
    assert "owned by another user" in result.output
    assert not record_path.exists(), (
        "stale foreign daemon record survived stop — a subsequent host start "
        "would be blocked by an 'already running' conflict"
    )


def test_add_daemon_host_status_skips_http_for_dead_process() -> None:
    """Dead processes must not trigger a network round-trip.

    ``_add_daemon_host_status`` is called for every daemon record, including
    the many stale records accumulated over development sessions. Before this
    fix each dead record caused a ``GET /v1/hosts/{id}`` call that hit
    ConnectError or a remote timeout, making ``omni host status`` slow.
    A dead process cannot have an online tunnel, so the correct answer is
    ``host_status = "offline"`` with no HTTP request made.
    """
    http_calls: list[str] = []

    def _fake_http(*args: object, **kwargs: object) -> object:
        http_calls.append(str(kwargs))
        raise AssertionError("HTTP must not be called for a dead process")

    payload = {
        "process": "offline",
        "server_url": "https://example.com",
        "host_id": "host_abc123",
        "host_status": None,
        "error": None,
    }
    with patch("omnigent.cli._host_http_json", _fake_http):
        _add_daemon_host_status(payload)

    assert payload["host_status"] == "offline"
    assert payload["error"] is None
    assert http_calls == [], "No HTTP calls expected for a dead process"


def test_host_http_json_handles_remote_headers_oserror() -> None:
    """Auth resolution errors inside _host_http_json must return status_code=0.

    _remote_headers() does file I/O and SDK calls that can raise OSError.
    Before the fix the cache-populating call was outside the try/except, so
    such a failure propagated unhandled (and aborted the whole host status
    listing when running under ThreadPoolExecutor). It must be caught and
    converted to a graceful _HostHttpResult(status_code=0) like any other
    transport failure.
    """
    from omnigent.cli import _host_http_headers_cache, _host_http_json

    # Clear cache so the resolution path is exercised.
    url = "https://oserror-test.example.com"
    _host_http_headers_cache.pop(url, None)

    def _raise_oserror(*_args: object, **_kwargs: object) -> object:
        raise OSError("credential file not found")

    with patch("omnigent.chat._remote_headers", _raise_oserror):
        result = _host_http_json(base_url=url, method="GET", path="/v1/test")

    assert result.status_code == 0
    assert "OSError" in result.body if isinstance(result.body, str) else True
    # Ensure we didn't cache the failed result.
    assert url not in _host_http_headers_cache


# ── host --background ─────────────────────────────────────────────


def _patch_background_host_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    pid: int = 4242,
) -> tuple[list[list[str]], Path]:
    """
    Isolate ``host --background`` from real daemon spawns and log files.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Test-scoped directory used as the config home.
    :param pid: Fake pid the stub spawn reports, e.g. ``4242``.
    :returns: The recorded spawn argv list and the fake daemon log path.
    """
    from omnigent.cli import _SpawnedDaemonProcess

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid")
    # No fixed grace: the stubbed pid is trivially "alive", so waiting for it
    # only slows the test down.
    monkeypatch.setattr("omnigent.cli._BACKGROUND_HOST_GRACE_S", 0.0)
    monkeypatch.setattr(
        "omnigent.cli._pid_is_recorded_daemon",
        lambda record: cli_module._pid_alive(record.pid),
    )
    monkeypatch.setattr("omnigent.cli._pid_alive", lambda checked: checked == pid)
    monkeypatch.setattr("omnigent.cli._daemon_host_online", lambda record, **kwargs: True)
    monkeypatch.setattr(
        "omnigent.cli._daemon_host_status_probe",
        lambda record, **kwargs: cli_module._HostHttpResult(
            status_code=200, body={"status": "online"}
        ),
    )
    # Local mode waits for the server the daemon owns; no real server here.
    monkeypatch.setattr("omnigent.cli._discover_local_server_url", lambda: "http://127.0.0.1:6767")
    log_path = tmp_path / "host-test.log"
    log_path.write_text("")
    spawned_args: list[list[str]] = []

    def _fake_spawn(*, args: list[str], env: dict[str, str]) -> _SpawnedDaemonProcess:
        """Record the daemon argv instead of spawning a process.

        :param args: Daemon process argv.
        :param env: Daemon environment (ignored).
        :returns: Fake spawned-process metadata.
        """
        del env
        spawned_args.append(args)
        return _SpawnedDaemonProcess(pid=pid, log_path=str(log_path))

    monkeypatch.setattr("omnigent.cli._spawn_host_daemon_process", _fake_spawn)
    monkeypatch.setattr("omnigent.cli._wait_for_daemon_claim", _persist_fake_daemon_claim)
    return spawned_args, log_path


def test_host_background_spawns_detached_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``host --background`` spawns the detached daemon and reports pid + log.

    A detached daemon prints nothing to the terminal, so the pid and log
    path are the only handles the user gets on it.
    """
    spawned_args, log_path = _patch_background_host_spawn(monkeypatch, tmp_path)

    result = CliRunner().invoke(cli, ["host", "--background"])

    assert result.exit_code == 0, result.output
    assert "pid 4242" in result.output
    assert log_path.name in result.output
    # `--server` was omitted, so the stop hint omits it too — a bare `host
    # stop` resolves its target exactly like the bare `host` that started it.
    assert "omnigent host stop" in result.output
    assert "--server" not in result.output
    # Bare `--background` is local mode: the daemon owns the local server, so
    # its URL is reported too (the Web UI is otherwise unreachable).
    assert spawned_args and "--local" in spawned_args[0]
    assert "server: http://127.0.0.1:6767" in result.output


def test_host_background_fails_when_daemon_never_registers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live PID without a registered channel must not be reported as started."""
    _spawned, log_path = _patch_background_host_spawn(monkeypatch, tmp_path)
    log_path.write_text("Host registration failed: database unavailable\n")
    monkeypatch.setattr("omnigent.cli._daemon_host_online", lambda record, **kwargs: False)
    # The server answers (host row offline), so the failure is the generic
    # registration timeout rather than the unreachable-server error.
    monkeypatch.setattr(
        "omnigent.cli._daemon_host_status_probe",
        lambda record, **kwargs: cli_module._HostHttpResult(
            status_code=200, body={"status": "offline"}
        ),
    )
    monkeypatch.setattr("omnigent.cli._BACKGROUND_HOST_REGISTRATION_GRACE_S", 0.0)
    monkeypatch.setattr(
        "omnigent.cli._ensure_databricks_server_auth", lambda *args, **kwargs: None
    )
    terminated: list[int] = []
    monkeypatch.setattr(
        "omnigent.cli._terminate_daemon",
        lambda record, *, force: terminated.append(record.pid),
    )

    result = CliRunner().invoke(
        cli,
        ["host", "--background", "--server", "https://example.databricksapps.com"],
    )

    assert result.exit_code != 0
    assert "did not register with the server" in result.output
    assert "database unavailable" in result.output
    assert "Started the host daemon" not in result.output
    assert terminated == [4242]


def test_host_background_does_not_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``host --background`` must not run the foreground daemon loop.

    The whole point of the flag is that the blocking ``run_host_process``
    loop (and the in-process local-server bring-up that precedes it) moves
    into the detached child.
    """
    _patch_background_host_spawn(monkeypatch, tmp_path)
    foreground_runs: list[str] = []

    def _fail_local_server() -> LocalServerStartup:
        raise AssertionError("--background must not start the local server in-process")

    with (
        patch(
            "omnigent.host.connect.run_host_process",
            lambda server_url, **kwargs: foreground_runs.append(server_url),
        ),
        patch("omnigent.cli.ensure_local_omnigent_server", _fail_local_server),
    ):
        result = CliRunner().invoke(cli, ["host", "--background"])

    assert result.exit_code == 0, result.output
    assert foreground_runs == []


def test_host_background_reuses_running_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``host --background`` adopts a healthy daemon instead of double-spawning.

    Two daemons for one target would register the same machine twice; the
    command reports the existing pid and returns.
    """
    from omnigent.cli import (
        _LOCAL_DAEMON_MARKER,
        _HostDaemonRecord,
        _write_daemon_record,
        server_config_signature,
    )

    spawned_args, _ = _patch_background_host_spawn(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "omnigent.cli._pid_is_recorded_daemon",
        lambda record: cli_module._pid_alive(record.pid),
    )
    monkeypatch.setattr("omnigent.cli._pid_alive", lambda checked: checked in {4242, 5150})
    _write_daemon_record(
        _HostDaemonRecord(
            pid=5150,
            target=_LOCAL_DAEMON_MARKER,
            mode="local",
            server_url=None,
            log_path=str(tmp_path / "existing.log"),
            # Fresh record: young daemons skip the tunnel-health probe.
            started_at=int(time.time()),
            # No host_id: the tmp config home has no identity file, and a
            # mismatch would tear the "running" daemon down as stale.
            host_id=None,
            config_sig=server_config_signature(),
        )
    )

    result = CliRunner().invoke(cli, ["host", "--background", "--server", ""])

    assert result.exit_code == 0, result.output
    assert "already running (pid 5150" in result.output
    assert "server: http://127.0.0.1:6767" in result.output
    # Local mode was requested explicitly, so the stop hint says so too.
    assert 'omnigent host stop --server ""' in result.output
    assert spawned_args == [], "a healthy daemon must not be respawned"


def test_recycled_pid_record_is_pruned_and_host_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record whose pid now names a foreign process is stale, not a conflict.

    After a reboot the kernel recycles low pids into system daemons; a bare
    existence check made the host refuse to start forever with "already
    running (pid=724)" while the pid belonged to, e.g., root's
    wifianalyticsd. With identity validation the record is pruned and the
    foreground host claims its target normally.
    """
    from omnigent.cli import (
        _claim_foreground_daemon_record,
        _daemon_record_path,
        _HostDaemonRecord,
        _read_daemon_record,
        _write_daemon_record,
        server_config_signature,
    )

    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid")
    monkeypatch.setattr("omnigent.cli._pid_alive", lambda pid: True)
    # Alive but NOT the daemon we recorded: the recycled-pid signature.
    monkeypatch.setattr("omnigent.cli._pid_is_recorded_daemon", lambda record: False)

    stale = _HostDaemonRecord(
        pid=724,
        target="https://omnigent.example.com",
        mode="server",
        server_url="https://omnigent.example.com",
        log_path=str(tmp_path / "old.log"),
        started_at=1_787_152_783,
        host_id=None,
        config_sig=server_config_signature(),
    )
    _write_daemon_record(stale)

    fresh = _HostDaemonRecord(
        pid=os.getpid(),
        target="https://omnigent.example.com",
        mode="server",
        server_url="https://omnigent.example.com",
        log_path=str(tmp_path / "new.log"),
        started_at=int(time.time()),
        host_id=None,
        config_sig=server_config_signature(),
    )
    # Without identity validation this raises "A host daemon is already
    # running (pid=724)"; with it the stale record is pruned, the claim
    # proceeds, and no stale "previous" is handed back for later restore.
    previous = _claim_foreground_daemon_record(fresh)
    assert previous is None
    current = _read_daemon_record(_daemon_record_path(fresh.target))
    assert current is not None and current.pid == fresh.pid


def test_terminate_daemon_recycled_pid_discards_record_without_signalling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Terminating a recycled-pid record drops it and never signals the pid.

    The recorded pid may now belong to an innocent process (even one owned
    by this user, which os.kill WOULD terminate). Stop must warn, drop the
    stale record, and continue — killing nothing and not crashing.
    """
    from omnigent.cli import (
        _daemon_record_path,
        _HostDaemonRecord,
        _read_daemon_record,
        _terminate_daemon,
        _write_daemon_record,
        server_config_signature,
    )

    record = _HostDaemonRecord(
        pid=724,
        target="https://omnigent.example.com",
        mode="server",
        server_url="https://omnigent.example.com",
        log_path=str(tmp_path / "log"),
        started_at=1_787_152_783,
        host_id=None,
        config_sig=server_config_signature(),
    )
    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid")
    _write_daemon_record(record)

    monkeypatch.setattr("omnigent.cli._pid_alive", lambda pid: True)
    monkeypatch.setattr("omnigent.cli._pid_is_recorded_daemon", lambda rec: False)

    def _forbidden_kill(pid: int, sig: int) -> None:
        raise AssertionError(f"os.kill({pid}, {sig}) reached for a recycled pid")

    monkeypatch.setattr("omnigent.cli.os.kill", _forbidden_kill)

    _terminate_daemon(record, force=False)

    # The stale record is gone — the operator's next start claims cleanly.
    assert _read_daemon_record(_daemon_record_path(record.target)) is None
    assert "is not this daemon" in capsys.readouterr().err


def test_terminate_daemon_permission_error_discards_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """EPERM from os.kill proves the pid is not ours: warn and drop the record.

    Last-resort guard for when identity validation can't rule the pid out
    (e.g. an unreadable creation time) but the signal is refused.
    """
    from omnigent.cli import (
        _daemon_record_path,
        _HostDaemonRecord,
        _read_daemon_record,
        _terminate_daemon,
        _write_daemon_record,
        server_config_signature,
    )

    record = _HostDaemonRecord(
        pid=4242,
        target="https://omnigent.example.com",
        mode="server",
        server_url="https://omnigent.example.com",
        log_path=str(tmp_path / "log"),
        started_at=1_787_152_783,
        host_id=None,
        config_sig=server_config_signature(),
    )
    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid")
    _write_daemon_record(record)

    monkeypatch.setattr("omnigent.cli._pid_alive", lambda pid: True)
    monkeypatch.setattr("omnigent.cli._pid_is_recorded_daemon", lambda rec: True)

    def _eperm_kill(pid: int, sig: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr("omnigent.cli.os.kill", _eperm_kill)

    _terminate_daemon(record, force=False)

    assert _read_daemon_record(_daemon_record_path(record.target)) is None
    assert "owned by another user" in capsys.readouterr().err


def test_pid_identity_survives_slow_daemon_startup() -> None:
    """A record written well after its process spawned is still that process.

    A foreground host can spend minutes between process creation and the
    registry claim (imports, server probing, interactive sign-in), so the
    identity check must be one-sided: creation before ``started_at`` is
    always ours, no matter how much earlier.
    """
    from omnigent.cli import _HostDaemonRecord, _pid_is_recorded_daemon

    created = psutil.Process(os.getpid()).create_time()
    record = _HostDaemonRecord(
        pid=os.getpid(),
        target="local",
        mode="local",
        server_url=None,
        log_path=None,
        started_at=int(created) + 300,
        host_id=None,
        config_sig=None,
    )
    assert _pid_is_recorded_daemon(record) is True


def test_pid_identity_rejects_pid_created_after_record() -> None:
    """A process created long after the record's start time is a recycled pid.

    Uses the real current process against a record whose ``started_at``
    predates it — the post-reboot signature, where the original daemon died
    and the kernel handed its pid to a fresh process.
    """
    from omnigent.cli import _HostDaemonRecord, _pid_is_recorded_daemon

    created = psutil.Process(os.getpid()).create_time()
    record = _HostDaemonRecord(
        pid=os.getpid(),
        target="local",
        mode="local",
        server_url=None,
        log_path=None,
        started_at=int(created) - 3600,
        host_id=None,
        config_sig=None,
    )
    assert _pid_is_recorded_daemon(record) is False


def test_pid_identity_legacy_record_without_start_time_is_alive_only() -> None:
    """Records lacking a real ``started_at`` fall back to bare liveness."""
    from omnigent.cli import _HostDaemonRecord, _pid_is_recorded_daemon

    record = _HostDaemonRecord(
        pid=os.getpid(),
        target="local",
        mode="local",
        server_url=None,
        log_path=None,
        started_at=0,
        host_id=None,
        config_sig=None,
    )
    assert _pid_is_recorded_daemon(record) is True


def test_host_status_reports_recycled_pid_daemon_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Status must not call a recycled pid an online daemon process."""
    from omnigent.cli import _base_daemon_status_payload, _HostDaemonRecord

    monkeypatch.setattr("omnigent.cli._pid_alive", lambda pid: True)
    monkeypatch.setattr("omnigent.cli._pid_is_recorded_daemon", lambda record: False)
    record = _HostDaemonRecord(
        pid=724,
        target="https://omnigent.example.com",
        mode="server",
        server_url="https://omnigent.example.com",
        log_path=None,
        started_at=1_787_152_783,
        host_id="host_abc",
        config_sig=None,
    )
    assert _base_daemon_status_payload(record)["process"] == "offline"


def test_host_background_signs_in_before_spawning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Sign-in for a remote server happens in the foreground, before the spawn.

    The detached daemon has no terminal to run the browser login on, so an
    un-authed Databricks-fronted target must be authenticated by this
    invocation first — and ``--non-interactive`` must still reach that check.
    """
    spawned_args, _ = _patch_background_host_spawn(monkeypatch, tmp_path)
    auth_calls: list[tuple[str, bool]] = []

    def _fake_auth(server: str, *, non_interactive: bool = False) -> None:
        """Record the sign-in pre-flight.

        :param server: Server URL being authenticated.
        :param non_interactive: Whether prompting is suppressed.
        """
        assert spawned_args == [], "sign-in must precede the daemon spawn"
        auth_calls.append((server, non_interactive))

    monkeypatch.setattr("omnigent.cli._ensure_databricks_server_auth", _fake_auth)

    result = CliRunner().invoke(
        cli, ["host", "--background", "--server", "https://example.databricksapps.com"]
    )

    assert result.exit_code == 0, result.output
    assert auth_calls == [("https://example.databricksapps.com", False)]
    assert spawned_args and "--server" in spawned_args[0]
    # Remote target: reported as-is (a server we don't own isn't discovered),
    # and named in the stop hint because a bare stop would resolve the
    # configured target instead.
    assert "server: https://example.databricksapps.com" in result.output
    assert "omnigent host stop --server https://example.databricksapps.com" in result.output


_VANITY_API = "https://vanity.databricks.com/api/2.0/omnigent"
_WORKSPACE_API = "https://dbc-1.cloud.databricks.com/api/2.0/omnigent"
_SAME_WORKSPACE_PID = 5150


def _seed_same_workspace_daemon(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    vanity_org_id: str = "1",
    age_s: int = 0,
) -> None:
    """
    Record a live daemon on the workspace host and log both URLs in.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Test-scoped directory holding the token file.
    :param vanity_org_id: Workspace id the vanity URL's login records, e.g.
        ``"1"`` for the daemon's own workspace.
    :param age_s: How long ago the daemon started, e.g. ``60``. ``0`` is
        within the startup grace, so its host status isn't checked.
    """
    from omnigent.cli_auth import store_databricks_auth

    monkeypatch.setenv("OMNIGENT_HOST_ID", "a" * 32)
    monkeypatch.setenv("OMNIGENT_HOST_NAME", "test-host")
    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path", lambda: tmp_path / "auth_tokens.json"
    )
    store_databricks_auth(_WORKSPACE_API, "https://dbc-1.cloud.databricks.com", org_id="1")
    store_databricks_auth(_VANITY_API, "https://dbc-1.cloud.databricks.com", org_id=vanity_org_id)
    monkeypatch.setattr(
        "omnigent.cli._ensure_databricks_server_auth", lambda *args, **kwargs: None
    )
    cli_module._write_daemon_record(
        cli_module._HostDaemonRecord(
            pid=_SAME_WORKSPACE_PID,
            target=_WORKSPACE_API,
            mode="server",
            server_url=_WORKSPACE_API,
            log_path=str(tmp_path / "existing.log"),
            started_at=int(time.time()) - age_s,
            host_id="a" * 32,
        )
    )


_HOST_ONLINE = cli_module._HostHttpResult(status_code=200, body={"status": "online"})
_HOST_OFFLINE = cli_module._HostHttpResult(status_code=200, body={"status": "offline"})
_HOST_NOT_FOUND = cli_module._HostHttpResult(status_code=404, body={"detail": "host not found"})
_HOST_REJECTED = cli_module._HostHttpResult(status_code=401, body="unauthorized")
_HOST_FORBIDDEN = cli_module._HostHttpResult(status_code=403, body="forbidden")
_HOST_UNREACHABLE = cli_module._HostHttpResult(status_code=0, body="connection refused")
_HOST_HTML_PAGE = cli_module._HostHttpResult(status_code=200, body="<html>Sign in</html>")
_HOST_UNKNOWN_STATE = cli_module._HostHttpResult(status_code=200, body={"status": "draining"})
_HOST_PROXY_NOT_FOUND = cli_module._HostHttpResult(status_code=404, body="<html>Not Found</html>")
_ROUTE_NOT_FOUND = cli_module._HostHttpResult(status_code=404, body={"detail": "Not Found"})


def _stub_host_status_by_url(
    monkeypatch: pytest.MonkeyPatch, statuses: dict[str, cli_module._HostHttpResult]
) -> None:
    """
    Answer host-status probes by the URL each one is sent through.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param statuses: Probe result per server URL, e.g.
        ``{_VANITY_API: _HOST_ONLINE}``.
    """
    monkeypatch.setattr("omnigent.cli._DAEMON_RECONNECT_GRACE_S", 0.0)
    monkeypatch.setattr(
        "omnigent.cli._daemon_host_status_probe",
        lambda record, **kwargs: statuses[record.server_url],
    )


@pytest.mark.parametrize(
    ("age_s", "via_requested_url", "via_own_url", "reason"),
    [
        # Its own sign-in expired and the server has it offline.
        (60, _HOST_OFFLINE, _HOST_REJECTED, "its server rejected its sign-in (HTTP 401)"),
        (60, _HOST_OFFLINE, _HOST_FORBIDDEN, "its server rejected its sign-in (HTTP 403)"),
        (60, _HOST_OFFLINE, _HOST_UNREACHABLE, "it can't reach its server"),
        (60, _HOST_OFFLINE, _HOST_OFFLINE, "its host is not connected to the server"),
        # Only its own URL answers, and the server has it offline.
        (60, _HOST_REJECTED, _HOST_OFFLINE, "its host is not connected to the server"),
        # The server has no row for it: it never registered.
        (60, _HOST_NOT_FOUND, _HOST_UNREACHABLE, "it can't reach its server"),
        # Online through the requested URL even though its own credentials fail.
        (60, _HOST_ONLINE, _HOST_REJECTED, None),
        # The requested URL isn't signed in yet; its own URL says online.
        (60, _HOST_REJECTED, _HOST_ONLINE, None),
        # Neither URL can be asked, so its state is unknown: reuse it as before.
        (60, _HOST_REJECTED, _HOST_REJECTED, None),
        (60, _HOST_UNREACHABLE, _HOST_UNREACHABLE, None),
        # A 200 or 404 that isn't the server's host answer proves nothing either.
        (60, _HOST_HTML_PAGE, _HOST_REJECTED, None),
        (60, _HOST_UNKNOWN_STATE, _HOST_REJECTED, None),
        (60, _HOST_PROXY_NOT_FOUND, _HOST_UNREACHABLE, None),
        (60, _ROUTE_NOT_FOUND, _HOST_UNREACHABLE, None),
        # Just started and still connecting: no verdict yet.
        (0, _HOST_OFFLINE, _HOST_OFFLINE, None),
        # No requested URL: only its own URL is asked.
        (60, None, _HOST_OFFLINE, "its host is not connected to the server"),
        (60, None, _HOST_ONLINE, None),
    ],
)
def test_same_workspace_daemon_offline_reason(
    monkeypatch: pytest.MonkeyPatch,
    age_s: int,
    via_requested_url: cli_module._HostHttpResult | None,
    via_own_url: cli_module._HostHttpResult,
    reason: str | None,
) -> None:
    statuses = {_WORKSPACE_API: via_own_url}
    if via_requested_url is not None:
        statuses[_VANITY_API] = via_requested_url
    _stub_host_status_by_url(monkeypatch, statuses)

    assert (
        cli_module._same_workspace_daemon_offline_reason(
            _same_workspace_sibling(age_s=age_s),
            _VANITY_API if via_requested_url is not None else None,
        )
        == reason
    )


@pytest.mark.parametrize(
    ("requested_url_replies", "reason", "probe_rounds"),
    [
        # Back online on the second round, within the reconnect grace.
        ([_HOST_OFFLINE, _HOST_ONLINE], None, 2),
        # Still offline when the grace runs out.
        ([_HOST_OFFLINE] * 11, "its server rejected its sign-in (HTTP 401)", 11),
        # No verdict from either URL: reused at once, without waiting out the grace.
        ([_HOST_REJECTED], None, 1),
    ],
)
def test_same_workspace_daemon_offline_reason_waits_out_a_reconnect(
    monkeypatch: pytest.MonkeyPatch,
    requested_url_replies: list[cli_module._HostHttpResult],
    reason: str | None,
    probe_rounds: int,
) -> None:
    """A sibling mid-reconnect gets the grace window before it is judged offline."""
    clock = [0.0]

    def _advance(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(
        cli_module,
        "time",
        SimpleNamespace(time=time.time, monotonic=lambda: clock[0], sleep=_advance),
    )
    monkeypatch.setattr("omnigent.cli._DAEMON_RECONNECT_GRACE_S", 5.0)
    replies = iter(requested_url_replies)
    probed: list[str | None] = []

    def _probe(
        record: cli_module._HostDaemonRecord, **kwargs: object
    ) -> cli_module._HostHttpResult:
        probed.append(record.server_url)
        # The sibling's own sign-in stays rejected throughout.
        return _HOST_REJECTED if record.server_url == _WORKSPACE_API else next(replies)

    monkeypatch.setattr("omnigent.cli._daemon_host_status_probe", _probe)

    assert (
        cli_module._same_workspace_daemon_offline_reason(_same_workspace_sibling(), _VANITY_API)
        == reason
    )
    assert probed.count(_VANITY_API) == probe_rounds


def test_offline_same_workspace_daemon_error_drops_url_credentials() -> None:
    """The refusal reaches terminal output and logs, so recorded userinfo is dropped."""
    target = "https://user:secret-token@omnigent.example.com"
    sibling = replace(_same_workspace_sibling(), target=target, server_url=target)

    message = cli_module._offline_same_workspace_daemon_error(
        sibling, "its host is not connected to the server"
    ).format_message()

    assert "secret-token" not in message
    assert "user:" not in message
    assert "login https://omnigent.example.com`" in message
    assert "host stop --server https://omnigent.example.com`" in message


def _same_workspace_sibling(*, age_s: int = 60) -> cli_module._HostDaemonRecord:
    """
    Build a live-looking daemon record on the workspace host.

    :param age_s: How long ago it started, e.g. ``60``.
    :returns: The record, as :func:`_same_workspace_daemon` would return it.
    """
    return cli_module._HostDaemonRecord(
        pid=_SAME_WORKSPACE_PID,
        target=_WORKSPACE_API,
        mode="server",
        server_url=_WORKSPACE_API,
        log_path=None,
        started_at=int(time.time()) - age_s,
        host_id="a" * 32,
    )


@pytest.mark.parametrize(
    ("via_requested_url", "via_own_url", "refusal"),
    [
        # Its own sign-in expired and the server has it offline.
        (_HOST_OFFLINE, _HOST_REJECTED, "its server rejected its sign-in (HTTP 401)"),
        # Only one URL can see it online; registration is confirmed through that one.
        (_HOST_ONLINE, _HOST_REJECTED, None),
        (_HOST_REJECTED, _HOST_ONLINE, None),
    ],
)
def test_host_background_reuses_same_workspace_daemon_only_when_online(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    via_requested_url: cli_module._HostHttpResult,
    via_own_url: cli_module._HostHttpResult,
    refusal: str | None,
) -> None:
    """
    A same-workspace daemon whose host is offline is neither reused nor joined.

    Reusing it would wait for a host that never comes online, and a second
    daemon would take over its connection once it reconnects, so the launch
    stops and says how to fix the running daemon. It isn't stopped for the user.
    """
    spawned_args, _ = _patch_background_host_spawn(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "omnigent.cli._pid_alive", lambda checked: checked in {4242, _SAME_WORKSPACE_PID}
    )
    _seed_same_workspace_daemon(monkeypatch, tmp_path, age_s=60)
    _stub_host_status_by_url(
        monkeypatch, {_VANITY_API: via_requested_url, _WORKSPACE_API: via_own_url}
    )
    terminated: list[int] = []
    monkeypatch.setattr(
        "omnigent.cli._terminate_daemon",
        lambda record, *, force: terminated.append(record.pid),
    )

    result = CliRunner().invoke(cli, ["host", "--background", "--server", _VANITY_API])

    assert spawned_args == [], "the workspace's daemon must not get a second one"
    assert terminated == []
    if refusal is None:
        assert result.exit_code == 0, result.output
        assert f"already running (pid {_SAME_WORKSPACE_PID})" in result.output
        return
    assert result.exit_code == 1, result.output
    assert refusal in result.output
    assert "login https://dbc-1.cloud.databricks.com/omnigent?o=1" in result.output
    assert "host stop --server https://dbc-1.cloud.databricks.com/omnigent?o=1" in result.output


def test_host_background_reuses_daemon_on_another_url_for_the_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A second URL for the same workspace reuses the running daemon.

    A second daemon with the same host id would take over the first one's
    server connection and back again every few seconds.
    """
    spawned_args, _ = _patch_background_host_spawn(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "omnigent.cli._pid_alive", lambda checked: checked in {4242, _SAME_WORKSPACE_PID}
    )
    _seed_same_workspace_daemon(monkeypatch, tmp_path)

    result = CliRunner().invoke(cli, ["host", "--background", "--server", _VANITY_API])

    assert result.exit_code == 0, result.output
    assert spawned_args == [], "the workspace's daemon must not get a second one"
    assert f"already running (pid {_SAME_WORKSPACE_PID})" in result.output
    assert "same workspace through another URL" in result.output
    # Reported under the daemon's own URL, not the one requested.
    server_lines = [line.split() for line in result.output.splitlines() if "server:" in line]
    assert server_lines == [["server:", "https://dbc-1.cloud.databricks.com/omnigent?o=1"]]


def test_host_background_spawns_for_another_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A different workspace id keeps its own daemon."""
    spawned_args, _ = _patch_background_host_spawn(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "omnigent.cli._pid_alive", lambda checked: checked in {4242, _SAME_WORKSPACE_PID}
    )
    _seed_same_workspace_daemon(monkeypatch, tmp_path, vanity_org_id="2")

    result = CliRunner().invoke(cli, ["host", "--background", "--server", _VANITY_API])

    assert result.exit_code == 0, result.output
    assert spawned_args and _VANITY_API in spawned_args[0]
    assert "Started the host daemon" in result.output


def test_foreground_host_refuses_another_url_for_the_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A foreground host can't reuse a daemon, so it refuses and names it."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid")
    monkeypatch.setattr(
        "omnigent.cli._pid_is_recorded_daemon",
        lambda record: record.pid in {os.getpid(), _SAME_WORKSPACE_PID},
    )
    _seed_same_workspace_daemon(monkeypatch, tmp_path)
    runs: list[str] = []

    with patch(
        "omnigent.host.connect.run_host_process",
        lambda server_url, **kwargs: runs.append(server_url),
    ):
        result = CliRunner().invoke(
            cli, ["host", "--server", _VANITY_API, "--no-open"], standalone_mode=False
        )

    assert isinstance(result.exception, click.ClickException)
    message = result.exception.format_message()
    assert "already running for this workspace under another URL" in message
    assert f"target={_WORKSPACE_API}" in message
    assert "nothing more to do" in message
    # The running daemon is the right one, so neither the message nor the
    # stale-host hint suggests stopping it.
    assert "stop" not in message
    assert suppresses_recovery_hint(result.exception)
    assert runs == []
    assert cli_module._find_daemon_record(_VANITY_API) is None


def test_foreground_host_points_at_fixing_an_offline_daemon_for_the_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An offline same-workspace daemon is the thing to fix, so the refusal says how."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid")
    monkeypatch.setattr(
        "omnigent.cli._pid_is_recorded_daemon",
        lambda record: record.pid in {os.getpid(), _SAME_WORKSPACE_PID},
    )
    _seed_same_workspace_daemon(monkeypatch, tmp_path, age_s=60)
    _stub_host_status_by_url(
        monkeypatch, {_VANITY_API: _HOST_OFFLINE, _WORKSPACE_API: _HOST_REJECTED}
    )
    runs: list[str] = []

    with patch(
        "omnigent.host.connect.run_host_process",
        lambda server_url, **kwargs: runs.append(server_url),
    ):
        result = CliRunner().invoke(
            cli, ["host", "--server", _VANITY_API, "--no-open"], standalone_mode=False
        )

    assert isinstance(result.exception, click.ClickException)
    message = result.exception.format_message()
    assert "rejected its sign-in (HTTP 401)" in message
    assert "nothing more to do" not in message
    assert "host stop --server https://dbc-1.cloud.databricks.com/omnigent?o=1" in message
    assert not suppresses_recovery_hint(result.exception)
    assert runs == []
    assert cli_module._find_daemon_record(_VANITY_API) is None


def _foreground_workspace_setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run a foreground host on ``_VANITY_API`` whose server names workspace ``1``."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_HOST_ID", "a" * 32)
    monkeypatch.setenv("OMNIGENT_HOST_NAME", "test-host")
    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid")
    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path", lambda: tmp_path / "auth_tokens.json"
    )
    monkeypatch.setattr(
        "omnigent.cli._pid_is_recorded_daemon", lambda record: record.pid == os.getpid()
    )
    monkeypatch.setattr(
        "omnigent.cli._ensure_databricks_server_auth", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "omnigent.host.daemon_lifecycle.probe_workspace_id", lambda url, **_kwargs: "1"
    )


def test_foreground_host_holds_the_workspace_lock_while_connected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The foreground host records its workspace and owns it until it exits."""
    from omnigent.host.daemon_lifecycle import DaemonLifecycleLock

    _foreground_workspace_setup(monkeypatch, tmp_path)
    seen: list[str | None] = []

    def _run(server_url: str, **kwargs: object) -> None:
        other = DaemonLifecycleLock.for_workspace("a" * 32, "1", base_dir=tmp_path)
        assert other.try_acquire() is False
        record = cli_module._find_daemon_record(_VANITY_API)
        seen.append(record.workspace_id if record is not None else None)

    with patch("omnigent.host.connect.run_host_process", _run):
        result = CliRunner().invoke(cli, ["host", "--server", _VANITY_API, "--no-open"])

    assert result.exit_code == 0, result.output
    assert seen == ["1"]
    released = DaemonLifecycleLock.for_workspace("a" * 32, "1", base_dir=tmp_path)
    assert released.try_acquire() is True
    released.release()


def test_foreground_host_exits_before_connecting_when_the_workspace_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another daemon holding the workspace lock stops a foreground host before it connects."""
    from omnigent.host.daemon_lifecycle import DaemonLifecycleLock

    _foreground_workspace_setup(monkeypatch, tmp_path)
    monkeypatch.setattr("omnigent.cli._FOREGROUND_WORKSPACE_LOCK_WAIT_S", 0.0)
    holder = DaemonLifecycleLock.for_workspace("a" * 32, "1", base_dir=tmp_path)
    assert holder.try_acquire() is True
    runs: list[str] = []
    try:
        with patch(
            "omnigent.host.connect.run_host_process",
            lambda server_url, **kwargs: runs.append(server_url),
        ):
            result = CliRunner().invoke(
                cli, ["host", "--server", _VANITY_API, "--no-open"], standalone_mode=False
            )
    finally:
        holder.release()

    assert result.exit_code == 1
    assert isinstance(result.exception, click.ClickException)
    assert "already serves workspace 1" in result.exception.format_message()
    assert runs == []
    assert cli_module._find_daemon_record(_VANITY_API) is None


def test_foreground_host_connects_once_a_starting_daemon_yields_the_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daemon still checking the workspace holds its lock briefly; the host waits it out.

    That daemon can yield to the host's own record, so refusing at once would
    leave the workspace with no host.
    """
    from omnigent.host.daemon_lifecycle import DaemonLifecycleLock

    _foreground_workspace_setup(monkeypatch, tmp_path)
    starting = DaemonLifecycleLock.for_workspace("a" * 32, "1", base_dir=tmp_path)
    assert starting.try_acquire() is True
    monkeypatch.setattr("omnigent.cli.time.sleep", lambda _seconds: starting.release())
    seen: list[str | None] = []

    def _run(server_url: str, **kwargs: object) -> None:
        record = cli_module._find_daemon_record(_VANITY_API)
        seen.append(record.workspace_id if record is not None else None)

    with patch("omnigent.host.connect.run_host_process", _run):
        result = CliRunner().invoke(cli, ["host", "--server", _VANITY_API, "--no-open"])

    assert result.exit_code == 0, result.output
    assert seen == ["1"]


def test_claim_foreground_workspace_names_the_daemon_holding_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daemon that claimed the workspace under another URL is named in the refusal."""
    from omnigent.host.daemon_lifecycle import DaemonLifecycleLock

    _foreground_workspace_setup(monkeypatch, tmp_path)
    holder_lock = DaemonLifecycleLock.for_workspace("a" * 32, "1", base_dir=tmp_path)
    assert holder_lock.try_acquire() is True
    cli_module._write_daemon_record(
        cli_module._HostDaemonRecord(
            pid=os.getpid(),
            target=_WORKSPACE_API,
            mode="server",
            server_url=_WORKSPACE_API,
            log_path=str(tmp_path / "holder.log"),
            started_at=int(time.time()),
            host_id="a" * 32,
            workspace_id="1",
        )
    )
    record = cli_module._foreground_daemon_record(
        target=_VANITY_API, server_url=_VANITY_API, host_id="a" * 32
    )
    try:
        with pytest.raises(click.ClickException) as raised:
            cli_module._claim_foreground_workspace(record, _VANITY_API, workspace_id="1")
    finally:
        holder_lock.release()

    message = raised.value.format_message()
    assert "already running for this workspace under another URL" in message
    assert f"target={_WORKSPACE_API}" in message


def test_host_status_selects_daemon_on_another_url_for_the_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``host status --server`` reports the workspace's daemon and names the request.

    The desktop app's Arca connect reads this JSON and needs to know the
    daemon serves the URL it asked for.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid")
    monkeypatch.setattr(
        "omnigent.cli._pid_is_recorded_daemon",
        lambda record: record.pid == _SAME_WORKSPACE_PID,
    )
    monkeypatch.setattr(
        "omnigent.cli._daemon_status_payload",
        lambda record, **kwargs: cli_module._base_daemon_status_payload(record),
    )
    _seed_same_workspace_daemon(monkeypatch, tmp_path)

    result = CliRunner().invoke(cli, ["host", "status", "--server", _VANITY_API, "--json"])

    assert result.exit_code == 0, result.output
    (daemon,) = json.loads(result.output)["daemons"]
    assert daemon["pid"] == _SAME_WORKSPACE_PID
    assert daemon["target"] == _WORKSPACE_API
    assert daemon["same_workspace_as"] == _VANITY_API


def _seed_yielding_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    registered_late: bool = False,
    requested_login: bool = True,
    failed_probes: int = 0,
    exit_code: int = 0,
    winner_target: str = _WORKSPACE_API,
) -> list[str]:
    """
    Make the spawned daemon yield to a same-workspace daemon on another URL.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Test-scoped directory for the registry and login records.
    :param registered_late: Whether the winner's record appears only after a
        few claim polls, rather than before the spawned child exits.
    :param requested_login: Whether the requested URL has a login record that
        names its workspace.
    :param failed_probes: How many probes of the requested URL fail first.
    :param exit_code: The spawned child's exit status; ``0`` is a yield.
    :param winner_target: Target the winning daemon's record names, e.g. an
        old spelling of the requested URL.
    :returns: The URLs probed, in order.
    """
    from omnigent.cli import _SpawnedDaemonProcess
    from omnigent.cli_auth import store_databricks_auth

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_HOST_ID", "a" * 32)
    monkeypatch.setenv("OMNIGENT_HOST_NAME", "test-host")
    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid")
    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path", lambda: tmp_path / "auth_tokens.json"
    )
    monkeypatch.setattr(
        "omnigent.cli._pid_is_recorded_daemon",
        lambda record: record.pid == _SAME_WORKSPACE_PID,
    )
    store_databricks_auth(_WORKSPACE_API, "https://dbc-1.cloud.databricks.com", org_id="1")
    if requested_login:
        store_databricks_auth(_VANITY_API, "https://dbc-1.cloud.databricks.com", org_id="1")
    monkeypatch.setattr(
        "omnigent.cli._stop_spawned_host_daemon_process",
        lambda spawned: pytest.fail("the yielded-to daemon must not be stopped"),
    )

    probed: list[str] = []

    def _probe(url: str, *, org_id: str | None = None, **_kwargs: object) -> str | None:
        probed.append(url)
        if url == _VANITY_API and probed.count(url) <= failed_probes:
            return None
        return "1"

    monkeypatch.setattr("omnigent.host.daemon_lifecycle.probe_workspace_id", _probe)

    class _ExitedChild:
        def poll(self) -> int:
            return exit_code

    def _register_winner() -> None:
        cli_module._write_daemon_record(
            cli_module._HostDaemonRecord(
                pid=_SAME_WORKSPACE_PID,
                target=winner_target,
                mode="server",
                server_url=winner_target,
                log_path=str(tmp_path / "existing.log"),
                started_at=int(time.time()),
                host_id="a" * 32,
                workspace_id="1",
            )
        )

    sleeps: list[float] = []

    def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if registered_late and len(sleeps) == 3:
            _register_winner()

    monkeypatch.setattr("omnigent.cli.time.sleep", _sleep)

    def _spawn(*, args: list[str], env: dict[str, str]) -> _SpawnedDaemonProcess:
        # The workspace's daemon registered after the pre-spawn check ran.
        if not registered_late:
            _register_winner()
        return _SpawnedDaemonProcess(
            pid=_SAME_WORKSPACE_PID + 1,
            log_path=str(tmp_path / "yielded.log"),
            process=_ExitedChild(),  # type: ignore[arg-type]
        )

    monkeypatch.setattr("omnigent.cli._spawn_host_daemon_process", _spawn)
    return probed


@pytest.mark.parametrize(
    ("registered_late", "requested_login", "failed_probes"),
    [
        (False, True, 0),
        # The winner's record appears a few polls later.
        (True, True, 0),
        # No login record names the requested URL's workspace, and the first
        # probes fail; a later probe still finds the winner.
        (False, False, 2),
    ],
)
def test_ensure_host_daemon_reuses_the_daemon_a_spawned_daemon_yielded_to(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registered_late: bool,
    requested_login: bool,
    failed_probes: int,
) -> None:
    """
    A spawned daemon that finds the workspace already served exits; that's a reuse.

    The daemon itself makes the final check, so a launch on another URL that
    raced the first daemon's start still ends with one daemon. Only a
    successful probe of the requested URL is remembered.
    """
    probed = _seed_yielding_spawn(
        monkeypatch,
        tmp_path,
        registered_late=registered_late,
        requested_login=requested_login,
        failed_probes=failed_probes,
    )

    assert _ensure_host_daemon(_VANITY_API) is False
    assert cli_module._find_daemon_record(_VANITY_API) is None
    kept = cli_module._find_daemon_record(_WORKSPACE_API)
    assert kept is not None and kept.pid == _SAME_WORKSPACE_PID
    assert probed.count(_VANITY_API) == failed_probes + 1


def test_ensure_host_daemon_refuses_an_offline_daemon_a_spawned_daemon_yielded_to(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Yielding to an offline daemon gets the refusal a launch gets before spawning."""
    _seed_yielding_spawn(monkeypatch, tmp_path)
    monkeypatch.setattr("omnigent.cli._DAEMON_REUSE_MIN_AGE_S", 0.0)
    _stub_host_status_by_url(
        monkeypatch, {_VANITY_API: _HOST_OFFLINE, _WORKSPACE_API: _HOST_REJECTED}
    )

    with pytest.raises(click.ClickException) as raised:
        _ensure_host_daemon(_VANITY_API)

    message = raised.value.format_message()
    assert "rejected its sign-in (HTTP 401)" in message
    assert "host stop --server https://dbc-1.cloud.databricks.com/omnigent?o=1" in message


@pytest.mark.parametrize(("exit_code", "reused"), [(0, True), (1, False)])
def test_wait_for_daemon_claim_treats_only_a_clean_exit_as_a_yield(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    reused: bool,
) -> None:
    """A crashed child is a failed start, even while another URL serves the workspace."""
    _seed_yielding_spawn(monkeypatch, tmp_path, exit_code=exit_code)
    spawned = cli_module._spawn_host_daemon_process(args=[], env={})

    claimed = cli_module._wait_for_daemon_claim(
        _VANITY_API, spawned, timeout_s=0.0, server_url=_VANITY_API
    )

    assert (claimed.pid if claimed is not None else None) == (
        _SAME_WORKSPACE_PID if reused else None
    )


@pytest.mark.parametrize("before_spawn", [True, False])
def test_ensure_host_daemon_reports_a_config_change_when_another_url_serves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    before_spawn: bool,
) -> None:
    """A config-change teardown is reported even when another URL's daemon is reused.

    The other daemon is found either before spawning, or after the spawned one yields.
    """
    _seed_yielding_spawn(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "omnigent.cli._reuse_existing_daemon_record",
        lambda target: cli_module._DaemonReuseDecision(reuse=False, config_changed=True),
    )
    if before_spawn:
        cli_module._write_daemon_record(
            cli_module._HostDaemonRecord(
                pid=_SAME_WORKSPACE_PID,
                target=_WORKSPACE_API,
                mode="server",
                server_url=_WORKSPACE_API,
                log_path=str(tmp_path / "existing.log"),
                started_at=int(time.time()),
                host_id="a" * 32,
                workspace_id="1",
            )
        )
        monkeypatch.setattr(
            "omnigent.cli._spawn_host_daemon_process",
            lambda **_kwargs: pytest.fail("the workspace's daemon is reused before spawning"),
        )

    assert _ensure_host_daemon(_VANITY_API) is True


def test_wait_for_daemon_claim_paces_failed_probes_within_its_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that keeps failing is retried every half second, never past the wait."""
    _seed_yielding_spawn(monkeypatch, tmp_path, requested_login=False)
    spawned = cli_module._spawn_host_daemon_process(args=[], env={})
    clock = [0.0]
    monkeypatch.setattr("omnigent.cli.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "omnigent.cli.time.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    probe_ends: list[float] = []

    def _probe(url: str, *, org_id: str | None = None, timeout_s: float = 3.0) -> str | None:
        probe_ends.append(clock[0] + timeout_s)
        return None

    monkeypatch.setattr("omnigent.host.daemon_lifecycle.probe_workspace_id", _probe)

    claimed = cli_module._wait_for_daemon_claim(
        _VANITY_API, spawned, timeout_s=2.0, server_url=_VANITY_API
    )

    assert claimed is None
    assert 0 < len(probe_ends) <= 5
    # No probe may run past the claim wait.
    assert max(probe_ends) <= 2.0


def test_ensure_host_daemon_treats_an_old_spelling_of_its_url_as_its_own_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record under an old spelling of the requested URL is not another URL's daemon."""
    _seed_yielding_spawn(
        monkeypatch, tmp_path, winner_target="https://VANITY.databricks.com:443/api/2.0/omnigent"
    )
    actions: list[str] = []

    def _record(event: str, *, details: dict[str, object], **_kwargs: object) -> None:
        if event == "host_state_observed":
            actions.append(str(details["host_process_action"]))

    monkeypatch.setattr("omnigent.cli.record_startup_event", _record)

    assert _ensure_host_daemon(_VANITY_API) is False
    assert actions[-1] == "started_concurrently_during_launch"


@pytest.mark.parametrize(
    "command",
    [
        ["host", "--background", "--server", _VANITY_API, "--no-open"],
        ["start", "--server", _VANITY_API, "--no-open"],
    ],
)
def test_background_commands_report_reuse_when_the_spawned_daemon_yields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
) -> None:
    """``host --background`` and ``start`` report the daemon the spawned one yielded to."""
    _seed_yielding_spawn(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "omnigent.cli._ensure_databricks_server_auth", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "omnigent.cli._confirm_background_host_registered", lambda record, **_kwargs: None
    )

    result = CliRunner().invoke(cli, command)

    assert result.exit_code == 0, result.output
    assert f"already running (pid {_SAME_WORKSPACE_PID})" in result.output
    assert "same workspace through another URL" in result.output


# ── omnigent start ────────────────────────────────────────────────


def test_start_spawns_background_host_and_suggests_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``start`` is the friendly spelling of ``host --background``.

    It must spawn the same detached daemon (local mode here, so the daemon
    brings up the local server too) and suggest the matching off switch —
    ``omnigent stop``, not the host-specific teardown.
    """
    spawned_args, _ = _patch_background_host_spawn(monkeypatch, tmp_path)
    foreground_runs: list[str] = []

    with patch(
        "omnigent.host.connect.run_host_process",
        lambda server_url, **kwargs: foreground_runs.append(server_url),
    ):
        result = CliRunner().invoke(cli, ["start"])

    assert result.exit_code == 0, result.output
    assert foreground_runs == []
    assert "pid 4242" in result.output
    assert "server: http://127.0.0.1:6767" in result.output
    assert "omnigent stop" in result.output
    assert "host stop" not in result.output
    assert spawned_args and "--local" in spawned_args[0]


def test_start_hosts_on_explicit_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``start --server <url>`` hosts on that server, signing in first.

    The alias must not quietly drop the target or the foreground sign-in
    pre-flight that a detached daemon cannot perform itself.
    """
    spawned_args, _ = _patch_background_host_spawn(monkeypatch, tmp_path)
    auth_calls: list[tuple[str, bool]] = []

    def _fake_auth(server: str, *, non_interactive: bool = False) -> None:
        """Record the sign-in pre-flight.

        :param server: Server URL being authenticated.
        :param non_interactive: Whether prompting is suppressed.
        """
        auth_calls.append((server, non_interactive))

    monkeypatch.setattr("omnigent.cli._ensure_databricks_server_auth", _fake_auth)

    result = CliRunner().invoke(
        cli, ["start", "--server", "https://example.databricksapps.com", "--non-interactive"]
    )

    assert result.exit_code == 0, result.output
    assert auth_calls == [("https://example.databricksapps.com", True)]
    assert "server: https://example.databricksapps.com" in result.output
    assert spawned_args == [
        [
            sys.executable,
            "-P",
            "-m",
            "omnigent.host._daemon_entry",
            "--server",
            "https://example.databricksapps.com",
        ]
    ]


@pytest.mark.parametrize(
    ("is_tty", "extra_args", "no_open_env", "config_content", "expected_opened"),
    [
        pytest.param(True, [], None, None, ["http://127.0.0.1:8123"], id="interactive-opens"),
        pytest.param(True, ["--no-open"], None, None, [], id="no-open-flag-skips"),
        pytest.param(True, [], "1", None, [], id="no-open-env-skips"),
        pytest.param(True, [], "true", None, [], id="no-open-env-true-skips"),
        pytest.param(True, [], "0", None, ["http://127.0.0.1:8123"], id="env-zero-opens"),
        pytest.param(True, [], "false", None, ["http://127.0.0.1:8123"], id="env-false-opens"),
        pytest.param(True, ["--no-open"], "0", None, [], id="flag-overrides-env"),
        pytest.param(True, ["--non-interactive"], None, None, [], id="non-interactive-skips"),
        pytest.param(False, [], None, None, [], id="no-tty-skips"),
        pytest.param(
            True, [], None, "auto_open_conversation: false\n", [], id="auto-open-disabled-skips"
        ),
        pytest.param(
            True,
            ["--no-open"],
            None,
            "auto_open_conversation: true\n",
            [],
            id="flag-overrides-config",
        ),
    ],
)
def test_host_web_ui_open_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    is_tty: bool,
    extra_args: list[str],
    no_open_env: str | None,
    config_content: str | None,
    expected_opened: list[str],
) -> None:
    """Open the host web UI only when interactive and enabled."""
    if config_content is not None:
        (tmp_path / "config.yaml").write_text(config_content)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid")
    monkeypatch.setattr("omnigent.cli._stdin_is_tty", lambda: is_tty)
    opened: list[str] = []

    with (
        patch(
            "omnigent.cli.ensure_local_omnigent_server",
            lambda: LocalServerStartup(url="http://127.0.0.1:8123", spawned=False),
        ),
        patch("omnigent.host.connect.run_host_process", lambda server_url, **kwargs: None),
        patch(
            "omnigent.conversation_browser.open_conversation_url",
            lambda url: opened.append(url) or True,
        ),
    ):
        result = CliRunner().invoke(
            cli, ["host", *extra_args], env={"OMNIGENT_HOST_NO_OPEN": no_open_env}
        )

    assert result.exit_code == 0, result.output
    assert opened == expected_opened


@pytest.mark.parametrize("no_open", [False, True])
def test_host_remote_web_ui_open_preference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_open: bool,
) -> None:
    """Honor the browser preference on remote hosts while preserving sign-in."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid")
    monkeypatch.setattr("omnigent.cli._stdin_is_tty", lambda: True)
    opened: list[str] = []

    with (
        patch("omnigent.cli._ensure_databricks_server_auth") as auth,
        patch("omnigent.host.connect.run_host_process", lambda server_url, **kwargs: None),
        patch(
            "omnigent.conversation_browser.open_conversation_url",
            lambda url: opened.append(url) or True,
        ),
    ):
        result = CliRunner().invoke(
            cli,
            [
                "host",
                "https://example.databricks.com/api/2.0/omnigent",
                *(["--no-open"] if no_open else []),
            ],
            env={"OMNIGENT_HOST_NO_OPEN": None},
        )

    assert result.exit_code == 0, result.output
    assert opened == ([] if no_open else ["https://example.databricks.com/omnigent"])
    auth.assert_called_once_with(
        "https://example.databricks.com/api/2.0/omnigent", non_interactive=False
    )


@pytest.mark.parametrize("command", [["start"], ["host", "--background"]])
@pytest.mark.parametrize(
    ("extra_args", "no_open_env", "expected_opened"),
    [
        pytest.param([], None, ["http://127.0.0.1:6767"], id="default-opens"),
        pytest.param(["--no-open"], None, [], id="flag-skips"),
        pytest.param([], "1", [], id="env-skips"),
        pytest.param([], "0", ["http://127.0.0.1:6767"], id="env-zero-opens"),
        pytest.param(["--no-open"], "0", [], id="flag-overrides-env"),
    ],
)
def test_background_host_web_ui_open_preference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    extra_args: list[str],
    no_open_env: str | None,
    expected_opened: list[str],
) -> None:
    """Both background entry points honor the flag and environment preference."""
    _patch_background_host_spawn(monkeypatch, tmp_path)
    monkeypatch.setattr("omnigent.cli._stdin_is_tty", lambda: True)
    opened: list[str] = []

    with patch(
        "omnigent.conversation_browser.open_conversation_url",
        lambda url: opened.append(url) or True,
    ):
        result = CliRunner().invoke(
            cli, [*command, *extra_args], env={"OMNIGENT_HOST_NO_OPEN": no_open_env}
        )

    assert result.exit_code == 0, result.output
    assert opened == expected_opened
