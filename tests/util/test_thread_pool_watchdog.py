"""Name what holds a thread pool once its queued jobs wait too long."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import os
import queue
import threading
from collections.abc import Iterator

import pytest

from omnigent.debug_logging import record_to_row
from omnigent.util import thread_pool_watchdog as watchdog
from tests.budgets import budget


@pytest.fixture
def pool_records() -> Iterator[queue.Queue[logging.LogRecord]]:
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


def hold_worker(release: threading.Event, private_argument: str) -> str:
    """Occupy a worker until released, like a slow ``git status``."""
    release.wait(budget(10))
    return private_argument


def read_small_file() -> str:
    """A quick job that only needs a free worker."""
    return "ok"


def read_hook_events() -> str:
    """Another quick job, queued behind the held worker."""
    return "events"


@pytest.mark.asyncio
async def test_backed_up_pool_reports_occupants_once_per_interval(pool_records) -> None:
    private_argument = "private-job-argument-canary"
    release = threading.Event()
    report_interval_s = budget(0.8)
    pool = watchdog.MonitoredThreadPoolExecutor(
        1,
        thread_name_prefix="runner",
        stall_threshold_s=0.3,
        sample_interval_s=0.05,
        report_interval_s=report_interval_s,
    )
    loop = asyncio.get_running_loop()
    try:
        held = loop.run_in_executor(pool, hold_worker, release, private_argument)
        # Queue through the same path asyncio.to_thread uses, so the label names the job.
        queued = loop.run_in_executor(
            pool, functools.partial(contextvars.copy_context().run, read_small_file)
        )

        record = await asyncio.to_thread(pool_records.get, timeout=budget(5))
        row = record_to_row(record, source="runner")
        assert row["event_name"] == "runner_thread_pool_backed_up"
        assert row["attributes"]["pid"] == str(os.getpid())
        assert record.attributes["max_workers"] == 1
        assert record.attributes["busy_workers"] == 1
        assert record.attributes["queued_jobs"] == 1
        assert record.attributes["oldest_queued_s"] >= 0.3
        assert "hold_worker x1" in record.attributes["running"]
        assert "read_small_file x1" in record.attributes["queued"]
        assert "hold_worker x1" in row["message"]
        assert "read_small_file x1" in row["message"]
        assert "\n" not in row["message"]
        assert "Worker runner_0 running" in row["stack_trace"]
        assert "in hold_worker" in row["stack_trace"]
        assert private_argument not in str(row)

        # Still backed up: the next report waits out the report interval.
        second = await asyncio.to_thread(pool_records.get, timeout=budget(5))
        assert second.created - record.created >= report_interval_s - 0.05
        assert second.attributes["queued_jobs"] == 1

        release.set()
        assert await held == private_argument
        assert await queued == "ok"
        assert pool.snapshot().queued == ()
        assert pool.snapshot().running == {}
    finally:
        release.set()
        pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_job_cancelled_while_queued_is_left_out_of_reports(pool_records) -> None:
    release = threading.Event()
    pool = watchdog.MonitoredThreadPoolExecutor(
        1, stall_threshold_s=0.3, sample_interval_s=0.05, report_interval_s=budget(30)
    )
    try:
        pool.submit(hold_worker, release, "held")
        cancelled = pool.submit(read_small_file)
        assert cancelled.cancel()
        assert pool.snapshot().queued == ()
        still_queued = pool.submit(read_hook_events)

        record = await asyncio.to_thread(pool_records.get, timeout=budget(5))
        assert record.attributes["queued_jobs"] == 1
        assert "read_hook_events x1" in record.attributes["queued"]
        assert "read_small_file" not in record.getMessage()

        release.set()
        assert still_queued.result(timeout=budget(5)) == "events"
    finally:
        release.set()
        pool.shutdown(wait=True)


def test_shutdown_stops_the_watchdog_thread() -> None:
    pool = watchdog.MonitoredThreadPoolExecutor(1, sample_interval_s=0.05, stall_threshold_s=0.3)
    assert pool.submit(read_small_file).result(timeout=budget(5)) == "ok"
    watch_thread = pool._watch_thread
    assert watch_thread is not None and watch_thread.is_alive()

    pool.shutdown(wait=True)

    watch_thread.join(timeout=budget(2))
    assert not watch_thread.is_alive()


@pytest.mark.parametrize(
    ("stall_threshold_s", "sample_interval_s", "report_interval_s"),
    [(0.0, 0.05, 1.0), (float("inf"), 0.05, 1.0), (1.0, 1.0, 1.0), (1.0, 0.1, -1.0)],
)
def test_rejects_unusable_intervals(
    stall_threshold_s: float, sample_interval_s: float, report_interval_s: float
) -> None:
    with pytest.raises(ValueError):
        watchdog.MonitoredThreadPoolExecutor(
            1,
            stall_threshold_s=stall_threshold_s,
            sample_interval_s=sample_interval_s,
            report_interval_s=report_interval_s,
        )
