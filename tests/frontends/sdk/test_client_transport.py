"""A caller-supplied transport carries every :class:`OmnigentClient` request."""

from __future__ import annotations

import httpx
import pytest
from omnigent_client._client import OmnigentClient
from omnigent_client._timeouts import _SSE_TIMEOUT


@pytest.mark.asyncio
async def test_requests_and_streams_go_through_the_callers_transport() -> None:
    seen: list[tuple[str, float | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.extensions["timeout"]["read"]))
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, content=b"data: [DONE]\n\n")

    async with OmnigentClient(
        "http://example.invalid", transport=httpx.MockTransport(handler)
    ) as client:
        assert (await client._http.get("http://example.invalid/health")).status_code == 200
        events = [event async for event in client.responses.stream(model="agent", input="hi")]

    assert events == []
    # The stream keeps its long read timeout when a caller supplies the transport.
    assert seen == [("/health", 30.0), ("/v1/responses", _SSE_TIMEOUT.read)]
