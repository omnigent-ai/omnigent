"""Tests for the host daemon lifecycle guard (flock + record self-monitor)."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from omnigent.host import connect
from omnigent.host.connect import HostProcess
from omnigent.host.daemon_lifecycle import (
    WORKSPACE_DISCOVERY_BUDGET_S,
    DaemonLifecycleLock,
    HostDaemonRecord,
    daemon_record_path,
    daemon_target_server_url,
    find_same_workspace_daemon,
    normalize_daemon_target,
    probe_workspace_id,
    record_flock_is_held,
    write_daemon_record,
)
from omnigent.host.identity import HostIdentity
from omnigent.util.tls import client_ssl_context


def _write_record(path: Path, pid: int) -> None:
    """Write a minimal daemon record naming *pid* as the owner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "target": "local", "mode": "local"}))


@pytest.mark.parametrize(
    ("server_url", "expected"),
    [
        (None, "local"),
        ("", "local"),
        ("https://x.example.com/", "https://x.example.com"),
        ("HTTPS://X.Example.COM:443/api/", "https://x.example.com/api"),
        ("http://X.Example.COM:80/", "http://x.example.com"),
        ("http://X.Example.COM:8080/Path/", "http://x.example.com:8080/Path"),
        (
            "https://X.Example.COM/api/?next=/",
            "https://x.example.com/api?next=/",
        ),
        ("https://X.Example.COM/api/#/", "https://x.example.com/api#/"),
        ("https://[2001:DB8::1]:443/", "https://[2001:db8::1]"),
        ("unix:///tmp/omnigent.sock/", "unix:///tmp/omnigent.sock"),
    ],
)
def test_normalize_daemon_target(server_url: str | None, expected: str) -> None:
    assert normalize_daemon_target(server_url) == expected


def test_equivalent_server_urls_share_record_path(tmp_path: Path) -> None:
    canonical = normalize_daemon_target("https://x.example.com/api")
    equivalent = normalize_daemon_target("HTTPS://X.EXAMPLE.COM:443/api/")

    assert canonical == equivalent
    assert daemon_record_path(canonical, base_dir=tmp_path) == daemon_record_path(
        equivalent, base_dir=tmp_path
    )


def test_daemon_targets_differ_per_workspace_on_one_account_host(tmp_path: Path) -> None:
    """Daemons for two workspaces behind one account host must not share a record."""
    server = "https://acct.example.com/api/2.0/omnigent"
    prod = normalize_daemon_target(server, org_id="111")
    canary = normalize_daemon_target(server, org_id="222")

    assert prod == "https://acct.example.com/api/2.0/omnigent?o=111"
    assert daemon_record_path(prod, base_dir=tmp_path) != daemon_record_path(
        canary, base_dir=tmp_path
    )
    # An explicit selector on the URL is kept rather than overridden...
    assert normalize_daemon_target(f"{server}?o=333", org_id="111") == f"{server}?o=333"
    # ...but another query parameter doesn't suppress the workspace key.
    assert normalize_daemon_target(f"{server}?trace=1", org_id="111") == f"{server}?trace=1&o=111"
    assert normalize_daemon_target(f"{server}?trace=1", org_id="222") != normalize_daemon_target(
        f"{server}?trace=1", org_id="111"
    )


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (
            "https://acct.example.com/api/2.0/omnigent?o=111",
            "https://acct.example.com/api/2.0/omnigent",
        ),
        ("https://x.example.com/api?next=/&o=1", "https://x.example.com/api?next=%2F"),
        ("https://x.example.com/api", "https://x.example.com/api"),
        ("local", "local"),
    ],
)
def test_daemon_target_server_url_drops_only_the_workspace_key(target: str, expected: str) -> None:
    """Request paths are appended to the server URL, so it must not carry ``?o=``."""
    assert daemon_target_server_url(target) == expected


def test_find_daemon_record_reuses_legacy_url_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent import cli

    monkeypatch.setattr(cli, "_HOST_PID_PATH", tmp_path / "host.pid")
    legacy_target = "https://X.Example.COM:443/api"
    legacy_record = cli._HostDaemonRecord(
        pid=4242,
        target=legacy_target,
        mode="server",
        server_url=legacy_target,
        log_path="daemon.log",
        started_at=100,
    )
    cli._write_daemon_record(legacy_record)

    canonical_target = normalize_daemon_target("https://x.example.com/api")
    found = cli._find_daemon_record(canonical_target)

    assert found == legacy_record


def test_reuse_legacy_record_probes_its_original_lock_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent import cli

    monkeypatch.setattr(cli, "_HOST_PID_PATH", tmp_path / "host.pid")
    legacy_target = "https://X.Example.COM:443/api"
    cli._write_daemon_record(
        cli._HostDaemonRecord(
            pid=4242,
            target=legacy_target,
            mode="server",
            server_url=legacy_target,
            log_path="daemon.log",
            started_at=100,
        )
    )
    probed_paths: list[Path] = []
    monkeypatch.setattr(
        cli,
        "_record_flock_is_held",
        lambda path: probed_paths.append(path) or True,
    )
    monkeypatch.setattr(cli, "_daemon_host_identity_changed", lambda record: False)

    decision = cli._reuse_existing_daemon_record(
        normalize_daemon_target("https://x.example.com/api")
    )

    assert decision.reuse is True
    assert probed_paths == [cli._daemon_record_path(legacy_target)]


