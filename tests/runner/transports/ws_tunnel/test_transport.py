"""Unit tests for the WSTunnelTransport httpx transport adapter.

Tests handle_async_request, _TunneledByteStream iteration and aclose,
and error paths — all using a fake registry (no real WebSockets).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from omnigent.runner.transports.ws_tunnel import transport as transport_module
from omnigent.runner.transports.ws_tunnel.frames import (
    HelloFrame,
    RequestCancelFrame,
    RequestFrame,
    ResponseBodyFrame,
    ResponseEndFrame,
    ResponseHeadFrame,
    decode_frame,
)
from omnigent.runner.transports.ws_tunnel.registry import RunnerSession, TunnelRegistry
from omnigent.runner.transports.ws_tunnel.transport import (
    WSTunnelTransport,
    _TunneledByteStream,
)


class _NoopWS:
    """Minimal WebSocket fake."""

    async def send_text(self, data: str) -> None:
        pass

    async def receive_text(self) -> str:
        return await asyncio.Future()


def _hello() -> HelloFrame:
    return HelloFrame(runner_version="0.1.0", frame_protocol_version=1, harnesses=[], envs=[])


def _make_request(method: str = "GET", path: str = "/health") -> httpx.Request:
    """Build a minimal httpx.Request for testing."""
    return httpx.Request(method, f"http://runner{path}")


# ── handle_async_request: offline runner ────────────────


@pytest.mark.asyncio
async def test_handle_async_request_raises_connect_error_when_offline() -> None:
    """Offline runner raises httpx.ConnectError."""
    reg = TunnelRegistry()
    transport = WSTunnelTransport(reg, "r1")

    with pytest.raises(httpx.ConnectError, match="offline"):
        await transport.handle_async_request(_make_request())


@pytest.mark.asyncio
async def test_handle_async_request_raises_connect_error_on_race() -> None:
    """Runner going offline between get() and open_request() raises ConnectError."""
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    transport = WSTunnelTransport(reg, "r1")

    # Deregister between get and open_request — simulate a race.
    reg.deregister("r1")

    with pytest.raises(httpx.ConnectError, match="offline"):
        await transport.handle_async_request(_make_request())


# ── handle_async_request: successful response ──────────


@pytest.mark.asyncio
async def test_handle_async_request_returns_response() -> None:
    """A full request/response cycle through the transport."""
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    transport = WSTunnelTransport(reg, "r1")

    # Start the request in a task.
    request = _make_request()
    task = asyncio.create_task(transport.handle_async_request(request))

    # Wait for the request to be opened in the registry.
    await asyncio.sleep(0.01)

    # Find the open request and feed it a response.
    session = reg.get("r1")
    assert session is not None
    assert len(session.in_flight) == 1
    req_id = next(iter(session.in_flight))

    reg.route_response_frame(
        "r1", ResponseHeadFrame(id=req_id, status=200, headers=[["content-type", "text/plain"]])
    )
    reg.route_response_frame("r1", ResponseBodyFrame(id=req_id, body="hello", encoding="utf-8"))
    reg.route_response_frame("r1", ResponseEndFrame(id=req_id))

    response = await task
    assert response.status_code == 200

    # Drain the streaming body.
    body = b""
    async for chunk in response.stream:
        body += chunk
    assert body == b"hello"

    # After iteration, the request should be closed.
    assert req_id not in session.in_flight


@pytest.mark.asyncio
async def test_handle_async_request_with_body() -> None:
    """POST requests encode the body into the request frame."""
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    transport = WSTunnelTransport(reg, "r1")

    request = httpx.Request(
        "POST",
        "http://runner/v1/sessions/s1/events",
        content=b'{"role":"user"}',
        headers={"content-type": "application/json"},
    )
    task = asyncio.create_task(transport.handle_async_request(request))
    await asyncio.sleep(0.01)

    session = reg.get("r1")
    assert session is not None
    req_id = next(iter(session.in_flight))

    reg.route_response_frame("r1", ResponseHeadFrame(id=req_id, status=201))
    reg.route_response_frame("r1", ResponseEndFrame(id=req_id))

    response = await task
    assert response.status_code == 201


# ── _TunneledByteStream: abort propagation ─────────────


@pytest.mark.asyncio
async def test_tunneled_byte_stream_propagates_abort() -> None:
    """A tunnel disconnect mid-stream raises the abort error.

    The stream checks ``aborted_with`` after each ``get()``, so even
    a queued body chunk that arrived before the abort is not yielded
    once the abort flag is set — the ConnectionError surfaces
    immediately.
    """
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    state = reg.open_request("r1", "req1")

    stream = _TunneledByteStream(reg, "r1", "req1", state)

    # Simulate head arriving then tunnel aborting.
    reg.route_response_frame("r1", ResponseHeadFrame(id="req1", status=200))
    reg.route_response_frame("r1", ResponseBodyFrame(id="req1", body="chunk1", encoding="utf-8"))

    # Now deregister to abort.
    reg.deregister("r1")

    chunks: list[bytes] = []
    with pytest.raises(ConnectionError, match="tunnel closed"):
        async for chunk in stream:
            chunks.append(chunk)

    # The abort flag is checked after get() returns, so the queued chunk
    # is discarded and the error raises before any yield.
    assert chunks == []


# ── _TunneledByteStream: aclose sends cancel ───────────


@pytest.mark.asyncio
async def test_tunneled_byte_stream_aclose_cleans_up() -> None:
    """aclose() closes the request in the registry."""
    reg = TunnelRegistry()
    session = reg.register("r1", _NoopWS(), _hello())
    state = reg.open_request("r1", "req1")

    stream = _TunneledByteStream(reg, "r1", "req1", state)
    await stream.aclose()

    assert "req1" not in session.in_flight


# ── WSTunnelTransport.aclose ────────────────────────────


@pytest.mark.asyncio
async def test_transport_aclose_is_noop() -> None:
    """Transport aclose is a safe no-op."""
    reg = TunnelRegistry()
    transport = WSTunnelTransport(reg, "r1")
    await transport.aclose()  # Should not raise.


# ── Read-timeout handling (request timeout extension) ──


def _drain_cancel_frames(session: RunnerSession) -> list[RequestCancelFrame]:
    """Pop the session's outbound queue and return its request.cancel frames."""
    frames: list[RequestCancelFrame] = []
    while not session.outbound_queue.empty():
        item = session.outbound_queue.get_nowait()
        if item is None:
            continue
        frame = decode_frame(item)
        if isinstance(frame, RequestCancelFrame):
            frames.append(frame)
    return frames


