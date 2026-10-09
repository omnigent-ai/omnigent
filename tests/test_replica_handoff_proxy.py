"""The fault relay must preserve real responses and release open streams."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests._helpers.replica_handoff import HandoffProxy


class OpenStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"data: {}\n\n"
        await asyncio.Event().wait()


@pytest.mark.parametrize(
    ("path", "request_body", "status", "response_body"),
    [
        ("/items", b"", 500, b"upstream unavailable"),
        ("/items", b"", 204, b""),
        ("/events", b'{"type":"message"}', 503, b"upstream unavailable"),
        ("/events", b"not json", 400, b"invalid JSON"),
        ("/events", b"[]", 400, b"expected an object"),
    ],
)
async def test_plain_errors_reach_the_client_unchanged(
    path: str, request_body: bytes, status: int, response_body: bytes
) -> None:
    proxy = HandoffProxy.__new__(HandoffProxy)
    proxy.target = "http://upstream"
    proxy.host_routes = {}
    proxy.records = []
    proxy.streams = set()
    proxy.gates = {"history": asyncio.Event()}
    proxy.gates["history"].set()
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": request_body}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    def upstream(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"http://upstream{path}"
        assert request.content == request_body
        return httpx.Response(status, content=response_body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        proxy.client = client
        await proxy._http(
            {"path": path, "method": "POST", "query_string": b"", "headers": []},
            receive,
            send,
        )
    assert messages[0]["status"] == status
    assert messages[1] == {"type": "http.response.body", "body": response_body}


async def test_cancel_before_sse_headers_does_not_send_a_body() -> None:
    proxy = HandoffProxy.__new__(HandoffProxy)
    proxy.target = "http://upstream"
    proxy.host_routes = {}
    proxy.records = []
    proxy.streams = set()
    start_requested = asyncio.Event()
    messages = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            start_requested.set()
            await asyncio.Event().wait()
        messages.append(message)

    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=OpenStream()
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        proxy.client = client
        task = asyncio.create_task(
            proxy._http(
                {"path": "/stream", "method": "GET", "query_string": b"", "headers": []},
                receive,
                send,
            )
        )
        await asyncio.wait_for(start_requested.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert messages == [], "the relay sent an SSE body before its headers"
    assert not proxy.streams


def test_shutdown_finishes_while_the_browser_keeps_its_stream_open(tmp_path: Path) -> None:
    proxy = HandoffProxy("http://upstream", tmp_path / "network.json")

    async def install_upstream() -> None:
        await proxy.client.aclose()
        proxy.client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=OpenStream()
                )
            )
        )

    proxy.loop.run(install_upstream())
    try:
        with httpx.Client(trust_env=False, timeout=2) as browser:
            with browser.stream("GET", f"{proxy.url}/stream") as response:
                chunks = response.iter_raw()
                assert next(chunks) == b"data: {}\n\n"
                proxy.close()
                assert list(chunks) == []
        assert not proxy.connections
    finally:
        if not proxy.task.done():
            with contextlib.suppress(Exception):
                proxy.close()
