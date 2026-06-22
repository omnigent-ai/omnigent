"""End-to-end WS tunnel tests with fake WebSockets."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import queue
import threading
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from omnigent.runner import create_runner_app
from omnigent.runner.transports.ws_tunnel.frames import (
    HelloFrame,
    RequestFrame,
    ResponseBodyFrame,
    ResponseEndFrame,
    ResponseHeadFrame,
    decode_frame,
)
from omnigent.runner.transports.ws_tunnel.registry import (
    RunnerSession,
    TunnelRegistry,
)
from omnigent.runner.transports.ws_tunnel.serve import dispatch_via_asgi
from omnigent.runner.transports.ws_tunnel.transport import WSTunnelTransport
from tests.unit.runner.helpers import NullServerClient

# ── Fake WebSocket pair ──────────────────────────────────


class _FakeWS:
    """One half of a bidirectional fake WebSocket.

    ``send_text`` pushes onto the peer's receive queue.
    """

    def __init__(self) -> None:
        self.recv_queue: asyncio.Queue[str] = asyncio.Queue()
        self._peer: _FakeWS | None = None

    def link(self, peer: _FakeWS) -> None:
        self._peer = peer
        peer._peer = self

    async def send_text(self, data: str) -> None:
        if self._peer is None:
            raise RuntimeError("FakeWS not linked to a peer")
        await self._peer.recv_queue.put(data)

    async def receive_text(self) -> str:
        return await self.recv_queue.get()


def _make_ws_pair() -> tuple[_FakeWS, _FakeWS]:
    """Two fake WebSockets linked to each other."""
    a, b = _FakeWS(), _FakeWS()
    a.link(b)
    return a, b


async def _drain_session_outbound(session: RunnerSession) -> None:
    """
    Simulate the server route's WebSocket sender task.

    :param session: Registered tunnel session whose outbound queue
        should be drained to its fake WebSocket.
    :returns: None when the session is retired.
    """
    while True:
        data = await session.outbound_queue.get()
        if data is None:
            return
        await session.ws.send_text(data)


class _ThreadHandoffWS:
    """Fake WebSocket that hands server-sent request frames to a thread.

    :param outbound: Thread-safe queue receiving text frames.
    """

    def __init__(self, outbound: queue.Queue[str]) -> None:
        self._outbound = outbound

    async def send_text(self, data: str) -> None:
        """Record a frame sent by the transport.

        :param data: Encoded tunnel frame JSON.
        :returns: None.
        """
        self._outbound.put(data)

    async def receive_text(self) -> str:
        """Fail because this fake is send-only.

        :raises NotImplementedError: Always.
        """
        raise NotImplementedError("receive_text is not used by this fake")


# ── Test infrastructure ──────────────────────────────────


@pytest.fixture
async def tunneled_client() -> AsyncIterator[
    tuple[httpx.AsyncClient, TunnelRegistry, asyncio.Task[None]]
]:
    """Build an httpx client tunneled to a fake runner."""
    runner_app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    server_ws, runner_ws = _make_ws_pair()
    registry = TunnelRegistry()
    hello = HelloFrame(
        runner_version="0.1.0-test",
        frame_protocol_version=1,
        harnesses=["claude-sdk"],
        envs=["os_sandbox"],
    )
    session = registry.register("runner-test-1", server_ws, hello)
    sender_task = asyncio.create_task(
        _drain_session_outbound(session),
        name="fake-route-sender-loop",
    )

    # Hold strong refs so dispatch tasks aren't GC'd mid-flight.
    dispatch_tasks: list[asyncio.Task[None]] = []

    async def runner_loop() -> None:
        while True:
            text = await runner_ws.receive_text()
            frame = decode_frame(text)
            if isinstance(frame, RequestFrame):
                dispatch_tasks.append(
                    asyncio.create_task(dispatch_via_asgi(runner_app, frame, runner_ws.send_text))
                )

    runner_task = asyncio.create_task(runner_loop(), name="fake-runner-loop")

    async def server_dispatcher_loop() -> None:
        while True:
            text = await server_ws.receive_text()
            frame = decode_frame(text)
            registry.route_response_frame("runner-test-1", frame)

    dispatcher_task = asyncio.create_task(server_dispatcher_loop(), name="server-dispatcher-loop")

    transport = WSTunnelTransport(registry, "runner-test-1")
    client = httpx.AsyncClient(transport=transport, base_url="http://runner")

    try:
        yield client, registry, runner_task
    finally:
        await client.aclose()
        sender_task.cancel()
        runner_task.cancel()
        dispatcher_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sender_task
        with contextlib.suppress(asyncio.CancelledError):
            await runner_task
        with contextlib.suppress(asyncio.CancelledError):
            await dispatcher_task


# ── End-to-end tests ─────────────────────────────────────


@pytest.mark.asyncio
async def test_health_round_trip_via_ws_tunnel(tunneled_client) -> None:
    """GET /health round-trips through the tunnel."""
    client, _registry, _task = tunneled_client
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_post_session_events_returns_501_via_ws_tunnel(tunneled_client) -> None:
    """The runner's 501 stub surfaces through the tunnel."""
    client, _registry, _task = tunneled_client
    response = await client.post(
        "/v1/sessions/conv_test/events",
        json={"type": "message", "role": "user", "content": []},
    )
    assert response.status_code == 501
    body = response.json()
    assert body["error"] == "not_implemented"
    assert "HarnessProcessManager" in body["detail"]


