"""A stalled server-side consumer must bound how much tunnel body the server buffers.
Real sockets end to end (uvicorn tunnel route, ``serve_tunnel`` runner,
``WSTunnelTransport`` consumer) so only TCP and the protocol hold the runner back."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import os
import time
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from omnigent.runner.transports.ws_tunnel.frames import (
    RESPONSE_BODY_FRAME_MAX_BYTES,
    RESPONSE_FLOW_WINDOW_FRAMES,
)
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.runner.transports.ws_tunnel.serve import serve_tunnel
from omnigent.runner.transports.ws_tunnel.transport import WSTunnelTransport
from omnigent.server.routes.runner_tunnel import create_runner_tunnel_router
from omnigent.util.tunnel_limits import uvicorn_tunnel_kwargs

_RUNNER_ID = "runner-backpressure-test"
_CHUNK = 64 * 1024
# 64 MiB: far more than loopback socket buffers can absorb on the runner's behalf.
_CHUNKS = 1024
# Frames the server may hold for one request while its consumer is stalled.
# The consumer here drains only one frame (< a credit batch), so no credit is
# returned and the runner cannot send past its initial send window.
_MAX_QUEUED_FRAMES = RESPONSE_FLOW_WINDOW_FRAMES


async def _start_tunnel_server(
    registry: TunnelRegistry,
) -> tuple[uvicorn.Server, asyncio.Task[None], int]:
    app = FastAPI()
    app.state.tunnel_registry = registry
    app.include_router(create_runner_tunnel_router(registry), prefix="/v1")
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning", **uvicorn_tunnel_kwargs()
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(), name="tunnel-uvicorn")
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, task, port


def _runner_app(produced: list[int]) -> FastAPI:
    app = FastAPI()

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def chunks() -> AsyncIterator[bytes]:
            for _ in range(_CHUNKS):
                yield os.urandom(_CHUNK)
                produced[0] += 1

        return StreamingResponse(chunks(), media_type="application/octet-stream")

    return app


async def _wait_until(predicate: Callable[[], bool], timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting for the tunnel"
        await asyncio.sleep(0.02)


async def _settle(
    sample: Callable[[], tuple[int, int]], *, quiet_s: float, timeout_s: float
) -> None:
    """Return once ``sample`` has stopped changing for ``quiet_s`` seconds."""
    deadline = time.monotonic() + timeout_s
    last = sample()
    changed_at = time.monotonic()
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        current = sample()
        if current != last:
            last, changed_at = current, time.monotonic()
        elif time.monotonic() - changed_at >= quiet_s:
            return
    # Still changing at the deadline: fail loudly rather than let the caller
    # sample a growing queue and conclude production had stopped.
    raise AssertionError(f"sample did not settle within {timeout_s}s (last={last})")


@pytest.mark.asyncio
async def test_stalled_consumer_bounds_tunnel_body_buffering() -> None:
    """While the consumer stalls, the server buffers a bounded tail and the runner waits."""
    registry = TunnelRegistry()
    server, server_task, port = await _start_tunnel_server(registry)
    produced = [0]
    shutdown = asyncio.Event()
    runner_task = asyncio.create_task(
        serve_tunnel(
            _runner_app(produced),
            server_url=f"http://127.0.0.1:{port}",
            runner_id=_RUNNER_ID,
            runner_version="0.1.0-test",
            shutdown_event=shutdown,
        ),
        name="tunnel-runner",
    )
    try:
        await _wait_until(lambda: registry.get(_RUNNER_ID) is not None, timeout_s=10)
        client = httpx.AsyncClient(
            transport=WSTunnelTransport(registry, _RUNNER_ID), base_url="http://runner"
        )
        async with client, client.stream("GET", "/stream") as response:
            assert response.status_code == 200
            body = response.aiter_raw()
            first = await anext(body)
            assert len(first) == _CHUNK

            session = registry.get(_RUNNER_ID)
            assert session is not None
            (state,) = session.in_flight.values()
            # The consumer now stalls; give the runner time to stream everything
            # it is allowed to.
            await _settle(
                lambda: (state.body_queue.qsize(), produced[0]), quiet_s=1.5, timeout_s=30
            )
            queued = state.body_queue.qsize()
            produced_while_stalled = produced[0]

            assert queued <= _MAX_QUEUED_FRAMES, (
                f"server queued {queued} of {_CHUNKS} body frames for one request while its "
                f"consumer was stalled (runner had produced {produced_while_stalled})"
            )
            assert produced_while_stalled < _CHUNKS, (
                "runner streamed the entire body while the consumer was stalled: nothing paused it"
            )

            total = len(first)
            async for chunk in body:
                total += len(chunk)
            assert total == _CHUNK * _CHUNKS
    finally:
        shutdown.set()
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(runner_task, timeout=10)
        if not runner_task.done():
            runner_task.cancel()
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(server_task, timeout=10)


_OVERSIZED_FRAME_COUNT = RESPONSE_FLOW_WINDOW_FRAMES + 8


def _oversized_chunk_runner_app() -> FastAPI:
    app = FastAPI()

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def chunks() -> AsyncIterator[bytes]:
            # One ASGI chunk far larger than the whole send window; the server
            # must fragment it into window-sized frames, not buffer it whole.
            yield os.urandom(_OVERSIZED_FRAME_COUNT * RESPONSE_BODY_FRAME_MAX_BYTES)

        return StreamingResponse(chunks(), media_type="application/octet-stream")

    return app


@pytest.mark.asyncio
async def test_oversized_single_chunk_is_fragmented_and_bounded() -> None:
    """A single ASGI chunk larger than the window is fragmented and still bounded.
    Flow control acts per response frame, so one huge yield cannot buffer past the
    send window on the server while the consumer stalls."""
    registry = TunnelRegistry()
    server, server_task, port = await _start_tunnel_server(registry)
    shutdown = asyncio.Event()
    runner_task = asyncio.create_task(
        serve_tunnel(
            _oversized_chunk_runner_app(),
            server_url=f"http://127.0.0.1:{port}",
            runner_id=_RUNNER_ID,
            runner_version="0.1.0-test",
            shutdown_event=shutdown,
        ),
        name="tunnel-runner",
    )
    try:
        await _wait_until(lambda: registry.get(_RUNNER_ID) is not None, timeout_s=10)
        client = httpx.AsyncClient(
            transport=WSTunnelTransport(registry, _RUNNER_ID), base_url="http://runner"
        )
        async with client, client.stream("GET", "/stream") as response:
            assert response.status_code == 200
            body = response.aiter_raw()
            first = await anext(body)
            assert len(first) == RESPONSE_BODY_FRAME_MAX_BYTES

            session = registry.get(_RUNNER_ID)
            assert session is not None
            (state,) = session.in_flight.values()
            # Let the runner stream everything its initial window allows.
            await _settle(lambda: (state.body_queue.qsize(), 0), quiet_s=1.5, timeout_s=30)
            queued = state.body_queue.qsize()

            assert queued <= _MAX_QUEUED_FRAMES, (
                f"server queued {queued} fragments from one oversized chunk while its "
                f"consumer was stalled"
            )
            # The whole chunk did not land in the queue — per-frame flow control
            # paused the runner mid-chunk instead of buffering it all.
            assert queued < _OVERSIZED_FRAME_COUNT, (
                "server fragmented and buffered the entire oversized chunk: nothing paused it"
            )

            total = len(first)
            async for chunk in body:
                total += len(chunk)
            assert total == _OVERSIZED_FRAME_COUNT * RESPONSE_BODY_FRAME_MAX_BYTES
    finally:
        shutdown.set()
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(runner_task, timeout=10)
        if not runner_task.done():
            runner_task.cancel()
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(server_task, timeout=10)


def _closing_runner_app(produced: list[int], closed: asyncio.Event) -> FastAPI:
    app = FastAPI()

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def chunks() -> AsyncIterator[bytes]:
            try:
                for _ in range(_CHUNKS):
                    yield os.urandom(_CHUNK)
                    produced[0] += 1
            finally:
                closed.set()

        return StreamingResponse(chunks(), media_type="application/octet-stream")

    return app


@pytest.mark.asyncio
async def test_abandoned_stream_cancels_runner_and_runs_upstream_finally() -> None:
    """Abandoning a credit-starved stream cancels the runner so its body unwinds.
    A consumer that disconnects while the runner is parked on an exhausted send
    window must still make the runner's dispatch cancel, so the upstream iterator
    is finalized rather than leaked with its request stuck in flight forever."""
    registry = TunnelRegistry()
    server, server_task, port = await _start_tunnel_server(registry)
    produced = [0]
    closed = asyncio.Event()
    shutdown = asyncio.Event()
    runner_task = asyncio.create_task(
        serve_tunnel(
            _closing_runner_app(produced, closed),
            server_url=f"http://127.0.0.1:{port}",
            runner_id=_RUNNER_ID,
            runner_version="0.1.0-test",
            shutdown_event=shutdown,
        ),
        name="tunnel-runner",
    )
    try:
        await _wait_until(lambda: registry.get(_RUNNER_ID) is not None, timeout_s=10)
        client = httpx.AsyncClient(
            transport=WSTunnelTransport(registry, _RUNNER_ID), base_url="http://runner"
        )
        async with client:
            async with client.stream("GET", "/stream") as response:
                assert response.status_code == 200
                body = response.aiter_raw()
                assert len(await anext(body)) == _CHUNK

                session = registry.get(_RUNNER_ID)
                assert session is not None
                (state,) = session.in_flight.values()
                await _settle(
                    lambda: (state.body_queue.qsize(), produced[0]), quiet_s=1.5, timeout_s=30
                )
                assert produced[0] < _CHUNKS, "runner finished; it never stalled on credit"
                assert not closed.is_set()
                # Abandon mid-stream: exiting the context closes the response,
                # which must send a request.cancel to the stalled runner.

            # The cancel frame crosses the socket and cancels the runner dispatch;
            # the orphaned upstream generator is then finalized (GC asyncgen hook),
            # which is what runs its ``finally``.
            deadline = time.monotonic() + 10
            while not closed.is_set():
                assert time.monotonic() < deadline, "runner upstream body finally never ran"
                gc.collect()
                await asyncio.sleep(0.05)
            assert state.req_id not in session.in_flight
    finally:
        shutdown.set()
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(runner_task, timeout=10)
        if not runner_task.done():
            runner_task.cancel()
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(server_task, timeout=10)
