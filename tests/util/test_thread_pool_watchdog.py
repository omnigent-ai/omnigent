"""Name where a thread pool's workers are once its queued jobs wait too long."""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import sys
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from types import FrameType
from typing import Any

import pytest

from omnigent.debug_logging import record_to_row
from omnigent.util import thread_pool_watchdog as watchdog
from tests._helpers.polling import poll_until
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


@pytest.mark.asyncio
async def test_report_failure_does_not_disable_the_watchdog(pool_records, monkeypatch) -> None:
    worker_stacks = watchdog._worker_stacks
    failed = False

    def fail_once(workers: list[threading.Thread]) -> str:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("stack capture failed")
        return worker_stacks(workers)

    monkeypatch.setattr(watchdog, "_worker_stacks", fail_once)
    with held_pool(1) as pool:
        watchdog.watch_thread_pool(
            pool,
            thread_name_prefix="runner",
            stall_threshold_s=0.3,
            probe_interval_s=0.05,
            report_interval_s=budget(0.3),
        )
        failure = await asyncio.to_thread(pool_records.get, timeout=budget(5))
        assert failure.levelno == logging.ERROR
        recovered = await asyncio.to_thread(pool_records.get, timeout=budget(5))
        assert recovered.event_name == "runner_thread_pool_backed_up"


def test_watchdog_that_cannot_start_is_logged_not_raised(pool_records, monkeypatch) -> None:
    start = threading.Thread.start

    def refuse_watchdog(thread: threading.Thread) -> None:
        if thread.name == "omnigent-runner-pool-watchdog":
            raise RuntimeError("can't start new thread")
        start(thread)

    monkeypatch.setattr(threading.Thread, "start", refuse_watchdog)
    pool = ThreadPoolExecutor(1, thread_name_prefix="runner")
    try:
        watch_thread = watchdog.watch_thread_pool(pool, thread_name_prefix="runner")

        assert not watch_thread.is_alive()
        record = pool_records.get(timeout=budget(5))
        assert record.event_name == "runner_thread_pool_watchdog_failed"
        assert pool.submit(sum, [1, 2]).result(timeout=budget(5)) == 3
    finally:
        pool.shutdown(wait=True)


class _PoolThatFailsFirstSubmit(ThreadPoolExecutor):
    """A live pool whose first submit fails, like a transient worker-start failure."""

    def __init__(self) -> None:
        super().__init__(1, thread_name_prefix="runner")
        self.submits = 0

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        self.submits += 1
        if self.submits == 1:
            raise RuntimeError("can't start new thread")
        return super().submit(fn, *args, **kwargs)


def test_probe_submit_failure_on_a_live_pool_keeps_watching(pool_records) -> None:
    pool = _PoolThatFailsFirstSubmit()
    watch_thread = watchdog.watch_thread_pool(
        pool, thread_name_prefix="runner", stall_threshold_s=0.3, probe_interval_s=0.05
    )
    try:
        record = pool_records.get(timeout=budget(5))
        assert record.event_name == "runner_thread_pool_probe_failed"
        assert record.levelno == logging.WARNING
        poll_until(lambda: pool.submits >= 3, timeout_s=budget(5), interval_s=0.01)
        assert watch_thread.is_alive()
    finally:
        pool.shutdown(wait=True)
    watch_thread.join(timeout=budget(2))
    assert not watch_thread.is_alive()


def _function_at(path: str, name: str, body: str, **namespace: object) -> Callable[..., object]:
    """Define *name* so its frames report *path* as their file."""
    scope: dict[str, Any] = {"sys": sys, **namespace}
    exec(compile(f"def {name}(callback):\n    {body}\n", path, "exec"), scope)
    return scope[name]


def _location_through(*layers: tuple[str, str]) -> str:
    """Call through each ``(path, name)`` layer, outermost first, and locate the innermost."""
    callee = _function_at(
        watchdog._STDLIB_ROOT + "fake_stdlib_leaf.py",
        "stdlib_leaf",
        "return callback(sys._getframe())",
    )
    for path, name in reversed(layers):
        callee = _function_at(path, name, "return callee(callback)", callee=callee)

    def locate(frame: FrameType) -> str:
        return watchdog._worker_location(frame)

    return str(callee(locate))


def test_worker_location_prefers_the_innermost_omnigent_frame() -> None:
    package = watchdog._PACKAGE_ROOT
    external = "/opt/venv/site-packages/somelib/client.py"

    location = _location_through(
        (package + "runner/outer.py", "outer_omnigent"),
        (external, "external_call"),
        (package + "runtime/inner.py", "inner_omnigent"),
    )
    assert location == "omnigent/runtime/inner.py:2 in inner_omnigent"

    # No Omnigent frame: fall back to the innermost frame outside the standard library.
    assert _location_through((external, "external_call")) == "client.py:2 in external_call"


def test_summary_counts_locations_it_leaves_out() -> None:
    locations = Counter({f"module_{index}.py:1 in job": 10 - index for index in range(7)})

    summary = watchdog._summarize_locations(locations)

    assert summary.startswith("module_0.py:1 in job x10, module_1.py:1 in job x9")
    assert summary.endswith(", +2 more locations")
    assert "module_5.py" not in summary


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