@pytest.mark.asyncio
async def test_concurrent_requests_dont_collide(tunneled_client) -> None:
    """Concurrent requests keep separate responses."""
    client, _registry, _task = tunneled_client
    responses = await asyncio.gather(*[client.get("/health") for _ in range(5)])
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json() == {"status": "ok"} for r in responses)


@pytest.mark.asyncio
async def test_runner_offline_raises_connect_error() -> None:
    """No registered runner → ConnectError, like a TCP connect refused."""
    registry = TunnelRegistry()  # no runners registered
    transport = WSTunnelTransport(registry, "ghost-runner")
    async with httpx.AsyncClient(transport=transport, base_url="http://x") as c:
        with pytest.raises(httpx.ConnectError):
            await c.get("/health")


@pytest.mark.asyncio
async def test_404_round_trip_via_ws_tunnel(tunneled_client) -> None:
    """A runner 404 makes it back to the server side."""
    client, _registry, _task = tunneled_client
    response = await client.get("/v1/conversations")  # not a runner endpoint
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_streaming_response_survives_post_body_read() -> None:
    """StreamingResponse keeps running after POST body reads."""
    runner_app = FastAPI()

    @runner_app.post("/stream")
    async def stream(request: Request) -> StreamingResponse:
        """
        Return a streaming response after consuming the request body.

        :param request: Incoming ASGI request.
        :returns: Streaming response with two body chunks.
        """
        body = await request.json()

        async def chunks() -> AsyncIterator[bytes]:
            """
            Yield two body chunks.

            :returns: Async byte iterator for the response body.
            """
            yield f"first:{body['value']}\n".encode()
            await asyncio.sleep(0)
            yield b"second\n"

        return StreamingResponse(chunks(), media_type="text/plain")

    server_ws, runner_ws = _make_ws_pair()
    registry = TunnelRegistry()
    session = registry.register(
        "runner-test-streaming",
        server_ws,
        HelloFrame(
            runner_version="0.1.0-test",
            frame_protocol_version=1,
            harnesses=["openai-agents"],
            envs=["os_sandbox"],
        ),
    )
    sender_task = asyncio.create_task(
        _drain_session_outbound(session),
        name="fake-streaming-route-sender-loop",
    )

    dispatch_tasks: list[asyncio.Task[None]] = []

    async def runner_loop() -> None:
        """
        Dispatch request frames from the fake runner WebSocket.

        :returns: None.
        """
        while True:
            text = await runner_ws.receive_text()
            frame = decode_frame(text)
            if isinstance(frame, RequestFrame):
                dispatch_tasks.append(
                    asyncio.create_task(dispatch_via_asgi(runner_app, frame, runner_ws.send_text))
                )

    async def server_dispatcher_loop() -> None:
        """
        Route response frames from the fake server WebSocket.

        :returns: None.
        """
        while True:
            text = await server_ws.receive_text()
            frame = decode_frame(text)
            registry.route_response_frame("runner-test-streaming", frame)

    runner_task = asyncio.create_task(runner_loop(), name="fake-streaming-runner-loop")
    dispatcher_task = asyncio.create_task(
        server_dispatcher_loop(),
        name="streaming-server-dispatcher-loop",
    )
    transport = WSTunnelTransport(registry, "runner-test-streaming")

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            response = await asyncio.wait_for(
                client.post("/stream", json={"value": "ok"}),
                timeout=2.0,
            )
        assert response.status_code == 200
        assert response.text == "first:ok\nsecond\n"
    finally:
        runner_task.cancel()
        sender_task.cancel()
        dispatcher_task.cancel()
        for task in dispatch_tasks:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sender_task
        with contextlib.suppress(asyncio.CancelledError):
            await runner_task
        with contextlib.suppress(asyncio.CancelledError):
            await dispatcher_task
        await asyncio.gather(*dispatch_tasks, return_exceptions=True)