def test_record_path_uses_digest(tmp_path: Path) -> None:
    record = daemon_record_path("local", base_dir=tmp_path)
    assert record.parent == tmp_path / "daemons"
    assert record.suffix == ".json"
    # Same target → same path (stable digest); different target → different.
    assert daemon_record_path("local", base_dir=tmp_path) == record
    assert daemon_record_path("https://x.example.com", base_dir=tmp_path) != record


def test_acquire_flocks_the_record_and_preserves_content(tmp_path: Path) -> None:
    record = daemon_record_path("local", base_dir=tmp_path)
    _write_record(record, 4242)
    lock = DaemonLifecycleLock.for_target("local", base_dir=tmp_path, pid=4242)
    assert lock.acquire() is True
    # Acquiring must not clobber the CLI-owned record content.
    assert json.loads(record.read_text())["pid"] == 4242

    # A second holder cannot take the record's exclusive lock while held.
    import fcntl

    fd = os.open(record, os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)

    # After release the lock is free and the record still exists.
    lock.release()
    assert record.exists()
    fd = os.open(record, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def test_acquire_creates_record_when_absent(tmp_path: Path) -> None:
    # The elected child locks before writing its metadata, so acquire() must
    # create + flock the record file rather than fail when it is absent.
    lock = DaemonLifecycleLock.for_target("local", base_dir=tmp_path, pid=7)
    assert lock.acquire() is True
    assert daemon_record_path("local", base_dir=tmp_path).exists()
    lock.release()


def test_different_targets_can_be_claimed_concurrently(tmp_path: Path) -> None:
    first = DaemonLifecycleLock.for_target("https://one.example.com", base_dir=tmp_path)
    second = DaemonLifecycleLock.for_target("https://two.example.com", base_dir=tmp_path)
    assert first.acquire() is True
    try:
        assert second.acquire() is True
    finally:
        first.release()
        second.release()


def test_still_owner_delete_and_mismatch(tmp_path: Path) -> None:
    lock = DaemonLifecycleLock.for_target("local", base_dir=tmp_path, pid=100)
    record = daemon_record_path("local", base_dir=tmp_path)

    # Missing record: not owner.
    assert lock.still_owner() is False

    # Matching pid: owner.
    _write_record(record, 100)
    assert lock.still_owner() is True

    # Reassigned pid: not owner.
    _write_record(record, 200)
    assert lock.still_owner() is False

    # Malformed record: treated as still-owner (transient), never a false kill.
    record.write_text("{ not json")
    assert lock.still_owner() is True


def test_record_flock_is_held_states(tmp_path: Path) -> None:
    record = daemon_record_path("local", base_dir=tmp_path)

    # No record file yet → indeterminate.
    assert record_flock_is_held(record) is None

    # Record exists but unlocked → free (owner dead / never locked).
    _write_record(record, 100)
    assert record_flock_is_held(record) is False

    # A held lock → probe reports it held; freed after release.
    lock = DaemonLifecycleLock.for_target("local", base_dir=tmp_path, pid=100)
    assert lock.acquire() is True
    assert record_flock_is_held(record) is True
    lock.release()
    assert record_flock_is_held(record) is False


def test_background_daemon_claims_record_before_connecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The elected child owns and records the target before opening a tunnel."""
    from omnigent.host import _daemon_entry
    from omnigent.host import identity as identity_module
    from omnigent.process_logging import DATA_DIR_ENV_VAR

    target = "https://server.example.com/api/2.0/omnigent"
    log_path = tmp_path / "host.log"
    monkeypatch.setenv(DATA_DIR_ENV_VAR, str(tmp_path))
    monkeypatch.setenv("OMNIGENT_HOST_DAEMON_CONFIG_SIG", "config-signature")
    monkeypatch.setattr(sys, "argv", ["omnigent.host._daemon_entry", "--server", target])
    monkeypatch.setattr(
        "omnigent.process_logging.configure_process_logging", lambda *_a, **_kw: log_path
    )
    monkeypatch.setattr(
        identity_module,
        "load_or_create_host_identity",
        lambda: HostIdentity(host_id=_HOST_ID, name="elected"),
    )
    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path", lambda: tmp_path / "auth_tokens.json"
    )
    monkeypatch.setattr(
        "omnigent.host.daemon_lifecycle.probe_workspace_id",
        lambda url, *, org_id=None, **_kwargs: "7",
    )

    connected: list[str] = []

    def _run(*, server_url: str, daemon_target: str, lifecycle_lock: object) -> None:
        assert record_flock_is_held(daemon_record_path(target, base_dir=tmp_path)) is True
        assert lifecycle_lock is not None
        # The workspace stays claimed while the daemon serves.
        other = DaemonLifecycleLock.for_workspace(_HOST_ID, "7", base_dir=tmp_path)
        assert other.try_acquire() is False
        connected.append(f"{server_url}|{daemon_target}")

    monkeypatch.setattr("omnigent.host.connect.run_host_process", _run)

    _daemon_entry.main()

    payload = json.loads(daemon_record_path(target, base_dir=tmp_path).read_text())
    assert payload["pid"] == os.getpid()
    assert payload["host_id"] == _HOST_ID
    assert payload["config_sig"] == "config-signature"
    assert payload["workspace_id"] == "7"
    assert connected == [f"{target}|{target}"]
    assert record_flock_is_held(daemon_record_path(target, base_dir=tmp_path)) is False
    released = DaemonLifecycleLock.for_workspace(_HOST_ID, "7", base_dir=tmp_path)
    assert released.try_acquire() is True
    released.release()


@pytest.mark.parametrize("held", ["target", "workspace"])
def test_background_daemon_loser_exits_before_connecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held: str
) -> None:
    """A second child for one target, or for one host id and workspace, never connects.

    It leaves the live owner's record alone, and a child that yields to the
    workspace's daemon removes its own record and releases its target lock.
    """
    from omnigent.host import _daemon_entry
    from omnigent.host import identity as identity_module
    from omnigent.process_logging import DATA_DIR_ENV_VAR

    target = "https://server.example.com/api/2.0/omnigent"
    monkeypatch.setenv(DATA_DIR_ENV_VAR, str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["omnigent.host._daemon_entry", "--server", target])
    monkeypatch.setattr(
        "omnigent.process_logging.configure_process_logging",
        lambda *_a, **_kw: tmp_path / "loser.log",
    )
    monkeypatch.setattr(
        "omnigent.host.connect.run_host_process",
        lambda **_kw: pytest.fail("losing daemon must not connect"),
    )
    monkeypatch.setattr(
        identity_module,
        "load_or_create_host_identity",
        lambda: HostIdentity(host_id=_HOST_ID, name="loser"),
    )
    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path", lambda: tmp_path / "auth_tokens.json"
    )
    monkeypatch.setattr(
        "omnigent.host.daemon_lifecycle.probe_workspace_id",
        lambda url, *, org_id=None, **_kwargs: "7",
    )
    record_path = daemon_record_path(target, base_dir=tmp_path)
    if held == "target":
        owner = DaemonLifecycleLock.for_target(target, base_dir=tmp_path, pid=4242)
        assert owner.acquire() is True
        _write_record(record_path, 4242)
    else:
        owner = DaemonLifecycleLock.for_workspace(_HOST_ID, "7", base_dir=tmp_path)
        assert owner.try_acquire() is True
    before = record_path.read_text() if record_path.exists() else None
    try:
        _daemon_entry.main()
        assert (record_path.read_text() if record_path.exists() else None) == before
        if held == "workspace":
            assert not (tmp_path / "host.pid").exists()
            target_lock = DaemonLifecycleLock.for_target(target, base_dir=tmp_path)
            assert target_lock.try_acquire() is True
            target_lock.release()
    finally:
        owner.release()


def _record(target: str, pid: int) -> HostDaemonRecord:
    from omnigent import cli

    return cli._HostDaemonRecord(
        pid=pid,
        target=target,
        mode="local",
        server_url=None,
        log_path="x",
        started_at=100,
    )


def test_daemon_owner_is_live_flock_then_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent import cli

    monkeypatch.setattr(cli, "_HOST_PID_PATH", tmp_path / "host.pid")
    target = "local"
    record_path = cli._daemon_record_path(target)
    _write_record(record_path, 999)

    # Held lock → alive, regardless of the PID check.
    lock = DaemonLifecycleLock.for_target(target, base_dir=tmp_path, pid=999)
    assert lock.acquire() is True
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: False)
    assert cli._daemon_owner_is_live(_record(target, 999)) is True
    lock.release()

    # Free lock → fall back to pid identity: a pid that is still the
    # recorded daemon (e.g. mid-startup, lock not yet grabbed) → alive.
    monkeypatch.setattr(cli, "_pid_is_recorded_daemon", lambda record: True)
    assert cli._daemon_owner_is_live(_record(target, 999)) is True

    # Free lock + a pid that is no longer the recorded daemon (dead, or
    # recycled to an unrelated process after a reboot) → dead (reapable).
    monkeypatch.setattr(cli, "_pid_is_recorded_daemon", lambda record: False)
    assert cli._daemon_owner_is_live(_record(target, 999)) is False


def test_live_daemon_conflict_uses_flock_then_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent import cli

    monkeypatch.setattr(cli, "_HOST_PID_PATH", tmp_path / "host.pid")
    target = "local"
    claimer = _record(target, 111)

    # A different-pid record whose flock is held → a real live conflict.
    cli._write_daemon_record(_record(target, 222))
    lock = DaemonLifecycleLock.for_target(target, base_dir=tmp_path, pid=222)
    assert lock.acquire() is True
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: True)
    assert cli._live_daemon_conflict(claimer) is not None
    lock.release()

    # Free lock + dead PID → the owner is gone, so not a conflict.
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: False)
    assert cli._live_daemon_conflict(claimer) is None


def test_live_daemon_conflict_probes_legacy_record_lock_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent import cli

    monkeypatch.setattr(cli, "_HOST_PID_PATH", tmp_path / "host.pid")
    legacy_target = "https://X.Example.COM:443/api"
    existing = _record(legacy_target, 222)
    cli._write_daemon_record(existing)
    probed_paths: list[Path] = []
    monkeypatch.setattr(
        cli,
        "_record_flock_is_held",
        lambda path: probed_paths.append(path) or True,
    )
    monkeypatch.setattr(cli, "_pid_is_recorded_daemon", lambda record: False)

    canonical_target = normalize_daemon_target("https://x.example.com/api")
    conflict = cli._live_daemon_conflict(_record(canonical_target, 111))

    assert conflict == existing
    assert probed_paths == [cli._daemon_record_path(legacy_target)]


def _host_with_lock(lock: DaemonLifecycleLock) -> HostProcess:
    return HostProcess(
        identity=HostIdentity(host_id="host_lifecycle", name="test"),
        server_url="http://localhost:8000",
        lifecycle_lock=lock,
    )


async def test_monitor_terminates_after_record_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connect, "_LIFECYCLE_POLL_INTERVAL_S", 0.01)
    pid = os.getpid()
    lock = DaemonLifecycleLock.for_target("local", base_dir=tmp_path, pid=pid)
    record = daemon_record_path("local", base_dir=tmp_path)
    _write_record(record, pid)

    host = _host_with_lock(lock)

    monitor = asyncio.create_task(host._lifecycle_monitor_loop())
    try:
        # Let it confirm ownership, then delete the record.
        await asyncio.sleep(0.05)
        assert not host._lifecycle_lost.is_set()
        record.unlink()
        # On loss it sets the flag (and aborts the tunnel — a no-op here, no
        # live _ws) so run() breaks, then returns on its own.
        await asyncio.wait_for(host._lifecycle_lost.wait(), timeout=2.0)
        await asyncio.wait_for(monitor, timeout=1.0)
    finally:
        if not monitor.done():
            monitor.cancel()


async def test_monitor_startup_grace_before_first_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No record exists yet (the launching CLI has not written it). The monitor
    # must not self-terminate until it has owned the record at least once.
    monkeypatch.setattr(connect, "_LIFECYCLE_POLL_INTERVAL_S", 0.01)
    lock = DaemonLifecycleLock.for_target("local", base_dir=tmp_path, pid=os.getpid())
    host = _host_with_lock(lock)

    monitor = asyncio.create_task(host._lifecycle_monitor_loop())
    try:
        await asyncio.sleep(0.1)
        assert not host._lifecycle_lost.is_set()
    finally:
        monitor.cancel()
        with pytest.raises(asyncio.CancelledError):
            await monitor


_VANITY = "https://vanity.databricks.com/api/2.0/omnigent"
_WORKSPACE_HOST = "https://dbc-1.cloud.databricks.com/api/2.0/omnigent"
_HOST_ID = "a" * 32
_APP_A = "https://app-a.aws.databricksapps.com"
_APP_B = "https://app-b.aws.databricksapps.com"


def _server_record(
    server_url: str, *, pid: int = 4242, host_id: str = _HOST_ID
) -> HostDaemonRecord:
    """Build a live-looking server-mode daemon record for *server_url*."""
    target = normalize_daemon_target(server_url)
    return HostDaemonRecord(
        pid=pid,
        target=target,
        mode="server",
        server_url=target,
        log_path=None,
        started_at=1,
        host_id=host_id,
    )


@pytest.mark.parametrize(
    ("requested", "record", "logins", "live", "matches"),
    [
        # Two URLs for one workspace collide on (workspace, host id).
        (
            _VANITY,
            _server_record(_WORKSPACE_HOST),
            {_VANITY: "1", _WORKSPACE_HOST: "1"},
            True,
            True,
        ),
        # The URL's own ?o= selector identifies the workspace without a login record.
        (f"{_VANITY}?o=1", _server_record(_WORKSPACE_HOST), {_WORKSPACE_HOST: "1"}, True, True),
        # The same URL's per-target record (keyed by workspace) is the caller's job...
        (
            _VANITY,
            HostDaemonRecord(
                pid=4242,
                target=f"{_VANITY}?o=1",
                mode="server",
                server_url=_VANITY,
                log_path=None,
                started_at=1,
                host_id=_HOST_ID,
            ),
            {_VANITY: "1"},
            True,
            False,
        ),
        # ...as is its bare per-URL record.
        (_VANITY, _server_record(_VANITY), {_VANITY: "1"}, True, False),
        (
            _VANITY,
            _server_record(_WORKSPACE_HOST),
            {_VANITY: "1", _WORKSPACE_HOST: "2"},
            True,
            False,
        ),
        # An unknown workspace id never matches: non-Databricks servers keep per-URL daemons.
        (_VANITY, _server_record(_WORKSPACE_HOST), {_WORKSPACE_HOST: "1"}, True, False),
        (_VANITY, _server_record(_WORKSPACE_HOST), {_VANITY: "1"}, True, False),
        # A different host id is a different host, which the server keeps separately.
        (
            _VANITY,
            _server_record(_WORKSPACE_HOST, host_id="b" * 32),
            {_VANITY: "1", _WORKSPACE_HOST: "1"},
            True,
            False,
        ),
        (
            _VANITY,
            _server_record(_WORKSPACE_HOST),
            {_VANITY: "1", _WORKSPACE_HOST: "1"},
            False,
            False,
        ),
        # Two apps on one workspace are separate servers, even with one org id.
        (
            _APP_B,
            _server_record(_APP_A),
            {_APP_A: "1", _APP_B: "1"},
            True,
            False,
        ),
    ],
)
def test_find_same_workspace_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requested: str,
    record: HostDaemonRecord,
    logins: dict[str, str],
    live: bool,
    matches: bool,
) -> None:
    from omnigent.cli_auth import store_databricks_auth

    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path", lambda: tmp_path / "auth_tokens.json"
    )
    for server_url, org_id in logins.items():
        store_databricks_auth(server_url, "https://dbc-1.cloud.databricks.com", org_id=org_id)

    found = find_same_workspace_daemon(
        [record], server_url=requested, host_id=_HOST_ID, is_live=lambda _record: live
    )

    assert (found == record) is matches


def test_find_same_workspace_daemon_skips_local_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.cli_auth import store_databricks_auth

    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path", lambda: tmp_path / "auth_tokens.json"
    )
    store_databricks_auth(_VANITY, "https://dbc-1.cloud.databricks.com", org_id="1")
    local = HostDaemonRecord(
        pid=4242,
        target="local",
        mode="local",
        server_url=None,
        log_path=None,
        started_at=1,
        host_id=_HOST_ID,
    )

    assert (
        find_same_workspace_daemon(
            [local], server_url=_VANITY, host_id=_HOST_ID, is_live=lambda _record: True
        )
        is None
    )


def test_find_same_workspace_daemon_uses_recorded_and_probed_workspace_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record's own workspace id, or the server's answer, decides the match.

    The server wins over a stale login record, as in
    :func:`resolve_daemon_workspace_id`.
    """
    from omnigent.cli_auth import store_databricks_auth

    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path", lambda: tmp_path / "auth_tokens.json"
    )
    store_databricks_auth(_VANITY, "https://dbc-1.cloud.databricks.com", org_id="2")
    recorded = dataclasses.replace(_server_record(_WORKSPACE_HOST), workspace_id="1")
    dead = dataclasses.replace(
        _server_record("https://dead.databricks.com/api/2.0/omnigent"), pid=7
    )
    probed: list[tuple[str, str | None]] = []

    def _probe(url: str, *, org_id: str | None = None) -> str | None:
        probed.append((url, org_id))
        return "1"

    found = find_same_workspace_daemon(
        [dead, recorded],
        server_url=_VANITY,
        host_id=_HOST_ID,
        is_live=lambda record: record.pid != 7,
        probe=_probe,
    )

    assert found == recorded
    # Only the requested URL needed the server, which got the login record's
    # selector; the dead record cost nothing.
    assert probed == [(_VANITY, "2")]


