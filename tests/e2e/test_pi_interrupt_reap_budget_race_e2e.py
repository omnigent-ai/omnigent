"""Regression e2e for the Pi harness interrupt/reap budget race.

Pi harness interrupt/reap budget race: interrupting a busy Pi session and then
re-prompting exposes two coupled defects that live in real product code:

1. ``_executor_adapter._safe_interrupt`` wraps ``interrupt_session()`` in a
   ``_INTERRUPT_SLICE_S`` timeout that is *shorter* than the 2.0s
   ``process.wait()`` inside ``_PiRpcSession.close()``. When the outer slice
   fires first it injects a ``CancelledError`` into ``close()``'s inner
   ``wait_for`` -- not the ``TimeoutError`` its ``except`` catches -- so the
   ``SIGKILL`` fallback never runs and the Pi subprocess is left ALIVE while
   omnigent believes the session was torn down.

2. ``PiExecutor.run_turn`` builds the RPC ``prompt`` command with no
   ``streamingBehavior`` key. Against the still-alive (unconfirmed-dead)
   subprocess, Pi's ``isStreaming`` busy check then throws the raw protocol
   error ``Agent is already processing. Specify streamingBehavior ('steer' or
   'followUp') to queue the message.`` straight at the user.

The tests drive the REAL product code paths. They assert the *correct*
post-fix behavior, so they FAIL on the buggy build (the reproduction) and PASS
once the interrupt slice is widened to cover the reap and every RPC prompt
carries ``streamingBehavior``. Neither test needs the ``pi`` CLI: facet 1
injects a real OS subprocess that ignores ``SIGTERM`` (modelling a wedged Pi
process mid-turn), and facet 2 stubs only ``_create_subprocess_exec`` so the
real ``run_turn`` wiring writes its command bytes to a captured stdin.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from typing import Any

import pytest

from omnigent.inner.executor import TurnComplete
from omnigent.inner.pi_executor import (
    PiExecutor,
    _PiRpcSession,
    _PiSessionState,
)
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.runtime.harnesses._scaffold import TurnContext
from tests._helpers.polling import poll_until_async

# A child process that arms a SIGTERM-ignoring handler, prints ``READY`` once
# the handler is live, then sleeps. Models a wedged Pi subprocess that will NOT
# exit within ``_PiRpcSession.close()``'s 2.0s ``process.wait()``.
_WEDGED_CHILD = textwrap.dedent(
    """
    import signal, sys, time
    signal.signal(signal.SIGTERM, lambda *a: None)
    sys.stdout.write("READY\\n")
    sys.stdout.flush()
    time.sleep(30)
    """
)
# Also closes its stdin, so Pi's abort write fails and the interrupt falls back to closing.
_WEDGED_CHILD_WITHOUT_STDIN = "import os\nos.close(0)\n" + _WEDGED_CHILD


async def _start_wedged_pi_session(
    executor: PiExecutor, script: str
) -> asyncio.subprocess.Process:
    """Wire a real wedged child into *executor*'s ``sess`` the way a live turn does."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    rpc = _PiRpcSession(process=proc)
    rpc._read_task = asyncio.create_task(rpc._reader())
    rpc._stderr_task = asyncio.create_task(rpc._stderr_reader())
    executor._session_states["sess"] = _PiSessionState(rpc=rpc)
    try:
        # Wait until the SIGTERM-ignoring handler is armed so the reap genuinely races it.
        ready = await rpc.read_line(timeout=10.0)
        assert ready == "READY", f"wedged child never became ready: {ready!r}"
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    return proc


async def test_safe_interrupt_kills_wedged_pi_subprocess() -> None:
    """Facet 1: after an abnormal-exit interrupt+reap the Pi subprocess must be
    confirmed dead, even when it ignores ``SIGTERM``.

    On the buggy build the outer 1.5s slice cancels ``close()``'s 2.0s wait
    before its ``TimeoutError`` -> ``SIGKILL`` fallback can run, orphaning the
    child. The fix gives the slice room for the full reap.
    """
    executor = PiExecutor(pi_path="/bin/true")  # never spawns pi; child injected below
    proc = await _start_wedged_pi_session(executor, _WEDGED_CHILD)
    try:
        adapter = ExecutorAdapter(executor_factory=lambda: executor)

        # Drive the REAL abnormal-exit interrupt path.
        await adapter._safe_interrupt(executor, "sess")

        # Let the reap window settle past its budget.
        await asyncio.sleep(0.5)

        assert proc.returncode is not None, (
            "Pi subprocess left ALIVE after interrupt+reap: the interrupt slice "
            "cancelled close()'s inner process.wait() before its own "
            "TimeoutError -> SIGKILL fallback could run (facet 1)."
        )
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


