"""Tests for the host daemon lifecycle guard (flock + record self-monitor)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from omnigent.host import connect
from omnigent.host.connect import HostProcess
from omnigent.host.daemon_lifecycle import (
    DaemonLifecycleLock,
    daemon_record_path,
    normalize_daemon_target,
    record_flock_is_held,
)
from omnigent.host.identity import HostIdentity


def _write_record(path: Path, pid: int) -> None:
    """Write a minimal daemon record naming *pid* as the owner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "target": "local", "mode": "local"}))


def test_normalize_daemon_target() -> None:
    assert normalize_daemon_target(None) == "local"
    assert normalize_daemon_target("") == "local"
    assert normalize_daemon_target("https://x.example.com/") == "https://x.example.com"


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
    # Auto-spawn can start the daemon before the launching CLI writes the
    # record, so acquire() must create + flock the file rather than fail.
    lock = DaemonLifecycleLock.for_target("local", base_dir=tmp_path, pid=7)
    assert lock.acquire() is True
    assert daemon_record_path("local", base_dir=tmp_path).exists()
    lock.release()


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


def _record(target: str, pid: int) -> object:
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
    assert cli._daemon_owner_is_live(_record(target, 999), target) is True
    lock.release()

    # Free lock → fall back to the PID: alive PID (e.g. still starting) → alive.
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: True)
    assert cli._daemon_owner_is_live(_record(target, 999), target) is True

    # Free lock + dead PID → dead (the only case that reaps).
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: False)
    assert cli._daemon_owner_is_live(_record(target, 999), target) is False


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
