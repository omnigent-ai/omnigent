"""Tests for the server-lifecycle CLI: ``server start/stop/status`` and top-level ``stop``."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from click import ClickException
from click.testing import CliRunner

from omnigent.cli import _HostDaemonRecord, _SessionPagesResult, cli
from omnigent.host.local_server import _DEFAULT_LOCAL_PORT, LocalServerInfo, LocalServerStartup


def _record(
    target: str = "local", *, mode: str = "local", pid: int = 999_999
) -> _HostDaemonRecord:
    """Build a real daemon record for stubbing the registry.

    :param target: Daemon target — ``"local"`` or a server URL.
    :param mode: ``"local"`` or ``"server"``.
    :param pid: Recorded daemon PID, e.g. ``999999``.
    :returns: A populated :class:`_HostDaemonRecord`.
    """
    return _HostDaemonRecord(
        pid=pid,
        target=target,
        mode=mode,
        server_url=None if mode == "local" else target,
        log_path=None,
        started_at=0,
    )


# ── server status ──────────────────────────────────────────────────


def test_server_status_not_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """``server status`` reports not-running when no background server is recorded."""
    monkeypatch.setattr(
        "omnigent.cli.local_server_status",
        lambda: LocalServerInfo(running=False, pid=None, port=None, url=None),
    )
    monkeypatch.setattr("omnigent.cli._find_daemon_record", lambda target: None)

    result = CliRunner().invoke(cli, ["server", "status"])

    assert result.exit_code == 0, result.output
    assert "not running" in result.output


def test_server_status_running_reports_details(monkeypatch: pytest.MonkeyPatch) -> None:
    """``server status`` prints url/pid/port, the live-session count, and daemon-attached state."""
    monkeypatch.setattr(
        "omnigent.cli.local_server_status",
        lambda: LocalServerInfo(running=True, pid=4321, port=8123, url="http://127.0.0.1:8123"),
    )
    # A record exists → "host daemon attached: yes".
    monkeypatch.setattr("omnigent.cli._find_daemon_record", lambda target: _record())
    monkeypatch.setattr(
        "omnigent.cli._fetch_session_pages",
        lambda **kwargs: _SessionPagesResult(sessions=[], error=None),
    )

    result = CliRunner().invoke(cli, ["server", "status"])

    assert result.exit_code == 0, result.output
    assert "http://127.0.0.1:8123" in result.output
    assert "pid 4321" in result.output
    assert "live sessions: 0" in result.output  # empty fetch → 0 live sessions
    assert "host daemon attached: yes" in result.output


def test_server_status_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """``server status --json`` emits the structured fields incl. the log path."""
    monkeypatch.setattr(
        "omnigent.cli.local_server_status",
        lambda: LocalServerInfo(
            running=True,
            pid=4321,
            port=8123,
            url="http://127.0.0.1:8123",
            log_path=Path("/tmp/.omnigent/logs/server/local-server-ab12.log"),
        ),
    )
    monkeypatch.setattr("omnigent.cli._find_daemon_record", lambda target: None)
    monkeypatch.setattr(
        "omnigent.cli._fetch_session_pages",
        lambda **kwargs: _SessionPagesResult(sessions=[], error=None),
    )

    result = CliRunner().invoke(cli, ["server", "status", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "running": True,
        "pid": 4321,
        "port": 8123,
        "url": "http://127.0.0.1:8123",
        "log_path": "/tmp/.omnigent/logs/server/local-server-ab12.log",
        "live_sessions": 0,
        "daemon_attached": False,
    }


def test_server_status_text_reports_log_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """``server status`` (text) names the running server's captured log file."""
    monkeypatch.setattr(
        "omnigent.cli.local_server_status",
        lambda: LocalServerInfo(
            running=True,
            pid=4321,
            port=8123,
            url="http://127.0.0.1:8123",
            log_path=Path.home() / ".omnigent" / "logs" / "server" / "local-server-ab12.log",
        ),
    )
    monkeypatch.setattr("omnigent.cli._find_daemon_record", lambda target: None)
    monkeypatch.setattr(
        "omnigent.cli._fetch_session_pages",
        lambda **kwargs: _SessionPagesResult(sessions=[], error=None),
    )

    result = CliRunner().invoke(cli, ["server", "status"])

    assert result.exit_code == 0, result.output
    assert "running at http://127.0.0.1:8123" in result.output
    assert "log: ~/.omnigent/logs/server/local-server-ab12.log" in result.output