def _make_stream_request(read_timeout: float | None) -> httpx.Request:
    """Build a request carrying an httpx read timeout extension."""
    return httpx.Request(
        "GET",
        "http://runner/v1/sessions/s1/stream",
        extensions={"timeout": {"connect": 5.0, "read": read_timeout}},
    )


@pytest.mark.asyncio
async def test_stalled_body_raises_read_timeout_and_keeps_tunnel_registered() -> None:
    """A silent body stream on a live tunnel raises httpx.ReadTimeout.

    The tunnel session must stay registered so disconnect attribution
    can distinguish stream loss from tunnel loss.
    """
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    transport = WSTunnelTransport(reg, "r1")

    task = asyncio.create_task(transport.handle_async_request(_make_stream_request(0.05)))
    await asyncio.sleep(0.01)

    session = reg.get("r1")
    assert session is not None
    req_id = next(iter(session.in_flight))
    reg.route_response_frame("r1", ResponseHeadFrame(id=req_id, status=200))
    reg.route_response_frame("r1", ResponseBodyFrame(id=req_id, body="first", encoding="utf-8"))
    # No end frame and no further chunks: the stream goes silent.

    response = await task

    async def _drain() -> list[bytes]:
        return [chunk async for chunk in response.stream]

    # The outer wait_for guards against a regression hanging forever:
    # it converts a hang into a plain TimeoutError, failing the test.
    with pytest.raises(httpx.ReadTimeout):
        await asyncio.wait_for(_drain(), timeout=2.0)

    assert reg.get("r1") is session
    assert req_id not in session.in_flight
    # The runner must be told to stop its dispatch task, or its stream
    # generator leaks and keeps draining the session's shared event queue.
    cancels = _drain_cancel_frames(session)
    assert [frame.id for frame in cancels] == [req_id]


