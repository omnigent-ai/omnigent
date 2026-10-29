"""Blocking calls on a dedicated pool, apart from the loop's default executor."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor

from omnigent.util.dedicated_executor import DedicatedExecutor
from tests.budgets import budget

request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="unset")


def _pool_threads(prefix: str) -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name.startswith(f"{prefix}_")]


async def test_runs_with_caller_context_on_its_own_threads_started_on_first_use() -> None:
    executor = DedicatedExecutor(max_workers=1, thread_name_prefix="dedicated-test")
    # No worker exists until first use, so a process can still fork safely.
    assert _pool_threads("dedicated-test") == []
    request_id.set("req-123")

    def observe() -> tuple[str, str]:
        return threading.current_thread().name, request_id.get()

    thread_name, seen_request_id = await executor.run(observe)

    assert thread_name == "dedicated-test_0"
    assert seen_request_id == "req-123"


async def test_runs_while_the_default_executor_is_full() -> None:
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
    release = threading.Event()
    default_job = asyncio.ensure_future(asyncio.to_thread(release.wait, budget(10)))
    executor = DedicatedExecutor(max_workers=1, thread_name_prefix="dedicated-busy-test")
    try:
        assert await asyncio.wait_for(executor.run(sum, [1, 2]), timeout=budget(2)) == 3
    finally:
        release.set()
        await default_job
