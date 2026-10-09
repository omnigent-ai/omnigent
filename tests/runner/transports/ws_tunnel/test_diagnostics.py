"""Distinguish stalled scheduling, congested sends, and missing app heartbeats."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import cast

import pytest

from omnigent.debug_logging import debug_event
from omnigent.runner.transports.ws_tunnel import diagnostics as diagnostics_module
from omnigent.runner.transports.ws_tunnel.diagnostics import (
    OutboundFrame,
    TunnelDiagnosticAttrs,
    TunnelDiagnostics,
    _host_pressure_attrs,
)
from tests.budgets import budget
from tests.debug_log_helpers import capture_debug_rows

# Synthetic procfs with a distinct value per field, so a mixed-up line or window shows.
_PROCFS_FILES = {
    "pressure/cpu": (
        "some avg10=2.15 avg60=0.59 avg300=0.75 total=1000\n"
        "full avg10=9.01 avg60=9.02 avg300=9.03 total=0\n"
    ),
    "pressure/memory": (
        "some avg10=41.10 avg60=33.20 avg300=21.30 total=2000\n"
        "full avg10=25.50 avg60=20.25 avg300=9.75 total=3000\n"
    ),
    "pressure/io": (
        "some avg10=7.00 avg60=6.00 avg300=5.00 total=4000\n"
        "full avg10=1.50 avg60=1.25 avg300=0.50 total=5000\n"
    ),
    "loadavg": "3.25 2.50 1.75 4/512 12345\n",
    "meminfo": (
        "MemTotal:        8192000 kB\n"
        "MemFree:          100000 kB\n"
        "MemAvailable:    2097152 kB\n"
        "Buffers:           50000 kB\n"
    ),
    "self/status": (
        "Name:\tpython\nVmPeak:\t  300000 kB\nVmRSS:\t  102400 kB\nRssAnon:\t   90000 kB\n"
    ),
}
_HOST_PRESSURE = {
    "psi_cpu_some_avg10": 2.15,
    "psi_cpu_some_avg60": 0.59,
    "psi_memory_full_avg10": 25.5,
    "psi_memory_full_avg60": 20.25,
    "psi_memory_full_avg300": 9.75,
    "psi_io_full_avg60": 1.25,
    "loadavg_1m": 3.25,
    "mem_available_mb": 2048.0,
    "process_rss_mb": 100.0,
}
_NO_PRESSURE = dict.fromkeys(_HOST_PRESSURE)


@dataclass
class _Clock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def proc_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An empty procfs stand-in, so snapshots never depend on the machine running the tests."""
    monkeypatch.setattr(
        diagnostics_module, "_host_pressure_attrs", partial(_host_pressure_attrs, tmp_path)
    )
    return tmp_path


def _write_procfs(root: Path) -> None:
    for name, text in _PROCFS_FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _host_pressure(snapshot: TunnelDiagnosticAttrs) -> dict[str, object]:
    # A dropped key must not pass for a null one.
    return {key: snapshot.get(key, "absent") for key in _HOST_PRESSURE}


