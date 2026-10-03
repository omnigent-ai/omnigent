"""E2E regression: claude-native failed-turn cleanup stalls the runner's event loop.

When a claude-native delivery fails -- Claude Code's pane exited before its
input box rendered, or the person cancels a delivery still waiting on that
box -- ``ClaudeNativeExecutor`` reaps the pane with ``kill_session`` from the
async turn itself. ``kill_session`` shells out to ``tmux kill-session`` with a
blocking ``subprocess.run`` (bounded only by the bridge's 10s per-command
budget), so a slow or unresponsive tmux server freezes every other coroutine on
that loop -- heartbeats, other sessions' turns, steering, cancellation -- until
the subprocess returns.

The transport is real: a tmux server on a private socket advertised through the
production ``write_tmux_target``, the real executor, and the real bridge
readiness and cleanup paths. The one injected fault is the reported one -- a
stalled tmux server -- installed as a ``tmux`` shim on PATH whose
``kill-session`` takes ``_SLOW_KILL_S`` to return. A heartbeat coroutine on the
same loop measures the longest pause it suffers while the failed turn is
cleaned up.

Runs with no LLM, no ``claude`` binary and no server -- only ``tmux``::

    pytest tests/e2e/test_claude_native_reap_event_loop_stall_e2e.py -v
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native.bridge import _BRIDGE_ROOT, write_tmux_target
from omnigent.inner.claude_native_executor import ClaudeNativeExecutor
from omnigent.inner.executor import ExecutorError, ExecutorEvent

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="requires tmux on PATH")

_MESSAGE = "hello from the web chat"
_TMUX_TARGET = "claude"

# Below the bridge's 10s per-command budget so the kill still succeeds, far
# above any pause a healthy loop takes.
_SLOW_KILL_S = 1.5
_MAX_TOLERATED_GAP_S = 0.5
_HEARTBEAT_INTERVAL_S = 0.01

# A stalled tmux server: every command is logged, kill-session is slow.
_SLOW_TMUX_SHIM = """\
#!/bin/sh
printf '%s\\n' "$*" >> "{log}"
for arg in "$@"; do
  case "$arg" in kill-session) sleep {slow} ;; esac
done
exec "{real_tmux}" "$@"
"""


@pytest.fixture
def real_tmux() -> str:
    tmux = shutil.which("tmux")
    assert tmux is not None
    return tmux


@pytest.fixture
def slow_tmux(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, real_tmux: str) -> Path:
    """Put a ``tmux`` shim ahead of the real binary on PATH; returns its command log."""
    log = tmp_path / "tmux-calls.log"
    log.touch()
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "tmux"
    shim.write_text(
        _SLOW_TMUX_SHIM.format(log=log, slow=_SLOW_KILL_S, real_tmux=real_tmux),
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    return log


@contextlib.contextmanager
def _advertised_pane(real_tmux: str, pane_command: str) -> Iterator[tuple[Path, str]]:
    """Run ``pane_command`` in a private tmux server advertised through a bridge dir."""
    work = Path(tempfile.mkdtemp(prefix="reap-"))
    # A long path overflows the AF_UNIX limit.
    socket_path = work / "t.sock"
    subprocess.run(
        [
            real_tmux,
            "-S",
            str(socket_path),
            "new-session",
            "-d",
            "-s",
            _TMUX_TARGET,
            "-x",
            "80",
            "-y",
            "24",
            "sh",
            "-c",
            pane_command,
            ";",
            "set-option",
            "-gq",
            "remain-on-exit",
            "on",
        ],
        check=True,
        timeout=30.0,
    )
    bridge_dir = _BRIDGE_ROOT / f"reap-{uuid.uuid4().hex}"
    write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target=_TMUX_TARGET)
    try:
        yield bridge_dir, str(socket_path)
    finally:
        subprocess.run(
            [real_tmux, "-S", str(socket_path), "kill-server"], check=False, timeout=30.0
        )
        shutil.rmtree(bridge_dir, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


@pytest.fixture
def exited_claude_pane(real_tmux: str) -> Iterator[tuple[Path, str]]:
    """A pane whose Claude Code process crashed before rendering the input box."""
    with _advertised_pane(real_tmux, "sleep 0.5; exit 3") as (bridge_dir, socket_path):
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            probe = subprocess.run(
                [
                    real_tmux,
                    "-S",
                    socket_path,
                    "display-message",
                    "-p",
                    "-t",
                    _TMUX_TARGET,
                    "#{pane_dead}",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=10.0,
            )
            if probe.stdout.strip() == "1":
                break
            time.sleep(0.1)
        else:
            pytest.fail("fake Claude pane never exited")
        yield bridge_dir, socket_path


@pytest.fixture
def booting_claude_pane(real_tmux: str) -> Iterator[tuple[Path, str]]:
    """A live pane that never renders Claude Code's input box."""
    with _advertised_pane(real_tmux, "echo booting; exec sleep 600") as pane:
        yield pane


