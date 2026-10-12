"""The MCP tools route is owner-only and fails fast for older hosts."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.host.frames import (
    CAP_MCP_TOOLS,
    HostHelloFrame,
    HostMcpToolsFrame,
    HostMcpToolsResultFrame,
    decode_host_frame,
)
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes.mcp_tools import (
    create_mcp_tools_router,
    request_host_mcp_tools,
)
from omnigent.stores.host_store import HostStore

_HOST_ID = "a828988dc0b441fb8d04dad3761773b9"
_URL = f"/v1/hosts/{_HOST_ID}/mcp-servers/tools"
_REQUEST = {"harness": "claude", "server": "odd/server", "plugin": "toolkit"}
_TOOLS = {
    "tools": [{"name": "read", "description": "Read docs"}],
    "connection": "connected",
    "truncated": False,
}


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
def tools_app(db_uri: str) -> tuple[FastAPI, HostRegistry, HostStore]:
    registry = HostRegistry()
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(_HOST_ID, "laptop", "owner")
    app = FastAPI()
    app.include_router(
        create_mcp_tools_router(registry, hosts, auth_provider=_Auth()), prefix="/v1"
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
        (HostMcpToolsResultFrame("", "ok", **_TOOLS), 200),
        (HostMcpToolsResultFrame("", "failed", error="lookup failed"), 502),
        (HostMcpToolsResultFrame("", "busy"), 503),
        # A malformed connection enum is a host failure.
        (
            HostMcpToolsResultFrame("", "ok", **{**_TOOLS, "connection": "bogus"}),
            502,
        ),
    ],
)
async def test_owner_gets_mcp_tools(
    tools_app, result: HostMcpToolsResultFrame, status: int
) -> None:
    app, registry, _ = tools_app
    conn = _register(registry, CAP_MCP_TOOLS)
    async with _client(app) as client:
        task = asyncio.create_task(
            client.post(
                _URL, json={**_REQUEST, "source_id": "a" * 64}, headers={"x-test-user": "owner"}
            )
        )
        frame = decode_host_frame(await asyncio.wait_for(conn.outbound_queue.get(), 2))
        assert isinstance(frame, HostMcpToolsFrame)
        assert frame.source_id == "a" * 64
        assert (frame.harness, frame.server, frame.plugin) == ("claude", "odd/server", "toolkit")
        result.request_id = frame.request_id
        conn.pending_mcp_tools[frame.request_id].set_result(result)
        response = await task
    assert response.status_code == status
    if status == 200:
        assert response.json() == _TOOLS
    assert not conn.pending_mcp_tools


@pytest.mark.parametrize("user,status", [("stranger", 403), (None, 401)])
async def test_non_owner_does_not_reach_the_host(tools_app, user: str | None, status: int) -> None:
    app, registry, _ = tools_app
    conn = _register(registry, CAP_MCP_TOOLS)
    async with _client(app) as client:
        response = await client.post(
            _URL, json=_REQUEST, headers={"x-test-user": user} if user else {}
        )
    assert response.status_code == status
    assert conn.outbound_queue.empty()


@pytest.mark.parametrize("harness", ["pi-native", "antigravity-native", "opencode-native"])
async def test_unsupported_harness_does_not_reach_the_host(tools_app, harness: str) -> None:
    app, registry, _ = tools_app
    conn = _register(registry, CAP_MCP_TOOLS)
    async with _client(app) as client:
        response = await client.post(
            _URL, json={**_REQUEST, "harness": harness}, headers={"x-test-user": "owner"}
        )
    assert response.status_code == 422
    assert conn.outbound_queue.empty()


async def test_offline_host_is_a_conflict(tools_app) -> None:
    app, _, hosts = tools_app
    hosts.set_offline(_HOST_ID)
    async with _client(app) as client:
        response = await client.post(_URL, json=_REQUEST, headers={"x-test-user": "owner"})
    assert response.status_code == 409


async def test_older_host_fails_fast_without_a_frame(tools_app) -> None:
    app, registry, _ = tools_app
    conn = _register(registry)
    async with _client(app) as client:
        response = await client.post(_URL, json=_REQUEST, headers={"x-test-user": "owner"})
    assert response.status_code == 501
    assert "update the host" in response.json()["detail"]
    assert conn.outbound_queue.empty()


@pytest.mark.parametrize("outcome,status", [("timeout", 504), ("stale", 502)])
async def test_proxy_cleans_up_unanswered_requests(
    monkeypatch: pytest.MonkeyPatch, outcome: str, status: int
) -> None:
    registry = HostRegistry()
    conn = _register(registry, CAP_MCP_TOOLS)
    if outcome == "timeout":
        monkeypatch.setattr("omnigent.server.routes.mcp_tools._MCP_TOOLS_TIMEOUT_S", 0.01)
    else:
        registry.deregister(conn.host_id)
    with pytest.raises(HTTPException) as exc_info:
        await request_host_mcp_tools(
            host_registry=registry, host_conn=conn, harness="claude", server="docs"
        )
    assert exc_info.value.status_code == status
    assert conn.pending_mcp_tools == {}


async def test_replacing_the_connection_after_sending_fails_fast() -> None:
    """A host reconnecting mid-request fails the old request (502) at once."""
    registry = HostRegistry()
    conn = _register(registry, CAP_MCP_TOOLS)
    task = asyncio.create_task(
        request_host_mcp_tools(
            host_registry=registry, host_conn=conn, harness="claude", server="docs"
        )
    )
    await asyncio.wait_for(conn.outbound_queue.get(), 2)  # the request went out
    _register(registry, CAP_MCP_TOOLS)  # the same host reconnects
    with pytest.raises(HTTPException) as exc_info:
        await asyncio.wait_for(task, 2)
    assert exc_info.value.status_code == 502
    assert conn.pending_mcp_tools == {}


async def test_disconnect_after_sending_fails_fast() -> None:
    """A host dropping mid-request is a lost connection (502) now, not a 504 later."""
    registry = HostRegistry()
    conn = _register(registry, CAP_MCP_TOOLS)
    task = asyncio.create_task(
        request_host_mcp_tools(
            host_registry=registry, host_conn=conn, harness="claude", server="docs"
        )
    )
    await asyncio.wait_for(conn.outbound_queue.get(), 2)  # the request went out
    registry.deregister(conn.host_id)
    with pytest.raises(HTTPException) as exc_info:
        await asyncio.wait_for(task, 2)  # well under the 15s timeout
    assert exc_info.value.status_code == 502
    assert conn.pending_mcp_tools == {}
