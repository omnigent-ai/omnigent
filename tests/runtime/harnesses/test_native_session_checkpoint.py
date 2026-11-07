"""Acknowledged native-session persistence without a new server endpoint."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from omnigent.inner.executor import MockExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.runtime.harnesses._scaffold import MessageEvent, TurnContext
from omnigent.runtime.harnesses.native_session import (
    NativeSessionCheckpointAck,
    NativeSessionCheckpointRequest,
    checkpoint_native_session,
    read_native_session_reference,
)
from omnigent.server.schemas import CreateResponseRequest


def _context(reference: str | None = None, *, present: bool = False) -> TurnContext:
    return TurnContext(
        "response-1",
        asyncio.Queue(),
        asyncio.Event(),
        session_id="conversation-1",
        native_session_id=reference,
        native_session_reference_present=present,
    )


@pytest.mark.asyncio
async def test_checkpoint_parks_until_matching_successful_acknowledgement() -> None:
    ctx = _context()
    task = asyncio.create_task(ctx.checkpoint_native_session("native-1"))
    request = await asyncio.wait_for(ctx._event_queue.get(), 1)
    assert isinstance(request, NativeSessionCheckpointRequest)
    assert not task.done()
    assert not ctx._complete_native_checkpoint(
        NativeSessionCheckpointAck(checkpoint_id="wrong-checkpoint", success=True)
    )
    assert ctx._complete_native_checkpoint(
        NativeSessionCheckpointAck(checkpoint_id=request.checkpoint_id, success=True)
    )
    await task
    assert ctx._pending_native_checkpoints == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_or_cancelled_checkpoint_never_continues(cancel: bool) -> None:
    ctx = _context()
    task = asyncio.create_task(ctx.checkpoint_native_session("native-1"))
    request = await ctx._event_queue.get()
    assert isinstance(request, NativeSessionCheckpointRequest)
    if cancel:
        ctx.cancelled.set()
        ctx._cancel_pending()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        ctx._complete_native_checkpoint(
            NativeSessionCheckpointAck(
                checkpoint_id=request.checkpoint_id, success=False, error="checkpoint rejected"
            )
        )
        with pytest.raises(RuntimeError, match="checkpoint rejected"):
            await task
    assert ctx._pending_native_checkpoints == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 403, 409, 503])
async def test_existing_server_patch_is_acknowledged_only_after_response(status: int) -> None:
    order: list[str] = []
    acknowledgements: list[dict] = []

    async def server(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert request.url.path == "/v1/sessions/conversation-1"
        assert request.content == b'{"external_session_id":"native-1"}'
        order.append("persist")
        return httpx.Response(
            status,
            json={"external_session_id": "native-1"}
            if status == 200
            else {"error": "private error body"},
        )

    async def harness(request: httpx.Request) -> httpx.Response:
        import json

        assert request.method == "POST"
        assert request.url.path == "/v1/sessions/conversation-1/events"
        order.append("acknowledge")
        acknowledgements.append(json.loads(request.content))
        return httpx.Response(204)

    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(server), base_url="https://server.example.invalid"
        ) as server_client,
        httpx.AsyncClient(
            transport=httpx.MockTransport(harness), base_url="http://harness"
        ) as harness_client,
    ):
        success = await checkpoint_native_session(
            server_client,
            harness_client,
            "conversation-1",
            NativeSessionCheckpointRequest(
                checkpoint_id="checkpoint-1", native_session_id="native-1"
            ),
        )
    assert success == (status == 200)
    assert order == ["persist", "acknowledge"]
    assert acknowledgements[0]["success"] == (status == 200)
    assert "private error body" not in str(acknowledgements)


@pytest.mark.asyncio
async def test_cancellation_during_write_does_not_acknowledge_success() -> None:
    entered = asyncio.Event()
    acknowledgements: list[httpx.Request] = []

    async def server(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        return httpx.Response(200)

    async def harness(request: httpx.Request) -> httpx.Response:
        acknowledgements.append(request)
        return httpx.Response(204)

    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(server), base_url="https://server.example.invalid"
        ) as server_client,
        httpx.AsyncClient(
            transport=httpx.MockTransport(harness), base_url="http://harness"
        ) as harness_client,
    ):
        task = asyncio.create_task(
            checkpoint_native_session(
                server_client,
                harness_client,
                "conversation-1",
                NativeSessionCheckpointRequest(
                    checkpoint_id="checkpoint-1", native_session_id="native-1"
                ),
            )
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert acknowledgements == []


@pytest.mark.asyncio
async def test_native_reference_is_fresh_after_a_committed_but_unacknowledged_write() -> None:
    references = [None, "native-1", None]

    async def server(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.params["include_items"] == "false"
        return httpx.Response(200, json={"external_session_id": references.pop(0)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(server), base_url="https://server.example.invalid"
    ) as client:
        assert await read_native_session_reference(client, "conversation-1") is None
        assert await read_native_session_reference(client, "conversation-1") == "native-1"
        assert await read_native_session_reference(client, "conversation-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot", [None, [], {"external_session_id": 7}])
async def test_malformed_session_metadata_fails_closed(snapshot: object) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=snapshot)),
        base_url="https://server.example.invalid",
    ) as client:
        with pytest.raises(ValueError, match="Invalid native session"):
            await read_native_session_reference(client, "conversation-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot", [None, [], {}, {"external_session_id": "another-session"}])
async def test_checkpoint_requires_confirmation_of_persisted_reference(snapshot: object) -> None:
    acknowledgements: list[dict] = []

    async def harness(request: httpx.Request) -> httpx.Response:
        import json

        acknowledgements.append(json.loads(request.content))
        return httpx.Response(204)

    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=snapshot)),
            base_url="https://server.example.invalid",
        ) as server_client,
        httpx.AsyncClient(
            transport=httpx.MockTransport(harness), base_url="http://harness"
        ) as harness_client,
    ):
        assert not await checkpoint_native_session(
            server_client,
            harness_client,
            "conversation-1",
            NativeSessionCheckpointRequest(
                checkpoint_id="checkpoint-1", native_session_id="native-1"
            ),
        )
    assert acknowledgements[0]["success"] is False


@pytest.mark.asyncio
async def test_authoritative_reference_change_closes_previous_executor() -> None:
    class RecordingExecutor(MockExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.reference: str | None = None
            self.closed = False
            self.enqueue_response("reply")

        def configure_native_session(self, session_id: str | None, checkpoint: object) -> None:
            self.reference = session_id

        async def close(self) -> None:
            self.closed = True

    executors: list[RecordingExecutor] = []

    def factory() -> RecordingExecutor:
        executor = RecordingExecutor()
        executors.append(executor)
        return executor

    adapter = ExecutorAdapter(factory)
    request = CreateResponseRequest(model="example-agent", input="question")
    await adapter.run_turn(request, _context("native-1", present=True))
    assert executors[0].reference == "native-1"
    await adapter.run_turn(request, _context(None, present=True))
    assert executors[0].closed
    assert executors[1].reference is None
    await adapter.on_shutdown()


@pytest.mark.asyncio
async def test_failed_teardown_does_not_admit_a_replacement_on_repeated_turns() -> None:
    from unittest.mock import AsyncMock

    executor = MockExecutor()
    executor.enqueue_response("reply")
    adapter = ExecutorAdapter(lambda: executor)
    request = CreateResponseRequest(model="example-agent", input="question")
    await adapter.run_turn(request, _context("native-1", present=True))
    adapter._safe_interrupt = AsyncMock(return_value=False)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="Could not close the previous native session"):
            await adapter.run_turn(request, _context(None, present=True))
        assert adapter._executor is executor
        assert adapter._native_session_id == "native-1"
    assert adapter._safe_interrupt.await_count == 2
    await adapter.on_shutdown()


@pytest.mark.asyncio
async def test_old_runner_message_does_not_enroll_private_checkpoint_protocol() -> None:
    from unittest.mock import Mock

    executor = MockExecutor()
    executor.enqueue_response("reply")
    executor.configure_native_session = Mock()
    adapter = ExecutorAdapter(lambda: executor)
    await adapter.run_turn(
        CreateResponseRequest(model="example-agent", input="question"), _context()
    )
    executor.configure_native_session.assert_not_called()
    executor.enqueue_response("follow up")
    await adapter.run_turn(
        CreateResponseRequest(model="example-agent", input="follow up"),
        _context(None, present=True),
    )
    assert adapter._executor is executor
    executor.configure_native_session.assert_called_once()
    await adapter.on_shutdown()


def test_native_reference_is_internal_to_harness_context() -> None:
    message = MessageEvent(
        type="message",
        role="user",
        model="example-agent",
        content="question",
        native_session_id="native-1",
    )
    assert "native_session_id" not in message.to_create_request().model_dump()
