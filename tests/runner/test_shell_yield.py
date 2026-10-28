"""Slow shells keep one execution and retain result-policy and cancellation gates."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from omnigent.runner.shell_yield import execute_shell_with_yield
from omnigent.runner.tool_dispatch import _drain_inbox, execute_tool

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    ("verdict", "expected", "fails"),
    [
        ({"result": "POLICY_ACTION_ALLOW"}, "private output", False),
        ({"result": "POLICY_ACTION_ALLOW", "data": "redacted"}, "redacted", False),
        (
            {"result": "POLICY_ACTION_DENY", "reason": "blocked"},
            "suppressed by policy: blocked",
            False,
        ),
        (
            {"result": "POLICY_ACTION_DENY", "reason": "blocked"},
            "suppressed by policy: blocked",
            True,
        ),
    ],
)
async def test_slow_shell_runs_once_and_checks_its_final_output(
    verdict: dict[str, Any], expected: str, fails: bool
) -> None:
    release = asyncio.Event()
    starts = 0
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}
    arguments = {"command": "build", "timeout": 120}
    full_output = "private output\n" + "x" * 12000 + "\nlast failure line"

    async def run() -> str:
        nonlocal starts
        starts += 1
        await release.wait()
        if fails:
            raise RuntimeError(full_output)
        return full_output

    output, deferred = await asyncio.wait_for(
        execute_shell_with_yield(
            run(), after=0.001, arguments=arguments, inbox=inbox, tasks=tasks
        ),
        timeout=1,
    )
    handle = json.loads(output)
    assert deferred and handle["status"] == "in_progress"
    assert inbox.empty()
    collection = tasks[handle["handle_id"]][0]
    release.set()
    await collection
    assert starts == 1 and tasks == {}

    requests = []

    def policy(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=verdict)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(policy), base_url="http://server"
    ) as client:
        delivered = await _drain_inbox(inbox, server_client=client, conversation_id="session")
    assert expected in delivered
    assert ("failed" in delivered) is fails
    if expected == "private output":
        assert full_output in delivered
    else:
        assert "private output" not in delivered
    assert len(requests) == 1
    event = requests[0]["event"]
    assert event["type"] == "PHASE_TOOL_RESULT"
    assert event["data"]["result"] == full_output
    assert event["request_data"] == {"name": "sys_os_shell", "arguments": arguments}
    assert inbox.empty()


async def test_quick_shell_keeps_its_foreground_result() -> None:
    async def run() -> str:
        return "done"

    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}
    result = await execute_shell_with_yield(run(), after=1, arguments={}, inbox=inbox, tasks=tasks)
    assert result == ("done", False)
    assert tasks == {} and inbox.empty()


@pytest.mark.parametrize("completes", [False, True])
async def test_yielded_shell_cancellation_handles_completion_race(completes: bool) -> None:
    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def run() -> str:
        try:
            await release.wait()
            return "completed before cancellation"
        finally:
            cancelled.set()

    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}
    output, _ = await execute_shell_with_yield(
        run(), after=0.001, arguments={}, inbox=inbox, tasks=tasks
    )
    handle_id = json.loads(output)["handle_id"]
    collection = tasks[handle_id][0]
    if completes:
        release.set()
    result = await execute_tool(
        tool_name="sys_cancel_async",
        arguments=json.dumps({"handle_id": handle_id}),
        session_async_tasks=tasks,
    )
    assert "cancel" in result
    await asyncio.wait_for(collection, timeout=1)
    assert cancelled.is_set() and tasks == {}
    assert inbox.get_nowait()["status"] == ("completed" if completes else "cancelled")
    assert inbox.empty()


async def test_caller_cancellation_before_yield_cleans_up_execution() -> None:
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def run() -> str:
        started.set()
        try:
            await asyncio.Event().wait()
            return "unexpected completion"
        finally:
            cleaned.set()

    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}
    caller = asyncio.create_task(
        execute_shell_with_yield(
            run(),
            after=60,
            arguments={},
            inbox=inbox,
            tasks=tasks,
        )
    )
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert cleaned.is_set() and tasks == {} and inbox.empty()


async def test_policy_outage_keeps_delayed_shell_output_private_and_retryable() -> None:
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    inbox.put_nowait(
        {
            "type": "tool_result",
            "tool_name": "sys_os_shell",
            "status": "completed",
            "handle_id": "shell-1",
            "output": "private output",
            "request_data": {"name": "sys_os_shell", "arguments": {"command": "build"}},
        }
    )
    responses = iter(
        [
            httpx.Response(503),
            httpx.Response(200, json={"result": "POLICY_ACTION_ALLOW", "data": "redacted"}),
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: next(responses)),
        base_url="http://server",
    ) as client:
        first = await _drain_inbox(inbox, server_client=client, conversation_id="session")
        assert "policy evaluation failed" in first and "private output" not in first
        assert inbox.qsize() == 1
        second = await _drain_inbox(inbox, server_client=client, conversation_id="session")
    assert "redacted" in second and "private output" not in second
    assert inbox.empty()
