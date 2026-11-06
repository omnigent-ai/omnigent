"""Tests for host-owned background maintenance."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, call

import pytest

from omnigent.host import maintenance
from omnigent.host.maintenance import HostMaintenanceJanitor, prune_stale_process_logs
from omnigent.process_logging import (
    _ProcessLogFileHandler,
    _unlink_if_empty,
    create_process_log_path,
    open_process_log_file,
    process_log_lease_marker,
)


async def test_host_janitor_covers_global_runner_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    prune = Mock(side_effect=[2, 3])
    monkeypatch.setattr("omnigent.host.maintenance.prune_stale_process_logs", prune)
    janitor = HostMaintenanceJanitor.for_host()

    assert [name for name, _stage in janitor._stages] == [
        "harness_process_orphans",
        "codex_process_registry",
        "terminal_orphans",
        "native_bridge_orphans",
        "process_log_retention",
    ]
    assert janitor._stage_skip_reasons == {
        "native_bridge_orphans": frozenset({"runner_superseded"})
    }
    assert await dict(janitor._stages)["process_log_retention"]() == 5
    assert prune.call_args_list == [
        call(tmp_path / "logs" / "runner", "runner-*.log"),
        call(tmp_path / "logs" / "host", "host-*.log"),
    ]


@pytest.mark.skipif(maintenance.fcntl is None, reason="process-log leases require flock")
def test_prune_stale_process_logs_removes_only_idle_leased_matches(tmp_path: Path) -> None:
    now = 2_000_000_000.0
    retention = 30 * 24 * 3600.0
    log_dir = tmp_path / "runner"

    def allocate(age_s: float, *, destination: str = "runner") -> Path:
        path = create_process_log_path(destination, root=tmp_path)
        path.write_text("diagnostics\n")
        os.utime(path, (now - age_s, now - age_s))
        return path

    stale = allocate(retention + 1)
    at_cutoff = allocate(retention)
    before_cutoff = allocate(retention - 1)
    recent = allocate(10)
    foreign = allocate(retention + 1, destination="host")
    unmarked = log_dir / "runner-legacy.log"
    unmarked.write_text("legacy diagnostics\n")
    os.utime(unmarked, (0, 0))
    mismatched = allocate(retention + 1)
    process_log_lease_marker(mismatched).write_text("another inode\n")
    malformed = allocate(retention + 1)
    process_log_lease_marker(malformed).write_bytes(b"\xff")
    held = allocate(retention + 1)
    link = log_dir / "runner-link.log"
    link.symlink_to(stale)
    handler = _ProcessLogFileHandler(held, encoding="utf-8")
    try:
        assert prune_stale_process_logs(log_dir, "runner-*.log", now=now) == 1
        assert not stale.exists()
        assert not process_log_lease_marker(stale).exists()
        assert all(
            path.exists()
            for path in [
                at_cutoff,
                before_cutoff,
                recent,
                foreign,
                unmarked,
                mismatched,
                malformed,
                held,
            ]
        )
        assert link.is_symlink()
    finally:
        handler.close()
    assert prune_stale_process_logs(log_dir, "runner-*.log", now=now) == 1
    assert not held.exists()
    assert foreign.exists()


@pytest.mark.skipif(maintenance.fcntl is None, reason="process-log leases require flock")
def test_process_log_lease_survives_inherited_child_descriptor(tmp_path: Path) -> None:
    path, capture = open_process_log_file("runner", root=tmp_path)
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdin.readline(); print('child diagnostics', flush=True)",
        ],
        stdin=subprocess.PIPE,
        stdout=capture,
        stderr=capture,
        text=True,
    )
    capture.close()
    try:
        os.utime(path, (0, 0))
        assert prune_stale_process_logs(path.parent, "runner-*.log") == 0
        assert path.exists()
        _unlink_if_empty(path)
        assert path.exists(), "exit cleanup must preserve the inherited writer too"
        child.communicate("finish\n", timeout=10)
        assert child.returncode == 0
        assert path.read_text() == "child diagnostics\n"
        os.utime(path, (0, 0))
        assert prune_stale_process_logs(path.parent, "runner-*.log") == 1
        assert not path.exists()
        assert not process_log_lease_marker(path).exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


@pytest.mark.skipif(maintenance.fcntl is None, reason="process-log leases require flock")
def test_process_log_cleanup_preserves_files_when_lease_cannot_be_proven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = create_process_log_path("runner", root=tmp_path)
    eligible = create_process_log_path("runner", root=tmp_path)
    os.utime(path, (0, 0))
    os.utime(eligible, (0, 0))
    denied_inode = path.stat().st_ino
    flock = maintenance.fcntl.flock

    def refuse_lock(fd: int, operation: int) -> None:
        if os.fstat(fd).st_ino == denied_inode:
            raise PermissionError("lease is unavailable")
        flock(fd, operation)

    monkeypatch.setattr(maintenance.fcntl, "flock", refuse_lock)
    assert prune_stale_process_logs(path.parent, "runner-*.log") == 1
    assert path.exists()
    assert process_log_lease_marker(path).exists()
    assert not eligible.exists()


def test_process_log_cleanup_handles_missing_directory(tmp_path: Path) -> None:
    assert prune_stale_process_logs(tmp_path / "absent", "runner-*.log") == 0


async def test_start_runs_stages_in_background_and_in_order(tmp_path: Path) -> None:
    first_started = asyncio.Event()
    first_release = asyncio.Event()
    finished = asyncio.Event()
    calls: list[str] = []

    async def _first() -> int:
        calls.append("first")
        first_started.set()
        await first_release.wait()
        return 1

    async def _second() -> int:
        calls.append("second")
        finished.set()
        return 1

    janitor = HostMaintenanceJanitor(
        stages=(("first", _first), ("second", _second)),
        lock_path=tmp_path / "maintenance.lock",
        startup_delay_s=0,
    )

    janitor.start()
    janitor.start()
    await asyncio.wait_for(first_started.wait(), timeout=5.0)
    task = janitor._task
    assert task is not None
    assert calls == ["first"]
    assert not task.done()
    first_release.set()
    await asyncio.wait_for(finished.wait(), timeout=5.0)
    await janitor.shutdown()

    assert calls == ["first", "second"]


async def test_host_janitor_uses_absolute_unresolved_harness_tmp_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_root = tmp_path / "relative-parent" / ".." / "harness-sockets"
    expected_root = Path(os.path.abspath(configured_root.expanduser()))
    observed_roots: list[Path | None] = []

    async def _sweep(*, tmp_parent: Path | None = None) -> None:
        observed_roots.append(tmp_parent)

    monkeypatch.setattr(
        "omnigent.runtime.harnesses.process_manager.sweep_orphaned_harness_processes",
        _sweep,
    )
    janitor = HostMaintenanceJanitor.for_host(harness_tmp_parent=configured_root)

    janitor.trigger("runner_exited")
    task = janitor._task
    assert task is not None
    await asyncio.wait_for(task, timeout=5.0)
    await janitor.shutdown()

    assert observed_roots == [expected_root]


async def test_runner_lifecycle_trigger_reaps_native_bridge_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge_sweeps: list[int] = []

    async def _sweep_harness_processes(*, tmp_parent: Path | None = None) -> None:
        assert tmp_parent is not None

    monkeypatch.setattr(
        "omnigent.runtime.harnesses.process_manager.sweep_orphaned_harness_processes",
        _sweep_harness_processes,
    )
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.process_registry.reconcile_codex_native_process_registry",
        lambda: None,
    )
    monkeypatch.setattr("omnigent.inner.terminal.reap_orphaned_terminals", lambda: None)
    monkeypatch.setattr(
        "omnigent.native.native_bridge_common.reap_orphaned_native_bridge_dirs",
        lambda: bridge_sweeps.append(1) or 2,
    )
    janitor = HostMaintenanceJanitor.for_host(harness_tmp_parent=tmp_path / "harness-sockets")
    janitor._lock_path = tmp_path / "maintenance.lock"

    janitor.trigger("runner_exited")
    task = janitor._task
    assert task is not None
    await asyncio.wait_for(task, timeout=5.0)
    await janitor.shutdown()

    assert bridge_sweeps == [1]


async def test_triggers_during_cleanup_coalesce_into_one_follow_up(tmp_path: Path) -> None:
    first_started = asyncio.Event()
    first_release = asyncio.Event()
    second_finished = asyncio.Event()
    calls = 0

    async def _stage() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await first_release.wait()
        else:
            second_finished.set()
        return 0

    janitor = HostMaintenanceJanitor(
        stages=(("stage", _stage),),
        lock_path=tmp_path / "maintenance.lock",
    )
    janitor.trigger("first_runner_exit")
    await asyncio.wait_for(first_started.wait(), timeout=5.0)
    janitor.trigger("runner_exited")
    janitor.trigger("runner_stopped")
    first_release.set()
    await asyncio.wait_for(second_finished.wait(), timeout=5.0)
    await janitor.shutdown()

    assert calls == 2


async def test_superseded_pass_skips_native_bridge_cleanup(tmp_path: Path) -> None:
    calls: list[str] = []

    async def _global_cleanup() -> None:
        calls.append("global")

    async def _native_bridge_cleanup() -> None:
        calls.append("native_bridge")

    janitor = HostMaintenanceJanitor(
        stages=(
            ("global", _global_cleanup),
            ("native_bridge_orphans", _native_bridge_cleanup),
        ),
        lock_path=tmp_path / "maintenance.lock",
        stage_skip_reasons={"native_bridge_orphans": {"runner_superseded"}},
    )

    janitor.trigger("runner_superseded")
    task = janitor._task
    assert task is not None
    await asyncio.wait_for(task, timeout=5.0)
    assert calls == ["global"]

    janitor.trigger("runner_exited")
    task = janitor._task
    assert task is not None
    await asyncio.wait_for(task, timeout=5.0)
    await janitor.shutdown()

    assert calls == ["global", "global", "native_bridge"]


async def test_runner_trigger_does_not_wait_for_startup_delay(tmp_path: Path) -> None:
    finished = asyncio.Event()

    async def _stage() -> None:
        finished.set()

    janitor = HostMaintenanceJanitor(
        stages=(("stage", _stage),),
        lock_path=tmp_path / "maintenance.lock",
        startup_delay_s=60,
    )

    janitor.start()
    janitor.trigger("runner_exited")
    await asyncio.wait_for(finished.wait(), timeout=5.0)
    await janitor.shutdown()


@pytest.mark.skipif(os.name != "posix", reason="flock is POSIX-only")
async def test_lock_prevents_cross_host_overlap(tmp_path: Path) -> None:
    first_started = asyncio.Event()
    first_release = asyncio.Event()
    second_calls = 0
    second_finished = asyncio.Event()

    async def _blocking_stage() -> int:
        first_started.set()
        await first_release.wait()
        return 0

    async def _second_stage() -> int:
        nonlocal second_calls
        second_calls += 1
        second_finished.set()
        return 0

    lock_path = tmp_path / "maintenance.lock"
    first = HostMaintenanceJanitor(
        stages=(("first", _blocking_stage),),
        lock_path=lock_path,
    )
    second = HostMaintenanceJanitor(
        stages=(("second", _second_stage),),
        lock_path=lock_path,
        lock_retry_s=0.01,
    )

    first.trigger("first_runner_exit")
    await asyncio.wait_for(first_started.wait(), timeout=5.0)
    second.trigger("second_runner_exit")
    await asyncio.sleep(0.05)
    assert second_calls == 0

    first_release.set()
    await asyncio.wait_for(second_finished.wait(), timeout=5.0)
    await first.shutdown()
    await second.shutdown()

    assert second_calls == 1


async def test_permanent_lock_failure_does_not_retry_forever(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def _stage() -> None:
        nonlocal calls
        calls += 1

    def _deny_open(*_args: object, **_kwargs: object) -> int:
        raise PermissionError("read-only data directory")

    monkeypatch.setattr("omnigent.host.maintenance.os.open", _deny_open)
    janitor = HostMaintenanceJanitor(
        stages=(("stage", _stage),),
        lock_path=tmp_path / "maintenance.lock",
        lock_retry_s=0.01,
    )

    janitor.trigger("runner_exit")
    task = janitor._task
    assert task is not None
    await asyncio.wait_for(task, timeout=1.0)

    assert calls == 0
    assert janitor._pending_reasons == set()


async def test_shutdown_cancels_active_cleanup(tmp_path: Path) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _stage() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    janitor = HostMaintenanceJanitor(
        stages=(("stage", _stage),),
        lock_path=tmp_path / "maintenance.lock",
    )
    janitor.trigger("runner_exit")
    task = janitor._task
    assert task is not None
    await asyncio.wait_for(started.wait(), timeout=5.0)
    await janitor.shutdown()

    assert task.cancelled()
    assert cancelled.is_set()


async def test_stage_failure_does_not_abort_later_stages(tmp_path: Path) -> None:
    later_ran = asyncio.Event()

    async def _broken() -> None:
        raise RuntimeError("boom")

    async def _later() -> None:
        later_ran.set()

    janitor = HostMaintenanceJanitor(
        stages=(("broken", _broken), ("later", _later)),
        lock_path=tmp_path / "maintenance.lock",
    )
    janitor.trigger("runner_exit")
    await asyncio.wait_for(later_ran.wait(), timeout=5.0)
    await janitor.shutdown()


async def test_shutdown_cancels_delayed_startup_pass(tmp_path: Path) -> None:
    calls = 0

    async def _stage() -> None:
        nonlocal calls
        calls += 1

    janitor = HostMaintenanceJanitor(
        stages=(("stage", _stage),),
        lock_path=tmp_path / "maintenance.lock",
        startup_delay_s=60,
    )

    janitor.start()
    await janitor.shutdown()

    assert calls == 0
    assert janitor._startup_task is None