@pytest.mark.asyncio
async def test_stalled_response_head_raises_read_timeout() -> None:
    """A response head that never arrives times out with httpx.ReadTimeout."""
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    transport = WSTunnelTransport(reg, "r1")

    with pytest.raises(httpx.ReadTimeout):
        await asyncio.wait_for(
            transport.handle_async_request(_make_stream_request(0.05)),
            timeout=2.0,
        )

    session = reg.get("r1")
    assert session is not None
    assert not session.in_flight
    assert len(_drain_cancel_frames(session)) == 1


@pytest.mark.asyncio
async def test_cancelled_response_head_wait_sends_request_cancel() -> None:
    """Cancelling before the response head stops the runner dispatch."""
    reg = TunnelRegistry()
    session = reg.register("r1", _NoopWS(), _hello())
    transport = WSTunnelTransport(reg, "r1")

    task = asyncio.create_task(transport.handle_async_request(_make_stream_request(None)))
    request_wire = await asyncio.wait_for(session.outbound_queue.get(), timeout=1.0)
    assert request_wire is not None
    request_frame = decode_frame(request_wire)
    assert isinstance(request_frame, RequestFrame)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    cancel_wire = await asyncio.wait_for(session.outbound_queue.get(), timeout=1.0)
    assert cancel_wire is not None
    cancel_frame = decode_frame(cancel_wire)
    assert isinstance(cancel_frame, RequestCancelFrame)
    assert cancel_frame.id == request_frame.id
    assert cancel_frame.reason == "client_disconnected"
    assert request_frame.id not in session.in_flight


@pytest.mark.asyncio
async def test_repeated_cancellation_during_head_cleanup_closes_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second cancellation cannot strand response-head request state."""
    reg = TunnelRegistry()
    session = reg.register("r1", _NoopWS(), _hello())
    transport = WSTunnelTransport(reg, "r1")
    task = asyncio.create_task(transport.handle_async_request(_make_stream_request(None)))
    request_wire = await asyncio.wait_for(session.outbound_queue.get(), timeout=1.0)
    assert request_wire is not None
    request_frame = decode_frame(request_wire)
    assert isinstance(request_frame, RequestFrame)
    cancel_send_started = asyncio.Event()

    async def _stall_cancel_send(runner_session: RunnerSession, data: str) -> None:
        del runner_session, data
        cancel_send_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(reg, "send_text", _stall_cancel_send)
    task.cancel()
    await asyncio.wait_for(cancel_send_started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert request_frame.id not in session.in_flight


@pytest.mark.asyncio
async def test_stalled_body_on_replaced_tunnel_raises_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read timeout on a stale-generation request is a tunnel transition.

    Newest-wins replacement can race a stalled read: the new tunnel is
    already registered while the old request's abort wakeup (scheduled
    onto the request loop) has not landed yet. The timeout must classify
    as ``ConnectionError`` (the runner_disconnected path), not
    ``httpx.ReadTimeout`` — a "runner still registered" liveness check
    would otherwise misattribute it as a live-tunnel stream loss. The
    abort is no-opped to hold that race window open deterministically.
    """
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    transport = WSTunnelTransport(reg, "r1")

    task = asyncio.create_task(transport.handle_async_request(_make_stream_request(0.05)))
    await asyncio.sleep(0.01)

    session = reg.get("r1")
    assert session is not None
    req_id = next(iter(session.in_flight))
    reg.route_response_frame("r1", ResponseHeadFrame(id=req_id, status=200))
    response = await task

    monkeypatch.setattr(
        TunnelRegistry,
        "_abort_session_inflight",
        staticmethod(lambda session, error: None),
    )
    reg.register("r1", _NoopWS(), _hello())
    assert reg.get("r1") is not session

    async def _drain() -> list[bytes]:
        return [chunk async for chunk in response.stream]

    with pytest.raises(ConnectionError, match="replaced"):
        await asyncio.wait_for(_drain(), timeout=2.0)


