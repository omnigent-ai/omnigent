"""The host MCP inventory route is owner-only and fails fast for older hosts."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.host.frames import (
    CAP_MCP_INVENTORY,
    HostHelloFrame,
    HostMcpServersFrame,
    HostMcpServersResultFrame,
    decode_host_frame,
)
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes.mcp_servers import (
    create_mcp_servers_router,
    request_host_mcp_servers,
)
from omnigent.stores.host_store import HostStore

_HOST_ID = "a828988dc0b441fb8d04dad3761773b9"
_GITHUB = {"name": "github", "harness": "claude", "transport": "stdio", "scope": "user"}


class _Auth:
    def get_user_id(self, request: Request) -> str | None:
        return request.headers.get("x-test-user")


def _register(registry: HostRegistry, *capabilities: str) -> HostConnection:
    return registry.register(
        _HOST_ID,
        AsyncMock(),
        HostHelloFrame(
            version="test",
            frame_protocol_version=1,
            name="laptop",
            capabilities=list(capabilities),
        ),
        owner="owner",
    )


@pytest.fixture
def mcp_app(db_uri: str) -> tuple[FastAPI, HostRegistry, HostStore]:
    registry = HostRegistry()
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(_HOST_ID, "laptop", "owner")
    app = FastAPI()
    app.include_router(
        create_mcp_servers_router(registry, hosts, auth_provider=_Auth()), prefix="/v1"
    )

    @app.exception_handler(OmnigentError)
    async def handle_error(request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"detail": exc.message})

    return app, registry, hosts


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.parametrize(
    "result,status",
    [
        (HostMcpServersResultFrame("", "ok", mcp_servers=[_GITHUB]), 200),
        (HostMcpServersResultFrame("", "failed", error="MCP inventory failed"), 502),
    ],
)
async def test_owner_gets_host_inventory(
    mcp_app, result: HostMcpServersResultFrame, status: int
) -> None:
    app, registry, _ = mcp_app
    conn = _register(registry, CAP_MCP_INVENTORY)
    async with _client(app) as client:
        task = asyncio.create_task(
            client.get(f"/v1/hosts/{_HOST_ID}/mcp-servers", headers={"x-test-user": "owner"})
        )
        frame = decode_host_frame(await asyncio.wait_for(conn.outbound_queue.get(), 2))
        assert isinstance(frame, HostMcpServersFrame)
        result.request_id = frame.request_id
        conn.pending_mcp_servers[frame.request_id].set_result(result)
        response = await task
    assert response.status_code == status
    if status == 200:
        assert response.json() == {"mcp_servers": [{**_GITHUB, "plugin": None, "url_host": None}]}
    assert not conn.pending_mcp_servers


@pytest.mark.parametrize("user,status", [("stranger", 403), (None, 401)])
async def test_non_owner_does_not_reach_the_host(mcp_app, user: str | None, status: int) -> None:
    app, registry, _ = mcp_app
    conn = _register(registry, CAP_MCP_INVENTORY)
    async with _client(app) as client:
        response = await client.get(
            f"/v1/hosts/{_HOST_ID}/mcp-servers",
            headers={"x-test-user": user} if user else {},
        )
    assert response.status_code == status
    assert conn.outbound_queue.empty()


async def test_unknown_host_is_not_found(mcp_app) -> None:
    app, _, _ = mcp_app
    async with _client(app) as client:
        response = await client.get(
            "/v1/hosts/0000000000000000000000000000beef/mcp-servers",
            headers={"x-test-user": "owner"},
        )
    assert response.status_code == 404


async def test_offline_host_is_a_conflict(mcp_app) -> None:
    app, _, hosts = mcp_app
    hosts.set_offline(_HOST_ID)
    async with _client(app) as client:
        response = await client.get(
            f"/v1/hosts/{_HOST_ID}/mcp-servers", headers={"x-test-user": "owner"}
        )
    assert response.status_code == 409


async def test_older_host_fails_fast_without_a_frame(mcp_app) -> None:
    app, registry, _ = mcp_app
    conn = _register(registry)
    async with _client(app) as client:
        response = await client.get(
            f"/v1/hosts/{_HOST_ID}/mcp-servers", headers={"x-test-user": "owner"}
        )
    assert response.status_code == 501
    assert "update the host" in response.json()["detail"]
    assert conn.outbound_queue.empty()


@pytest.mark.parametrize("outcome,status", [("timeout", 504), ("replaced", 502)])
async def test_proxy_cleans_up_unanswered_requests(
    monkeypatch: pytest.MonkeyPatch, outcome: str, status: int
) -> None:
    registry = HostRegistry()
    conn = _register(registry, CAP_MCP_INVENTORY)
    if outcome == "timeout":
        monkeypatch.setattr("omnigent.server.routes.mcp_servers._MCP_SERVERS_TIMEOUT_S", 0.01)
    else:
        registry.deregister(conn.host_id)
    with pytest.raises(HTTPException) as exc_info:
        await request_host_mcp_servers(host_registry=registry, host_conn=conn)
    assert exc_info.value.status_code == status
    assert conn.pending_mcp_servers == {}
