"""Tests for the host daemon lifecycle guard (flock + record self-monitor)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.host import connect
from omnigent.host.connect import HostProcess
from omnigent.host.daemon_lifecycle import (
    DaemonLifecycleLock,
    HostDaemonRecord,
    daemon_record_path,
    loopback_server_port,
    normalize_daemon_target,
    record_flock_is_held,
)
from omnigent.host.identity import HostIdentity


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


def _track_local_server(base: Path, port: int, pid: int | None = None) -> None:
    """Write a data-dir pidfile declaring *port* as the tracked local server.

    The recorded pid defaults to this process so the server counts as alive.
    """
    owner = os.getpid() if pid is None else pid
    (base / "local_server.pid").write_text(f"{owner}\n{port}\n")


def test_normalize_collapses_tracked_loopback_spellings(tmp_path: Path) -> None:
    """Every loopback spelling of the tracked server keys to the local record."""
    _track_local_server(tmp_path, 6767)
    for spelling in (
        "http://127.0.0.1:6767",
        "http://localhost:6767",
        "http://[::1]:6767",
        "http://localhost:6767/",
        "HTTP://LocalHost:6767/",
    ):
        assert normalize_daemon_target(spelling, base_dir=tmp_path) == "local"


def test_normalize_collapses_tracked_default_port_spelling(tmp_path: Path) -> None:
    """A loopback URL eliding the scheme-default port still names the instance."""
    _track_local_server(tmp_path, 80)
    assert normalize_daemon_target("http://localhost", base_dir=tmp_path) == "local"


def test_normalize_keeps_urls_of_other_servers(tmp_path: Path) -> None:
    """URLs that do not name the tracked instance keep their own key."""
    _track_local_server(tmp_path, 6767)
    # Another loopback port may be a forwarded tunnel to a different server.
    assert (
        normalize_daemon_target("http://127.0.0.1:9999", base_dir=tmp_path)
        == "http://127.0.0.1:9999"
    )
    assert (
        normalize_daemon_target("https://x.example.com:6767", base_dir=tmp_path)
        == "https://x.example.com:6767"
    )
    # The local server speaks plain http; an https spelling is a different endpoint.
    assert (
        normalize_daemon_target("https://localhost:6767", base_dir=tmp_path)
        == "https://localhost:6767"
    )


def test_normalize_keeps_loopback_urls_without_a_tracked_server(tmp_path: Path) -> None:
    assert (
        normalize_daemon_target("http://127.0.0.1:6767", base_dir=tmp_path)
        == "http://127.0.0.1:6767"
    )


def test_normalize_keeps_loopback_url_of_a_dead_tracked_server(tmp_path: Path) -> None:
    """A stale pidfile left by a crashed server must not capture an explicit target."""
    reaped = subprocess.Popen([sys.executable, "-c", "pass"])
    reaped.wait()
    _track_local_server(tmp_path, 6767, pid=reaped.pid)

    assert (
        normalize_daemon_target("http://127.0.0.1:6767", base_dir=tmp_path)
        == "http://127.0.0.1:6767"
    )
    # The same claim collapses once its server is reported alive.
    assert (
        normalize_daemon_target(
            "http://127.0.0.1:6767", base_dir=tmp_path, pid_alive=lambda pid: True
        )
        == "local"
    )


@pytest.mark.parametrize(
    "contents",
    [b"pid-and-port-missing\n", b"\xff\xfe\n6767\n", b"not-a-pid\n6767\n", b"4242\nnot-a-port\n"],
)
def test_normalize_tolerates_a_malformed_pidfile(tmp_path: Path, contents: bytes) -> None:
    (tmp_path / "local_server.pid").write_bytes(contents)
    assert (
        normalize_daemon_target("http://127.0.0.1:6767", base_dir=tmp_path)
        == "http://127.0.0.1:6767"
    )


@pytest.mark.parametrize(
    ("server_url", "expected"),
    [
        ("http://127.0.0.1:6767", 6767),
        ("http://localhost:6767/", 6767),
        ("HTTP://LocalHost:6767", 6767),
        ("http://[::1]:6767", 6767),
        ("http://localhost", 80),
        ("https://localhost", 443),
        ("https://x.example.com:6767", None),
        ("local", None),
        ("http://localhost:not-a-port", None),
    ],
)
def test_loopback_server_port(server_url: str, expected: int | None) -> None:
    """Loopback spellings resolve to their port; anything else resolves to ``None``."""
    assert loopback_server_port(server_url) == expected


def test_equivalent_server_urls_share_record_path(tmp_path: Path) -> None:
    canonical = normalize_daemon_target("https://x.example.com/api")
    equivalent = normalize_daemon_target("HTTPS://X.EXAMPLE.COM:443/api/")

    assert canonical == equivalent
    assert daemon_record_path(canonical, base_dir=tmp_path) == daemon_record_path(
        equivalent, base_dir=tmp_path
    )


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

    target = "https://server.example.com"
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
        lambda: HostIdentity(host_id="host_elected", name="elected"),
    )

    connected: list[str] = []

    def _run(*, server_url: str, daemon_target: str, lifecycle_lock: object) -> None:
        assert record_flock_is_held(daemon_record_path(target, base_dir=tmp_path)) is True
        assert lifecycle_lock is not None
        connected.append(f"{server_url}|{daemon_target}")

    monkeypatch.setattr("omnigent.host.connect.run_host_process", _run)

    _daemon_entry.main()

    payload = json.loads(daemon_record_path(target, base_dir=tmp_path).read_text())
    assert payload["pid"] == os.getpid()
    assert payload["host_id"] == "host_elected"
    assert payload["config_sig"] == "config-signature"
    assert connected == [f"{target}|{target}"]
    assert record_flock_is_held(daemon_record_path(target, base_dir=tmp_path)) is False


def test_adopting_local_daemon_records_the_server_url_it_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daemon spawned for an explicit loopback URL adopts the server and publishes its URL.

    The published URL is what keeps the record reachable by
    ``host stop --server <url>`` once the server pidfile is gone.
    """
    from omnigent import cli
    from omnigent.host import _daemon_entry, local_server
    from omnigent.host import identity as identity_module
    from omnigent.process_logging import DATA_DIR_ENV_VAR

    monkeypatch.setenv(DATA_DIR_ENV_VAR, str(tmp_path))
    # The CLI stamps an empty signature when the adopted server has no sidecar.
    monkeypatch.setenv("OMNIGENT_HOST_DAEMON_CONFIG_SIG", "")
    monkeypatch.setattr(cli, "_HOST_PID_PATH", tmp_path / "host.pid")
    monkeypatch.setattr(sys, "argv", ["omnigent.host._daemon_entry", "--local", "--adopt-server"])
    monkeypatch.setattr(
        "omnigent.process_logging.configure_process_logging",
        lambda *_a, **_kw: tmp_path / "host.log",
    )
    monkeypatch.setattr(
        identity_module,
        "load_or_create_host_identity",
        lambda: HostIdentity(host_id="host_adopt", name="adopt"),
    )
    ensure_calls: list[dict[str, object]] = []

    def _ensure(**kwargs: object) -> local_server.LocalServerStartup:
        ensure_calls.append(kwargs)
        return local_server.LocalServerStartup(
            url="http://127.0.0.1:6767", spawned=False, log_path=None
        )

    monkeypatch.setattr(local_server, "ensure_local_omnigent_server", _ensure)
    monkeypatch.setattr("omnigent.host.connect.run_host_process", lambda **_kw: None)

    _daemon_entry.main()

    assert ensure_calls == [{"replace_on_config_drift": False}]
    payload = json.loads(daemon_record_path("local", base_dir=tmp_path).read_text())
    assert payload["mode"] == "local"
    assert payload["adopted"] is True
    assert payload["config_sig"] is None
    assert payload["resolved_server_url"] == "http://127.0.0.1:6767"
    # No local_server.pid exists here, so the URL keys on itself — and the
    # published URL is what still leads the lookup to the local record.
    target = cli._normalize_daemon_target("http://localhost:6767")
    assert target == "http://localhost:6767"
    found = cli._find_daemon_record(target)
    assert found is not None
    assert found.target == "local"


