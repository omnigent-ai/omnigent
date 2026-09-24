"""Unit tests for the WSTunnelTransport httpx transport adapter.

Tests handle_async_request, _TunneledByteStream iteration and aclose,
and error paths — all using a fake registry (no real WebSockets).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from omnigent.runner.transports.ws_tunnel.frames import (
    HelloFrame,
    RequestCancelFrame,
    RequestFrame,
    ResponseBodyFrame,
    ResponseEndFrame,
    ResponseHeadFrame,
    decode_frame,
    encode_body,
)
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry, _abort_request_state
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


def _timed_request(read: float | None, path: str = "/health") -> httpx.Request:
    """Build a request carrying the timeout extension httpx sets from a Timeout."""
    timeout = httpx.Timeout(5.0, read=read)
    return httpx.Request("GET", f"http://runner{path}", extensions={"timeout": timeout.as_dict()})


async def _sent_frames(reg: TunnelRegistry, runner_id: str) -> list[object]:
    """Drain the frames the registry queued for the runner's sender loop."""
    await asyncio.sleep(0)  # send_text enqueues via call_soon_threadsafe
    session = reg.get(runner_id)
    assert session is not None
    frames: list[object] = []
    while not session.outbound_queue.empty():
        text = session.outbound_queue.get_nowait()
        if text is not None:
            frames.append(decode_frame(text))
    return frames


def _cancels(frames: list[object]) -> list[tuple[str, str]]:
    return [(f.id, f.reason) for f in frames if isinstance(f, RequestCancelFrame)]


def _request_id(frames: list[object]) -> str:
    requests = [f for f in frames if isinstance(f, RequestFrame)]
    assert len(requests) == 1
    return requests[0].id


# ── read timeout ────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_timeout_bounds_the_wait_for_the_response_head() -> None:
    """A runner that never answers costs the read budget, not the tunnel's lifetime.

    Before this, the head wait had no deadline, so every ``timeout=`` a
    caller passed over the tunnel was a no-op and a stalled runner held the
    request until its tunnel dropped. The handler is not cancelled: runner
    handlers are not uniformly safe to cancel mid-request, and a caller that
    gives up before the head has always just dropped the late response.
    """
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    transport = WSTunnelTransport(reg, "r1")

    with pytest.raises(httpx.ReadTimeout, match="did not answer within"):
        await transport.handle_async_request(_timed_request(read=0.05))

    session = reg.get("r1")
    assert session is not None
    assert session.in_flight == {}
    frames = await _sent_frames(reg, "r1")
    assert _request_id(frames)  # the request itself was sent
    assert _cancels(frames) == []


@pytest.mark.asyncio
async def test_read_timeout_none_waits_for_a_slow_head() -> None:
    """``read=None`` keeps today's behavior: the head may take as long as it takes."""
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    transport = WSTunnelTransport(reg, "r1")

    task = asyncio.create_task(transport.handle_async_request(_timed_request(read=None)))
    await asyncio.sleep(0.15)
    assert not task.done()
    session = reg.get("r1")
    assert session is not None
    req_id = next(iter(session.in_flight))
    reg.route_response_frame("r1", ResponseHeadFrame(id=req_id, status=204, headers=[]))
    reg.route_response_frame("r1", ResponseEndFrame(id=req_id))

    assert (await task).status_code == 204


@pytest.mark.asyncio
async def test_read_timeout_bounds_each_body_frame() -> None:
    """A head followed by silence times out while reading the body, frees the slot, and cancels.

    Cancelling here mirrors a consumer that stops reading the stream.
    """
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    transport = WSTunnelTransport(reg, "r1")

    task = asyncio.create_task(transport.handle_async_request(_timed_request(read=0.05)))
    await asyncio.sleep(0.01)
    session = reg.get("r1")
    assert session is not None
    req_id = next(iter(session.in_flight))
    reg.route_response_frame("r1", ResponseHeadFrame(id=req_id, status=200, headers=[]))
    response = await task

    with pytest.raises(httpx.ReadTimeout, match="sent no response body within"):
        async for _chunk in response.stream:  # type: ignore[union-attr]
            pass

    assert req_id not in session.in_flight
    assert _cancels(await _sent_frames(reg, "r1")) == [(req_id, "read_timeout")]


