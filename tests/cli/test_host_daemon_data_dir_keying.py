"""One data dir, one host daemon: loopback spellings share the local record.

The daemon registry used to key records on the raw ``--server`` string, so a
single tracked local server accrued one record (and one live daemon) per
loopback spelling, and ``host stop`` for one spelling left the others running.
These tests pin the CLI-level keying: a loopback URL naming the data dir's
tracked server collapses to the per-data-dir ``local`` record, and a collapsed
spawn runs the daemon in local mode.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent import cli


def _track_local_server(base: Path, port: int) -> None:
    """Write a data-dir pidfile declaring *port* as the tracked local server."""
    (base / "local_server.pid").write_text(f"4242\n{port}\n")


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the daemon registry (and its data dir) at the per-test tmp dir."""
    monkeypatch.setattr(cli, "_HOST_PID_PATH", tmp_path / "host.pid")


def test_loopback_spellings_of_tracked_server_share_the_local_record(
    tmp_path: Path,
) -> None:
    """Every loopback spelling of the tracked server keys to one record."""
    _track_local_server(tmp_path, 6767)
    for spelling in (
        "http://127.0.0.1:6767",
        "http://localhost:6767",
        "http://127.0.0.1:6767/",
    ):
        assert cli._normalize_daemon_target(spelling) == cli._LOCAL_DAEMON_MARKER
    assert cli._daemon_record_path(
        cli._normalize_daemon_target("http://localhost:6767")
    ) == cli._daemon_record_path(cli._LOCAL_DAEMON_MARKER)


def test_urls_of_other_servers_keep_their_own_key(tmp_path: Path) -> None:
    """A URL that does not name the tracked instance keys on itself."""
    _track_local_server(tmp_path, 6767)
    assert cli._normalize_daemon_target("http://127.0.0.1:9999") == "http://127.0.0.1:9999"
    assert (
        cli._normalize_daemon_target("https://x.example.com:6767")
        == "https://x.example.com:6767"
    )


def test_loopback_url_without_a_tracked_server_keeps_its_own_key() -> None:
    assert cli._normalize_daemon_target("http://127.0.0.1:6767") == "http://127.0.0.1:6767"


def test_collapsed_spawn_runs_the_daemon_in_local_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``--server`` spelling of the tracked server spawns the local daemon.

    A collapsed target must take the full local path: local-mode args, the
    local config signature, and the local daemon env allowlist — otherwise the
    record under the ``local`` key would carry server-mode metadata and the
    reuse gate would tear it down as config drift on every invocation.
    """
    _track_local_server(tmp_path, 6767)
    monkeypatch.setattr(cli, "_load_existing_host_id", lambda: "host_abc")

    sig_calls: list[bool] = []

    def _fake_signature(*, include_features: bool = True) -> str:
        sig_calls.append(include_features)
        return "sig"

    monkeypatch.setattr(cli, "server_config_signature", _fake_signature)

    env_urls: list[str | None] = []

    def _fake_env(*, server_url: str | None) -> dict[str, str]:
        env_urls.append(server_url)
        return {}

    monkeypatch.setattr(cli, "_build_host_daemon_env", _fake_env)

    captured_args: list[str] = []

    def _capture_spawn(*, args: list[str], env: dict[str, str]) -> cli._SpawnedDaemonProcess:
        captured_args.extend(args)
        return cli._SpawnedDaemonProcess(pid=4321, log_path=str(tmp_path / "daemon.log"))

    monkeypatch.setattr(cli, "_spawn_host_daemon_process", _capture_spawn)
    monkeypatch.setattr(cli, "_wait_for_daemon_claim", lambda target, spawned: None)

    assert cli._ensure_host_daemon("http://localhost:6767") is False

    assert "--local" in captured_args
    assert "http://localhost:6767" not in captured_args
    assert sig_calls == [True]
    assert env_urls == [None]
