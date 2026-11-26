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
    CAP_MCP_SERVER_ENABLED,
    HostHelloFrame,
    HostMcpServersFrame,
    HostMcpServersResultFrame,
    HostSetMcpServerEnabledFrame,
    HostSetMcpServerEnabledResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes.mcp_servers import (
    McpServerEnabledRequest,
    create_mcp_servers_router,
    request_host_mcp_servers,
    request_host_set_mcp_server_enabled,
)
from omnigent.stores.host_store import HostStore
from tests.server.helpers import sent_to_host

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
    app.state.mcp_enabled = False
    app.include_router(
        create_mcp_servers_router(
            registry,
            hosts,
            auth_provider=_Auth(),
            mcp_disabled_servers=lambda: app.state.mcp_enabled,
            feature_flags=FeatureFlags(
                frozenset(
                    {
                        Feature.MCP_DISABLED_SERVERS_CLAUDE,
                        Feature.MCP_DISABLED_SERVERS_CODEX,
                    }
                )
            ),
        ),
        prefix="/v1",
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
        frame = await sent_to_host(conn, task)
        assert isinstance(frame, HostMcpServersFrame)
        result.request_id = frame.request_id
        conn.pending_mcp_servers[frame.request_id].set_result(result)
        response = await task
    assert response.status_code == status
    if status == 200:
        assert response.json() == {
            "mcp_servers": [
                {
                    **_GITHUB,
                    "plugin": None,
                    "url_host": None,
                    "source_id": None,
                    "auth_status": None,
                    "sign_in_url": None,
                    "managed": False,
                    "enabled": None,
                    "locked": False,
                }
            ]
        }
    assert not conn.pending_mcp_servers


async def test_account_state_reaches_the_caller_through_the_tunnel(mcp_app) -> None:
    """The route publishes what the host reported, decoded the way the tunnel decodes it."""
    app, registry, _ = mcp_app
    conn = _register(registry, CAP_MCP_INVENTORY)
    reported = {
        **_GITHUB,
        "auth_status": "sign_in_required",
        "sign_in_url": "https://auth.example.com/start",
    }
    async with _client(app) as client:
        task = asyncio.create_task(
            client.get(f"/v1/hosts/{_HOST_ID}/mcp-servers", headers={"x-test-user": "owner"})
        )
        frame = await sent_to_host(conn, task)
        assert isinstance(frame, HostMcpServersFrame)
        # Round-trip the host's answer so the decoder's allow-list is in the path.
        answered = decode_host_frame(
            encode_host_frame(
                HostMcpServersResultFrame(frame.request_id, "ok", mcp_servers=[reported])
            )
        )
        assert isinstance(answered, HostMcpServersResultFrame)
        conn.pending_mcp_servers[frame.request_id].set_result(answered)
        response = await task
    assert response.status_code == 200
    server = response.json()["mcp_servers"][0]
    assert server["auth_status"] == "sign_in_required"
    assert server["sign_in_url"] == "https://auth.example.com/start"


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
@pytest.mark.parametrize("enabled_request", [False, True])
async def test_proxy_cleans_up_unanswered_requests(
    monkeypatch: pytest.MonkeyPatch, outcome: str, status: int, enabled_request: bool
) -> None:
    registry = HostRegistry()
    conn = _register(registry, CAP_MCP_INVENTORY)
    if outcome == "timeout":
        monkeypatch.setattr("omnigent.server.routes.mcp_servers._MCP_SERVERS_TIMEOUT_S", 0.01)
    else:
        registry.deregister(conn.host_id)
    with pytest.raises(HTTPException) as exc_info:
        if enabled_request:
            await request_host_set_mcp_server_enabled(
                host_registry=registry,
                host_conn=conn,
                body=McpServerEnabledRequest(harness="claude", server="docs", enabled=False),
            )
        else:
            await request_host_mcp_servers(host_registry=registry, host_conn=conn)
    assert exc_info.value.status_code == (502 if enabled_request else status)
    assert conn.pending_mcp_servers == {}
    assert conn.pending_mcp_server_enabled == {}


@pytest.mark.parametrize("enabled", [True, False])
async def test_enablement_inventory_gate(mcp_app, enabled):
    app, registry, _ = mcp_app
    app.state.mcp_enabled = enabled
    conn = _register(registry, CAP_MCP_INVENTORY)
    async with _client(app) as client:
        task = asyncio.create_task(
            client.get(f"/v1/hosts/{_HOST_ID}/mcp-servers", headers={"x-test-user": "owner"})
        )
        frame = decode_host_frame((await conn.outbound_queue.get()).data)
        reply = decode_host_frame(
            encode_host_frame(
                HostMcpServersResultFrame(
                    frame.request_id,
                    "ok",
                    mcp_servers=[{**_GITHUB, "enabled": "false", "locked": "true"}],
                )
            )
        )
        conn.pending_mcp_servers[frame.request_id].set_result(reply)
        response = await task
    row = response.json()["mcp_servers"][0]
    assert row["enabled"] is (False if enabled else None)
    assert row["locked"] is enabled


@pytest.mark.parametrize(
    "status,error_code,expected",
    [
        ("ok", None, 200),
        ("failed", "invalid_server", 422),
        ("failed", "io", 502),
    ],
)
async def test_owner_put_through_tunnel(mcp_app, status, error_code, expected):
    app, registry, _ = mcp_app
    app.state.mcp_enabled = True
    conn = _register(registry, CAP_MCP_SERVER_ENABLED)
    async with _client(app) as client:
        task = asyncio.create_task(
            client.put(
                f"/v1/hosts/{_HOST_ID}/mcp-servers/enabled",
                headers={"x-test-user": "owner"},
                json={"harness": "claude", "server": "github", "enabled": False},
            )
        )
        frame = decode_host_frame((await conn.outbound_queue.get()).data)
        assert isinstance(frame, HostSetMcpServerEnabledFrame)
        assert (frame.harness, frame.server, frame.enabled) == ("claude", "github", False)
        reply = decode_host_frame(
            encode_host_frame(
                HostSetMcpServerEnabledResultFrame(
                    frame.request_id,
                    status,
                    server={**_GITHUB, "enabled": "false", "locked": "false"},
                    error_code=error_code,
                    error="Cannot update disabled MCP servers: io" if error_code == "io" else None,
                )
            )
        )
        conn.pending_mcp_server_enabled[frame.request_id].set_result(reply)
        response = await task
    assert response.status_code == expected
    if expected == 200:
        assert response.json()["server"]["enabled"] is False
    elif error_code == "io":
        assert response.json()["detail"] == "Cannot update disabled MCP servers: io"
    assert not conn.pending_mcp_server_enabled


@pytest.mark.parametrize(
    "user,gate,capabilities,body,status",
    [
        ("owner", False, [], {}, 404),
        ("other", True, [], {}, 403),
        (None, True, [], {}, 401),
        ("owner", True, [], {}, 501),
        ("owner", True, [], {"server": "omnigent"}, 422),
        ("owner", True, [], {"harness": "cursor"}, 422),
        ("owner", True, [], {"server": ""}, 422),
        ("owner", True, [], {"server": "x" * 1025}, 422),
        ("owner", True, [], {"plugin": "x" * 1025}, 422),
        ("owner", True, [], {"source_id": "bad"}, 422),
    ],
)
async def test_put_rejections_do_not_reach_host(mcp_app, user, gate, capabilities, body, status):
    app, registry, _ = mcp_app
    app.state.mcp_enabled = gate
    conn = _register(registry, *capabilities)
    async with _client(app) as client:
        response = await client.put(
            f"/v1/hosts/{_HOST_ID}/mcp-servers/enabled",
            headers={"x-test-user": user} if user else {},
            json={"harness": "claude", "server": "github", "enabled": False, **body},
        )
    assert response.status_code == status
    assert conn.outbound_queue.empty()
