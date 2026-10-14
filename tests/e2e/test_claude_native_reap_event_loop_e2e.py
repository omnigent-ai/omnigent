"""E2E regression: reaping a failed or cancelled claude-native delivery must not
block the runner's asyncio event loop.

A real tmux pane sits behind a ``tmux`` shim that holds ``kill-session`` for
:data:`_KILL_DELAY_S`; a 10ms heartbeat on the same loop records its longest
pause. Needs only ``tmux``: no LLM, ``claude`` binary, or server.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge as claude_bridge
from omnigent.harnesses.claude_native.bridge import (
    _BRIDGE_ROOT,
    inject_user_message,
    write_tmux_target,
)
from omnigent.inner import claude_native_executor
from omnigent.inner.claude_native_executor import ClaudeNativeExecutor
from omnigent.inner.executor import ExecutorError

_REAL_TMUX = shutil.which("tmux")
pytestmark = pytest.mark.skipif(_REAL_TMUX is None, reason="requires tmux on PATH")

_MESSAGE = "hello from the web chat"
_TMUX_TARGET = "claude"
# How long the shim holds ``kill-session`` before running the real kill.
_KILL_DELAY_S = 3.0
# Longest heartbeat pause a loop that is not blocked by the kill may show: well
# below _KILL_DELAY_S, with headroom for scheduler jitter on loaded CI hosts.
_MAX_HEARTBEAT_GAP_S = 1.0
# Base readiness budget for the real ``inject_user_message``; the liveness
# probe that reports an exited pane fires when it elapses (30s in production).
_READINESS_BUDGET_S = 3.0
# Grace for the heartbeat's overdue wakeup to record a stall that landed just
# before the measured section ended.
_HEARTBEAT_SETTLE_S = 0.3

_SLOW_KILL_TMUX = """\
#!/usr/bin/env bash
for arg in "$@"; do
  if [ "$arg" = "kill-session" ]; then
    echo "start $({python} -c 'import time; print(time.time())')" >> {kill_log}
    sleep {delay}
    echo "exec $({python} -c 'import time; print(time.time())')" >> {kill_log}
    break
  fi
done
exec {real_tmux} "$@"
"""


class _ClaudePaneRig:
    """A real tmux pane advertised to the bridge, reached through the slow-kill shim."""

    def __init__(self, work: Path) -> None:
        assert _REAL_TMUX is not None
        self.work = work
        # Keep the socket path short: a long path overflows the AF_UNIX limit.
        self.socket_path = work / "t.sock"
        self.kill_log = work / "kills.log"
        self.bridge_dir = _BRIDGE_ROOT / f"reap-{uuid.uuid4().hex}"
        self.shim_dir = work / "bin"
        self.shim_dir.mkdir()
        shim = self.shim_dir / "tmux"
        shim.write_text(
            _SLOW_KILL_TMUX.format(
                kill_log=shlex.quote(str(self.kill_log)),
                delay=_KILL_DELAY_S,
                python=shlex.quote(sys.executable),
                real_tmux=shlex.quote(_REAL_TMUX),
            ),
            encoding="utf-8",
        )
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)

    def _tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        assert _REAL_TMUX is not None
        return subprocess.run(
            [_REAL_TMUX, "-S", str(self.socket_path), *args],
            check=check,
            capture_output=True,
            text=True,
            timeout=30.0,
        )

    def start_pane(self, command: str) -> None:
        """Run *command* in the pane and advertise it the way the runner does."""
        self._tmux(
            "-f",
            "/dev/null",
            "new-session",
            "-d",
            "-s",
            _TMUX_TARGET,
            "-x",
            "80",
            "-y",
            "24",
            "sleep 600",
        )
        # The runner launches Claude Code with keep_alive_after_exit, so a dead
        # pane is retained and #{pane_dead} answers the liveness probe.
        self._tmux("set-option", "-g", "remain-on-exit", "on")
        self._tmux("respawn-pane", "-k", "-t", _TMUX_TARGET, command)
        write_tmux_target(self.bridge_dir, socket_path=self.socket_path, tmux_target=_TMUX_TARGET)

    def wait_pane_dead(self, timeout_s: float = 10.0) -> str:
        """Return the pane's exit status once tmux reports it dead."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            proc = self._tmux(
                "list-panes",
                "-t",
                _TMUX_TARGET,
                "-F",
                "#{pane_dead} #{pane_dead_status}",
                check=False,
            )
            fields = proc.stdout.split()
            if fields[:1] == ["1"]:
                if len(fields) > 1:
                    return fields[1]
                # Ubuntu's tmux links libutempter, whose helper resets SIGCHLD
                # during pane teardown; a shell exiting then stays an unreaped
                # zombie with no status until tmux next reaps. Fork a job for it.
                self._tmux("run-shell", "true", check=False)
            time.sleep(0.05)
        raise AssertionError("pane did not exit in time")

    def session_alive(self) -> bool:
        return self._tmux("has-session", "-t", _TMUX_TARGET, check=False).returncode == 0

    def kill_events(self) -> list[tuple[str, float]]:
        if not self.kill_log.exists():
            return []
        events = []
        for line in self.kill_log.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            assert len(fields) == 2, f"malformed kill log line: {line!r}"
            events.append((fields[0], float(fields[1])))
        return events

    def close(self) -> None:
        self._tmux("kill-server", check=False)
        shutil.rmtree(self.work, ignore_errors=True)
        shutil.rmtree(self.bridge_dir, ignore_errors=True)


