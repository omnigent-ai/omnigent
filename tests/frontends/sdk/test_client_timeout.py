"""Timeout configuration tests for :class:`OmnigentClient`."""

from __future__ import annotations

import httpx
import pytest
from omnigent_client._client import OmnigentClient
from omnigent_client._timeouts import _SSE_TIMEOUT


@pytest.mark.asyncio
async def test_client_timeout_applies_to_regular_requests_and_preserves_sse_timeout() -> None:
    captured: list[dict[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.extensions["timeout"])
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, content=b"data: [DONE]\n\n")

    client = OmnigentClient("http://example.invalid", timeout=0.1)
    original_transport = client._http._transport
    mock_transport = httpx.MockTransport(handler)
    client._http._transport = mock_transport
    try:
        response = await client._http.get("http://example.invalid/health")
        assert response.status_code == 200

        events = [event async for event in client.responses.stream(model="agent", input="hello")]
    finally:
        client._http._transport = original_transport
        await client.close()
        await mock_transport.aclose()

    assert events == []
    assert captured == [
        {"connect": 0.1, "read": 0.1, "write": 0.1, "pool": 0.1},
        {
            "connect": _SSE_TIMEOUT.connect,
            "read": _SSE_TIMEOUT.read,
            "write": _SSE_TIMEOUT.write,
            "pool": _SSE_TIMEOUT.pool,
        },
    ]


@pytest.mark.asyncio
async def test_client_timeout_defaults_to_thirty_seconds() -> None:
    client = OmnigentClient("http://example.invalid")
    try:
        assert client._http.timeout == httpx.Timeout(30.0)
    finally:
        await client.close()
