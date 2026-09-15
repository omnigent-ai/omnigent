"""Fault injection at the runner-to-harness tool-result boundary."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.responses import StreamingResponse

from omnigent.runner import create_runner_app
from omnigent.runner.tool_dispatch import dispatch_tool_locally
from omnigent.runtime.harnesses._scaffold import (
    HarnessApp,
    MessageEvent,
    ToolResultEvent,
    TurnContext,
)
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager
from omnigent.server.schemas import CreateResponseRequest
from tests.runner.conftest import _FakeProcessManager, _runner_client
from tests.runner.helpers import NullServerClient

pytestmark = pytest.mark.asyncio


@pytest.fixture
def execute(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    tool = AsyncMock(return_value="shell finished successfully")
    monkeypatch.setattr("omnigent.runner.tool_dispatch.execute_tool", tool)
    return tool


async def _dispatch(client: httpx.AsyncClient, **kwargs: Any) -> str:
    return await dispatch_tool_locally(
        tool_name="sys_os_shell",
        call_id="call_shell",
        arguments='{"command":"echo hello"}',
        response_id="resp_tool",
        conversation_id="conv_delivery",
        harness_client=client,
        **kwargs,
    )


@pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
async def test_delivery_http_errors_raise(status: int, execute: AsyncMock) -> None:
    attempts: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.content)
        return httpx.Response(status)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://harness.local"
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await _dispatch(client)

    assert len(attempts) == (2 if status >= 500 else 1)
    assert len(set(attempts)) == 1
    execute.assert_awaited_once()


async def test_delivery_cancellation_does_not_retry_or_recover(execute: AsyncMock) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise asyncio.CancelledError

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://harness.local"
    ) as client:
        with pytest.raises(asyncio.CancelledError):
            await _dispatch(client)

    assert attempts == 1
    execute.assert_awaited_once()


async def test_dead_channel_without_recovery_callback_raises(execute: AsyncMock) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.RemoteProtocolError("Server disconnected without sending a response")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://harness.local"
    ) as client:
        with pytest.raises(httpx.RemoteProtocolError):
            await _dispatch(client)

    assert attempts == 2
    execute.assert_awaited_once()


class _ToolHarness(HarnessApp):
    def __init__(self) -> None:
        super().__init__()
        self.outputs: list[str] = []
        self.finished = asyncio.Event()

    async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        try:
            output = await ctx.dispatch_tool(
                call_id="call_shell",
                name="sys_os_shell",
                arguments='{"command":"echo hello"}',
                agent="agent",
            )
            self.outputs.append(output)
        finally:
            self.finished.set()


class _HarnessStream(httpx.AsyncByteStream):
    """Keep scaffold SSE streaming; ASGITransport buffers it until turn end."""

    def __init__(self, response: StreamingResponse) -> None:
        self.response = response

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.response.body_iterator:
            yield chunk.encode() if isinstance(chunk, str) else bytes(chunk)

    async def aclose(self) -> None:
        await self.response.body_iterator.aclose()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "stream,failures,accept_before_drop,superseded,dead_interrupt",
    [
        (stream, failures, ack, superseded, False)
        for stream in (True, False)
        for failures, ack, superseded in [
            (0, False, False),
            (1, False, False),
            (1, True, False),
            (2, False, False),
            (2, False, True),
        ]
    ]
    + [(False, 2, False, False, True)],
)
async def test_proxy_recovers_tool_delivery(
    stream: bool,
    failures: int,
    accept_before_drop: bool,
    superseded: bool,
    dead_interrupt: bool,
    execute: AsyncMock,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise real dispatch, parked harness futures, and runner recovery together."""
    conv = f"conv_delivery_{stream}_{failures}_{accept_before_drop}_{superseded}_{dead_interrupt}"
    # Capture expected teardown errors without rich traceback rendering,
    # which can consume the recovery deadline on loaded CI workers.
    logger = logging.getLogger("omnigent.runner.app")
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    monkeypatch.setattr(logger, "propagate", False)
    caplog.set_level(logging.DEBUG, logger=logger.name)
    harness = _ToolHarness()
    deliveries: list[dict[str, Any]] = []
    interrupts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["type"] == "message":
            response = await harness._start_or_inject_turn(
                MessageEvent.model_validate(body).to_create_request(), session_id=conv
            )
            assert isinstance(response, StreamingResponse)
            return httpx.Response(200, stream=_HarnessStream(response))
        if body["type"] == "interrupt":
            interrupts.append(request.url.path)
            if dead_interrupt:
                raise httpx.RemoteProtocolError("harness channel still disconnected")
            response = await harness._handle_interrupt_event()
            return httpx.Response(response.status_code)
        assert body["type"] == "tool_result"
        deliveries.append(body)
        if accept_before_drop or len(deliveries) > failures:
            await harness._handle_tool_result_event(ToolResultEvent.model_validate(body))
        if superseded and len(deliveries) == failures:
            app.state.live_response_id[conv] = "resp_new"
            app.state.active_turns[conv] = None
            await harness._handle_interrupt_event()
        if len(deliveries) <= failures:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response")
        return httpx.Response(204)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://harness.local"
    ) as client:
        pm = _FakeProcessManager(client)  # type: ignore[arg-type]
        app = create_runner_app(
            process_manager=cast(HarnessProcessManager, pm),
            server_client=NullServerClient(),  # type: ignore[arg-type]
        )
        async with _runner_client(app) as http, asyncio.timeout(5):
            response = await http.post(
                f"/v1/sessions/{conv}/events?stream={str(stream).lower()}",
                json={
                    "type": "message",
                    "role": "user",
                    "harness": "claude-sdk",
                    "agent_id": "ag_tool",
                    "model": "agent",
                    "content": "run the tool",
                },
            )
            assert response.status_code == (200 if stream else 202), response.text
            await harness.finished.wait()
            while harness._in_flight:
                await asyncio.sleep(0)
            if superseded:
                while "superseded by resp_new" not in caplog.text:
                    await asyncio.sleep(0)
            while not superseded and conv in app.state.active_turns:
                await asyncio.sleep(0)
            queue = app.state.session_event_queues.get(conv)
            events = []
            assert queue is not None
            if failures == 2 and not superseded:
                while True:
                    event = await queue.get()
                    events.append(event)
                    if event.get("type") == "session.status" and event.get("status") == "failed":
                        break
            while not queue.empty():
                events.append(queue.get_nowait())

    execute.assert_awaited_once()
    assert len(deliveries) == min(failures + 1, 2)
    assert all(item == deliveries[0] for item in deliveries)
    failed = [
        event
        for event in events
        if event.get("type") == "session.status" and event.get("status") == "failed"
    ]
    if superseded:
        assert harness.outputs == []
        assert interrupts == []
        assert failed == []
        assert conv in app.state.active_turns
        assert app.state.live_response_id[conv] == "resp_new"
    elif failures == 2:
        assert harness.outputs == []
        assert interrupts == [f"/v1/sessions/{conv}/events"]
        assert len(failed) == 1
        assert failed[0]["error"]["code"] == "runner_turn_context_desync"
    else:
        assert harness.outputs == ["shell finished successfully"]
        assert interrupts == []
        assert failed == []
    assert not harness._in_flight
    if not superseded:
        assert conv not in app.state.live_response_id
