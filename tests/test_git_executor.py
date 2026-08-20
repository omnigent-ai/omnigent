"""Tests for the dedicated, bounded git thread pool.

The point of the pool is isolation: git runs here, never on asyncio's shared
default executor, so a slow git can't starve the workers the host/runner
control plane needs, and ``max_workers`` caps concurrent git processes.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

import omnigent.git_executor as git_executor


@pytest.fixture(autouse=True)
def _reset_pool() -> Iterator[None]:
    """Rebuild the process-wide pool per test so env changes take effect."""
    git_executor._executor = None
    yield
    pool = git_executor._executor
    git_executor._executor = None
    if pool is not None:
        pool.shutdown(wait=True)


def test_max_workers_default() -> None:
    """Unset/invalid env falls back to the default cap."""
    assert git_executor._max_workers() == git_executor._DEFAULT_MAX_GIT_WORKERS


@pytest.mark.parametrize(
    ("raw", "expected"), [("2", 2), ("16", 16), ("0", 4), ("nope", 4), ("-3", 4)]
)
def test_max_workers_env_override(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
) -> None:
    """A positive integer overrides the cap; junk/non-positive uses the default."""
    monkeypatch.setenv("OMNIGENT_GIT_MAX_WORKERS", raw)
    assert git_executor._max_workers() == expected


def test_executor_is_singleton() -> None:
    """Repeated calls return the same pool, tagged for git."""
    assert git_executor.git_executor() is git_executor.git_executor()
    assert git_executor.git_executor()._thread_name_prefix == "omni-git"


async def test_run_git_returns_value_off_the_main_thread() -> None:
    """run_git executes the callable on a git-pool worker and returns its result."""
    result = await git_executor.run_git(lambda x: (x * 2, threading.current_thread().name), 21)
    value, thread_name = result
    assert value == 42
    assert thread_name.startswith("omni-git")


async def test_pool_caps_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    """No more than ``max_workers`` git callables run at once; the rest queue.

    This is the containment guarantee: a burst of git work can occupy at most
    the cap, so it can never exhaust the box's processes or (were it the shared
    pool) the control-plane workers.
    """
    import asyncio

    monkeypatch.setenv("OMNIGENT_GIT_MAX_WORKERS", "2")
    git_executor._executor = None  # rebuild with the new cap

    release = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0

    def _blocking() -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        release.wait(timeout=5)
        with lock:
            active -= 1

    tasks = [asyncio.ensure_future(git_executor.run_git(_blocking)) for _ in range(4)]
    # Let the pool pick up work; with a cap of 2 only 2 can be running.
    await asyncio.sleep(0.3)
    with lock:
        assert active == 2
        assert peak == 2
    release.set()
    await asyncio.gather(*tasks)
    assert peak == 2
