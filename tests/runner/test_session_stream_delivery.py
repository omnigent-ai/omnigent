"""Interrupted runner streams preserve event order across their replacement."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.requests import ClientDisconnect
from starlette.responses import StreamingResponse

from omnigent.runner import create_runner_app
from tests.debug_log_helpers import capture_debug_rows

_SESSION = "synthetic-stream-order"
_HEARTBEAT = b'data: {"type": "session.heartbeat"}\n\n'


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(404))
    ) as client:
        app = create_runner_app(server_client=client)
        app.state.session_event_queues.pop(_SESSION, None)
        try:
            yield app
        finally:
            app.state.session_event_queues.pop(_SESSION, None)


async def _response(app: FastAPI) -> StreamingResponse:
    route = next(
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path == "/v1/sessions/{session_id}/stream"
    )
    return await route.endpoint(_SESSION)


def _decode(frame: bytes) -> dict[str, Any]:
    return json.loads(frame.removeprefix(b"data: ").strip())


async def test_interrupted_event_returns_before_newer_status(app: FastAPI) -> None:
    response = await _response(app)
    stream = response.body_iterator
    assert await anext(stream) == _HEARTBEAT
    queue = app.state.session_event_queues[_SESSION]
    running = {"type": "session.status", "status": "running"}
    idle = {"type": "session.status", "status": "idle"}
    for event in (running, idle, None):
        queue.put_nowait(event)
    assert _decode(await anext(stream)) == running
    with capture_debug_rows("runner") as rows:
        await stream.aclose()
    (restored,) = [row for row in rows if row["event_name"] == "runner_stream_event_requeued"]
    assert restored["session_id"] == _SESSION
    assert restored["attributes"]["event_type"] == "session.status"
    assert restored["attributes"]["queue_depth"] == "3"
    assert restored["attributes"]["delivery_state"] == "unconfirmed"

    replacement = (await _response(app)).body_iterator
    try:
        assert await anext(replacement) == _HEARTBEAT
        assert _decode(await anext(replacement)) == running
        assert _decode(await anext(replacement)) == idle
        assert await anext(replacement) == b"data: [DONE]\n\n"
        with pytest.raises(StopAsyncIteration):
            await anext(replacement)
        assert queue.empty()
        await asyncio.wait_for(queue.join(), timeout=1.0)
    finally:
        await replacement.aclose()


async def test_interrupted_done_is_available_to_replacement(app: FastAPI) -> None:
    stream = (await _response(app)).body_iterator
    assert await anext(stream) == _HEARTBEAT
    queue = app.state.session_event_queues[_SESSION]
    queue.put_nowait(None)
    assert await anext(stream) == b"data: [DONE]\n\n"
    await stream.aclose()
    assert queue.qsize() == 1
    assert queue.get_nowait() is None


async def test_replacement_waits_for_old_reader_to_restore_its_item(app: FastAPI) -> None:
    old = (await _response(app)).body_iterator
    assert await anext(old) == _HEARTBEAT
    queue = app.state.session_event_queues[_SESSION]
    running = {"type": "session.status", "status": "running"}
    idle = {"type": "session.status", "status": "idle"}
    queue.put_nowait(running)
    queue.put_nowait(idle)
    queue.put_nowait(None)
    assert _decode(await anext(old)) == running
    new = (await _response(app)).body_iterator
    ready = asyncio.create_task(anext(new))
    try:
        await asyncio.sleep(0)
        assert not ready.done()
        await old.aclose()
        assert await asyncio.wait_for(ready, timeout=1.0) == _HEARTBEAT
        assert _decode(await anext(new)) == running
        assert _decode(await anext(new)) == idle
        assert await anext(new) == b"data: [DONE]\n\n"
        with pytest.raises(StopAsyncIteration):
            await anext(new)
        await asyncio.wait_for(queue.join(), timeout=1.0)
    finally:
        ready.cancel()
        await asyncio.gather(ready, return_exceptions=True)
        await old.aclose()
        await new.aclose()


async def test_cancelled_replacement_does_not_steal_old_readers_events(app: FastAPI) -> None:
    old = (await _response(app)).body_iterator
    assert await anext(old) == _HEARTBEAT
    queue = app.state.session_event_queues[_SESSION]
    event = {"type": "session.status", "status": "idle"}
    queue.put_nowait(event)
    waiting = (await _response(app)).body_iterator
    ready = asyncio.create_task(anext(waiting))
    try:
        await asyncio.sleep(0)
        assert not ready.done()
        ready.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ready
        assert _decode(await anext(old)) == event
    finally:
        ready.cancel()
        await asyncio.gather(ready, return_exceptions=True)
        await waiting.aclose()
        await old.aclose()


@pytest.mark.parametrize("spec_version", ["2.0", "2.4"])
@pytest.mark.parametrize("terminal", [False, True])
async def test_response_closes_generator_before_reconnect(
    app: FastAPI, spec_version: str, terminal: bool
) -> None:
    response = await _response(app)
    interrupted = asyncio.Event()
    queued = {"type": "session.status", "status": "running"}

    async def receive() -> dict[str, Any]:
        await interrupted.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        body = message.get("body")
        if body == _HEARTBEAT:
            queue = app.state.session_event_queues[_SESSION]
            queue.put_nowait(None if terminal else queued)
            return
        if not body:
            return
        interrupted.set()
        if spec_version == "2.4":
            raise OSError("synthetic stream disconnect")
        await asyncio.Event().wait()

    scope = {"type": "http", "asgi": {"spec_version": spec_version}}
    if spec_version == "2.4":
        with pytest.raises(ClientDisconnect):
            await asyncio.wait_for(response(scope, receive, send), timeout=2.0)
    else:
        await asyncio.wait_for(response(scope, receive, send), timeout=2.0)

    # Keep the response alive: recovery must not depend on generator GC.
    queue = app.state.session_event_queues[_SESSION]
    assert queue.qsize() == 1
    assert queue.get_nowait() == (None if terminal else queued)
    await response.body_iterator.aclose()
