"""The forwarder keeps its pane's launch-time files fresh while it runs (#9900)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import forwarder
from tests.harnesses.claude_native.forwarder._support import _start_recording_server


async def _wait_for(predicate, *, timeout_s: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_launch_files_refresh_repeats_until_the_forwarder_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refresh runs at start and on every interval, and stops with the forwarder."""
    calls: list[Path] = []
    monkeypatch.setattr(forwarder, "refresh_launch_files", lambda path: calls.append(path) or 0)

    async with forwarder._keep_launch_files_fresh(tmp_path, "sess-1", interval_s=0.01):
        await _wait_for(lambda: len(calls) >= 3)

    assert calls[0] == tmp_path
    assert not [t for t in asyncio.all_tasks() if t.get_name() == "claude-launch-files-sess-1"]


@pytest.mark.asyncio
async def test_launch_files_refresh_failure_does_not_stop_upkeep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed refresh is retried on the next interval instead of ending the task."""
    calls: list[int] = []

    def flaky(_path: Path) -> int:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("bridge dir busy")
        return 0

    monkeypatch.setattr(forwarder, "refresh_launch_files", flaky)

    async with forwarder._keep_launch_files_fresh(tmp_path, "sess-1", interval_s=0.01):
        await _wait_for(lambda: len(calls) >= 2)


@pytest.mark.asyncio
async def test_launch_files_refresh_does_not_swallow_the_callers_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling the forwarder still cancels it; the upkeep task does not absorb it."""
    monkeypatch.setattr(forwarder, "refresh_launch_files", lambda _path: 0)

    async def run_forever() -> None:
        async with forwarder._keep_launch_files_fresh(tmp_path, "sess-1", interval_s=0.01):
            await asyncio.Event().wait()

    task = asyncio.create_task(run_forever())
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_running_forwarder_refreshes_its_bridge_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The transcript forwarder starts the upkeep for the bridge dir it follows."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    calls: list[Path] = []
    monkeypatch.setattr(forwarder, "refresh_launch_files", lambda path: calls.append(path) or 0)

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        await _wait_for(lambda: bool(calls))
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert calls == [bridge_dir]