async def test_interrupt_handler_kills_wedged_pi_subprocess_after_failed_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupt answered before Pi's fallback close finishes still kills the process."""
    monkeypatch.setattr(
        "omnigent.runtime.harnesses._executor_adapter._INTERRUPT_SETTLE_TIMEOUT_S", 0.2
    )
    executor = PiExecutor(pi_path="/bin/true")
    proc = await _start_wedged_pi_session(executor, _WEDGED_CHILD_WITHOUT_STDIN)
    try:
        adapter = ExecutorAdapter(executor_factory=lambda: executor, session_key="sess")
        adapter._ensure_executor()
        adapter._in_flight["resp_1"] = TurnContext(
            response_id="resp_1", event_queue=asyncio.Queue(), cancelled=asyncio.Event()
        )
        adapter._turn_finished = asyncio.Event()

        response = await asyncio.wait_for(adapter._handle_interrupt_event(), timeout=1.0)

        assert response.status_code == 204
        assert adapter._executor is None
        cleanup = adapter._abandoned_executor_cleanup
        assert cleanup is not None
        await asyncio.wait_for(cleanup, timeout=10.0)
        await poll_until_async(lambda: proc.returncode is not None, timeout_s=3.0)
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


async def test_rpc_prompt_command_sets_streaming_behavior() -> None:
    """Facet 2: every RPC ``prompt`` command must carry ``streamingBehavior``.

    Drives the real ``PiExecutor.run_turn`` wiring with only the OS spawn
    stubbed, and inspects the exact JSONL command bytes written to Pi's stdin.
    On the buggy build the command omits ``streamingBehavior``, so a residual
    race surfaces as Pi's raw "Agent is already processing" protocol error.
    """
    captured: dict[str, Any] = {}

    class _CapturingStdin:
        def __init__(self) -> None:
            self.data: list[bytes] = []

        def write(self, b: bytes) -> None:
            self.data.append(b)

        async def drain(self) -> None:
            return None

    class _StubReader:
        def __init__(self, lines: list[str]) -> None:
            self._chunks = [(line + "\n").encode() for line in lines]

        async def read(self, n: int = -1) -> bytes:
            if self._chunks:
                return self._chunks.pop(0)
            return b""

    class _FakeProc:
        def __init__(self, stdout_lines: list[str]) -> None:
            self.stdin = _CapturingStdin()
            self.stdout = _StubReader(stdout_lines)
            self.stderr = _StubReader([])
            self.returncode: int | None = None
            self.pid = 4242

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            return self.returncode or 0

    async def _fake_spawn(*args: Any, **kwargs: Any) -> Any:
        proc = _FakeProc(
            stdout_lines=[
                json.dumps({"type": "response", "success": True}),
                json.dumps(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {"type": "text_delta", "delta": "hi"},
                    }
                ),
                json.dumps({"type": "agent_end", "messages": []}),
            ]
        )
        captured["proc"] = proc
        return proc

    import omnigent.inner.pi_executor as pi_mod

    original_spawn = pi_mod._create_subprocess_exec
    pi_mod._create_subprocess_exec = _fake_spawn  # type: ignore[assignment]
    try:
        executor = PiExecutor(pi_path="/usr/bin/pi")
        try:
            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hello"}], [], "sys-prompt"
                )
            ]
        finally:
            await executor.close()
    finally:
        pi_mod._create_subprocess_exec = original_spawn  # type: ignore[assignment]

    # Sanity: the real turn completed through the stubbed subprocess.
    turn_complete = [e for e in events if isinstance(e, TurnComplete)]
    assert len(turn_complete) == 1
    assert turn_complete[0].response == "hi"

    # Reconstruct the JSONL commands the REAL code wrote to Pi's stdin.
    written = b"".join(captured["proc"].stdin.data).decode()
    commands = [json.loads(line) for line in written.splitlines() if line.strip()]
    prompts = [c for c in commands if c.get("type") == "prompt"]
    assert prompts, "expected the real run_turn path to emit a prompt command"

    cmd = prompts[0]
    assert cmd.get("streamingBehavior") == "followUp", (
        "RPC prompt command must set streamingBehavior='followUp' so a residual "
        "race against a still-alive Pi process is queued instead of surfacing "
        "Pi's raw 'Agent is already processing' error; got "
        f"{cmd!r} (facet 2)."
    )
