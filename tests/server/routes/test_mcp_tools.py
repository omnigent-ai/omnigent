"""The MCP tools and sign-in routes are owner-only and fail fast for older hosts."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.host.frames import (
    CAP_MCP_LOGIN,
    CAP_MCP_TOOLS,
    MCP_LOGIN_MAX_CALLBACK,
    HostHelloFrame,
    HostMcpLoginFrame,
    HostMcpLoginResultFrame,
    HostMcpToolsFrame,
    HostMcpToolsResultFrame,
    decode_host_frame,
)
from omnigent.host.mcp_login import CALLBACK_TIMEOUT_SECONDS, START_TIMEOUT_SECONDS
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes import _host_launch
from omnigent.server.routes import mcp_tools as mcp_tools_routes
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
        (
            HostMcpToolsResultFrame(
                "", "ok", tools=[], connection="needs_auth", harness_sign_in="available"
            ),
            200,
        ),
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
        frame = decode_host_frame((await asyncio.wait_for(conn.outbound_queue.get(), 2)).data)
        assert isinstance(frame, HostMcpToolsFrame)
        assert frame.source_id == "a" * 64
        assert (frame.harness, frame.server, frame.plugin) == ("claude", "odd/server", "toolkit")
        result.request_id = frame.request_id
        conn.pending_mcp_tools[frame.request_id].set_result(result)
        response = await task
    assert response.status_code == status
    if status == 200:
        assert response.json() == {
            "tools": result.tools,
            "connection": result.connection,
            "truncated": False,
            "harness_sign_in": result.harness_sign_in,
        }
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


_LOGIN_URL = f"/v1/hosts/{_HOST_ID}/mcp-servers/login"
_OWNER = {"x-test-user": "owner"}
_PENDING = {
    "login_id": "abc_DEF-123",
    "state": "pending",
    "authorization_url": "https://auth.example.test/authorize?redirect_uri=x",
    "error": None,
}


async def _answer_login(
    conn: HostConnection, result: HostMcpLoginResultFrame
) -> HostMcpLoginFrame:
    frame = decode_host_frame((await asyncio.wait_for(conn.outbound_queue.get(), 2)).data)
    assert isinstance(frame, HostMcpLoginFrame)
    result.request_id = frame.request_id
    conn.pending_mcp_logins[frame.request_id].set_result(result)
    return frame


@pytest.mark.parametrize(
    "method,path,body,expected",
    [
        (
            "POST",
            "",
            {"harness": "claude", "server": "linear", "plugin": "tools", "source_id": "b" * 64},
            {
                "action": "start",
                "harness": "claude",
                "server": "linear",
                "plugin": "tools",
                "source_id": "b" * 64,
            },
        ),
        ("GET", "/abc_DEF-123", None, {"action": "status", "login_id": "abc_DEF-123"}),
        (
            "POST",
            "/abc_DEF-123/callback",
            {"callback_url": "http://localhost:1/callback?code=c"},
            {
                "action": "submit",
                "login_id": "abc_DEF-123",
                "callback_url": "http://localhost:1/callback?code=c",
            },
        ),
        ("DELETE", "/abc_DEF-123", None, {"action": "cancel", "login_id": "abc_DEF-123"}),
    ],
)
async def test_owner_drives_mcp_login(
    tools_app, method: str, path: str, body: dict | None, expected: dict
) -> None:
    app, registry, _ = tools_app
    conn = _register(registry, CAP_MCP_LOGIN)
    async with _client(app) as client:
        task = asyncio.create_task(
            client.request(method, _LOGIN_URL + path, json=body, headers=_OWNER)
        )
        frame = await _answer_login(conn, HostMcpLoginResultFrame("", "ok", **_PENDING))
        response = await task
    assert response.status_code == 200
    assert response.json() == _PENDING
    assert {key: getattr(frame, key) for key in expected} == expected
    assert not conn.pending_mcp_logins


@pytest.mark.parametrize(
    "result,status,detail",
    [
        (
            HostMcpLoginResultFrame("", "not_found", error="Sign-in not found"),
            404,
            "Sign-in not found",
        ),
        (
            HostMcpLoginResultFrame("", "invalid", error="That isn't the address"),
            400,
            "That isn't the address",
        ),
        (HostMcpLoginResultFrame("", "busy", error="Too many sign-ins"), 503, "Too many sign-ins"),
        # Host failure text stays on the host.
        (
            HostMcpLoginResultFrame("", "failed", error="private detail"),
            502,
            "host MCP sign-in failed",
        ),
        (
            HostMcpLoginResultFrame("", "ok", login_id="x", state="bogus", error="private detail"),
            502,
            "host sent a malformed MCP sign-in reply",
        ),
    ],
)
async def test_mcp_login_host_errors(
    tools_app, result: HostMcpLoginResultFrame, status: int, detail: str
) -> None:
    app, registry, _ = tools_app
    conn = _register(registry, CAP_MCP_LOGIN)
    async with _client(app) as client:
        task = asyncio.create_task(client.get(f"{_LOGIN_URL}/abc", headers=_OWNER))
        await _answer_login(conn, result)
        response = await task
    assert (response.status_code, response.json()["detail"]) == (status, detail)


@pytest.mark.parametrize(
    "user,capabilities,method,path,body,status",
    [
        ("stranger", (CAP_MCP_LOGIN,), "GET", "/abc", None, 403),
        (None, (CAP_MCP_LOGIN,), "DELETE", "/abc", None, 401),
        ("owner", (CAP_MCP_TOOLS,), "GET", "/abc", None, 501),
        ("owner", (CAP_MCP_LOGIN,), "GET", "/a.b", None, 422),
        ("owner", (CAP_MCP_LOGIN,), "POST", "", {"harness": "cursor", "server": "x"}, 422),
        (
            "owner",
            (CAP_MCP_LOGIN,),
            "POST",
            "/abc/callback",
            {"callback_url": "x" * (MCP_LOGIN_MAX_CALLBACK + 1)},
            422,
        ),
    ],
)
async def test_mcp_login_requests_rejected_before_the_host(
    tools_app,
    user: str | None,
    capabilities: tuple[str, ...],
    method: str,
    path: str,
    body: dict | None,
    status: int,
) -> None:
    app, registry, _ = tools_app
    conn = _register(registry, *capabilities)
    async with _client(app) as client:
        response = await client.request(
            method,
            _LOGIN_URL + path,
            json=body,
            headers={"x-test-user": user} if user else {},
        )
    assert response.status_code == status
    assert conn.outbound_queue.empty()


# A live host absent from this replica's registry is re-addressed only when sharded.
@pytest.mark.parametrize(("sharded", "status"), [(True, 400), (False, 409)])
async def test_mcp_login_for_a_host_on_another_replica(
    tools_app, monkeypatch: pytest.MonkeyPatch, sharded: bool, status: int
) -> None:
    app, _, _ = tools_app
    monkeypatch.setattr(_host_launch, "_deployment_is_sharded", lambda: sharded)
    async with _client(app) as client:
        response = await client.post(
            _LOGIN_URL, json={"harness": "claude", "server": "docs"}, headers=_OWNER
        )
    assert response.status_code == status


@pytest.mark.parametrize("outcome,status", [("timeout", 504), ("disconnect", 502)])
async def test_unanswered_mcp_login_requests_are_cleaned_up(
    tools_app, monkeypatch: pytest.MonkeyPatch, outcome: str, status: int
) -> None:
    app, registry, _ = tools_app
    conn = _register(registry, CAP_MCP_LOGIN)
    if outcome == "timeout":
        monkeypatch.setattr(mcp_tools_routes, "_MCP_LOGIN_TIMEOUT_S", 0.01)
    async with _client(app) as client:
        task = asyncio.create_task(client.get(f"{_LOGIN_URL}/abc", headers=_OWNER))
        await asyncio.wait_for(conn.outbound_queue.get(), 2)
        if outcome == "disconnect":
            registry.deregister(conn.host_id)
        response = await asyncio.wait_for(task, 2)
    assert response.status_code == status
    assert conn.pending_mcp_logins == {}


def test_server_outwaits_host_sign_in_steps() -> None:
    # A host that gives up on a start still stops its CLI (up to 2s) before replying.
    assert mcp_tools_routes._MCP_LOGIN_START_TIMEOUT_S >= START_TIMEOUT_SECONDS + 5
    # A submit's connect, request and reply may each take the whole callback timeout.
    assert mcp_tools_routes._MCP_LOGIN_SUBMIT_TIMEOUT_S >= 3 * CALLBACK_TIMEOUT_SECONDS + 5
