"""ACP response grace through the adapter's interrupt and abandoned-turn reap."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.inner import acp_executor as acp
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter, _executor_reap_timeout
from omnigent.runtime.harnesses._scaffold import TurnContext
from omnigent.server.schemas import CreateResponseRequest


@pytest.mark.parametrize(
    ("answer", "exit_on_term"),
    [("immediate", True), ("delayed", True), ("missing", True), ("missing", False)],
)
async def test_cancel_keeps_prompt_reader_alive_until_response_or_deadline(
    answer: str,
    exit_on_term: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Use real pending IDs/reader/run_turn/reap; stub only the subprocess boundary."""
    loop = asyncio.get_running_loop()
    grace = acp.ACP_CANCEL_GRACE_SECONDS
    events: list[tuple[str, float]] = []
    prompt_ready = asyncio.Event()
    exited = asyncio.Event()
    stdout = asyncio.StreamReader()
    process = Mock(spec=asyncio.subprocess.Process, returncode=None, stdout=stdout)
    process.stderr = asyncio.StreamReader()
    process.stdin = Mock()
    process.stdin.drain = AsyncMock()
    process.wait = exited.wait
    prompt_id: int | None = None
    cancel_at: float | None = None
    response_timer: asyncio.TimerHandle | None = None

    def record(name: str) -> None:
        events.append((name, loop.time()))

    def respond(request_id: int, result: dict) -> None:
        stdout.feed_data(json.dumps({"id": request_id, "result": result}).encode() + b"\n")

    def answer_cancel() -> None:
        assert prompt_id is not None
        record("response_written")
        respond(prompt_id, {"stopReason": "cancelled"})

    def write(data: bytes) -> None:
        nonlocal prompt_id, cancel_at, response_timer
        message = json.loads(data)
        method = message["method"]
        if method == "initialize":
            respond(message["id"], {"protocolVersion": 1, "agentCapabilities": {}})
        elif method == "session/new":
            respond(message["id"], {"sessionId": "stub"})
        elif method == "session/prompt":
            prompt_id = message["id"]
            prompt_ready.set()
        elif method == "session/cancel" and cancel_at is None:
            cancel_at = loop.time()
            record("cancel")
            if answer == "immediate":
                answer_cancel()
            elif answer == "delayed":
                response_timer = loop.call_later(grace / 2, answer_cancel)

    def close_stdin() -> None:
        record("stdin_eof")
        if answer != "missing":
            assert f"session/prompt {prompt_id} stopReason=cancelled" in caplog.text

    def terminate(target: object) -> None:
        assert target is process
        record("terminate")
        if exit_on_term:
            exited.set()

    def kill(target: object) -> None:
        assert target is process
        record("kill")
        exited.set()

    process.stdin.write.side_effect = write
    process.stdin.close.side_effect = close_stdin
    # The existing ACP primitive always completes prompts; cancellation requires
    # a caller-controlled response and signal/EOF observations at this boundary.
    monkeypatch.setattr(
        acp,
        "asyncio",
        SimpleNamespace(
            **(vars(asyncio) | {"create_subprocess_exec": AsyncMock(return_value=process)})
        ),
    )
    monkeypatch.setattr(acp._proc, "terminate_tree", terminate)
    monkeypatch.setattr(acp._proc, "kill_tree", kill)
    executor = acp.AcpExecutor(
        acp.AcpAgentConfig(command="stub-agent", name="stub", omnigent_mcp=False)
    )
    adapter = ExecutorAdapter(lambda: executor, session_key="stub")
    ctx = TurnContext(response_id="reply", event_queue=asyncio.Queue(), cancelled=asyncio.Event())
    adapter._in_flight[ctx.response_id] = ctx
    task = asyncio.create_task(
        adapter.run_turn(CreateResponseRequest(model="agent", input="hi"), ctx)
    )

    with caplog.at_level(logging.DEBUG, logger=acp.__name__):
        try:
            await asyncio.wait_for(prompt_ready.wait(), timeout=2)
            # The HTTP interrupt path returns while the delayed response is outstanding.
            reply = await asyncio.wait_for(adapter._handle_interrupt_event(), timeout=grace / 4)
            assert reply.status_code == 204
            assert not any(name == "terminate" for name, _ in events)
            if answer == "delayed":
                assert not any(name == "response_written" for name, _ in events)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(adapter.on_shutdown(), timeout=acp.ACP_REAP_TIMEOUT_SECONDS + 1)
        finally:
            if response_timer is not None:
                response_timer.cancel()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await adapter.on_shutdown()

    assert cancel_at is not None
    names = [name for name, _ in events]
    terminated_at = dict(events)["terminate"]
    if answer == "missing":
        assert "response_written" not in names
        assert grace - 0.01 <= terminated_at - cancel_at < grace + 0.5
        assert "stopReason=cancelled" not in caplog.text
    else:
        assert (
            names.index("response_written") < names.index("stdin_eof") < names.index("terminate")
        )
        response_at = dict(events)["response_written"]
        assert terminated_at - response_at < grace / 2
    assert ("kill" in names) == (not exit_on_term)
    assert loop.time() - cancel_at < grace + acp._ACP_TERMINATION_TIMEOUT_SECONDS + 0.5
    assert not any("teardown failed" in record.message for record in caplog.records)
    assert executor._reader_task is None
    assert executor._proc is None


def test_reap_budget_is_acp_specific() -> None:
    from omnigent.inner.executor import MockExecutor
    from omnigent.runtime.harnesses._executor_adapter import INTERRUPT_TIMEOUT_S

    executor = acp.AcpExecutor(acp.AcpAgentConfig(command="stub-agent"))
    assert _executor_reap_timeout(executor) > (
        acp.ACP_CANCEL_GRACE_SECONDS + acp._ACP_TERMINATION_TIMEOUT_SECONDS
    )
    assert _executor_reap_timeout(MockExecutor()) == INTERRUPT_TIMEOUT_S
