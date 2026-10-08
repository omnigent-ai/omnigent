"""Distinguish stalled scheduling, congested sends, and missing app heartbeats."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import pytest

from omnigent.runner.transports.ws_tunnel import diagnostics as diagnostics_module
from omnigent.runner.transports.ws_tunnel.diagnostics import (
    OutboundFrame,
    TunnelDiagnosticAttrs,
    TunnelDiagnostics,
)
from tests.budgets import budget


@dataclass
class _Clock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


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