def test_response_frames_from_second_event_loop_wake_transport_waiters() -> None:
    """Response frames from another event-loop thread wake the waiting request."""
    raw_requests: queue.Queue[str] = queue.Queue()
    registry = TunnelRegistry()
    route_ready: concurrent.futures.Future[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = (
        concurrent.futures.Future()
    )
    request_result: concurrent.futures.Future[tuple[int, str]] = concurrent.futures.Future()
    response_result: concurrent.futures.Future[None] = concurrent.futures.Future()

    def route_loop_main() -> None:
        """
        Own the registered session and drain outbound frames.

        :returns: None.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _run_route_loop() -> None:
            """
            Register the runner and keep its sender task alive.

            :returns: None.
            """
            stop_event = asyncio.Event()
            session = registry.register(
                "runner-threaded",
                _ThreadHandoffWS(raw_requests),
                HelloFrame(
                    runner_version="0.1.0-test",
                    frame_protocol_version=1,
                    harnesses=["openai-agents"],
                    envs=["os_sandbox"],
                ),
            )
            sender_task = asyncio.create_task(
                _drain_session_outbound(session),
                name="threaded-route-sender",
            )
            route_ready.set_result((loop, stop_event))
            try:
                await stop_event.wait()
            finally:
                registry.deregister("runner-threaded", session)
                sender_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sender_task

        try:
            loop.run_until_complete(_run_route_loop())
        except BaseException as exc:
            if not route_ready.done():
                route_ready.set_exception(exc)
            elif not request_result.done():
                request_result.set_exception(exc)
        finally:
            loop.close()

    def request_loop_main() -> None:
        """
        Run the request on a separate event loop thread.

        :returns: None.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _run_request() -> tuple[int, str]:
            """
            Issue one tunneled HTTP request.

            :returns: Response status and body text.
            """
            transport = WSTunnelTransport(registry, "runner-threaded")
            async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
                response = await client.get("/threaded")
            return response.status_code, response.text

        try:
            request_result.set_result(loop.run_until_complete(_run_request()))
        except BaseException as exc:
            request_result.set_exception(exc)
        finally:
            loop.close()

    def response_loop_main(request_frame: RequestFrame) -> None:
        """
        Route response frames from a third event loop thread.

        :param request_frame: Request frame captured from the
            route-loop sender queue.
        :returns: None.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _route_response() -> None:
            """
            Deliver a full response while this loop is running.

            :returns: None.
            """
            registry.route_response_frame(
                "runner-threaded",
                ResponseHeadFrame(
                    id=request_frame.id,
                    status=200,
                    headers=[["content-type", "text/plain; charset=utf-8"]],
                ),
            )
            registry.route_response_frame(
                "runner-threaded",
                ResponseBodyFrame(
                    id=request_frame.id,
                    body="ok",
                    encoding="utf-8",
                ),
            )
            registry.route_response_frame(
                "runner-threaded",
                ResponseEndFrame(id=request_frame.id),
            )

        try:
            loop.run_until_complete(_route_response())
            response_result.set_result(None)
        except BaseException as exc:
            response_result.set_exception(exc)
        finally:
            loop.close()

    route_thread = threading.Thread(
        target=route_loop_main,
        name="ws-tunnel-route-loop",
    )
    route_thread.start()
    route_loop, stop_event = route_ready.result(timeout=2.0)

    request_thread = threading.Thread(
        target=request_loop_main,
        name="ws-tunnel-request-loop",
    )
    request_thread.start()
    response_thread: threading.Thread | None = None
    try:
        raw = raw_requests.get(timeout=2.0)
        request_frame = decode_frame(raw)
        assert isinstance(request_frame, RequestFrame)
        response_thread = threading.Thread(
            target=response_loop_main,
            args=(request_frame,),
            name="ws-tunnel-response-loop",
        )
        response_thread.start()
        response_result.result(timeout=1.0)
        status_code, text = request_result.result(timeout=1.0)
        assert status_code == 200
        assert text == "ok"
    finally:
        with contextlib.suppress(RuntimeError):
            route_loop.call_soon_threadsafe(stop_event.set)
        if response_thread is not None:
            response_thread.join(timeout=2.0)
            assert not response_thread.is_alive()
        request_thread.join(timeout=2.0)
        route_thread.join(timeout=2.0)
        assert not request_thread.is_alive()
        assert not route_thread.is_alive()