def test_find_same_workspace_daemon_matches_host_id_spellings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server canonicalizes host ids, so a dashed spelling is the same host."""
    import uuid

    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path", lambda: tmp_path / "auth_tokens.json"
    )
    dashed = str(uuid.UUID(_HOST_ID)).upper()
    record = dataclasses.replace(_server_record(_WORKSPACE_HOST, host_id=dashed), workspace_id="1")

    found = find_same_workspace_daemon(
        [record],
        server_url=_VANITY,
        host_id=_HOST_ID,
        is_live=lambda _record: True,
        workspace_id="1",
    )

    assert found == record


def test_find_same_workspace_daemon_legacy_only_skips_lock_holders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.cli_auth import store_databricks_auth

    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path", lambda: tmp_path / "auth_tokens.json"
    )
    store_databricks_auth(_WORKSPACE_HOST, "https://dbc-1.cloud.databricks.com", org_id="1")
    holder = dataclasses.replace(_server_record(_WORKSPACE_HOST), workspace_id="1")
    legacy = _server_record(_WORKSPACE_HOST)

    def _find(record: HostDaemonRecord) -> HostDaemonRecord | None:
        return find_same_workspace_daemon(
            [record],
            server_url=_VANITY,
            host_id=_HOST_ID,
            is_live=lambda _record: True,
            workspace_id="1",
            legacy_only=True,
        )

    assert _find(holder) is None
    assert _find(legacy) == legacy


def test_resolve_daemon_workspace_id_prefers_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server's answer beats a stale login record; the record covers a silent server."""
    from omnigent.cli_auth import store_databricks_auth
    from omnigent.host.daemon_lifecycle import resolve_daemon_workspace_id

    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path", lambda: tmp_path / "auth_tokens.json"
    )
    store_databricks_auth(_VANITY, "https://dbc-1.cloud.databricks.com", org_id="2")
    sent: list[str | None] = []

    def _answers(answer: str | None):
        def _probe(url: str, *, org_id: str | None = None, **_kwargs: object) -> str | None:
            sent.append(org_id)
            return answer

        return _probe

    assert resolve_daemon_workspace_id(_VANITY, probe=_answers("1")) == "1"
    assert resolve_daemon_workspace_id(_VANITY, probe=_answers(None)) == "2"
    # An app's URL names no workspace for this check, so it is never probed.
    store_databricks_auth(_APP_A, "https://dbc-1.cloud.databricks.com", org_id="2")
    assert resolve_daemon_workspace_id(_APP_A, probe=_answers("1")) is None
    # The known selector is sent, so a shared host can name the workspace.
    assert sent == ["2", "2"]


