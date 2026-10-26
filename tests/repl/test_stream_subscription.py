"""Exercise REPL subscription ordering over the real SDK HTTP boundary."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from omnigent_client._sessions import SessionsNamespace

from omnigent.repl._repl import _SessionsChatReplAdapter
from omnigent.server.schemas import (
    ElicitationRequestEvent,
    ElicitationResolvedEvent,
    SessionHeartbeatEvent,
)


class _Stream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.headers_received = asyncio.Event()
        self.subscribe = asyncio.Event()
        self.events: asyncio.Queue[dict[str, Any] | Exception] = asyncio.Queue()
        self.close_before_ready = False
        self.first_event: dict[str, Any] = {"type": "session.heartbeat"}

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.headers_received.set()
        await self.subscribe.wait()
        if self.close_before_ready:
            return
        first = self.first_event
        yield f"event: {first['type']}\ndata: {json.dumps(first)}\n\n".encode()
        while True:
            event = await self.events.get()
            if isinstance(event, Exception):
                raise event
            yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


class _Server:
    def __init__(self) -> None:
        self.streams = [_Stream(), _Stream()]
        self.connections = 0
        self.posts: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []
        self.posted = asyncio.Event()
        self.approved = asyncio.Event()
        self.startup_error: Exception | None = None
        self.post_error: Exception | None = None

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stream"):
            if self.startup_error:
                raise self.startup_error
            stream = self.streams[self.connections]
            self.connections += 1
            return httpx.Response(200, stream=stream)
        if request.method in {"GET", "PATCH"}:
            if request.method == "PATCH":
                assert json.loads(request.content) == {"runner_id": ""}
            return httpx.Response(
                200,
                json={
                    "id": request.url.path.rsplit("/", 1)[-1],
                    "agent_id": "ag_test",
                    "status": "idle",
                    "created_at": 0,
                    "pending_elicitations": list(self.pending),
                },
            )
        assert request.url.path.endswith("/events")
        self.posts.append(json.loads(request.content))
        self.pending = [
            {
                "type": "response.elicitation_request",
                "elicitation_id": "elicit_test",
                "params": {"mode": "form", "message": "Approve synthetic input?"},
            }
        ]
        self.posted.set()
        if self.post_error:
            raise self.post_error
        await self.approved.wait()
        self.pending.clear()
        await self.streams[self.connections - 1].events.put(
            {
                "type": "session.status",
                "conversation_id": "conv_test",
                "status": "idle",
            }
        )
        return httpx.Response(200, json={"queued": False})


@contextlib.asynccontextmanager
async def _adapter(server: _Server) -> AsyncIterator[_SessionsChatReplAdapter]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as http:
        adapter = _SessionsChatReplAdapter(
            SimpleNamespace(sessions=SessionsNamespace(http, "http://test.invalid")),  # type: ignore[arg-type]
            "test",
            session_id="conv_test",
            attach_only=True,
        )
        try:
            yield adapter
        finally:
            await adapter.aclose()


async def _send(adapter: _SessionsChatReplAdapter, *, skill: bool = False) -> None:
    events = (
        adapter.send_skill_slash_command("review", "synthetic") if skill else adapter.send("hello")
    )
    async for _ in events:
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("skill", [False, True])
async def test_send_waits_for_stream_body_not_http_headers(skill: bool) -> None:
    server = _Server()
    async with _adapter(server) as adapter:
        task = asyncio.create_task(_send(adapter, skill=skill))
        try:
            await asyncio.wait_for(server.streams[0].headers_received.wait(), 2)
            assert server.posts == [], "HTTP 200 headers do not acknowledge subscription"
            server.streams[0].subscribe.set()
            await asyncio.wait_for(server.posted.wait(), 2)
            assert len(server.posts) == 1
            server.approved.set()
            await asyncio.wait_for(task, 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_stream_startup_does_not_post_input() -> None:
    server = _Server()
    server.startup_error = httpx.ConnectError("synthetic stream connection failure")
    async with _adapter(server) as adapter:
        with pytest.raises(httpx.ConnectError, match="synthetic stream"):
            await asyncio.wait_for(_send(adapter), 2)
        assert server.posts == []
        assert adapter._pending_local_user_sends == 0
        server.startup_error = None
        server.streams[0].subscribe.set()
        server.approved.set()
        await asyncio.wait_for(_send(adapter), 2)
        assert len(server.posts) == 1


@pytest.mark.asyncio
async def test_stream_closed_before_acknowledgment_does_not_post() -> None:
    server = _Server()
    server.streams[0].close_before_ready = True
    server.streams[0].subscribe.set()
    async with _adapter(server) as adapter:
        with pytest.raises(RuntimeError, match="closed before subscribing"):
            await asyncio.wait_for(_send(adapter), 2)
        assert server.posts == []


@pytest.mark.asyncio
async def test_silent_stream_startup_has_a_finite_send_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omnigent.repl._repl._STREAM_SUBSCRIBE_TIMEOUT_S", 0.01)
    server = _Server()
    async with _adapter(server) as adapter:
        with pytest.raises(TimeoutError, match="input was not sent"):
            await asyncio.wait_for(_send(adapter), 2)
        assert server.posts == []


@pytest.mark.asyncio
async def test_cancelled_send_does_not_cancel_shared_subscription() -> None:
    server = _Server()
    async with _adapter(server) as adapter:
        task = asyncio.create_task(_send(adapter))
        await asyncio.wait_for(server.streams[0].headers_received.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert server.posts == []
        assert adapter._pending_local_user_sends == 0
        server.streams[0].subscribe.set()
        server.approved.set()
        await asyncio.wait_for(_send(adapter), 2)
        assert len(server.posts) == 1


@pytest.mark.asyncio
async def test_close_wakes_a_send_waiting_for_subscription() -> None:
    server = _Server()
    async with _adapter(server) as adapter:
        task = asyncio.create_task(_send(adapter))
        await asyncio.wait_for(server.streams[0].headers_received.wait(), 2)
        await adapter.aclose()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert server.posts == []


@pytest.mark.asyncio
async def test_cancelling_an_acknowledged_stream_invalidates_readiness() -> None:
    server = _Server()
    server.streams[0].subscribe.set()
    async with _adapter(server) as adapter:
        await adapter._ensure_session()
        await asyncio.wait_for(adapter._wait_for_stream_subscription("conv_test"), 2)
        assert adapter._stream_task is not None
        adapter._stream_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await _send(adapter)
        assert server.posts == []


@pytest.mark.asyncio
async def test_reconnect_recovers_approval_while_original_post_is_parked() -> None:
    server = _Server()
    server.streams[0].subscribe.set()
    server.streams[1].subscribe.set()
    async with _adapter(server) as adapter:
        prompts: list[ElicitationRequestEvent] = []
        prompt_arrived = asyncio.Event()

        def on_event(event: object) -> None:
            if isinstance(event, ElicitationRequestEvent):
                prompts.append(event)
                prompt_arrived.set()

        adapter._on_event = on_event
        task = asyncio.create_task(_send(adapter))
        try:
            await asyncio.wait_for(server.posted.wait(), 2)
            assert not task.done()
            await server.streams[0].events.put(httpx.ReadError("synthetic disconnect"))
            await asyncio.wait_for(prompt_arrived.wait(), 2)
            assert len(server.posts) == 1
            assert not task.done(), "Snapshot recovery must not approve the pending input"
            # The same request can arrive live after snapshot reconciliation.
            await server.streams[1].events.put(server.pending[0])
            server.approved.set()
            await asyncio.wait_for(task, 2)
            assert [prompt.elicitation_id for prompt in prompts] == ["elicit_test"]
            assert len(server.posts) == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_uncertain_message_post_is_not_retried() -> None:
    server = _Server()
    server.streams[0].subscribe.set()
    server.post_error = httpx.ReadTimeout("synthetic response lost after write")
    async with _adapter(server) as adapter:
        with pytest.raises(httpx.ReadTimeout, match="response lost"):
            await asyncio.wait_for(_send(adapter), 2)
        assert len(server.posts) == 1


@pytest.mark.asyncio
async def test_switched_session_requires_its_own_subscription() -> None:
    server = _Server()
    server.streams[0].subscribe.set()
    async with _adapter(server) as adapter:
        await adapter._ensure_session()
        await asyncio.wait_for(adapter._wait_for_stream_subscription("conv_test"), 2)
        await adapter.view_session("conv_next", read_only=True, interactive=True)
        task = asyncio.create_task(_send(adapter))
        try:
            await asyncio.wait_for(server.streams[1].headers_received.wait(), 2)
            assert server.posts == []
            server.streams[1].subscribe.set()
            server.approved.set()
            await asyncio.wait_for(task, 2)
            assert len(server.posts) == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class _Observer:
    def __init__(self) -> None:
        self.events: list[object] = []
        self.heartbeat = asyncio.Event()

    def __call__(self, event: object) -> None:
        self.events.append(event)
        if isinstance(event, SessionHeartbeatEvent):
            self.heartbeat.set()

    @property
    def requests(self) -> list[ElicitationRequestEvent]:
        return [event for event in self.events if isinstance(event, ElicitationRequestEvent)]

    async def emit(self, stream: _Stream, *events: dict[str, Any]) -> None:
        """Wait for a trailing heartbeat to prove the preceding events were handled."""
        self.heartbeat.clear()
        for event in events:
            stream.events.put_nowait(event)
        stream.events.put_nowait({"type": "session.heartbeat"})
        await asyncio.wait_for(self.heartbeat.wait(), 2)


def _request() -> dict[str, Any]:
    return {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit_reused",
        "params": {"mode": "form", "message": "Approve synthetic input?"},
    }


def _resolved(action: str | None = None) -> dict[str, Any]:
    return {
        "type": "response.elicitation_resolved",
        "elicitation_id": "elicit_reused",
        "action": action,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [None, "accept"])
async def test_live_request_rearms_resolved_id_without_replaying_snapshot(
    action: str | None,
) -> None:
    server = _Server()
    server.streams[0].subscribe.set()
    async with _adapter(server) as adapter:
        observer = _Observer()
        adapter._on_event = observer
        await adapter._ensure_session()
        await asyncio.wait_for(adapter._wait_for_stream_subscription("conv_test"), 2)
        await observer.emit(server.streams[0], _request(), _request())
        assert len(observer.requests) == 1
        server.pending = [_request()]
        await adapter._reconcile_pending_elicitations("conv_test")
        assert len(observer.requests) == 1, "An active live request suppresses its snapshot copy"

        await observer.emit(server.streams[0], _resolved(action))
        await adapter._reconcile_pending_elicitations("conv_test")
        assert len(observer.requests) == 1, "A stale snapshot cannot revive a resolved request"

        await observer.emit(server.streams[0], _request(), _request())
        assert len(observer.requests) == 2, "A new live request must re-arm the same ID"
        await adapter._reconcile_pending_elicitations("conv_test")
        assert len(observer.requests) == 2
        assert server.posts == [], "Reconciliation must never answer an approval"


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", ["view_session", "switch_to_session", "switch_session"])
@pytest.mark.parametrize("resolved", [False, True])
async def test_switched_session_can_reuse_elicitation_id(switch: str, resolved: bool) -> None:
    server = _Server()
    server.pending = [_request()]
    for stream in server.streams:
        stream.subscribe.set()
    async with _adapter(server) as adapter:
        observer = _Observer()
        adapter._on_event = observer
        await adapter._ensure_session()
        await asyncio.wait_for(adapter._wait_for_stream_subscription("conv_test"), 2)
        assert len(observer.requests) == 1
        if resolved:
            await observer.emit(server.streams[0], _resolved())

        if switch == "view_session":
            await adapter.view_session("conv_next", read_only=True)
        elif switch == "switch_to_session":
            await adapter.switch_to_session("conv_next")
        else:
            adapter.switch_session("conv_next")
            await adapter._ensure_session()
        await asyncio.wait_for(adapter._wait_for_stream_subscription("conv_next"), 2)
        assert len(observer.requests) == 2, "An ID belongs to the displayed session"
        await observer.emit(server.streams[1], _request(), _request())
        assert len(observer.requests) == 2, "Snapshot and live copies still deduplicate"
        assert server.posts == []


@pytest.mark.asyncio
async def test_first_reconnect_resolution_precedes_stale_snapshot_request() -> None:
    server = _Server()
    server.streams[0].subscribe.set()
    server.streams[1].subscribe.set()
    server.streams[1].first_event = _resolved("accept")
    async with _adapter(server) as adapter:
        observer = _Observer()
        adapter._on_event = observer
        await adapter._ensure_session()
        await asyncio.wait_for(adapter._wait_for_stream_subscription("conv_test"), 2)
        # A missed ASK was answered elsewhere; the reconnect snapshot is stale.
        server.pending = [_request()]
        server.streams[0].events.put_nowait(httpx.ReadError("synthetic disconnect"))
        await asyncio.wait_for(server.streams[1].headers_received.wait(), 2)
        await asyncio.wait_for(adapter._wait_for_stream_subscription("conv_test"), 2)
        assert any(isinstance(event, ElicitationResolvedEvent) for event in observer.events)
        assert observer.requests == [], "Do not invoke even a cached approval hook for a dead ASK"
        await observer.emit(server.streams[1], _request(), _request())
        assert len(observer.requests) == 1, "A later live request can still re-arm the ID"
        assert server.posts == []