def test_server_status_session_count_failure_is_graceful(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed session fetch leaves the count unknown rather than erroring the command."""
    monkeypatch.setattr(
        "omnigent.cli.local_server_status",
        lambda: LocalServerInfo(running=True, pid=1, port=8123, url="http://127.0.0.1:8123"),
    )
    monkeypatch.setattr("omnigent.cli._find_daemon_record", lambda target: None)

    def _boom(**kwargs: object) -> _SessionPagesResult:
        raise ClickException("server unreachable")

    monkeypatch.setattr("omnigent.cli._fetch_session_pages", _boom)

    result = CliRunner().invoke(cli, ["server", "status"])

    assert result.exit_code == 0, result.output
    assert "running at http://127.0.0.1:8123" in result.output
    assert "live sessions:" not in result.output  # count omitted on fetch failure


# ── server start ───────────────────────────────────────────────────


def test_server_start_spawns(monkeypatch: pytest.MonkeyPatch) -> None:
    """``server start`` reports the URL and exact log file of a spawned server."""
    monkeypatch.setattr(
        "omnigent.cli.ensure_local_omnigent_server",
        lambda: LocalServerStartup(
            url="http://127.0.0.1:8123",
            spawned=True,
            log_path=Path.home() / ".omnigent" / "logs" / "server" / "local-server-ab12.log",
        ),
    )

    result = CliRunner().invoke(cli, ["server", "start"])

    assert result.exit_code == 0, result.output
    assert "Started background server at http://127.0.0.1:8123" in result.output
    # The exact captured-log file is surfaced so the detached server isn't a
    # black box — collapsed to ``~`` for readability.
    assert "log: ~/.omnigent/logs/server/local-server-ab12.log" in result.output


def test_server_start_reuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """``server start`` reports reuse and the reused server's log file."""
    monkeypatch.setattr(
        "omnigent.cli.ensure_local_omnigent_server",
        lambda: LocalServerStartup(
            url="http://127.0.0.1:8123",
            spawned=False,
            log_path=Path.home() / ".omnigent" / "logs" / "server" / "local-server-cd34.log",
        ),
    )

    result = CliRunner().invoke(cli, ["server", "start"])

    assert result.exit_code == 0, result.output
    assert "already running at http://127.0.0.1:8123" in result.output
    # Even a reused server (one this invocation didn't spawn) names its log,
    # read back from the sidecar.
    assert "log: ~/.omnigent/logs/server/local-server-cd34.log" in result.output


def test_server_start_omits_log_when_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """No log line when the running server has no captured-log file.

    A foreground ``omnigent server`` streams logs to its terminal, so a
    reuse of it carries ``log_path=None`` and ``server start`` must not print
    a bogus or empty ``log:`` line.
    """
    monkeypatch.setattr(
        "omnigent.cli.ensure_local_omnigent_server",
        lambda: LocalServerStartup(url="http://127.0.0.1:8123", spawned=False, log_path=None),
    )

    result = CliRunner().invoke(cli, ["server", "start"])

    assert result.exit_code == 0, result.output
    assert "log:" not in result.output


# ── server stop ────────────────────────────────────────────────────


def test_server_stop_stops_server_and_local_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """``server stop`` terminates the local daemon, then stops the background server."""
    monkeypatch.setattr(
        "omnigent.cli.local_server_url_if_healthy", lambda: "http://127.0.0.1:8123"
    )
    local_record = _record()
    monkeypatch.setattr(
        "omnigent.cli._find_daemon_record",
        lambda target: local_record if target == "local" else None,
    )
    terminated: list[_HostDaemonRecord] = []
    monkeypatch.setattr(
        "omnigent.cli._terminate_daemon",
        lambda record, *, force: terminated.append(record),
    )
    stop_server = Mock()
    monkeypatch.setattr("omnigent.cli.stop_local_omnigent_server", stop_server)
    monkeypatch.setattr("omnigent.cli.stop_untracked_local_server", lambda: None)

    result = CliRunner().invoke(cli, ["server", "stop"])

    assert result.exit_code == 0, result.output
    assert "Stopped the background server." in result.output
    assert terminated == [local_record]  # the local daemon was terminated
    stop_server.assert_called_once_with()  # and the server stopped


def test_server_stop_no_server_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """``server stop`` reports nothing running when no background server exists."""
    monkeypatch.setattr("omnigent.cli.local_server_url_if_healthy", lambda: None)
    monkeypatch.setattr("omnigent.cli._find_daemon_record", lambda target: None)
    stop_server = Mock()
    monkeypatch.setattr("omnigent.cli.stop_local_omnigent_server", stop_server)
    monkeypatch.setattr("omnigent.cli.stop_untracked_local_server", lambda: None)

    result = CliRunner().invoke(cli, ["server", "stop"])

    assert result.exit_code == 0, result.output
    assert "No background server is running." in result.output
    stop_server.assert_called_once_with()  # idempotent: still clears any stale pidfile


# ── top-level stop ─────────────────────────────────────────────────


def test_stop_terminates_all_daemons_and_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """``stop`` terminates every daemon and stops the background server."""
    records = [_record("local"), _record("https://example.com", mode="server")]
    monkeypatch.setattr("omnigent.cli._list_daemon_records", lambda: records)
    terminated: list[_HostDaemonRecord] = []
    monkeypatch.setattr(
        "omnigent.cli._terminate_daemon",
        lambda record, *, force: terminated.append(record),
    )
    monkeypatch.setattr(
        "omnigent.cli.local_server_url_if_healthy", lambda: "http://127.0.0.1:8123"
    )
    stop_server = Mock()
    monkeypatch.setattr("omnigent.cli.stop_local_omnigent_server", stop_server)
    monkeypatch.setattr("omnigent.cli.stop_untracked_local_server", lambda: None)

    result = CliRunner().invoke(cli, ["stop"])

    assert result.exit_code == 0, result.output
    assert terminated == records  # both daemons terminated
    stop_server.assert_called_once_with()
    assert "Stopped 2 daemon(s) and the background server." in result.output


def test_stop_nothing_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """``stop`` reports nothing to stop when no daemons or server exist."""
    monkeypatch.setattr("omnigent.cli._list_daemon_records", list)
    monkeypatch.setattr("omnigent.cli.local_server_url_if_healthy", lambda: None)
    monkeypatch.setattr("omnigent.cli.stop_local_omnigent_server", Mock())
    monkeypatch.setattr("omnigent.cli.stop_untracked_local_server", lambda: None)

    result = CliRunner().invoke(cli, ["stop"])

    assert result.exit_code == 0, result.output
    assert "Nothing to stop." in result.output


def test_stop_surfaces_failures_and_suggests_force(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stubborn daemon makes ``stop`` exit nonzero and suggest ``--force``."""
    records = [_record("local"), _record("https://example.com", mode="server")]
    monkeypatch.setattr("omnigent.cli._list_daemon_records", lambda: records)

    def _terminate(record: _HostDaemonRecord, *, force: bool) -> None:
        if record.target == "local":
            raise ClickException(f"daemon {record.pid} did not exit")

    monkeypatch.setattr("omnigent.cli._terminate_daemon", _terminate)
    monkeypatch.setattr("omnigent.cli.local_server_url_if_healthy", lambda: None)
    monkeypatch.setattr("omnigent.cli.stop_local_omnigent_server", Mock())
    monkeypatch.setattr("omnigent.cli.stop_untracked_local_server", lambda: None)

    result = CliRunner().invoke(cli, ["stop"])

    assert result.exit_code != 0  # the stubborn daemon surfaces as a failure
    assert "retry with --force" in result.output


def test_stop_clears_stale_legacy_host_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``stop`` clears a stale legacy ``host.pid`` (tracked only by the legacy pidfile).

    Regression for the phantom where ``_delete_daemon_record`` removed only the
    JSON record, leaving ``host.pid`` to reappear on every subsequent ``stop``.
    Uses a dead PID so termination falls straight through to record deletion.
    """
    host_pid = tmp_path / "host.pid"
    # 2147483647 is not a real PID, so _pid_alive() is False → delete-only path.
    host_pid.write_text("2147483647\nhttps://stale.example\n")
    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", host_pid)
    monkeypatch.setattr("omnigent.cli.local_server_url_if_healthy", lambda: None)
    monkeypatch.setattr("omnigent.cli.stop_local_omnigent_server", Mock())
    monkeypatch.setattr("omnigent.cli.stop_untracked_local_server", lambda: None)

    first = CliRunner().invoke(cli, ["stop"])
    assert first.exit_code == 0, first.output
    assert not host_pid.exists()  # the legacy pidfile is cleared

    second = CliRunner().invoke(cli, ["stop"])
    assert "Nothing to stop." in second.output  # phantom does not reappear


def test_stop_reports_untracked_orphan_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """``stop`` sweeps and reports an orphaned server the pidfile lost track of.

    Reproduces the reported symptom: no daemons, no pidfile-tracked server,
    yet a live server lingers on :6767. The off-switch must stop it (via
    :func:`stop_untracked_local_server`) and say so — not "Nothing to stop."
    """
    monkeypatch.setattr("omnigent.cli._list_daemon_records", list)
    monkeypatch.setattr("omnigent.cli.local_server_url_if_healthy", lambda: None)
    monkeypatch.setattr("omnigent.cli.stop_local_omnigent_server", Mock())
    monkeypatch.setattr("omnigent.cli.stop_untracked_local_server", lambda: 93359)

    result = CliRunner().invoke(cli, ["stop"])

    assert result.exit_code == 0, result.output
    assert "untracked server on :6767 (pid 93359)" in result.output
    assert "Nothing to stop." not in result.output


def test_server_stop_finds_untracked_orphan_when_pidfile_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``server stop`` reports success when only an untracked orphan is found.

    The pidfile is gone (``local_server_url_if_healthy`` → ``None``), but a
    live server is still on :6767. Previously this printed "No background
    server is running" while the server kept running; now the orphan sweep
    catches it.
    """
    monkeypatch.setattr("omnigent.cli.local_server_url_if_healthy", lambda: None)
    monkeypatch.setattr("omnigent.cli._find_daemon_record", lambda target: None)
    monkeypatch.setattr("omnigent.cli.stop_local_omnigent_server", Mock())
    monkeypatch.setattr("omnigent.cli.stop_untracked_local_server", lambda: 93359)

    result = CliRunner().invoke(cli, ["server", "stop"])

    assert result.exit_code == 0, result.output
    assert "Stopped the background server." in result.output


# ── foreground `server`: non-loopback + no-auth warning ─────────────


class _StopBeforeServe(Exception):
    """Sentinel raised by a patched ``_load_config`` to stop the foreground
    server boot right after the bind/auth warning decision, before any real
    server setup (no stores, no uvicorn bind)."""


def _stop_after_auth_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    """Short-circuit the foreground ``omnigent server`` boot.

    ``_load_config`` is the first heavy step after the bind/auth warning
    block, so raising there lets the warning (if any) be emitted while
    stopping before stores are built or a socket is bound.
    """

    def _raise(_path: str | None) -> dict[str, object]:
        raise _StopBeforeServe

    monkeypatch.setattr("omnigent.cli._load_config", _raise)


def _clear_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset every auth-related env var to a known-unset baseline.

    ``monkeypatch.delenv`` records the prior value so teardown also undoes
    the ``OMNIGENT_LOCAL_SINGLE_USER`` that the loopback path sets via
    ``os.environ.setdefault`` — keeping the warning tests isolated.
    """
    for name in (
        "OMNIGENT_AUTH_PROVIDER",
        "OMNIGENT_AUTH_ENABLED",
        "OMNIGENT_ACCOUNTS_ENABLED",
        "OMNIGENT_OIDC_ISSUER",
        "OMNIGENT_LOCAL_SINGLE_USER",
    ):
        monkeypatch.delenv(name, raising=False)


_WARNING_MARKER = "Every request without an 'X-Forwarded-Email' header will be rejected with 401"


def test_foreground_non_loopback_header_no_auth_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-loopback bind in header mode with no single-user fallback warns.

    Header mode + no ``OMNIGENT_LOCAL_SINGLE_USER`` + a non-loopback bind
    means every request 401s; the foreground server must say so at startup.
    """
    _clear_auth_env(monkeypatch)
    _stop_after_auth_decision(monkeypatch)

    result = CliRunner().invoke(cli, ["server", "--host", "0.0.0.0"])

    # The boot stopped at the patched seam — i.e. we ran past the warning.
    assert isinstance(result.exception, _StopBeforeServe), result.output
    assert f"WARNING: binding 0.0.0.0:{_DEFAULT_LOCAL_PORT} (non-loopback)" in result.output
    assert _WARNING_MARKER in result.output
    # Both audiences are addressed: proxy operators and naive LAN binders.
    assert "X-Forwarded-Email" in result.output
    assert "OMNIGENT_AUTH_ENABLED=1" in result.output


def test_foreground_loopback_does_not_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A loopback bind never triggers the non-loopback auth warning.

    Loopback is the single-user local posture (the fallback is auto-enabled),
    so there is no 401 foot-gun and no warning. Stubbing a healthy server
    makes the canonical-local path reuse it and exit cleanly.
    """
    _clear_auth_env(monkeypatch)
    monkeypatch.setattr(
        "omnigent.host.local_server.local_server_url_if_healthy",
        lambda: f"http://127.0.0.1:{_DEFAULT_LOCAL_PORT}",
    )

    result = CliRunner().invoke(cli, ["server", "--host", "127.0.0.1"])

    assert result.exit_code == 0, result.output
    assert "reusing it" in result.output  # took the canonical-local reuse path
    assert "WARNING" not in result.output
    assert "401" not in result.output


def test_foreground_non_loopback_accounts_does_not_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-loopback with accounts auth (OMNIGENT_AUTH_ENABLED=1) does not warn.

    Accounts mode authenticates every request, so there is no silent-401
    foot-gun — the warning is header-mode-only.
    """
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", "1")
    _stop_after_auth_decision(monkeypatch)

    result = CliRunner().invoke(cli, ["server", "--host", "0.0.0.0"])

    assert isinstance(result.exception, _StopBeforeServe), result.output
    assert "WARNING" not in result.output
    assert "401" not in result.output


def test_foreground_non_loopback_oidc_does_not_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-loopback with OIDC configured does not warn.

    An operator-supplied OIDC issuer resolves to ``oidc`` mode (not header),
    so the warning is suppressed without ever constructing OIDC config.
    """
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", "1")
    monkeypatch.setenv("OMNIGENT_OIDC_ISSUER", "https://issuer.example")
    _stop_after_auth_decision(monkeypatch)

    result = CliRunner().invoke(cli, ["server", "--host", "0.0.0.0"])

    assert isinstance(result.exception, _StopBeforeServe), result.output
    assert "WARNING" not in result.output
    assert "401" not in result.output