def test_workspace_lock_admits_one_daemon_per_host_and_workspace(tmp_path: Path) -> None:
    import uuid

    first = DaemonLifecycleLock.for_workspace(_HOST_ID, "1", base_dir=tmp_path)
    second = DaemonLifecycleLock.for_workspace(str(uuid.UUID(_HOST_ID)), "1", base_dir=tmp_path)
    other_workspace = DaemonLifecycleLock.for_workspace(_HOST_ID, "2", base_dir=tmp_path)
    other_host = DaemonLifecycleLock.for_workspace("b" * 32, "1", base_dir=tmp_path)
    try:
        assert first.try_acquire() is True
        assert second.try_acquire() is False
        assert other_workspace.try_acquire() is True
        assert other_host.try_acquire() is True
        first.release()
        assert second.try_acquire() is True
    finally:
        for lock in (first, second, other_workspace, other_host):
            lock.release()


@pytest.fixture
def _daemon_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the daemon registry and login records at *tmp_path*."""
    monkeypatch.setattr("omnigent.host.daemon_lifecycle.data_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path", lambda: tmp_path / "auth_tokens.json"
    )
    monkeypatch.setattr(
        "omnigent.host.daemon_lifecycle.probe_workspace_id",
        lambda url, *, org_id=None, **_kwargs: "1",
    )
    return tmp_path


def test_daemon_yields_when_another_daemon_holds_the_workspace(_daemon_registry: Path) -> None:
    from omnigent.host._daemon_entry import _claim_workspace

    holder = DaemonLifecycleLock.for_workspace(_HOST_ID, "1")
    assert holder.try_acquire() is True
    try:
        workspace_id, lock = _claim_workspace(
            server_url=_VANITY, daemon_target=_VANITY, host_id=_HOST_ID
        )
    finally:
        holder.release()

    assert (workspace_id, lock) == ("1", None)


def test_daemon_yields_to_an_older_version_daemon_on_the_workspace(
    _daemon_registry: Path,
) -> None:
    """A live daemon without the workspace lock still owns the workspace."""
    from omnigent.host._daemon_entry import _claim_workspace

    legacy = _server_record(_WORKSPACE_HOST, pid=os.getpid())
    legacy_lock = DaemonLifecycleLock.for_target(legacy.target)
    assert legacy_lock.try_acquire() is True
    write_daemon_record(legacy)
    try:
        workspace_id, lock = _claim_workspace(
            server_url=_VANITY, daemon_target=_VANITY, host_id=_HOST_ID
        )
    finally:
        legacy_lock.release()

    assert (workspace_id, lock) == ("1", None)
    # The yielding daemon released the workspace lock it briefly took.
    released = DaemonLifecycleLock.for_workspace(_HOST_ID, "1")
    assert released.try_acquire() is True
    released.release()


def test_daemon_without_flock_yields_to_current_workspace_record(
    _daemon_registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a working workspace lock, a live current-version record still wins."""
    from omnigent.host._daemon_entry import _claim_workspace

    current = dataclasses.replace(
        _server_record(_WORKSPACE_HOST, pid=os.getpid()), workspace_id="1"
    )
    current_lock = DaemonLifecycleLock.for_target(current.target)
    assert current_lock.try_acquire() is True
    write_daemon_record(current)
    try:
        with monkeypatch.context() as no_flock:
            no_flock.setattr(DaemonLifecycleLock, "try_acquire", lambda self: None)
            workspace_id, lock = _claim_workspace(
                server_url=_VANITY, daemon_target=_VANITY, host_id=_HOST_ID
            )
    finally:
        current_lock.release()

    assert (workspace_id, lock) == ("1", None)


