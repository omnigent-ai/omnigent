"""Regression tests for per-data-dir daemon keying and loopback alias reuse."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omnigent import cli


def _track_local_server(base: Path, port: int) -> None:
    """Write a data-dir pidfile declaring *port* as this (live) process's local server."""
    (base / "local_server.pid").write_text(f"{os.getpid()}\n{port}\n")


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
        cli._normalize_daemon_target("https://x.example.com:6767") == "https://x.example.com:6767"
    )


def test_loopback_url_without_a_tracked_server_keeps_its_own_key() -> None:
    assert cli._normalize_daemon_target("http://127.0.0.1:6767") == "http://127.0.0.1:6767"


def test_find_daemon_record_resolves_raw_url_record_as_local(tmp_path: Path) -> None:
    """A record keyed on a raw loopback URL resolves when looked up as ``local``.

    A daemon recorded under its raw ``http://127.0.0.1:<port>`` key (spawned
    before the local server was tracked, or by a pre-upgrade build) must not
    orphan once the pidfile appears and that URL starts collapsing to
    ``local``: the lookup re-normalizes each stored target, so the drifted
    record is found and reused instead of a duplicate daemon being spawned.
    """
    _track_local_server(tmp_path, 6767)
    cli._write_daemon_record(
        cli._HostDaemonRecord(
            pid=4242,
            target="http://127.0.0.1:6767",
            mode="local",
            server_url=None,
            log_path=None,
            started_at=100,
            host_id="host_abc",
        )
    )

    found = cli._find_daemon_record(cli._LOCAL_DAEMON_MARKER)

    assert found is not None
    assert found.target == "http://127.0.0.1:6767"
    assert found.pid == 4242


def test_find_daemon_record_matches_local_record_by_server_url_without_pidfile(
    tmp_path: Path,
) -> None:
    """A local daemon stays addressable by its server URL after the pidfile is gone.

    A foreground server removes ``local_server.pid`` on exit while a daemon that
    collapsed onto ``local`` may still be running, so ``host stop --server <url>``
    must reach that record through its resolved server URL.
    """
    _track_local_server(tmp_path, 6767)
    cli._write_daemon_record(
        cli._HostDaemonRecord(
            pid=4242,
            target=cli._LOCAL_DAEMON_MARKER,
            mode="local",
            server_url=None,
            log_path=None,
            started_at=100,
            host_id="host_abc",
            resolved_server_url="http://127.0.0.1:6767",
        )
    )
    (tmp_path / "local_server.pid").unlink()

    target = cli._normalize_daemon_target("http://localhost:6767")
    found = cli._find_daemon_record(target)

    assert target == "http://localhost:6767"
    assert found is not None
    assert found.target == cli._LOCAL_DAEMON_MARKER


def test_same_local_server_rejects_an_unparsable_url() -> None:
    """A malformed URL (unclosed IPv6 bracket) is a mismatch, not a crash."""
    assert cli._same_local_server("http://127.0.0.1:6767", "http://[::1:6767") is False


@pytest.mark.parametrize("running_sig", ["sig-of-the-running-server", None])
def test_collapsed_spawn_runs_the_daemon_in_local_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, running_sig: str | None
) -> None:
    """A ``--server`` spelling of the tracked server spawns the local daemon in adopt mode.

    A collapsed target must take the local path (local-mode args and the local
    daemon env allowlist) so the record under the ``local`` key never carries
    server-mode metadata, but it adopts the running server: ``--adopt-server``
    is passed and the record is stamped with the running server's signature
    (or left unverifiable when that server has none), never this shell's.
    """
    _track_local_server(tmp_path, 6767)
    monkeypatch.setattr(cli, "_load_existing_host_id", lambda: "host_abc")
    monkeypatch.setattr(cli, "_read_local_server_sig", lambda: running_sig)

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
    captured_env: dict[str, str] = {}
    spawned = cli._SpawnedDaemonProcess(pid=4321, log_path=str(tmp_path / "daemon.log"))

    def _capture_spawn(*, args: list[str], env: dict[str, str]) -> cli._SpawnedDaemonProcess:
        captured_args.extend(args)
        captured_env.update(env)
        return spawned

    monkeypatch.setattr(cli, "_spawn_host_daemon_process", _capture_spawn)
    claimed = cli._HostDaemonRecord(
        pid=spawned.pid,
        target=cli._LOCAL_DAEMON_MARKER,
        mode="local",
        server_url=None,
        log_path=spawned.log_path,
        started_at=1_000_000,
        host_id="host_abc",
    )
    monkeypatch.setattr(cli, "_wait_for_daemon_claim", lambda target, spawned: claimed)

    assert cli._ensure_host_daemon("http://localhost:6767") is False

    assert "--local" in captured_args
    assert "--adopt-server" in captured_args
    assert "http://localhost:6767" not in captured_args
    assert captured_env[cli.DAEMON_CONFIG_SIG_ENV_VAR] == (running_sig or "")
    assert sig_calls == []
    assert env_urls == [None]