async def test_stall_is_visible_even_before_monitor_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A close handled before the sampler wakes must still report a loop stall."""
    monkeypatch.setattr(diagnostics_module, "_SAMPLE_INTERVAL_S", 0.01)
    monkeypatch.setattr(diagnostics_module, "_SLOW_OPERATION_S", 0.02)
    diagnostics = TunnelDiagnostics()
    reports: list[TunnelDiagnosticAttrs] = []
    tasks_before = asyncio.all_tasks()
    async with diagnostics.monitoring(lambda: reports.append(diagnostics.snapshot())):
        await asyncio.sleep(0)
        time.sleep(0.08)  # Deliberately stop this loop, without doing socket I/O.
        snapshot = diagnostics.snapshot()
        assert snapshot["loop_lag_max_s"] >= 0.06
        assert snapshot["send_duration_max_s"] is None
        assert snapshot["sends_in_flight"] == 0
        await asyncio.sleep(0.02)
        assert len(reports) == 1
    assert asyncio.all_tasks() == tasks_before


async def test_waiting_send_leaves_event_loop_responsive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An async send can wait while the sampler continues making progress."""
    monkeypatch.setattr(diagnostics_module, "_SAMPLE_INTERVAL_S", 0.005)
    monkeypatch.setattr(diagnostics_module, "_SLOW_OPERATION_S", 0.02)
    diagnostics = TunnelDiagnostics()
    entered = asyncio.Event()
    release = asyncio.Event()
    reports: list[TunnelDiagnosticAttrs] = []

    async def send(_data: str) -> None:
        entered.set()
        await release.wait()

    task = asyncio.create_task(diagnostics.send(send, "not logged"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        async with diagnostics.monitoring(lambda: reports.append(diagnostics.snapshot())):
            await asyncio.sleep(0.08)
            snapshot = diagnostics.snapshot()
            assert snapshot["sends_in_flight"] == 1
            assert snapshot["oldest_tracked_send_age_s"] >= 0.06
            assert snapshot["loop_lag_max_s"] < snapshot["oldest_tracked_send_age_s"]
            assert snapshot["send_duration_s"] is None
            assert reports
            release.set()
            await asyncio.wait_for(task, timeout=2)
            snapshot = diagnostics.snapshot()
            assert snapshot["sends_in_flight"] == 0
            assert snapshot["send_duration_s"] >= 0.06
            assert snapshot["last_send_outcome"] == "completed"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_monitor_failure_is_logged_without_interrupting_sends(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = 100.0
    fail_clock = False
    failed = asyncio.Event()
    error = RuntimeError("sampling clock failed")

    def clock() -> float:
        nonlocal fail_clock
        if fail_clock:
            fail_clock = False
            failed.set()
            raise error
        return now

    async def send(_data: str) -> None:
        pass

    diagnostics = TunnelDiagnostics(clock=clock)
    diagnostics.settings["tunnel_side"] = "server"
    assert diagnostics.snapshot()["sampler_failed"] is False
    async with diagnostics.monitoring(lambda: None, connection_id="conn-sampler-failure"):
        now += 8
        assert diagnostics.snapshot()["loop_lag_max_s"] == 3.0
        fail_clock = True
        await asyncio.wait_for(failed.wait(), timeout=budget(1))
        records = [r for r in caplog.records if r.message == "Tunnel diagnostics monitor failed"]
        assert len(records) == 1
        assert records[0].exc_info is not None
        assert records[0].exc_info[1] is error
        assert records[0].attributes == {
            "connection_id": "conn-sampler-failure",
            "tunnel_side": "server",
        }
        await diagnostics.send(send, "still connected")
        now += 100
        snapshot = diagnostics.snapshot()
        assert snapshot["last_send_outcome"] == "completed"
        assert snapshot["loop_lag_max_s"] is None
        assert snapshot["sampler_failed"] is True
    assert diagnostics.snapshot()["sends_in_flight"] == 0
    assert diagnostics.snapshot()["sampler_failed"] is True
    assert TunnelDiagnostics(clock=clock).snapshot()["sampler_failed"] is False


async def test_loop_lag_sample_age_is_preserved_across_snapshots() -> None:
    clock = _Clock()
    diagnostics = TunnelDiagnostics(clock=clock)
    sampled = asyncio.Event()
    async with diagnostics.monitoring(sampled.set):
        clock.advance(8)
        assert diagnostics.snapshot()["loop_lag_max_s"] == 3.0
        clock.advance(1)
        assert diagnostics.snapshot()["loop_lag_max_s"] == 4.0
        await asyncio.wait_for(sampled.wait(), timeout=budget(1))
        clock.advance(2)
        snapshot = diagnostics.snapshot()
        assert snapshot["loop_lag_max_s"] == 4.0
        assert snapshot["loop_lag_max_age_s"] == 2.0
        clock.advance(1)
        assert diagnostics.snapshot()["loop_lag_max_age_s"] == 3.0


async def test_queue_handoff_send_and_ping_rtt_are_separate_timings() -> None:
    """RTT uses the local send clock, never the peer's echoed wall timestamp."""
    clock = _Clock()
    diagnostics = TunnelDiagnostics(clock=clock)
    frame = OutboundFrame("ping payload", queued_at=102.0, app_ping_ts=9999999)
    clock.advance(2)
    diagnostics.enqueued(frame, depth=3, requested_at=100.0)
    clock.advance(5)
    diagnostics.dequeued(frame)

    async def send(data: str) -> None:
        assert data == frame.data
        clock.advance(2)

    await diagnostics.send(send, frame.data, app_ping_ts=frame.app_ping_ts)
    clock.advance(3)
    diagnostics.frame_received()
    diagnostics.app_pong_received(9999999)
    snapshot = diagnostics.snapshot()
    assert snapshot["enqueue_delay_s"] == 2.0
    assert snapshot["queue_wait_s"] == 5.0
    assert snapshot["send_duration_s"] == 2.0
    assert snapshot["app_ping_rtt_s"] == 5.0
    assert snapshot["last_app_ping_queued_age_s"] == 10.0
    assert snapshot["last_app_ping_sent_age_s"] == 3.0
    assert snapshot["last_app_pong_received_age_s"] == 0.0
    assert snapshot["last_received_frame_age_s"] == 0.0
    assert snapshot["outbound_queue_depth"] == 2
    assert snapshot["outbound_queue_high_water"] == 3
    assert snapshot["app_pings_queued"] == 0

    clock.advance(20)
    snapshot = diagnostics.snapshot()
    assert snapshot["send_duration_max_age_s"] == 23.0
    assert snapshot["queue_wait_max_age_s"] == 25.0
    assert "ping payload" not in str(snapshot)


@pytest.mark.parametrize("cancel", [False, True], ids=["error", "cancellation"])
async def test_failed_send_preserves_evidence_and_original_exception(cancel: bool) -> None:
    clock = _Clock()
    diagnostics = TunnelDiagnostics(clock=clock)
    error = asyncio.CancelledError() if cancel else ConnectionError("send failed")

    async def send(_data: str) -> None:
        clock.advance(4)
        raise error

    with pytest.raises(type(error)) as raised:
        await diagnostics.send(send, "not logged", app_ping_ts=1)
    assert raised.value is error
    snapshot = diagnostics.snapshot()
    assert snapshot["send_duration_max_s"] == 4.0
    assert snapshot["last_send_outcome"] == ("cancelled" if cancel else "error")
    assert snapshot["send_cancellations"] == int(cancel)
    assert snapshot["send_errors"] == int(not cancel)
    assert snapshot["last_sent_frame_age_s"] is None
    assert snapshot["last_app_ping_sent_age_s"] is None
    assert snapshot["sends_in_flight"] == 0


async def test_concurrent_sends_freeze_before_cleanup_and_reset_on_reconnect() -> None:
    clock = _Clock()
    diagnostics = TunnelDiagnostics(clock=clock)
    entered: asyncio.Queue[str] = asyncio.Queue()
    releases = {name: asyncio.Event() for name in ("first", "second")}

    async def send(data: str) -> None:
        entered.put_nowait(data)
        await releases[data].wait()

    first = asyncio.create_task(diagnostics.send(send, "first"))
    assert await asyncio.wait_for(entered.get(), timeout=2) == "first"
    clock.advance(2)
    second = asyncio.create_task(diagnostics.send(send, "second"))
    try:
        assert await asyncio.wait_for(entered.get(), timeout=2) == "second"
        clock.advance(3)
        releases["second"].set()
        await asyncio.wait_for(second, timeout=2)
        snapshot = diagnostics.snapshot()
        assert snapshot["sends_in_flight"] == 1
        assert snapshot["oldest_tracked_send_age_s"] == 5.0
        diagnostics.freeze()
    finally:
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
    clock.advance(30)
    snapshot["diagnostics_age_s"] = 30.0
    assert diagnostics.snapshot() == snapshot
    snapshot["sends_in_flight"] = 999
    assert diagnostics.snapshot()["sends_in_flight"] == 1
    reconnected = TunnelDiagnostics(clock=clock).snapshot()
    assert reconnected["sends_in_flight"] == 0
    assert reconnected["oldest_tracked_send_age_s"] is None
    assert reconnected["send_duration_max_s"] is None
    assert reconnected["last_received_frame_age_s"] is None


async def test_reports_are_rate_limited_and_logging_failure_does_not_break_io() -> None:
    clock = _Clock()
    diagnostics = TunnelDiagnostics(clock=clock)
    reports: list[TunnelDiagnosticAttrs] = []

    def report() -> None:
        reports.append(diagnostics.snapshot())
        raise RuntimeError("log sink unavailable")

    async def send(_data: str) -> None:
        clock.advance(2)

    async with diagnostics.monitoring(report):
        await diagnostics.send(send, "first")
        await diagnostics.send(send, "second")
        assert len(reports) == 1
        clock.advance(60)
        await diagnostics.send(send, "third")
        assert len(reports) == 2
    assert diagnostics.snapshot()["send_errors"] == 0


async def test_old_pings_are_evicted_without_fabricating_an_rtt() -> None:
    clock = _Clock()
    diagnostics = TunnelDiagnostics(clock=clock)

    async def send(_data: str) -> None:
        pass

    for ts in range(100):
        await diagnostics.send(send, "ping", app_ping_ts=ts)
    clock.advance(1)
    diagnostics.app_pong_received(0)
    snapshot = diagnostics.snapshot()
    assert snapshot["app_ping_rtt_s"] is None
    assert snapshot["app_ping_samples_dropped"] == 92
    assert snapshot["last_app_pong_received_age_s"] == 0.0
    diagnostics.app_pong_received(99)
    assert diagnostics.snapshot()["app_ping_rtt_s"] == 1.0


async def test_concurrent_send_sampling_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostics_module, "_MAX_TRACKED_SENDS", 2)
    diagnostics = TunnelDiagnostics()
    entered: asyncio.Queue[None] = asyncio.Queue()

    async def send(_data: str) -> None:
        entered.put_nowait(None)
        await asyncio.Future()

    tasks = [asyncio.create_task(diagnostics.send(send, "frame")) for _ in range(4)]
    try:
        for _ in tasks:
            await asyncio.wait_for(entered.get(), timeout=2)
        snapshot = diagnostics.snapshot()
        assert snapshot["sends_in_flight"] == 4
        assert snapshot["send_samples_dropped"] == 2
        assert snapshot["oldest_tracked_send_age_s"] >= 0
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert diagnostics.snapshot()["sends_in_flight"] == 0
    assert diagnostics.snapshot()["oldest_tracked_send_age_s"] is None


def test_host_pressure_reads_the_selected_procfs_fields(proc_root: Path) -> None:
    _write_procfs(proc_root)
    assert _host_pressure_attrs(proc_root) == _HOST_PRESSURE


def test_server_side_snapshot_skips_host_pressure(proc_root: Path) -> None:
    """A stalled server reports every tunnel at once; it must not read procfs per tunnel."""
    _write_procfs(proc_root)
    server = TunnelDiagnostics()
    server.settings["tunnel_side"] = "server"
    runner = TunnelDiagnostics()
    runner.settings["tunnel_side"] = "runner"
    assert not set(_HOST_PRESSURE) & set(server.snapshot())
    assert _host_pressure(runner.snapshot()) == _HOST_PRESSURE


def test_snapshot_freezes_host_pressure_and_a_reconnect_reads_it_afresh(proc_root: Path) -> None:
    _write_procfs(proc_root)
    clock = _Clock()
    diagnostics = TunnelDiagnostics(clock=clock)
    live = diagnostics.snapshot()
    assert _host_pressure(live) == _HOST_PRESSURE
    assert set(live) <= TunnelDiagnosticAttrs.__optional_keys__
    (proc_root / "loadavg").write_text("7.00 2.50 1.75 4/512 12345\n", encoding="utf-8")
    assert diagnostics.snapshot()["loadavg_1m"] == 7.0
    diagnostics.freeze()
    (proc_root / "loadavg").write_text("0.10 2.50 1.75 4/512 12345\n", encoding="utf-8")
    clock.advance(4)
    frozen = diagnostics.snapshot()
    assert frozen["loadavg_1m"] == 7.0
    assert frozen["diagnostics_age_s"] == 4.0
    assert TunnelDiagnostics(clock=clock).snapshot()["loadavg_1m"] == 0.1


def _remove_procfs(root: Path) -> None:
    for name in _PROCFS_FILES:
        (root / name).unlink()


def _garble_procfs(root: Path) -> None:
    junk = b"\xff\xfe\x00 some avg10=oops avg60= full avg300=1=2\nMemAvailable: lots kB\nVmRSS:\n"
    for name in _PROCFS_FILES:
        (root / name).write_bytes(junk)


def _make_procfs_unreadable(root: Path) -> None:
    for name in _PROCFS_FILES:
        (root / name).unlink()
        (root / name).mkdir()


@pytest.mark.parametrize(
    "break_procfs",
    [_remove_procfs, _garble_procfs, _make_procfs_unreadable],
    ids=["missing", "garbage", "unreadable"],
)
def test_unavailable_host_pressure_is_null_never_zero_and_never_raises(
    proc_root: Path, break_procfs: Callable[[Path], None]
) -> None:
    _write_procfs(proc_root)
    break_procfs(proc_root)
    diagnostics = TunnelDiagnostics(clock=_Clock())
    assert _host_pressure(diagnostics.snapshot()) == _NO_PRESSURE
    diagnostics.freeze()
    assert _host_pressure(diagnostics.snapshot()) == _NO_PRESSURE


@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="needs a Linux procfs")
def test_default_root_reads_this_process_from_the_real_procfs() -> None:
    attrs = _host_pressure_attrs()
    assert attrs["process_rss_mb"] is not None and attrs["process_rss_mb"] > 0
    assert attrs["loadavg_1m"] is not None and attrs["loadavg_1m"] >= 0


def test_kernel_without_psi_still_reports_load_and_memory(proc_root: Path) -> None:
    _write_procfs(proc_root)
    shutil.rmtree(proc_root / "pressure")
    assert _host_pressure_attrs(proc_root) == {
        key: None if key.startswith("psi_") else value for key, value in _HOST_PRESSURE.items()
    }


async def test_sampler_ticks_do_not_read_procfs(monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list[None] = []
    read_procfs = diagnostics_module._host_pressure_attrs

    def counting_read() -> dict[str, float | None]:
        reads.append(None)
        return read_procfs()

    monkeypatch.setattr(diagnostics_module, "_host_pressure_attrs", counting_read)
    clock = _Clock()
    diagnostics = TunnelDiagnostics(clock=clock)
    sampled = asyncio.Event()
    async with diagnostics.monitoring(sampled.set):
        clock.advance(8)  # The sampler wakes late and reports without taking a snapshot.
        await asyncio.wait_for(sampled.wait(), timeout=budget(1))
        assert not reads
        diagnostics.snapshot()
        assert len(reads) == 1
    assert len(reads) == 2  # The disconnect freeze reads once more.
    diagnostics.snapshot()
    diagnostics.snapshot()
    assert len(reads) == 2


def test_debug_row_stringifies_host_pressure_and_omits_unavailable_values(
    proc_root: Path,
) -> None:
    _write_procfs(proc_root)
    (proc_root / "pressure" / "io").unlink()
    snapshot = TunnelDiagnostics(clock=_Clock()).snapshot()
    with capture_debug_rows("runner") as rows:
        logging.getLogger("omnigent.runner.test").info(
            "health", extra=debug_event("runner_tunnel_health", **snapshot)
        )
    (row,) = rows
    attributes = cast(dict[str, str], row["attributes"])
    assert attributes["psi_memory_full_avg60"] == "20.25"
    assert attributes["mem_available_mb"] == "2048.0"
    assert "psi_io_full_avg60" not in attributes