def test_daemon_bounds_its_total_workspace_probe_time(
    _daemon_registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probes of several older daemons share one budget, below the launcher's claim wait."""
    from omnigent import cli
    from omnigent.host import _daemon_entry
    from omnigent.host._daemon_entry import _claim_workspace

    clock = [0.0]
    monkeypatch.setattr(_daemon_entry.time, "monotonic", lambda: clock[0])
    timeouts: list[float] = []

    def _probe(url: str, *, org_id: str | None = None, timeout_s: float = 3.0) -> str | None:
        timeouts.append(timeout_s)
        clock[0] += timeout_s  # Every probe runs to its deadline.
        return "1" if url == _VANITY else None

    monkeypatch.setattr("omnigent.host.daemon_lifecycle.probe_workspace_id", _probe)
    legacy_locks = []
    for index in range(3):
        legacy = _server_record(
            f"https://legacy-{index}.example.com/api/2.0/omnigent", pid=os.getpid()
        )
        legacy_lock = DaemonLifecycleLock.for_target(legacy.target)
        assert legacy_lock.try_acquire() is True
        legacy_locks.append(legacy_lock)
        write_daemon_record(legacy)
    try:
        workspace_id, lock = _claim_workspace(
            server_url=_VANITY, daemon_target=_VANITY, host_id=_HOST_ID
        )
    finally:
        for legacy_lock in legacy_locks:
            legacy_lock.release()
    try:
        assert workspace_id == "1"
        assert lock is not None
        assert sum(timeouts) <= WORKSPACE_DISCOVERY_BUDGET_S
        assert WORKSPACE_DISCOVERY_BUDGET_S < cli._DAEMON_CLAIM_TIMEOUT_S
        # The budget ran out before every older daemon was probed.
        assert len(timeouts) < 4
    finally:
        if lock is not None:
            lock.release()


def test_yielding_daemon_removes_its_record_left_from_before_a_restart_exec(
    _daemon_registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a same-pid exec, the record written before it is this process's own."""
    from omnigent.host._daemon_entry import _drop_own_record

    monkeypatch.setattr("omnigent.process_logging.data_dir", lambda: _daemon_registry)
    own = dataclasses.replace(_server_record(_VANITY), pid=os.getpid())
    write_daemon_record(own, update_legacy_pidfile=True)
    other = dataclasses.replace(_server_record(_WORKSPACE_HOST), pid=os.getpid() + 1)
    write_daemon_record(other)

    _drop_own_record(DaemonLifecycleLock.for_target(own.target))
    _drop_own_record(DaemonLifecycleLock.for_target(other.target))

    assert not daemon_record_path(own.target).exists()
    assert not (_daemon_registry / "host.pid").exists()
    # A record another process wrote is left for its owner or the CLI to prune.
    assert daemon_record_path(other.target).exists()


def test_daemon_skips_the_workspace_check_when_the_workspace_is_unknown(
    _daemon_registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.host._daemon_entry import _claim_workspace

    monkeypatch.setattr(
        "omnigent.host.daemon_lifecycle.probe_workspace_id",
        lambda url, *, org_id=None, **_kwargs: None,
    )

    assert _claim_workspace(server_url=_VANITY, daemon_target=_VANITY, host_id=_HOST_ID) == (
        None,
        None,
    )


def test_read_daemon_records_loads_old_records_and_skips_lock_files(
    _daemon_registry: Path,
) -> None:
    from omnigent.host.daemon_lifecycle import read_daemon_records

    old = _server_record(_WORKSPACE_HOST)
    path = daemon_record_path(old.target)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: v for k, v in dataclasses.asdict(old).items() if k != "workspace_id"}
    path.write_text(json.dumps(payload))
    # A yielding daemon's empty record file and a workspace lock file.
    daemon_record_path(_VANITY).write_text("")
    workspace_lock = DaemonLifecycleLock.for_workspace(_HOST_ID, "1")
    assert workspace_lock.try_acquire() is True
    # Syntactically valid JSON with wrong-typed fields.
    daemon_record_path("https://typo.example").write_text(
        json.dumps({**payload, "target": "https://typo.example", "pid": [1]})
    )
    numeric_target = daemon_record_path("https://numeric.example")
    numeric_target.write_text(json.dumps({**payload, "target": 123, "server_url": 7}))

    records = read_daemon_records()

    assert old in records
    assert "https://typo.example" not in {record.target for record in records}
    numeric = next(record for record in records if record.target == "123")
    assert numeric.server_url is None
    # Malformed records never abort the lookup, which still finds the valid
    # old record (its workspace comes from the probe).
    try:
        assert (
            find_same_workspace_daemon(
                records,
                server_url=_VANITY,
                host_id=_HOST_ID,
                is_live=lambda _record: True,
                workspace_id="1",
                probe=lambda url, *, org_id=None, **_kwargs: "1",
            )
            == old
        )
    finally:
        workspace_lock.release()


def test_record_owner_is_live_without_flock_never_signals(
    _daemon_registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a flock probe, liveness uses the process table, never a signal.

    On Windows ``os.kill(pid, 0)`` terminates the process, so the fallback
    must not call it.
    """
    import subprocess
    import time

    from omnigent.host import daemon_lifecycle
    from omnigent.host.daemon_lifecycle import record_owner_is_live

    monkeypatch.setattr(daemon_lifecycle, "record_flock_is_held", lambda _path: None)

    def _no_signals(*_args: object) -> None:
        raise AssertionError("liveness must not signal a process")

    monkeypatch.setattr(os, "kill", _no_signals)
    exited = subprocess.Popen([sys.executable, "-c", "pass"])
    exited.wait()
    now = int(time.time())

    assert record_owner_is_live(
        dataclasses.replace(_server_record(_VANITY), pid=os.getpid(), started_at=now)
    )
    assert not record_owner_is_live(
        dataclasses.replace(_server_record(_VANITY), pid=exited.pid, started_at=now)
    )
    # A pid created long after the record was written is a recycled pid.
    assert not record_owner_is_live(
        dataclasses.replace(_server_record(_VANITY), pid=os.getpid(), started_at=1)
    )


@pytest.mark.parametrize(
    ("response", "org_id", "expected"),
    [
        # Databricks names the workspace even when it rejects the request.
        ({"status": 401, "headers": {"x-databricks-org-id": "123"}}, "789", "123"),
        ({"status": 200, "headers": {"x-databricks-org-id": " 456 "}}, "789", "456"),
        ({"status": 401, "headers": {}}, "789", None),
        ({"status": 200, "headers": {"x-databricks-org-id": "not-a-number"}}, "789", None),
        # Non-ASCII digits and over-long values are not workspace ids.
        (
            {"status": 200, "headers": [(b"x-databricks-org-id", "\u0661\u0662".encode())]},
            "789",
            None,
        ),
        ({"status": 200, "headers": {"x-databricks-org-id": "1" * 33}}, "789", None),
        (httpx.ConnectError("unreachable"), "789", None),
        # A URL httpx rejects, or a selector it can't encode, is "unknown" too.
        (httpx.InvalidURL("bad URL"), "789", None),
        (UnicodeEncodeError("ascii", "\u00e9", 0, 1, "not ASCII"), "789", None),
        # With no known selector, none is sent.
        ({"status": 401, "headers": {"x-databricks-org-id": "123"}}, None, "123"),
    ],
)
def test_probe_workspace_id_reads_the_org_header(
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, object] | Exception,
    org_id: str | None,
    expected: str | None,
) -> None:
    calls: list[dict[str, object]] = []

    @contextlib.contextmanager
    def _stream(method: str, url: str, **kwargs: object) -> Iterator[httpx.Response]:
        calls.append({"method": method, "url": url, **kwargs})
        if isinstance(response, Exception):
            raise response
        # Only the headers are read: the body is never consumed.
        yield httpx.Response(
            response["status"],  # type: ignore[arg-type]
            headers=response["headers"],  # type: ignore[arg-type]
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(httpx, "stream", _stream)

    assert probe_workspace_id(f"{_VANITY}/", org_id=org_id) == expected
    (call,) = calls
    assert (call["method"], call["url"]) == ("GET", f"{_VANITY}/v1/me")
    assert call["verify"] is client_ssl_context()
    # The selector is forwarded only when known, no credentials are sent, and
    # a login redirect is never followed.
    assert call["headers"] == ({"X-Databricks-Org-Id": org_id} if org_id else {})
    assert call["follow_redirects"] is False


@pytest.mark.parametrize(
    ("server_url", "probed_url"),
    [
        # Userinfo is dropped, so the probe sends no credentials.
        (
            "https://user:secret@example.databricks.com:8443/api/2.0/omnigent",
            "https://example.databricks.com:8443/api/2.0/omnigent/v1/me",
        ),
        ("http://[::1]:8000/api/2.0/omnigent", "http://[::1]:8000/api/2.0/omnigent/v1/me"),
        # Malformed and non-HTTP URLs are never requested.
        ("file:///etc/passwd", None),
        ("https://[::1/api", None),
        ("https://example.databricks.com:99999/api", None),
    ],
)
def test_probe_workspace_id_requests_only_plain_http_urls(
    monkeypatch: pytest.MonkeyPatch, server_url: str, probed_url: str | None
) -> None:
    requested: list[str] = []

    @contextlib.contextmanager
    def _stream(method: str, url: str, **_kwargs: object) -> Iterator[httpx.Response]:
        requested.append(url)
        yield httpx.Response(
            401, headers={"x-databricks-org-id": "1"}, request=httpx.Request(method, url)
        )

    monkeypatch.setattr(httpx, "stream", _stream)

    assert probe_workspace_id(server_url) == ("1" if probed_url else None)
    assert requested == ([probed_url] if probed_url else [])


def test_probe_workspace_id_gives_up_at_its_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request stuck before any httpx timeout fires (name resolution, say) still ends."""
    import threading
    import time

    released = threading.Event()

    @contextlib.contextmanager
    def _stream(method: str, url: str, **_kwargs: object) -> Iterator[httpx.Response]:
        released.wait(5)
        yield httpx.Response(
            401, headers={"x-databricks-org-id": "1"}, request=httpx.Request(method, url)
        )

    monkeypatch.setattr(httpx, "stream", _stream)
    started = time.monotonic()
    try:
        assert probe_workspace_id(_VANITY, timeout_s=0.2) is None
        assert time.monotonic() - started < 2.0
    finally:
        released.set()


def test_daemon_entry_keys_by_workspace_but_connects_to_the_bare_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The background daemon's record key carries ``?o=``; its server URL does not."""
    import omnigent.host.connect as connect
    import omnigent.host.crash_reporting as crash_reporting
    import omnigent.host.daemon_lifecycle as daemon_lifecycle
    import omnigent.process_logging as process_logging
    from omnigent.host import _daemon_entry

    server = "https://acct.example.com/api/2.0/omnigent"
    monkeypatch.setattr("sys.argv", ["_daemon_entry", "--server", server])
    from omnigent.cli_auth import PINNED_DATABRICKS_ORG_ENV_VAR

    monkeypatch.setenv(PINNED_DATABRICKS_ORG_ENV_VAR, f"{server}?o=111")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    # Process plumbing the record logic doesn't depend on.
    monkeypatch.setattr(process_logging, "ensure_stdio_survives_unencodable_output", lambda: None)
    monkeypatch.setattr(
        process_logging, "configure_process_logging", lambda *_a, **_kw: tmp_path / "host.log"
    )
    monkeypatch.setattr(crash_reporting, "install_host_crash_hooks", lambda **_kw: None)
    monkeypatch.setattr(crash_reporting, "set_host_exit_context", lambda **_kw: None)

    class _Lock:
        def try_acquire(self) -> bool:
            return True

        def release(self) -> None:
            pass

    monkeypatch.setattr(daemon_lifecycle.DaemonLifecycleLock, "for_target", lambda target: _Lock())
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        daemon_lifecycle, "write_daemon_record", lambda record, **_kw: seen.update(record=record)
    )
    monkeypatch.setattr(
        connect,
        "run_host_process",
        lambda *, server_url, daemon_target, lifecycle_lock: seen.update(
            server_url=server_url, daemon_target=daemon_target
        ),
    )

    _daemon_entry.main()

    record = seen["record"]
    assert isinstance(record, daemon_lifecycle.HostDaemonRecord)
    assert record.target == f"{server}?o=111"
    assert record.server_url == server
    assert seen["daemon_target"] == record.target
    assert seen["server_url"] == server