@pytest.fixture
def pane_rig(monkeypatch: pytest.MonkeyPatch) -> Iterator[_ClaudePaneRig]:
    work = Path(tempfile.mkdtemp(prefix="reap-"))
    try:
        rig = _ClaudePaneRig(work)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    monkeypatch.setenv("PATH", f"{rig.shim_dir}{os.pathsep}{os.environ['PATH']}")
    try:
        yield rig
    finally:
        rig.close()


class _Heartbeat:
    """Records the longest pause between 10ms wakeups on the running loop."""

    def __init__(self) -> None:
        self.max_gap_s = 0.0
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            self.max_gap_s = max(self.max_gap_s, now - last)
            last = now

    async def __aenter__(self) -> _Heartbeat:
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._task is not None
        await asyncio.sleep(_HEARTBEAT_SETTLE_S)
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task


def _assert_pane_reaped(rig: _ClaudePaneRig) -> None:
    """The failed turn's pane was killed exactly once, through the slow kill."""
    events = rig.kill_events()
    assert [kind for kind, _ in events] == ["start", "exec"], events
    held_s = events[1][1] - events[0][1]
    assert held_s >= _KILL_DELAY_S - 0.1, held_s
    assert not rig.session_alive()


def _run_turn(executor: ClaudeNativeExecutor):
    return executor.run_turn(
        messages=[{"role": "user", "content": _MESSAGE}],
        tools=[],
        system_prompt="",
    )


async def test_exited_terminal_cleanup_does_not_block_event_loop(
    pane_rig: _ClaudePaneRig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal that exits before the input box renders fails the turn, and
    reaping its pane must not pause the rest of the loop."""
    # Exit 0: the turn still fails and reaps, but logs without a traceback, so
    # the gap measures only the reap and not the cost of error-logging a crash.
    pane_rig.start_pane("echo 'Claude Code is starting...'; sleep 0.5; exit 0")
    assert pane_rig.wait_pane_dead() == "0"
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        functools.partial(inject_user_message, timeout_s=_READINESS_BUDGET_S),
    )
    executor = ClaudeNativeExecutor(bridge_dir=pane_rig.bridge_dir)

    async with _Heartbeat() as heartbeat:
        events = [event async for event in _run_turn(executor)]

    assert len(events) == 1 and isinstance(events[0], ExecutorError), events
    assert "terminal has exited" in events[0].message, events[0].message
    _assert_pane_reaped(pane_rig)
    assert heartbeat.max_gap_s < _MAX_HEARTBEAT_GAP_S, (
        f"event loop paused for {heartbeat.max_gap_s:.2f}s while the pane was killed"
    )


async def test_cancelled_delivery_cleanup_does_not_block_event_loop(
    pane_rig: _ClaudePaneRig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a delivery that is still waiting for the input box reaps the
    pane before the cancellation propagates, without pausing the rest of the loop."""
    pane_rig.start_pane("echo 'Claude Code is starting...'; sleep 600")
    polling = threading.Event()
    real_capture = claude_bridge._capture_pane

    def observed_capture(*args: object, **kwargs: object) -> str:
        polling.set()
        return real_capture(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(claude_bridge, "_capture_pane", observed_capture)
    executor = ClaudeNativeExecutor(bridge_dir=pane_rig.bridge_dir)

    async def deliver() -> None:
        async for _event in _run_turn(executor):
            raise AssertionError("cancelled delivery completed")

    async with _Heartbeat() as heartbeat:
        task = asyncio.create_task(deliver())
        try:
            assert await asyncio.to_thread(polling.wait, 10), "delivery never polled the pane"
            task.cancel()
            done, _pending = await asyncio.wait({task}, timeout=15)
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    assert task in done, "cancelled delivery did not finish"
    assert task.cancelled()
    assert not executor._inject_lock.locked()
    _assert_pane_reaped(pane_rig)
    assert heartbeat.max_gap_s < _MAX_HEARTBEAT_GAP_S, (
        f"event loop paused for {heartbeat.max_gap_s:.2f}s while the pane was killed"
    )