@pytest.mark.asyncio
async def test_client_per_call_timeout_reaches_the_tunnel() -> None:
    """The timeout a caller passes to ``client.get`` is what bounds the tunnel wait.

    The router builds its runner clients with ``httpx.Timeout(5.0, read=None)``,
    so a call without its own timeout still waits without bound.
    """
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    client = httpx.AsyncClient(
        transport=WSTunnelTransport(reg, "r1"),
        base_url="http://runner",
        timeout=httpx.Timeout(5.0, read=None),
    )
    try:
        with pytest.raises(httpx.ReadTimeout):
            await client.get("/v1/sessions/abc", timeout=0.1)

        task = asyncio.create_task(client.get("/v1/sessions/abc"))
        await asyncio.sleep(0.2)
        assert not task.done()
        session = reg.get("r1")
        assert session is not None
        req_id = next(iter(session.in_flight))
        reg.route_response_frame("r1", ResponseHeadFrame(id=req_id, status=200, headers=[]))
        reg.route_response_frame("r1", ResponseBodyFrame(id=req_id, body="{}", encoding="utf-8"))
        reg.route_response_frame("r1", ResponseEndFrame(id=req_id))
        assert (await task).status_code == 200
    finally:
        await client.aclose()


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


@pytest.mark.asyncio
async def test_read_timeout_resets_for_each_body_frame() -> None:
    """The budget bounds each frame, not the whole body: timely chunks keep streaming."""
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    transport = WSTunnelTransport(reg, "r1")

    task = asyncio.create_task(transport.handle_async_request(_timed_request(read=0.2)))
    await asyncio.sleep(0.01)
    session = reg.get("r1")
    assert session is not None
    req_id = next(iter(session.in_flight))
    reg.route_response_frame("r1", ResponseHeadFrame(id=req_id, status=200, headers=[]))
    response = await task

    received: list[bytes] = []

    async def _consume() -> None:
        async for chunk in response.stream:  # type: ignore[union-attr]
            received.append(chunk)

    consumer = asyncio.create_task(_consume())
    # Three timely chunks spanning 0.36s total — past the 0.2s budget, which a
    # whole-body deadline would have blown before the last chunk.
    for index in range(3):
        await asyncio.sleep(0.12)
        body_str, encoding = encode_body(f"chunk{index}".encode(), "text/plain")
        reg.route_response_frame(
            "r1", ResponseBodyFrame(id=req_id, body=body_str, encoding=encoding)
        )
    with pytest.raises(httpx.ReadTimeout, match="sent no response body within"):
        await consumer

    assert received == [b"chunk0", b"chunk1", b"chunk2"]
    assert req_id not in session.in_flight
    assert _cancels(await _sent_frames(reg, "r1")) == [(req_id, "read_timeout")]


@pytest.mark.asyncio
async def test_head_timeout_disarms_the_abandoned_head_future() -> None:
    """An abort racing a head timeout must not set an exception nobody retrieves.

    The shielded head future outlives the timed-out request; a disconnect that
    captured the state before cleanup would otherwise set an exception on it
    that no waiter ever consumes.
    """
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    transport = WSTunnelTransport(reg, "r1")

    task = asyncio.create_task(transport.handle_async_request(_timed_request(read=0.05)))
    await asyncio.sleep(0.01)
    session = reg.get("r1")
    assert session is not None
    req_id = next(iter(session.in_flight))
    state = session.in_flight[req_id]

    with pytest.raises(httpx.ReadTimeout, match="did not answer within"):
        await task

    assert state.head_future.cancelled()
    _abort_request_state(state, ConnectionError("tunnel closed"))
    assert state.head_future.cancelled()
