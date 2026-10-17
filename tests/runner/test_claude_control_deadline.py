"""Caller cancellation stops the original Claude pane-driving worker."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge
from omnigent.runner.native_controls import _ClaudeControlDeadline, _drive_claude_pane


@pytest.mark.asyncio
async def test_cancelled_control_stops_its_original_blocked_worker() -> None:
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    stopped = threading.Event()
    simulated_keys: list[str] = []
    deadline = _ClaudeControlDeadline()

    def worker() -> None:
        entered.set()
        try:
            assert release.wait(3)
            bridge._check_injection_cancelled()
            simulated_keys.append("simulated-key")
        except bridge.ClaudeInjectionCancelled:
            stopped.set()
        finally:
            finished.set()

    task = asyncio.create_task(_drive_claude_pane(deadline, worker))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 3)
        await asyncio.gather(task, return_exceptions=True)
    assert deadline.is_set()
    assert stopped.is_set()
    assert simulated_keys == []


@pytest.mark.asyncio
async def test_cancelled_control_never_enters_its_queued_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiting, finished, stopped = threading.Event(), threading.Event(), threading.Event()
    simulated_keys: list[str] = []
    deadline = _ClaudeControlDeadline()
    check = bridge._check_injection_cancelled

    def observed_check() -> None:
        waiting.set()
        check()

    monkeypatch.setattr(bridge, "_check_injection_cancelled", observed_check)

    @bridge._serialize_bridge_injection
    def injection(bridge_dir: Path) -> None:
        simulated_keys.append("simulated-key")

    def worker() -> None:
        try:
            injection(tmp_path)
        except bridge.ClaudeInjectionCancelled:
            stopped.set()
        finally:
            finished.set()

    lock = bridge._bridge_injection_lock(tmp_path)
    lock.acquire()
    task = asyncio.create_task(_drive_claude_pane(deadline, worker))
    try:
        assert await asyncio.to_thread(waiting.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        lock.release()
        assert await asyncio.to_thread(finished.wait, 3)
        await asyncio.gather(task, return_exceptions=True)
    assert deadline.is_set()
    assert stopped.is_set()
    assert simulated_keys == []
