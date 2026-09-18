"""End-to-end regression: claude-native failed-turn cleanup blocks the event loop.

``ClaudeNativeExecutor._reap_failed_turn()`` calls ``kill_session(...,
timeout_s=1.0)`` synchronously from the async ``run_turn`` coroutine. The
``timeout_s`` argument only bounds waiting for the ``tmux.json``
advertisement; the actual ``tmux kill-session`` subprocess is bounded by
``_TMUX_SEND_TIMEOUT_S`` (10s). A slow or unresponsive tmux server therefore
stalls the whole event loop — heartbeats, steering-message delivery, and
cancellation on that loop cannot advance until the subprocess returns.

This test drives the real executor and the real bridge cleanup path
(``kill_session`` → ``_wait_for_tmux_info`` → ``_run_tmux`` →
``subprocess.run``). The only fault injections are the two the report
prescribes: a ``tmux`` shim on PATH that stands in for a slow tmux server
(its ``kill-session`` blocks), and the injection helper raising
``ClaudePromptTimeout`` — the exact exception the production readiness gate
raises when Claude's input box never renders — to route ``run_turn`` into
``_reap_failed_turn``. A heartbeat coroutine on the same loop must keep
ticking while cleanup waits on tmux; today it freezes for the full duration
of the kill-session subprocess.
"""

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
    """
    Install a ``tmux`` shim that behaves like a slow tmux server.

    ``kill-session`` blocks for ``_SLOW_KILL_S`` before succeeding and
    touches ``kill_marker`` so the test can prove the real cleanup
    subprocess ran; every other tmux subcommand succeeds immediately.

    :param shim_dir: Directory placed at the front of ``PATH``.
    :param kill_marker: File the shim touches after the slow kill.
    :returns: Path to the executable shim.
    """
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
    """
    Heartbeats keep ticking while claude-native cleanup waits on tmux.

    Reproduces the defect: after a prompt-delivery timeout,
    ``_reap_failed_turn`` runs ``tmux kill-session`` synchronously on the
    event loop, so a heartbeat coroutine on the same loop cannot advance
    until the (up to 10s) subprocess returns. The turn must still end in
    ``ExecutorError`` and cleanup must still complete before that error is
    yielded — offloading must not skip or orphan the kill.
    """
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
        """
        Stand in for a readiness failure: the production gate's exception.

        :param bridge_dir_arg: Bridge directory passed by the executor.
        :param content: Text the executor tried to deliver.
        :param timeout_s: Readiness timeout (unused — the fake fails fast
            instead of spending the real gate's 30s).
        :returns: None. Always raises.
        :raises ClaudePromptTimeout: Unconditionally, exactly as
            ``_wait_for_claude_prompt_ready`` does when Claude's input box
            never renders.
        """
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
