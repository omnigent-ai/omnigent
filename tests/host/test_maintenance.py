"""Tests for host-owned background maintenance."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from omnigent.host.maintenance import HostMaintenanceJanitor


def test_host_janitor_covers_global_runner_cleanup() -> None:
    janitor = HostMaintenanceJanitor.for_host()

    assert [name for name, _stage in janitor._stages] == [
        "harness_process_orphans",
        "codex_process_registry",
        "terminal_orphans",
        "native_bridge_orphans",
    ]
    assert janitor._stage_skip_reasons == {
        "native_bridge_orphans": frozenset({"runner_superseded"})
    }


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