async def _heartbeat(stop: asyncio.Event) -> float:
    """Tick until ``stop`` is set; return the longest pause between ticks."""
    loop = asyncio.get_running_loop()
    last = loop.time()
    longest = 0.0
    while not stop.is_set():
        await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
        now = loop.time()
        longest = max(longest, now - last)
        last = now
    return longest


def _tmux_calls(log: Path, command: str) -> list[str]:
    return [line for line in log.read_text(encoding="utf-8").splitlines() if command in line]


async def _wait_for_tmux_calls(log: Path, command: str, count: int) -> None:
    async with asyncio.timeout(10.0):
        while len(_tmux_calls(log, command)) < count:
            await asyncio.sleep(0.02)


def _session_alive(real_tmux: str, socket_path: str) -> bool:
    probe = subprocess.run(
        [real_tmux, "-S", socket_path, "has-session", "-t", _TMUX_TARGET],
        capture_output=True,
        check=False,
        timeout=10.0,
    )
    return probe.returncode == 0


def _run_turn(executor: ClaudeNativeExecutor) -> AsyncIterator[ExecutorEvent]:
    return executor.run_turn(
        messages=[{"role": "user", "content": _MESSAGE}],
        tools=[],
        system_prompt="",
        config=None,
    )


async def test_exited_pane_cleanup_keeps_the_event_loop_responsive(
    exited_claude_pane: tuple[Path, str],
    slow_tmux: Path,
    real_tmux: str,
) -> None:
    """A delivery that fails because the pane exited is cleaned up without
    freezing the loop for the duration of the slow ``kill-session``."""
    bridge_dir, socket_path = exited_claude_pane
    executor = ClaudeNativeExecutor(bridge_dir=bridge_dir)

    stop = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(stop))
    try:
        events = [event async for event in _run_turn(executor)]
    finally:
        stop.set()
    longest_gap = await heartbeat

    assert len(events) == 1, events
    assert isinstance(events[0], ExecutorError), events[0]
    assert "has exited" in events[0].message, events[0].message
    assert "Cleanup also failed" not in events[0].message, events[0].message
    assert len(_tmux_calls(slow_tmux, "kill-session")) == 1
    assert not _session_alive(real_tmux, socket_path)

    assert longest_gap < _MAX_TOLERATED_GAP_S, (
        f"event loop stalled for {longest_gap:.2f}s while the failed turn was cleaned up"
    )


async def test_cancelled_delivery_cleanup_keeps_the_event_loop_responsive(
    booting_claude_pane: tuple[Path, str],
    slow_tmux: Path,
    real_tmux: str,
) -> None:
    """Cancelling a delivery still waiting for the input box drains and reaps
    the pane without freezing the loop for the duration of ``kill-session``."""
    bridge_dir, socket_path = booting_claude_pane
    executor = ClaudeNativeExecutor(bridge_dir=bridge_dir)

    async def deliver() -> None:
        async for _event in _run_turn(executor):
            raise AssertionError("cancelled turn emitted a completion")

    turn = asyncio.create_task(deliver())
    try:
        await _wait_for_tmux_calls(slow_tmux, "capture-pane", 2)
        stop = asyncio.Event()
        heartbeat = asyncio.create_task(_heartbeat(stop))
        turn.cancel()
        try:
            await asyncio.wait({turn}, timeout=30.0)
        finally:
            stop.set()
        longest_gap = await heartbeat
    finally:
        if not turn.done():
            turn.cancel()
            await asyncio.wait({turn})

    assert turn.done(), "cancelled delivery never finished"
    with pytest.raises(asyncio.CancelledError):
        turn.result()
    assert not executor._inject_lock.locked()
    assert len(_tmux_calls(slow_tmux, "kill-session")) == 1
    assert not _session_alive(real_tmux, socket_path)

    assert longest_gap < _MAX_TOLERATED_GAP_S, (
        f"event loop stalled for {longest_gap:.2f}s while the cancelled delivery was cleaned up"
    )