@pytest.mark.asyncio
async def test_stalled_body_read_timeout_carries_request() -> None:
    """The body-path ReadTimeout carries the originating request.

    httpx raises ``RuntimeError`` from ``exc.request`` when the error
    was built without one, breaking callers that log or classify by
    request.
    """
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    transport = WSTunnelTransport(reg, "r1")

    request = _make_stream_request(0.05)
    task = asyncio.create_task(transport.handle_async_request(request))
    await asyncio.sleep(0.01)

    session = reg.get("r1")
    assert session is not None
    req_id = next(iter(session.in_flight))
    reg.route_response_frame("r1", ResponseHeadFrame(id=req_id, status=200))
    response = await task

    async def _drain() -> list[bytes]:
        return [chunk async for chunk in response.stream]

    with pytest.raises(httpx.ReadTimeout) as excinfo:
        await asyncio.wait_for(_drain(), timeout=2.0)
    assert excinfo.value.request is request


@pytest.mark.asyncio
async def test_read_timeout_surfaces_when_cancel_send_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresponsive session-owner loop can't hang timeout recovery.

    The best-effort request.cancel send awaits an ack from the session's
    owner loop; a wedged loop never acks. The send must be bounded so
    ``httpx.ReadTimeout`` still surfaces and the request is cleaned up.
    """
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    transport = WSTunnelTransport(reg, "r1")

    task = asyncio.create_task(transport.handle_async_request(_make_stream_request(0.05)))
    await asyncio.sleep(0.01)

    session = reg.get("r1")
    assert session is not None
    req_id = next(iter(session.in_flight))
    reg.route_response_frame("r1", ResponseHeadFrame(id=req_id, status=200))
    response = await task

    async def _hang(session: RunnerSession, data: str) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(reg, "send_text", _hang)
    monkeypatch.setattr(transport_module, "_CANCEL_SEND_TIMEOUT_S", 0.05, raising=False)

    async def _drain() -> list[bytes]:
        return [chunk async for chunk in response.stream]

    with pytest.raises(httpx.ReadTimeout):
        await asyncio.wait_for(_drain(), timeout=2.0)
    assert req_id not in session.in_flight


@pytest.mark.asyncio
async def test_read_timeout_none_waits_past_delay_for_body() -> None:
    """``read=None`` (streaming clients) keeps waiting for late chunks."""
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    transport = WSTunnelTransport(reg, "r1")

    task = asyncio.create_task(transport.handle_async_request(_make_stream_request(None)))
    await asyncio.sleep(0.01)

    session = reg.get("r1")
    assert session is not None
    req_id = next(iter(session.in_flight))
    reg.route_response_frame("r1", ResponseHeadFrame(id=req_id, status=200))
    response = await task

    async def _feed_late() -> None:
        await asyncio.sleep(0.15)
        reg.route_response_frame("r1", ResponseBodyFrame(id=req_id, body="late", encoding="utf-8"))
        reg.route_response_frame("r1", ResponseEndFrame(id=req_id))

    feeder = asyncio.create_task(_feed_late())
    body = b""
    async for chunk in response.stream:
        body += chunk
    await feeder
    assert body == b"late"
