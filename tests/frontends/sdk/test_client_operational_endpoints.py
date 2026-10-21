"""Operational endpoint wrappers on :class:`OmnigentClient`.

Server info, the usage report and the hosts list are plain GETs; each
wrapper must hit its route and hand back the server's data without the
caller reaching into the private HTTP client.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from omnigent_client._client import OmnigentClient
from omnigent_client._errors import OmnigentError

from omnigent.server.schemas import UsageReport


def _mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[OmnigentClient, httpx.AsyncBaseTransport, httpx.MockTransport]:
    """Build an :class:`OmnigentClient` whose transport is *handler*.

    The loopback base URL makes the client ignore ambient proxy settings,
    which would otherwise route requests around the swapped transport.

    :returns: The client, its original transport (restore before closing)
        and the mock transport (close in teardown).
    """
    client = OmnigentClient("http://127.0.0.1")
    original = client._http._transport
    mock_transport = httpx.MockTransport(handler)
    client._http._transport = mock_transport
    return client, original, mock_transport


async def _close(
    client: OmnigentClient,
    original: httpx.AsyncBaseTransport,
    mock_transport: httpx.MockTransport,
) -> None:
    client._http._transport = original
    await client.close()
    await mock_transport.aclose()


@pytest.mark.asyncio
async def test_info_returns_server_info() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        return httpx.Response(
            200,
            json={"server_version": "0.19.0.dev0", "single_user": True, "accounts_enabled": False},
        )

    client, original, mock_transport = _mock_client(handler)
    try:
        info = await client.info()
    finally:
        await _close(client, original, mock_transport)

    assert calls == ["GET /v1/info"]
    assert info == {
        "server_version": "0.19.0.dev0",
        "single_user": True,
        "accounts_enabled": False,
    }


@pytest.mark.asyncio
async def test_usage_returns_usage_report() -> None:
    calls: list[str] = []
    report: dict[str, Any] = {
        "object": "usage_report",
        "cost_today": 0.5,
        "cost_last_7d": 1.25,
        "cost_last_30d": 3.0,
        "total_cost_usd": 3.0,
        "daily_costs": [],
        "sessions": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json=report)

    client, original, mock_transport = _mock_client(handler)
    try:
        usage = await client.usage()
    finally:
        await _close(client, original, mock_transport)

    assert calls == ["GET /v1/usage"]
    assert isinstance(usage, UsageReport)
    assert usage.total_cost_usd == 3.0
    assert usage.cost_today == 0.5
    assert usage.cost_last_7d == 1.25


@pytest.mark.asyncio
async def test_usage_401_raises_omnigent_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            401, json={"error": {"code": "unauthorized", "message": "Authentication required"}}
        )

    client, original, mock_transport = _mock_client(handler)
    try:
        with pytest.raises(OmnigentError) as exc_info:
            await client.usage()
    finally:
        await _close(client, original, mock_transport)

    assert "Authentication required" in str(exc_info.value)


@pytest.mark.asyncio
async def test_usage_malformed_body_raises_omnigent_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"object": "usage_report", "cost_today": "lots"})

    client, original, mock_transport = _mock_client(handler)
    try:
        with pytest.raises(OmnigentError, match="GET /v1/usage returned an unexpected body"):
            await client.usage()
    finally:
        await _close(client, original, mock_transport)


@pytest.mark.asyncio
async def test_list_hosts_returns_hosts() -> None:
    calls: list[str] = []
    host = {
        "host_id": "host_a1",
        "name": "laptop",
        "owner": "local",
        "status": "online",
        "sandbox_provider": None,
        "configured_harnesses": {"claude": True},
        "gateway_inference": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json={"hosts": [host]})

    client, original, mock_transport = _mock_client(handler)
    try:
        hosts = await client.list_hosts()
    finally:
        await _close(client, original, mock_transport)

    assert calls == ["GET /v1/hosts"]
    assert hosts == [host]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="missing-hosts"),
        pytest.param({"hosts": "none"}, id="hosts-not-a-list"),
        pytest.param({"hosts": ["host_a1"]}, id="host-not-an-object"),
    ],
)
async def test_list_hosts_malformed_body_raises_omnigent_error(body: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=body)

    client, original, mock_transport = _mock_client(handler)
    try:
        with pytest.raises(
            OmnigentError, match="GET /v1/hosts returned a malformed 'hosts' array"
        ):
            await client.list_hosts()
    finally:
        await _close(client, original, mock_transport)
