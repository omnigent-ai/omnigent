"""ACP response grace through the adapter's interrupt and abandoned-turn reap."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.db.utils import generate_task_id
from omnigent.inner import acp_executor as acp
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.runtime.harnesses._scaffold import TurnContext
from omnigent.server.schemas import CreateResponseRequest

_AnswerMode = Literal["immediate", "delayed", "missing", "error"]
_TIMING_SLACK_SECONDS = 0.5


@dataclass
class _CancelScenario:
    events: list[tuple[str, float]]
    cancel_at: float
    logs_at_stdin: str
    prompt_id: int


@pytest.fixture
def cancel_scenario(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> Callable[[_AnswerMode, bool], Awaitable[_CancelScenario]]:
    """Drive the real adapter and ACP reader with a controlled subprocess boundary."""

    async def run(answer: _AnswerMode, exit_on_term: bool) -> _CancelScenario:
        loop = asyncio.get_running_loop()
        grace = acp.ACP_CANCEL_GRACE_SECONDS
        events: list[tuple[str, float]] = []
        logs_at_stdin: list[str] = []
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
            if answer == "error":
                stdout.feed_data(
                    json.dumps(
                        {"id": prompt_id, "error": {"code": -32603, "message": "cancel failed"}}
                    ).encode()
                    + b"\n"
                )
            else:
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
                if answer in {"immediate", "error"}:
                    answer_cancel()
                elif answer == "delayed":
                    response_timer = loop.call_later(grace / 2, answer_cancel)

        def close_stdin() -> None:
            record("stdin_eof")
            logs_at_stdin.append(caplog.text)

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
        ctx = TurnContext(
            response_id=generate_task_id(), event_queue=asyncio.Queue(), cancelled=asyncio.Event()
        )
        adapter._in_flight[ctx.response_id] = ctx
        task = asyncio.create_task(
            adapter.run_turn(CreateResponseRequest(model="agent", input="hi"), ctx)
        )

        with caplog.at_level(logging.DEBUG, logger=acp.__name__):
            try:
                await asyncio.wait_for(prompt_ready.wait(), timeout=2)
                # The HTTP interrupt path returns while the delayed response is outstanding.
                reply = await asyncio.wait_for(
                    adapter._handle_interrupt_event(), timeout=grace / 4
                )
                assert reply.status_code == 204
                assert not any(name == "terminate" for name, _ in events)
                if answer == "delayed":
                    assert not any(name == "response_written" for name, _ in events)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                await asyncio.wait_for(
                    adapter.on_shutdown(), timeout=acp.ACP_REAP_TIMEOUT_SECONDS + 1
                )
            finally:
                if response_timer is not None:
                    response_timer.cancel()
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                await adapter.on_shutdown()

        assert cancel_at is not None
        assert prompt_id is not None
        names = [name for name, _ in events]
        assert ("kill" in names) == (not exit_on_term)
        assert loop.time() - cancel_at < (
            grace + acp._ACP_TERMINATION_TIMEOUT_SECONDS + _TIMING_SLACK_SECONDS
        )
        assert not any("teardown failed" in record.message for record in caplog.records)
        assert executor._reader_task is None
        assert executor._proc is None
        return _CancelScenario(events, cancel_at, logs_at_stdin[0], prompt_id)

    return run


@pytest.mark.parametrize("answer", ["immediate", "delayed"])
async def test_cancel_reads_response_before_teardown(
    answer: _AnswerMode,
    cancel_scenario: Callable[[_AnswerMode, bool], Awaitable[_CancelScenario]],
) -> None:
    scenario = await cancel_scenario(answer, True)
    names = [name for name, _ in scenario.events]
    assert names.index("response_written") < names.index("stdin_eof") < names.index("terminate")
    times = dict(scenario.events)
    assert times["terminate"] - times["response_written"] < acp.ACP_CANCEL_GRACE_SECONDS / 2
    assert f"session/prompt {scenario.prompt_id} stopReason=cancelled" in scenario.logs_at_stdin
    assert "cancel grace expired" not in scenario.logs_at_stdin


@pytest.mark.parametrize("exit_on_term", [True, False])
async def test_cancel_reaps_after_response_grace_expires(
    exit_on_term: bool,
    cancel_scenario: Callable[[_AnswerMode, bool], Awaitable[_CancelScenario]],
) -> None:
    scenario = await cancel_scenario("missing", exit_on_term)
    times = dict(scenario.events)
    assert "response_written" not in times
    grace = acp.ACP_CANCEL_GRACE_SECONDS
    assert grace - 0.01 <= times["terminate"] - scenario.cancel_at < grace + _TIMING_SLACK_SECONDS
    assert "stopReason=cancelled" not in scenario.logs_at_stdin
    assert (
        f"session/prompt {scenario.prompt_id} cancel grace expired without a response"
        in scenario.logs_at_stdin
    )


async def test_cancel_logs_prompt_error_before_teardown(
    cancel_scenario: Callable[[_AnswerMode, bool], Awaitable[_CancelScenario]],
) -> None:
    scenario = await cancel_scenario("error", True)
    assert (
        f"session/prompt {scenario.prompt_id} error code=-32603 message=cancel failed"
        in scenario.logs_at_stdin
    )
    assert "stopReason=None" not in scenario.logs_at_stdin
    assert "cancel grace expired" not in scenario.logs_at_stdin


def test_reap_budget_is_executor_owned() -> None:
    from omnigent.inner.executor import MockExecutor

    executor = acp.AcpExecutor(acp.AcpAgentConfig(command="stub-agent"))
    assert executor.close_timeout() > (
        acp.ACP_CANCEL_GRACE_SECONDS + acp._ACP_TERMINATION_TIMEOUT_SECONDS
    )
    assert MockExecutor().close_timeout() == 3.0
