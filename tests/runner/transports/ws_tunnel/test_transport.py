"""Unit tests for the WSTunnelTransport httpx transport adapter.

Tests handle_async_request, _TunneledByteStream iteration and aclose,
and error paths — all using a fake registry (no real WebSockets).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from omnigent.runner.transports.ws_tunnel.frames import (
    RESPONSE_FLOW_CAPABILITY,
    RESPONSE_FLOW_CREDIT_BATCH,
    RESPONSE_FLOW_WINDOW_FRAMES,
    HelloFrame,
    RequestCancelFrame,
    RequestFlowFrame,
    RequestFrame,
    ResponseBodyFrame,
    ResponseEndFrame,
    ResponseHeadFrame,
    decode_frame,
)
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
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


@pytest.mark.asyncio
async def test_handle_async_request_rejects_replaced_generation() -> None:
    reg = TunnelRegistry()
    old = reg.register("r1", _NoopWS(), _hello())
    transport = WSTunnelTransport(reg, "r1")
    request = _make_request()
    request.extensions["runner_tunnel_generation"] = old.generation
    new = reg.register("r1", _NoopWS(), _hello())

    with pytest.raises(ConnectionError, match="before request was sent"):
        await transport.handle_async_request(request)
    assert not new.in_flight
    assert new.outbound_queue.empty()


# ── handle_async_request: successful response ──────────


@pytest.mark.asyncio
async def test_wait_for_runner_resolves_on_register_and_times_out_when_absent() -> None:
    """wait_for_runner parks until the runner registers, else returns False at the deadline."""
    reg = TunnelRegistry()
    transport = WSTunnelTransport(reg, "r1")

    assert await transport.wait_for_runner(0.05) is False

    waiter = asyncio.ensure_future(transport.wait_for_runner(5.0))
    await asyncio.sleep(0)
    reg.register("r1", _NoopWS(), _hello())
    assert await asyncio.wait_for(waiter, timeout=1.0) is True


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


@pytest.mark.asyncio
async def test_response_end_before_head_aborts_request_and_releases_slot() -> None:
    """An end frame without a head must not leave the transport waiting forever."""
    reg = TunnelRegistry()
    session = reg.register("r1", _NoopWS(), _hello())
    transport = WSTunnelTransport(reg, "r1")

    task = asyncio.create_task(transport.handle_async_request(_make_request("GET", "/bad")))
    for _ in range(100):
        if session.in_flight:
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("request did not open before the bounded test window")

    req_id = next(iter(session.in_flight))
    assert reg.route_response_frame("r1", ResponseEndFrame(id=req_id)) is True

    with pytest.raises(httpx.RemoteProtocolError, match=r"before response\.head"):
        await asyncio.wait_for(task, timeout=0.2)
    assert req_id not in session.in_flight


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


@pytest.mark.asyncio
async def test_cancelled_body_wait_sends_request_cancel_before_forgetting_request() -> None:
    """A disconnect mid-read must cancel the runner before forgetting the request.

    The iterator's teardown runs before the response's ``aclose``; if it forgets
    the request without cancelling, a flow-controlled runner parked on exhausted
    credit never learns the consumer left and blocks forever, stranding its open
    upstream body. The cancel must go out first, exactly once across both paths.
    """
    reg = TunnelRegistry()
    session = reg.register("r1", _NoopWS(), _hello())
    state = reg.open_request("r1", "req1")

    stream = _TunneledByteStream(reg, "r1", "req1", state)
    body = stream.__aiter__()
    # Park the read on an empty queue, then cancel it like a client disconnect.
    pull = asyncio.ensure_future(anext(body))
    await asyncio.sleep(0)
    assert not pull.done()
    pull.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pull
    # httpx closes the response stream after the iterator unwinds.
    await stream.aclose()
    await asyncio.sleep(0)

    assert "req1" not in session.in_flight
    cancels: list[RequestCancelFrame] = []
    while not session.outbound_queue.empty():
        frame = decode_frame(session.outbound_queue.get_nowait().data)
        if isinstance(frame, RequestCancelFrame):
            cancels.append(frame)
    assert len(cancels) == 1
    assert cancels[0].id == "req1"


@pytest.mark.asyncio
async def test_cancelled_head_wait_sends_request_cancel_before_forgetting_request() -> None:
    """A cancel while awaiting the response head must cancel the runner too.

    Once the request frame reached a flow-controlled runner, abandoning the head
    wait without a cancel strands that dispatch on exhausted send credit. The
    head-exception path must send exactly one matching cancel before it forgets
    the request, sharing ordering with the body-stream cleanup.
    """
    reg = TunnelRegistry()
    hello = _hello()
    hello.capabilities.append(RESPONSE_FLOW_CAPABILITY)
    session = reg.register("r1", _NoopWS(), hello)
    transport = WSTunnelTransport(reg, "r1")

    task = asyncio.ensure_future(transport.handle_async_request(_make_request("GET", "/download")))
    # Let the request frame go out and the head wait park; no head arrives.
    await asyncio.sleep(0.01)
    assert not task.done()
    sent = decode_frame(session.outbound_queue.get_nowait().data)
    assert isinstance(sent, RequestFrame)
    req_id = sent.id
    assert req_id in session.in_flight

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert req_id not in session.in_flight
    cancels: list[RequestCancelFrame] = []
    while not session.outbound_queue.empty():
        frame = decode_frame(session.outbound_queue.get_nowait().data)
        if isinstance(frame, RequestCancelFrame):
            cancels.append(frame)
    assert len(cancels) == 1
    assert cancels[0].id == req_id


@pytest.mark.asyncio
async def test_body_generator_finalization_sends_request_cancel() -> None:
    """Finalizing a partially-consumed body generator still cancels the runner.

    httpx closes the response via ``stream.aclose``, but an abandoned iterator is
    finalized by throwing ``GeneratorExit`` into it. That ``finally`` must run the
    same cancel-before-forget cleanup, exactly once, so a flow-controlled runner
    is never stranded on exhausted credit.
    """
    reg = TunnelRegistry()
    session = reg.register("r1", _NoopWS(), _hello())
    state = reg.open_request("r1", "req1")
    reg.route_response_frame("r1", ResponseHeadFrame(id="req1", status=200))
    reg.route_response_frame("r1", ResponseBodyFrame(id="req1", body="chunk1", encoding="utf-8"))

    stream = _TunneledByteStream(reg, "r1", "req1", state)
    body = stream.__aiter__()
    assert await anext(body) == b"chunk1"
    # Finalize without draining the rest, as GC does to an abandoned generator.
    await body.aclose()
    await asyncio.sleep(0)

    assert "req1" not in session.in_flight
    cancels: list[RequestCancelFrame] = []
    while not session.outbound_queue.empty():
        frame = decode_frame(session.outbound_queue.get_nowait().data)
        if isinstance(frame, RequestCancelFrame):
            cancels.append(frame)
    assert len(cancels) == 1
    assert cancels[0].id == "req1"


# ── WSTunnelTransport.aclose ────────────────────────────


@pytest.mark.asyncio
async def test_transport_aclose_is_noop() -> None:
    """Transport aclose is a safe no-op."""
    reg = TunnelRegistry()
    transport = WSTunnelTransport(reg, "r1")
    await transport.aclose()  # Should not raise.


# ── flow control negotiation ─────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("capable", [True, False])
async def test_flow_control_follows_runner_capability(capable: bool) -> None:
    """Only runners advertising flow control get a send window and credit grants."""
    reg = TunnelRegistry()
    hello = _hello()
    if capable:
        hello.capabilities.append(RESPONSE_FLOW_CAPABILITY)
    session = reg.register("r1", _NoopWS(), hello)
    transport = WSTunnelTransport(reg, "r1")

    task = asyncio.create_task(transport.handle_async_request(_make_request("GET", "/download")))
    await asyncio.sleep(0.01)
    raw = session.outbound_queue.get_nowait()
    assert raw is not None
    sent = decode_frame(raw.data)
    assert isinstance(sent, RequestFrame)
    assert sent.flow_window == (RESPONSE_FLOW_WINDOW_FRAMES if capable else None)

    reg.route_response_frame(
        "r1", ResponseHeadFrame(id=sent.id, status=200, headers=[["content-type", "text/plain"]])
    )
    for _ in range(RESPONSE_FLOW_CREDIT_BATCH):
        reg.route_response_frame("r1", ResponseBodyFrame(id=sent.id, body="x", encoding="utf-8"))
    reg.route_response_frame("r1", ResponseEndFrame(id=sent.id))
    response = await task
    body = b"".join([chunk async for chunk in response.stream])
    assert body == b"x" * RESPONSE_FLOW_CREDIT_BATCH

    grants: list[tuple[str, int]] = []
    while not session.outbound_queue.empty():
        raw = session.outbound_queue.get_nowait()
        assert raw is not None
        frame = decode_frame(raw.data)
        if isinstance(frame, RequestFlowFrame):
            grants.append((frame.id, frame.credits))
    assert grants == ([(sent.id, RESPONSE_FLOW_CREDIT_BATCH)] if capable else [])
