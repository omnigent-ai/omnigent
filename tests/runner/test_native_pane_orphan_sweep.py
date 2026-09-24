"""The orphaned-sidecar sweep: sidecars that outlived their native pane.

A terminal DELETE during live work, a crashed pane, or a codex TUI that exited
on its own leaves the session's forwarder, relay and vendor server running
with no pane. The reaper offers such sessions as ``runtime`` rows, judged by
the same signals and the same deep check as a pane, and releases them after an
idle window. An active codex thread, a launch in progress, an exempt harness
and an SDK session are never swept.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from omnigent.entities.session_resources import terminal_resource_id
from omnigent.runner.session_status import StatusSource
from omnigent.terminals import pane_reaper as pane_reaper_module
from omnigent.terminals.pane_reaper import RUNTIME_KIND, PaneRef
from tests.terminals.native_pane_rig import (
    PaneRig,
    PlantedSidecars,
    build_pane_rig,
    plant_sidecars,
    report_harness_state,
)

_IDLE_WINDOW_S = 3600.0
_INTERVAL_S = 60.0


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


async def _rig(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> tuple[PaneRig, _Clock, PlantedSidecars]:
    clock = _Clock()
    monkeypatch.setattr(pane_reaper_module, "time", SimpleNamespace(monotonic=clock))
    rig = await build_pane_rig(
        tmp_path, monkeypatch, key=key, idle_timeout_s=_IDLE_WINDOW_S, status_clock=clock
    )
    rig.app.state.session_harness_overrides[rig.conv_id] = rig.agent.harness
    return rig, clock, plant_sidecars(rig.app, rig.conv_id, tmp_path, harness_key=rig.agent.key)


async def _delete_pane(rig: PaneRig) -> None:
    terminal_id = terminal_resource_id(rig.agent.terminal_name, "main")
    transport = httpx.ASGITransport(app=rig.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        resp = await client.delete(f"/v1/sessions/{rig.conv_id}/resources/terminals/{terminal_id}")
    assert resp.status_code == 200, resp.text
    tasks = set(rig.app.state.native_sidecar_release_tasks)
    if tasks:
        await asyncio.wait(tasks, timeout=10)


def _runtime_rows(rig: PaneRig) -> list[PaneRef]:
    listing = rig.reaper._list_orphan_runtimes
    assert listing is not None
    return [row for row in listing() if row.conversation_id == rig.conv_id]


async def _scan_until_released(
    rig: PaneRig, clock: _Clock, sidecars: PlantedSidecars, *, windows: float
) -> float | None:
    start = clock.now
    while clock.now - start <= windows * _IDLE_WINDOW_S:
        await rig.reaper._scan_once()
        if sidecars.leftovers(rig.app) == []:
            return clock.now - start
        clock.now += _INTERVAL_S
    return None


async def test_sidecars_a_busy_delete_kept_are_swept_one_window_after_the_work_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, clock, sidecars = await _rig(tmp_path, monkeypatch, "codex")
    report_harness_state(rig, monkeypatch, tmp_path, "idle")
    rig.book.record(rig.conv_id, "running", source=StatusSource.RELAY)
    try:
        await _delete_pane(rig)
        assert not rig.alive()
        assert sidecars.intact(rig.app)
        rows = _runtime_rows(rig)
        assert [(r.kind, r.terminal_name, r.socket_path) for r in rows] == [
            (RUNTIME_KIND, "codex", None)
        ]
        elapsed = await _scan_until_released(rig, clock, sidecars, windows=2)
        assert elapsed is not None and elapsed <= _IDLE_WINDOW_S + _INTERVAL_S
        assert _runtime_rows(rig) == []
        # No pane to close, so no resource event.
        assert rig.closed == [rig.conv_id]
    finally:
        sidecars.discard()


async def test_an_active_codex_thread_keeps_its_orphaned_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, clock, sidecars = await _rig(tmp_path, monkeypatch, "codex")
    report_harness_state(rig, monkeypatch, tmp_path, "active")
    try:
        await _delete_pane(rig)
        assert _runtime_rows(rig)
        assert await _scan_until_released(rig, clock, sidecars, windows=3) is None
        assert sidecars.intact(rig.app)
    finally:
        sidecars.discard()


async def test_sidecars_of_a_crashed_pane_are_swept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, clock, sidecars = await _rig(tmp_path, monkeypatch, "opencode")
    report_harness_state(rig, monkeypatch, tmp_path, "idle")
    try:
        await rig.terminal_registry.close(rig.conv_id, "opencode", "main")
        elapsed = await _scan_until_released(rig, clock, sidecars, windows=2)
        assert elapsed is not None and elapsed <= _IDLE_WINDOW_S + _INTERVAL_S
    finally:
        sidecars.discard()


async def test_a_live_signal_spares_orphaned_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, clock, sidecars = await _rig(tmp_path, monkeypatch, "goose")
    try:
        await rig.terminal_registry.close(rig.conv_id, "goose", "main")
        rig.app.state.mcp_execution_registry.retain_operation(rig.conv_id, "mcpop_1")
        assert await _scan_until_released(rig, clock, sidecars, windows=2) is None
        rig.app.state.mcp_execution_registry.release_operation(rig.conv_id, "mcpop_1")
        assert await _scan_until_released(rig, clock, sidecars, windows=2) is not None
    finally:
        sidecars.discard()


async def test_a_launch_in_progress_is_never_swept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, _clock, sidecars = await _rig(tmp_path, monkeypatch, "codex")
    try:
        await rig.terminal_registry.close(rig.conv_id, "codex", "main")
        lock = rig.app.state.native_terminal_ensure_locks["codex"].setdefault(
            rig.conv_id, asyncio.Lock()
        )
        async with lock:
            # The launch registers its sidecars before its pane.
            assert _runtime_rows(rig) == []
        assert _runtime_rows(rig)
    finally:
        sidecars.discard()


@pytest.mark.parametrize("harness", ["kimi-native", "claude-sdk", None])
async def test_exempt_sdk_and_unresolved_sessions_are_never_swept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str | None
) -> None:
    rig, _clock, sidecars = await _rig(tmp_path, monkeypatch, "goose")
    try:
        await rig.terminal_registry.close(rig.conv_id, "goose", "main")
        if harness is None:
            del rig.app.state.session_harness_overrides[rig.conv_id]
        else:
            rig.app.state.session_harness_overrides[rig.conv_id] = harness
        assert _runtime_rows(rig) == []
    finally:
        sidecars.discard()


async def test_a_session_with_its_pane_is_not_an_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, _clock, sidecars = await _rig(tmp_path, monkeypatch, "cursor")
    try:
        assert _runtime_rows(rig) == []
        # Nor when the pane carries no role (a user-launched terminal).
        with rig.resources._lock:
            rig.resources._terminal_roles.clear()
        assert _runtime_rows(rig) == []
    finally:
        sidecars.discard()


def test_runtime_and_pane_clocks_never_collide() -> None:
    pane = PaneRef("conv_a", "terminal_codex_main", "codex", Path("/tmp/x.sock"))
    runtime = PaneRef("conv_a", "terminal_codex_main", "codex", None, RUNTIME_KIND)
    assert pane.clock_key == "conv_a"
    assert runtime.clock_key != pane.clock_key

    reaped: list[str] = []

    async def _reap(row: PaneRef) -> bool:
        reaped.append(row.kind)
        return True

    async def _idle(_row: PaneRef) -> bool:
        return False

    reaper = pane_reaper_module.NativePaneReaper(
        list_native_panes=lambda: [pane],
        list_orphan_runtimes=lambda: [runtime],
        is_busy=_idle,
        reap=_reap,
        idle_timeout_s=100.0,
    )
    assert reaper._classify(0.0, [pane, runtime], {pane.clock_key}) == []
    # The pane's busy scan re-arms only the pane's clock.
    assert reaper._classify(150.0, [pane, runtime], {pane.clock_key}) == [runtime]
    assert set(reaper.snapshot()) == {pane.clock_key, runtime.clock_key}


async def test_a_runtime_release_re_checks_live_work_under_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Work that started after the scan decided is re-tested at the release.

    The release is logged as a runtime teardown: no pane is closed.
    """
    import logging

    rig, _clock, sidecars = await _rig(tmp_path, monkeypatch, "goose")
    try:
        await rig.terminal_registry.close(rig.conv_id, "goose", "main")
        (row,) = _runtime_rows(rig)
        rig.app.state.mcp_execution_registry.retain_operation(rig.conv_id, "mcpop_1")
        assert await rig.reaper._reap(row) is False
        assert sidecars.intact(rig.app)
        rig.app.state.mcp_execution_registry.release_operation(rig.conv_id, "mcpop_1")
        with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
            assert await rig.reaper._reap(row) is True
        assert sidecars.leftovers(rig.app) == []
        (teardown,) = [
            r for r in caplog.records if getattr(r, "event_name", None) == "native_pane_teardown"
        ]
        attributes = teardown.attributes  # type: ignore[attr-defined]
        assert attributes["kind"] == "runtime"
        assert attributes["reason"] == "idle_sidecar_sweep"
        assert attributes["closed"] is False
    finally:
        sidecars.discard()
