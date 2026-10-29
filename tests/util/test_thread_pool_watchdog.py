"""Name where a thread pool's workers are once its queued jobs wait too long."""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

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


@contextmanager
def held_pool(workers: int, private_argument: str = "held") -> Iterator[ThreadPoolExecutor]:
    """A pool whose every worker is inside ``hold_worker`` until the context exits."""
    release = threading.Event()
    started = threading.Barrier(workers + 1)

    def hold() -> str:
        started.wait(budget(5))
        return hold_worker(release, private_argument)

    pool = ThreadPoolExecutor(workers, thread_name_prefix="runner")
    try:
        for _ in range(workers):
            pool.submit(hold)
        started.wait(budget(5))
        yield pool
    finally:
        release.set()
        pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_backed_up_pool_reports_where_workers_are_once_per_interval(pool_records) -> None:
    private_argument = "private-job-argument-canary"
    report_interval_s = budget(0.8)
    with held_pool(1, private_argument) as pool:
        watchdog.watch_thread_pool(
            pool,
            thread_name_prefix="runner",
            stall_threshold_s=0.3,
            probe_interval_s=0.05,
            report_interval_s=report_interval_s,
        )

        record = await asyncio.to_thread(pool_records.get, timeout=budget(5))
        row = record_to_row(record, source="runner")
        message = str(row["message"])
        stack_trace = str(row["stack_trace"])
        assert row["event_name"] == "runner_thread_pool_backed_up"
        assert record.attributes["pid"] == os.getpid()
        assert record.attributes["workers"] == 1
        assert record.attributes["waited_s"] >= 0.3
        assert "in hold_worker x1" in record.attributes["worker_locations"]
        assert "in hold_worker x1" in message
        assert "\n" not in message
        assert "Worker runner_0" in stack_trace
        assert "in hold_worker" in stack_trace
        assert private_argument not in str(row)

        # Still backed up: the next report waits out the report interval.
        second = await asyncio.to_thread(pool_records.get, timeout=budget(5))
        assert second.created - record.created >= report_interval_s - 0.05


def test_watchdog_exits_once_the_pool_shuts_down() -> None:
    pool = ThreadPoolExecutor(1, thread_name_prefix="runner")
    watch_thread = watchdog.watch_thread_pool(
        pool, thread_name_prefix="runner", stall_threshold_s=0.3, probe_interval_s=0.05
    )
    assert watch_thread.is_alive()

    pool.shutdown(wait=True)

    watch_thread.join(timeout=budget(2))
    assert not watch_thread.is_alive()


def test_worker_stacks_stay_within_the_report_budget(monkeypatch) -> None:
    with held_pool(3):
        workers = sorted(
            (thread for thread in threading.enumerate() if thread.name.startswith("runner_")),
            key=lambda thread: thread.name,
        )
        first_section = watchdog._worker_stacks(workers[:1])
        marker = "... 2 more workers omitted ...\n"
        limit = len(first_section.encode()) + len(marker)
        monkeypatch.setattr(watchdog, "_MAX_REPORT_STACK_BYTES", limit)

        stacks = watchdog._worker_stacks(workers)

    assert stacks == first_section + marker
    assert len(stacks.encode()) <= limit


@pytest.mark.parametrize(
    ("stall_threshold_s", "probe_interval_s", "report_interval_s"),
    [(0.0, 0.05, 1.0), (float("inf"), 0.05, 1.0), (1.0, -1.0, 1.0), (1.0, 0.1, float("nan"))],
)
def test_rejects_unusable_intervals(
    stall_threshold_s: float, probe_interval_s: float, report_interval_s: float
) -> None:
    with pytest.raises(ValueError):
        watchdog.watch_thread_pool(
            ThreadPoolExecutor(1),
            thread_name_prefix="runner",
            stall_threshold_s=stall_threshold_s,
            probe_interval_s=probe_interval_s,
            report_interval_s=report_interval_s,
        )
