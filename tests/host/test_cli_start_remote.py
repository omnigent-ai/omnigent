"""`omnigent start` against an unreachable server (#4986).

A live-but-offline remote daemon (zombie tunnel) must be torn down and
respawned rather than reused.
"""

from __future__ import annotations

from omnigent import cli as cli_module
from omnigent.cli import _HostDaemonRecord


def _record(pid: int, log_path: str | None = "/tmp/host.log") -> _HostDaemonRecord:
    return _HostDaemonRecord(
        pid=pid,
        target="http://127.0.0.1:6767",
        mode="server",
        server_url="http://127.0.0.1:6767",
        log_path=log_path,
        started_at=1_700_000_000,
        host_id="host_1",
        config_sig="sig",
    )


def test_remote_reuse_tears_down_offline_zombie(monkeypatch) -> None:
    """A live-but-offline remote daemon is torn down and respawned, not reused."""
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(cli_module, "_find_daemon_record", lambda target: _record(4242))
    monkeypatch.setattr(cli_module, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cli_module, "_daemon_host_identity_changed", lambda rec: False)
    monkeypatch.setattr(cli_module, "_daemon_tunnel_recovers", lambda rec, **kw: False)
    monkeypatch.setattr(
        cli_module, "_terminate_host_unit", lambda rec, reason: calls.append(("terminate", reason))
    )

    decision = cli_module._reuse_existing_daemon_record("http://127.0.0.1:6767")
    assert decision.reuse is False
    assert calls and calls[0][0] == "terminate"
    assert "not online" in calls[0][1]


def test_remote_reuse_keeps_recovering_tunnel(monkeypatch) -> None:
    """A daemon whose tunnel recovers within the grace window is reused."""
    monkeypatch.setattr(cli_module, "_find_daemon_record", lambda target: _record(4243))
    monkeypatch.setattr(cli_module, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cli_module, "_daemon_host_identity_changed", lambda rec: False)
    monkeypatch.setattr(cli_module, "_daemon_tunnel_recovers", lambda rec, **kw: True)
    terminated: list[str] = []
    monkeypatch.setattr(
        cli_module, "_terminate_host_unit", lambda rec, reason: terminated.append(reason)
    )

    decision = cli_module._reuse_existing_daemon_record("http://127.0.0.1:6767")
    assert decision.reuse is True
    assert not terminated


def test_remote_foreground_daemon_still_reused_by_pid(monkeypatch) -> None:
    """Foreground hosts (no log_path) keep PID-liveness-only reuse — the CLI
    never kills an interactive process."""
    monkeypatch.setattr(
        cli_module, "_find_daemon_record", lambda target: _record(4244, log_path=None)
    )
    monkeypatch.setattr(cli_module, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cli_module, "_daemon_host_identity_changed", lambda rec: False)
    probed: list[object] = []
    monkeypatch.setattr(
        cli_module,
        "_daemon_tunnel_recovers",
        lambda rec, **kw: probed.append(rec) or False,
    )

    decision = cli_module._reuse_existing_daemon_record("http://127.0.0.1:6767")
    assert decision.reuse is True
    assert not probed, "foreground daemons must not be probed/terminated"
