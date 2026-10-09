"""Runner-generated failures remain valid on direct and queued streams."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import TypeAdapter

from omnigent.runner import create_runner_app
from omnigent.runner.app import _is_context_overflow_error
from omnigent.server.schemas import FailedEvent, ServerStreamEvent
from omnigent.spec.types import AgentSpec
from tests.runner.conftest import (
    _drain_session_event_queue,
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient

_SESSION_ID = "11111111111111111111111111111111"
_EVENT_ADAPTER = TypeAdapter(ServerStreamEvent)


async def _assert_failure_on_both_streams(
    app: FastAPI, *, source: str, **message_fields: object
) -> dict[str, Any]:
    """Send a synthetic turn and validate its failure through the server schema."""
    async with _runner_client(app) as client:
        response = await client.post(
            f"/v1/sessions/{_SESSION_ID}/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "hello"}],
                "harness": "openai-agents",
                **message_fields,
            },
        )
        queued = _drain_session_event_queue(app.state.session_event_queues.get(_SESSION_ID))

    assert response.status_code == 200
    direct = [
        json.loads(line.removeprefix("data:"))
        for line in response.text.splitlines()
        if line.startswith("data:")
    ]
    failures = []
    for events in (direct, queued):
        failed = [event for event in events if event["type"] == "response.failed"]
        assert len(failed) == 1
        event = _EVENT_ADAPTER.validate_python(failed[0])
        assert isinstance(event, FailedEvent)
        assert event.source == source
        assert event.response.status == "failed"
        assert failed[0]["response"]["error"] == failed[0]["error"]
        failures.append(failed[0])
    assert failures[0] == failures[1]
    return failures[0]["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [204, 400, 503])
async def test_harness_rejection_has_a_valid_queued_failure(status_code: int) -> None:
    class RejectedHarnessClient(_ScriptedHarnessClient):
        @contextlib.asynccontextmanager
        async def stream(
            self, method: str, url: str, *, json: dict[str, Any], timeout: Any
        ) -> AsyncIterator[httpx.Response]:
            yield httpx.Response(status_code)

    app = create_runner_app(
        process_manager=_FakeProcessManager(RejectedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    error = await _assert_failure_on_both_streams(app, source="harness")
    assert error["status"] == status_code
    assert error["code"] == "runner_error"
    assert error["message"] == f"turn failed (status {status_code})"


@pytest.mark.asyncio
@pytest.mark.parametrize("eager", [True, False], ids=["eager", "lazy"])
async def test_spec_resolution_error_has_a_valid_queued_failure(eager: bool) -> None:
    async def fail_resolution(agent_id: str, session_id: str | None = None) -> AgentSpec:
        raise RuntimeError("synthetic spec resolution failure")

    harness = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_test"}}),
            _sse(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "status": "action_required",
                        "name": "sys_os_read",
                        "call_id": "call_test",
                        "arguments": "{}",
                    },
                }
            ),
        ]
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        spec_resolver=fail_resolution,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    error = await _assert_failure_on_both_streams(
        app,
        source="execution",
        agent_id="22222222222222222222222222222222",
        has_mcp_servers=eager,
    )
    assert error == {
        "code": "RuntimeError",
        "message": "Failed to resolve the agent spec for this turn.",
        "type": "RuntimeError",
    }


@pytest.mark.asyncio
async def test_connection_error_preserves_a_valid_queued_failure() -> None:
    class DisconnectedHarnessClient(_ScriptedHarnessClient):
        @contextlib.asynccontextmanager
        async def stream(
            self, method: str, url: str, *, json: dict[str, Any], timeout: Any
        ) -> AsyncIterator[httpx.Response]:
            raise httpx.ReadError("synthetic harness disconnect")
            yield  # pragma: no cover

    app = create_runner_app(
        process_manager=_FakeProcessManager(DisconnectedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    error = await _assert_failure_on_both_streams(app, source="harness")
    assert error["code"] == "connection_error"
    assert error["type"] == "ReadError"


@pytest.mark.asyncio
async def test_context_overflow_preserves_a_valid_queued_failure() -> None:
    harness = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_test"}}),
            _sse(
                {
                    "type": "response.failed",
                    "error": {
                        "code": "context_length_exceeded",
                        "message": "5000 tokens > 4096 maximum context length",
                    },
                }
            ),
        ]
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    error = await _assert_failure_on_both_streams(app, source="llm")
    assert error["code"] == "context_length_exceeded"
    assert error["type"] == "_ContextWindowOverflow"


@pytest.mark.asyncio
async def test_content_length_cap_failure_is_normalized_to_context_overflow() -> None:
    """A byte-cap rejection surfaced by the harness classifies as a context overflow."""
    harness = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_test"}}),
            _sse(
                {
                    "type": "response.failed",
                    "error": {
                        "code": "unknown_error",
                        "message": (
                            'LLM returned HTTP 400: {"error_code":"BAD_REQUEST",'
                            '"message":"Server received a request which exceeds '
                            "maximum allowed content length. "
                            'RequestSize(bytes): 33967957, Limit(bytes): 33554432"}'
                        ),
                    },
                }
            ),
        ]
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    error = await _assert_failure_on_both_streams(app, source="llm")
    assert error["code"] == "context_length_exceeded"
    assert error["type"] == "_ContextWindowOverflow"
    # The raw rejection — including its RequestSize/Limit bytes — survives
    # normalization rather than being replaced by a token-count approximation,
    # so the expandable detail still names the real cause.
    assert "exceeds maximum allowed content length" in error["message"]
    assert "RequestSize(bytes): 33967957" in error["message"]
    assert "Limit(bytes): 33554432" in error["message"]


def _overflow_event(message: str) -> dict[str, Any]:
    return {"type": "response.failed", "error": {"message": message}}


def test_byte_cap_rejection_classifies_as_overflow_without_inversion() -> None:
    """A byte-cap rejection reads actual > max despite reporting bytes."""
    message = (
        "Server received a request which exceeds maximum allowed content "
        "length. RequestSize(bytes): 33967957, Limit(bytes): 33554432"
    )
    overflow = _is_context_overflow_error(_overflow_event(message))
    assert overflow is not None
    max_tokens, actual_tokens, detail = overflow
    # Each byte count is divided by the 4-byte token estimate, so the request
    # reads above the limit rather than being inverted into actual < max.
    assert max_tokens == 33554432 // 4
    assert actual_tokens == 33967957 // 4
    assert actual_tokens > max_tokens
    # The complete raw rejection is carried through once for the error detail.
    assert detail == message


def test_content_length_phrase_without_sizes_is_not_overflow() -> None:
    """A size-less content-length phrase stays generic, like the native path."""
    assert (
        _is_context_overflow_error(
            _overflow_event("request exceeds maximum allowed content length")
        )
        is None
    )
