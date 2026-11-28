"""Capture blocking code before the loop recovers, without using its executor."""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import re
import threading
from collections.abc import Iterator
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omnigent.debug_logging import PRIMARY_SESSION_ID_ENV_VAR, record_to_row
from omnigent.runner.identity import RUNNER_ID_ENV_VAR
from omnigent.util import event_loop_watchdog as watchdog
from tests.budgets import budget


@pytest.fixture
def stall_records() -> Iterator[queue.Queue[logging.LogRecord]]:
    records: queue.Queue[logging.LogRecord] = queue.Queue()

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.put(record)

    handler = Capture()
    watchdog._logger.addHandler(handler)
    try:
        yield records
    finally:
        watchdog._logger.removeHandler(handler)
        handler.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("process_role", ["server", "runner"])
async def test_captures_blocking_stack_rate_limits_and_recovers(
    stall_records, monkeypatch, process_role
) -> None:
    # This local must never appear in diagnostics, even though this frame is sampled.
    private_request_content = "private-request-body-canary"
    threads_before = set(threading.enumerate())
    monkeypatch.setenv(RUNNER_ID_ENV_VAR, "runner-watchdog-test")
    monkeypatch.setenv(PRIMARY_SESSION_ID_ENV_VAR, "conv-watchdog-test")
    with watchdog.monitor_event_loop(
        process_role=process_role,
        stall_threshold_s=budget(0.5),
        sample_interval_s=budget(0.05),
        report_interval_s=budget(0.8),
    ):
        await asyncio.sleep(budget(0.6))
        assert stall_records.empty()
        # Synchronous Queue.get deliberately blocks this loop until the watchdog logs.
        first = stall_records.get(timeout=budget(3))
        row = record_to_row(first, source=process_role)
        assert row["event_name"] == f"{process_role}_event_loop_stalled"
        assert row["attributes"]["pid"] == str(os.getpid())
        if process_role == "runner":
            assert row["attributes"]["runner_id"] == "runner-watchdog-test"
            assert row["session_id"] == "conv-watchdog-test"
        assert "test_captures_blocking_stack_rate_limits_and_recovers" in row["stack_trace"]
        assert "in _capture_stack" not in row["stack_trace"]
        assert "\n" not in row["message"]
        assert row["stack_trace"] not in row["message"]
        assert private_request_content not in str(row)
        assert first.attributes["loop_thread_id"] == threading.get_ident()
        assert first.attributes["stalled_s"] >= budget(0.5) - 0.001
        second = stall_records.get(timeout=budget(3))
        assert second.created - first.created >= budget(0.79)
        # Async waiting allows heartbeats to resume; a healthy idle loop stays quiet.
        await asyncio.sleep(budget(1.4))
        assert stall_records.empty()
        third = stall_records.get(timeout=budget(3))
        assert third.event_name == f"{process_role}_event_loop_stalled"
        threads = set(threading.enumerate()) - threads_before
        watchdog_threads = [t for t in threads if t.name == "omnigent-event-loop-watchdog"]
        assert len(watchdog_threads) == 1
    watchdog_threads[0].join(timeout=budget(3))
    assert not watchdog_threads[0].is_alive()
    with pytest.raises(queue.Empty):
        stall_records.get(timeout=budget(0.15))


@pytest.mark.asyncio
async def test_watchdog_stops_when_serving_raises(stall_records, monkeypatch) -> None:
    loop = asyncio.get_running_loop()
    scheduled_heartbeats: list[asyncio.TimerHandle] = []
    call_later = loop.call_later

    def track_timer(delay, callback, *args, **kwargs):
        handle = call_later(delay, callback, *args, **kwargs)
        if callback.__name__ == "heartbeat":
            scheduled_heartbeats.append(handle)
        return handle

    monkeypatch.setattr(loop, "call_later", track_timer)
    with pytest.raises(RuntimeError, match="serving failed"):
        with watchdog.monitor_event_loop(
            stall_threshold_s=budget(0.03), sample_interval_s=budget(0.005)
        ):
            raise RuntimeError("serving failed")
    assert len(scheduled_heartbeats) == 1
    assert scheduled_heartbeats[0].cancelled()
    with pytest.raises(queue.Empty):
        stall_records.get(timeout=budget(0.1))


@pytest.mark.asyncio
async def test_capture_failure_does_not_disable_watchdog(stall_records, monkeypatch) -> None:
    capture = watchdog._capture_stack
    failed = False

    def fail_once(thread_id: int) -> str:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("sampling failed")
        return capture(thread_id)

    monkeypatch.setattr(watchdog, "_capture_stack", fail_once)
    with watchdog.monitor_event_loop(
        stall_threshold_s=budget(0.03),
        sample_interval_s=budget(0.005),
        report_interval_s=budget(0.05),
    ):
        assert stall_records.get(timeout=budget(3)).levelno == logging.ERROR
        assert stall_records.get(timeout=budget(3)).event_name == "server_event_loop_stalled"


