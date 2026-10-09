"""Failed-turn cleanup must keep heartbeats responsive while tmux blocks.

The real executor/bridge calls a slow tmux shim; delivery is forced to time out.
This measures elapsed time without a live Claude process or provider."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import stat
import time
from pathlib import Path

import pytest

import omnigent.inner.claude_native_executor as claude_native_executor
from omnigent.harnesses.claude_native.bridge import (
    REQUEST_SESSION_ID_ENV_VAR,
    ClaudePromptTimeout,
)
from omnigent.inner.claude_native_executor import ClaudeNativeExecutor
from omnigent.inner.executor import ExecutorError

# How long the stand-in tmux server blocks a kill-session. Well below the
# bridge's 10s subprocess timeout (the cleanup path must survive a slow —
# not just a dead — tmux) but far above any legitimate loop pause.
_SLOW_KILL_S = 1.0
# The loosest heartbeat gap a responsive loop is allowed. A 10ms heartbeat
# under CI jitter stays comfortably below this; the synchronous cleanup
# stalls the loop for the full _SLOW_KILL_S and lands far above it.
_MAX_ACCEPTABLE_STALL_S = 0.5
_HEARTBEAT_INTERVAL_S = 0.01


def _write_slow_tmux_shim(shim_dir: Path, kill_marker: Path) -> Path:
    """Write a tmux shim whose delayed kill-session marks successful cleanup."""
    shim = shim_dir / "tmux"
    shim.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *kill-session*)\n"
        f"    sleep {_SLOW_KILL_S}\n"
        f'    touch "{kill_marker}"\n'
        "    ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


@pytest.mark.asyncio
async def test_reap_failed_turn_does_not_stall_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Heartbeats must keep ticking while failed-turn cleanup waits for the tmux shim."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    # A real advertisement, so kill_session's _wait_for_tmux_info gate
    # (the part its timeout_s=1.0 actually bounds) passes immediately and
    # the slow part — the kill-session subprocess — is what runs.
    (bridge_dir / "tmux.json").write_text(
        json.dumps(
            {
                "socket_path": str(tmp_path / "tmux.sock"),
                "tmux_target": "claude",
            }
        ),
        encoding="utf-8",
    )

    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    kill_marker = tmp_path / "kill-session.ran"
    _write_slow_tmux_shim(shim_dir, kill_marker)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv(REQUEST_SESSION_ID_ENV_VAR, raising=False)

    def _time_out_delivery(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """Raise the same exception as the production readiness timeout."""
        del bridge_dir_arg, content, timeout_s
        raise ClaudePromptTimeout("Claude's input box never rendered; message not delivered.")

    monkeypatch.setattr(claude_native_executor, "inject_user_message", _time_out_delivery)

    executor = ClaudeNativeExecutor(bridge_dir)

    ticks: list[float] = []

    async def _heartbeat() -> None:
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(_HEARTBEAT_INTERVAL_S)

    heartbeat = asyncio.create_task(_heartbeat())
    try:
        # Establish a baseline cadence before the failing turn.
        await asyncio.sleep(0.1)
        events = [
            event
            async for event in executor.run_turn(
                messages=[{"role": "user", "content": "hello from web"}],
                tools=[],
                system_prompt="ignored",
            )
        ]
        # And a beat afterwards so the post-cleanup cadence is sampled too.
        await asyncio.sleep(0.1)
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat

    # The failed-turn path ran to completion: the delivery timeout became an
    # ExecutorError, and cleanup's real tmux kill-session subprocess finished
    # before that error was yielded (a fix must offload, not skip, the kill).
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert kill_marker.exists(), "cleanup never ran tmux kill-session"

    assert len(ticks) >= 2
    max_gap = max(later - earlier for earlier, later in itertools.pairwise(ticks))
    assert max_gap < _MAX_ACCEPTABLE_STALL_S, (
        f"event loop stalled for {max_gap:.2f}s while claude-native cleanup "
        f"waited on a slow tmux kill-session ({_SLOW_KILL_S:.1f}s): "
        f"_reap_failed_turn must not run blocking tmux operations on the "
        f"event loop"
    )
