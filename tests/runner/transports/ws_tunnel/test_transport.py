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
    ResponseBodyFrame,
    ResponseEndFrame,
    ResponseHeadFrame,
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
async def test_response_identity_does_not_follow_replacement_connection() -> None:
    registry = TunnelRegistry()
    old = registry.register("runner", _NoopWS(), _hello())
    transport = WSTunnelTransport(registry, "runner")
    task = asyncio.create_task(transport.handle_async_request(_make_request()))
    await asyncio.sleep(0)
    request_id = next(iter(old.in_flight))
    registry.route_response_frame("runner", ResponseHeadFrame(id=request_id, status=200))
    registry.route_response_frame("runner", ResponseEndFrame(id=request_id))
    replacement = registry.register("runner", _NoopWS(), _hello())
    response = await task
    assert response.extensions["omnigent.runner_connection_id"] == old.connection_id
    assert old.connection_id != replacement.connection_id
    assert not transport.is_current_connection(old.connection_id)
    assert transport.is_current_connection(replacement.connection_id)
    await response.aclose()


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


# ── WSTunnelTransport.aclose ────────────────────────────


@pytest.mark.asyncio
async def test_transport_aclose_is_noop() -> None:
    """Transport aclose is a safe no-op."""
    reg = TunnelRegistry()
    transport = WSTunnelTransport(reg, "r1")
    await transport.aclose()  # Should not raise.