def test_background_daemon_loser_exits_before_connecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second child for one target exits without replacing the live owner."""
    from omnigent.host import _daemon_entry
    from omnigent.process_logging import DATA_DIR_ENV_VAR

    target = "https://server.example.com"
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
    owner = DaemonLifecycleLock.for_target(target, base_dir=tmp_path, pid=4242)
    assert owner.acquire() is True
    try:
        _write_record(daemon_record_path(target, base_dir=tmp_path), 4242)
        before = daemon_record_path(target, base_dir=tmp_path).read_text()
        _daemon_entry.main()
        assert daemon_record_path(target, base_dir=tmp_path).read_text() == before
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


def test_live_daemon_conflict_matches_loopback_spelling_of_local_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A foreground ``--server`` naming the local daemon's port conflicts with it."""
    from omnigent import cli

    monkeypatch.setattr(cli, "_HOST_PID_PATH", tmp_path / "host.pid")
    local = HostDaemonRecord(
        pid=222,
        target="local",
        mode="local",
        server_url=None,
        log_path=None,
        started_at=100,
        resolved_server_url="http://127.0.0.1:8123",
    )
    cli._write_daemon_record(local)
    lock = DaemonLifecycleLock.for_target("local", base_dir=tmp_path, pid=222)
    assert lock.acquire() is True

    def _claimer(server_url: str) -> HostDaemonRecord:
        return HostDaemonRecord(
            pid=111,
            target=normalize_daemon_target(server_url, base_dir=tmp_path),
            mode="server",
            server_url=server_url,
            log_path=None,
            started_at=200,
        )

    try:
        assert cli._live_daemon_conflict(_claimer("http://localhost:8123")) == local
        assert cli._live_daemon_conflict(_claimer("http://localhost:9999")) is None
    finally:
        lock.release()


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