@pytest.mark.asyncio
async def test_discarded_capture_does_not_suppress_next_stall(stall_records, monkeypatch) -> None:
    capture_started = threading.Event()
    finish_capture = threading.Event()
    capture = watchdog._capture_stack

    def delay_first_capture(thread_id: int) -> str:
        if not capture_started.is_set():
            capture_started.set()
            if not finish_capture.wait(timeout=budget(3)):
                raise RuntimeError("Test did not release the first capture")
        return capture(thread_id)

    monkeypatch.setattr(watchdog, "_capture_stack", delay_first_capture)
    with watchdog.monitor_event_loop(
        stall_threshold_s=budget(0.1), sample_interval_s=budget(0.01), report_interval_s=budget(60)
    ):
        try:
            assert capture_started.wait(timeout=budget(3))
            # Resume heartbeats while capture is in progress, making its snapshot stale.
            await asyncio.sleep(budget(0.05))
        finally:
            finish_capture.set()
        record = stall_records.get(timeout=budget(3))
        assert record.event_name == "server_event_loop_stalled"


@pytest.mark.asyncio
@pytest.mark.parametrize("process_role", ["server", "runner"])
async def test_thread_start_failure_does_not_prevent_serving(
    stall_records, monkeypatch, process_role
) -> None:
    threading_proxy = Mock(wraps=threading)
    thread = Mock(ident=None)
    threading_proxy.Thread = Mock(return_value=thread)
    thread.start.side_effect = RuntimeError("can't start new thread")
    monkeypatch.setattr(watchdog, "threading", threading_proxy)

    with watchdog.monitor_event_loop(process_role=process_role):
        record = stall_records.get(timeout=budget(1))
        assert record.event_name == f"{process_role}_event_loop_watchdog_failed"
        assert record.attributes["pid"] == os.getpid()
        assert "can't start new thread" in str(record.exc_info[1])
    thread.join.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_does_not_wait_for_blocked_logging(monkeypatch) -> None:
    logging_started = threading.Event()
    release_logging = threading.Event()
    logging_threads: list[threading.Thread] = []

    def blocked_warning(*args, **kwargs) -> None:
        logging_threads.append(threading.current_thread())
        logging_started.set()
        release_logging.wait(timeout=budget(3))

    monkeypatch.setattr(watchdog._logger, "warning", blocked_warning)
    try:
        with watchdog.monitor_event_loop(
            stall_threshold_s=budget(0.03), sample_interval_s=budget(0.005)
        ):
            assert logging_started.wait(timeout=budget(3))
            shutdown_started = monotonic()
        assert monotonic() - shutdown_started < budget(1)
        assert not release_logging.is_set()
    finally:
        release_logging.set()
        for thread in logging_threads:
            thread.join(timeout=budget(3))
            assert not thread.is_alive()


@pytest.mark.parametrize(
    ("frame_count", "filename", "truncated"),
    [
        (100, "x" * 10, True),
        (100, "x" * 140, True),
        (100, "x" * 1000, True),
        (1, "x" * 9000, True),
        (3, "x" * 10, False),
        (100, "路径" * 100, True),
        (1, "路径" * 2000, True),
    ],
)
def test_stack_capture_is_bounded(monkeypatch, frame_count, filename, truncated) -> None:
    frame = None
    for index in range(frame_count):
        frame = SimpleNamespace(
            f_code=SimpleNamespace(co_filename=filename, co_name=f"call_{index}"),
            f_lineno=index,
            f_back=frame,
        )
    monkeypatch.setattr(watchdog, "sys", SimpleNamespace(_current_frames=lambda: {123: frame}))
    stack = watchdog._capture_stack(123)
    marker = "  ... older frames omitted ...\n"
    assert stack.endswith(marker) == truncated
    assert stack.endswith("\n")
    frame_lines = stack.splitlines()[1:-1] if truncated else stack.splitlines()[1:]
    assert len(frame_lines) <= 64
    assert all(
        re.fullmatch(rf'  File "{re.escape(filename)}", line \d+, in call_\d+', line)
        for line in frame_lines
    )
    if len(filename.encode("utf-8")) < 8192:
        assert frame_lines[0].endswith(f"in call_{frame_count - 1}")
    if not truncated:
        assert len(frame_lines) == frame_count
    assert len(stack.encode("utf-8")) <= 8192


@pytest.mark.parametrize("threshold", [0, -1, float("inf"), float("nan"), 0.5])
def test_invalid_intervals_are_rejected(threshold) -> None:
    with pytest.raises(ValueError):
        with watchdog.monitor_event_loop(stall_threshold_s=threshold):
            pytest.fail("Invalid watchdog configuration was accepted")


def test_invalid_process_role_is_rejected() -> None:
    with pytest.raises(ValueError, match="process role"):
        with watchdog.monitor_event_loop(process_role="worker"):
            pytest.fail("Unknown watchdog role was accepted")
