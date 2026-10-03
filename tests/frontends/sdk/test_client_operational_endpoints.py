"""Operational endpoints exposed on :class:`OmnigentClient` itself.

Server-version checks, spend accounting, and host selection need
``GET /v1/info``, ``GET /v1/usage``, and ``GET /v1/hosts``. These tests pin
that each has a public client method returning the server's payload, so
consumers never have to reach into the HTTP client. Mocks at the transport
boundary like the other SDK tests.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from omnigent_client import OmnigentClient, OmnigentError

from omnigent.server.schemas import UsageReport

# Loopback base so OmnigentClient sets trust_env=False and every request
# routes through the swapped-in MockTransport.
_BASE = "http://127.0.0.1:9"

_INFO_BODY: dict[str, Any] = {
    "accounts_enabled": False,
    "single_user": True,
    "login_url": None,
    "sharing_mode": "on",
    "server_version": "0.17.0.dev0",
    "features": {"usage_page": False},
}

_HOSTS_BODY: dict[str, Any] = {
    "hosts": [
        {
            "host_id": "host_a1b2",
            "name": "laptop",
            "owner": "local",
            "status": "online",
            "sandbox_provider": None,
            "configured_harnesses": {"claude": True},
            "gateway_inference": None,
        }
    ]
}


def _recording_handler(
    seen: dict[str, str], body: dict[str, Any]
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(200, json=body)

    return handler


@pytest.mark.asyncio
async def test_info_returns_server_info() -> None:
    seen: dict[str, str] = {}
    async with OmnigentClient(base_url=_BASE) as client:
        client._http._transport = httpx.MockTransport(_recording_handler(seen, _INFO_BODY))
        info = await client.info()

    assert seen == {"method": "GET", "url": f"{_BASE}/v1/info"}
    assert info["server_version"] == "0.17.0.dev0"
    assert info["single_user"] is True


@pytest.mark.asyncio
async def test_usage_returns_usage_report() -> None:
    seen: dict[str, str] = {}
    body = UsageReport(
        cost_today=0.5, cost_last_7d=1.5, cost_last_30d=2.5, total_cost_usd=2.5
    ).model_dump(mode="json")
    async with OmnigentClient(base_url=_BASE) as client:
        client._http._transport = httpx.MockTransport(_recording_handler(seen, body))
        report = await client.usage()

    assert seen == {"method": "GET", "url": f"{_BASE}/v1/usage"}
    assert isinstance(report, UsageReport)
    assert report.total_cost_usd == 2.5
    assert report.cost_today == 0.5


@pytest.mark.asyncio
async def test_list_hosts_returns_hosts() -> None:
    seen: dict[str, str] = {}
    async with OmnigentClient(base_url=_BASE) as client:
        client._http._transport = httpx.MockTransport(_recording_handler(seen, _HOSTS_BODY))
        hosts = await client.list_hosts()

    assert seen == {"method": "GET", "url": f"{_BASE}/v1/hosts"}
    assert [host["host_id"] for host in hosts] == ["host_a1b2"]
    assert hosts[0]["status"] == "online"


@pytest.mark.asyncio
async def test_usage_malformed_body_raises_omnigent_error() -> None:
    body = {"object": "usage_report", "cost_today": "not-a-number"}
    async with OmnigentClient(base_url=_BASE) as client:
        client._http._transport = httpx.MockTransport(_recording_handler({}, body))
        with pytest.raises(OmnigentError) as exc_info:
            await client.usage()

    assert exc_info.value.status_code == 200
    assert "GET /v1/usage" in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [{}, {"hosts": "laptop"}, {"hosts": [{"host_id": "host_a1b2"}, "oops"]}],
)
async def test_list_hosts_malformed_body_raises_omnigent_error(body: dict[str, Any]) -> None:
    async with OmnigentClient(base_url=_BASE) as client:
        client._http._transport = httpx.MockTransport(_recording_handler({}, body))
        with pytest.raises(OmnigentError) as exc_info:
            await client.list_hosts()

    assert exc_info.value.status_code == 200
    assert "GET /v1/hosts" in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["info", "usage", "list_hosts"])
async def test_operational_endpoints_propagate_server_errors(method_name: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            401, json={"error": {"code": "unauthorized", "message": "login required"}}
        )

    async with OmnigentClient(base_url=_BASE) as client:
        client._http._transport = httpx.MockTransport(handler)
        with pytest.raises(OmnigentError) as exc_info:
            await getattr(client, method_name)()

    assert exc_info.value.status_code == 401
    assert str(exc_info.value) == "login required"
