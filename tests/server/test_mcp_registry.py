"""Credential reuse and account/operation boundaries for the MCP registry."""

from __future__ import annotations

import asyncio
import json
from builtins import ExceptionGroup
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from omnigent.server.auth import AuthProvider
from omnigent.server.mcp_registry import (
    McpRegistry,
    McpRegistryConfig,
    McpService,
    McpUpstreamError,
)
from omnigent.server.routes.connections_base import ConnectionError
from omnigent.server.routes.mcp_registry import create_mcp_registry_router
from omnigent.stores.credential_store.sqlalchemy_store import CredentialStore
from tests.server.test_credential_store import _FakeCipher


def service(**kwargs) -> McpService:
    return McpService.model_validate(
        {
            "id": "tracker",
            "title": "Tracker",
            "url": "https://mcp.example.test/mcp",
            "tools": ["read_ticket"],
            **kwargs,
        }
    )


@pytest.fixture
def registry(db_uri):
    return McpRegistry(
        McpRegistryConfig(services=[service()]), CredentialStore(db_uri, _FakeCipher())
    )


class TestIdentity(AuthProvider):
    __test__ = False

    def get_user_id(self, request):
        return request.headers.get("x-test-user")


@pytest.mark.asyncio
async def test_account_connection_is_reusable_and_private(registry):
    app = FastAPI()
    app.include_router(create_mcp_registry_router(registry, TestIdentity()), prefix="/v1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"x-test-user": "alice"},
    ) as client:
        saved = await client.put(
            "/v1/mcp-registry/services/tracker/connection", json={"token": "private-test-token"}
        )
        assert saved.status_code == 200
        catalog = await client.get("/v1/mcp-registry/services")
        assert catalog.json()["data"][0]["connected"] is True
        assert "private-test-token" not in catalog.text
        other = await client.get("/v1/mcp-registry/services", headers={"x-test-user": "bob"})
        assert other.json()["data"][0]["connected"] is False
        assert await registry.token(service(), "alice", app.state) == "private-test-token"
        with pytest.raises(ConnectionError, match="Connect"):
            await registry.token(service(), "bob", app.state)
        await client.delete("/v1/mcp-registry/services/tracker/connection")
        with pytest.raises(ConnectionError, match="Connect"):
            await registry.token(service(), "alice", app.state)


@pytest.mark.asyncio
async def test_tool_and_service_rules_are_checked_before_credential_resolution(
    registry, monkeypatch
):
    resolve = AsyncMock()
    monkeypatch.setattr(registry, "token", resolve)
    with pytest.raises(ConnectionError, match="not allowed"):
        await registry.execute("tracker", "alice", SimpleNamespace(), tool="delete_ticket")
    registry.services["tracker"] = service(allowed_users=["alice"])
    with pytest.raises(ConnectionError, match="unavailable"):
        await registry.execute("tracker", "bob", SimpleNamespace(), tool="read_ticket")
    resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_is_reused_and_does_not_restore_disconnected_account(registry, monkeypatch):
    entry = service(
        auth="oauth",
        oauth={
            "authorize_url": "https://auth.example.test/authorize",
            "token_url": "https://auth.example.test/token",
            "client_id": "demo",
        },
    )
    registry.store.upsert(
        "alice",
        "mcp:tracker",
        secret={"access_token": "expired", "refresh_token": "refresh"},
        metadata={"expires_at": 0},
    )
    refresh = AsyncMock(
        return_value={"access_token": "fresh", "refresh_token": "rotated", "expires_in": 3600}
    )
    monkeypatch.setattr(registry, "exchange", refresh)
    tokens = await asyncio.gather(
        *(registry.token(entry, "alice", SimpleNamespace()) for _ in range(5))
    )
    assert tokens == ["fresh"] * 5
    refresh.assert_awaited_once()
    registry.store.upsert(
        "alice",
        "mcp:tracker",
        secret={"access_token": "expired", "refresh_token": "rotated"},
        metadata={"expires_at": 0},
    )

    async def disconnect_during_refresh(*args):
        registry.store.delete("alice", "mcp:tracker")
        return {"access_token": "too-late", "expires_in": 3600}

    monkeypatch.setattr(registry, "exchange", disconnect_during_refresh)
    with pytest.raises(ConnectionError, match="disconnected"):
        await registry.token(entry, "alice", SimpleNamespace())
    assert registry.store.get("alice", "mcp:tracker") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("grant_type", ["authorization_code", "refresh_token"])
async def test_oauth_exchange_requests_json(registry, monkeypatch, grant_type):
    entry = service(
        auth="oauth",
        oauth={
            "authorize_url": "https://auth.example.test/authorize",
            "token_url": "https://auth.example.test/token",
            "client_id": "demo",
        },
    )

    def exchange(request):
        assert request.headers["accept"] == "application/json"
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        assert f"grant_type={grant_type}" in request.content.decode()
        return httpx.Response(200, json={"access_token": "test-token", "token_type": "bearer"})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(exchange), **kwargs),
    )
    tokens = await registry.exchange(entry, {"grant_type": grant_type})
    assert tokens["access_token"] == "test-token"


@pytest.mark.parametrize(
    "override",
    [
        {"url": "http://example.test/mcp"},
        {"tools": []},
        {"auth": "oauth"},
        {"unknown": "typo"},
        {"id": "ambiguous__namespace"},
    ],
)
def test_invalid_operator_configuration_fails_early(override):
    with pytest.raises(ValidationError):
        service(**override)


@pytest.mark.parametrize("url", ["http://public.example.test", "https://user:pass@example.test"])
def test_invalid_public_callback_url_is_rejected(url):
    with pytest.raises(ValidationError, match="public_url"):
        McpRegistryConfig(public_url=url, services=[])


@pytest.mark.asyncio
async def test_existing_provider_resolver_is_reused(registry):
    from omnigent.connections.github import GithubConnectionStore
    from omnigent.server.github_app import GitHubTokenSet

    store = GithubConnectionStore(registry.store.storage_location, _FakeCipher())
    store.upsert(
        "alice",
        github_login="alice-gh",
        github_user_id=1,
        tokens=GitHubTokenSet("github-token", None, None, None, "repo"),
    )
    assert (
        await registry.token(
            service(auth="github"),
            "alice",
            SimpleNamespace(github_store=store, github_client=None),
        )
        == "github-token"
    )


@pytest.mark.asyncio
async def test_http_permission_failure_after_successful_discovery(registry, monkeypatch):
    registry.store.upsert(
        "alice", "mcp:tracker", secret={"access_token": "private-test-token"}, metadata={}
    )
    calls = []

    def upstream(request):
        assert request.headers["authorization"] == "Bearer private-test-token"
        if request.method == "GET":
            return httpx.Response(405)
        if request.method == "DELETE":
            return httpx.Response(200)
        message = json.loads(request.content)
        calls.append(message["method"])
        if "id" not in message:
            return httpx.Response(202)
        if message["method"] == "tools/call":
            return httpx.Response(403, text="insufficient scopes: private-test-token")
        result = (
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "permission-test", "version": "1"},
            }
            if message["method"] == "initialize"
            else {"tools": [{"name": "read_ticket", "inputSchema": {"type": "object"}}]}
        )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(upstream), **kwargs),
    )
    tools = await registry.execute("tracker", "alice", SimpleNamespace())
    assert [tool.name for tool in tools] == ["read_ticket"]
    with pytest.raises(ConnectionError, match="HTTP 403") as failure:
        await registry.execute("tracker", "alice", SimpleNamespace(), tool="read_ticket")
    assert "private-test-token" not in str(failure.value)
    assert calls.count("tools/call") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
@pytest.mark.parametrize("nested", [False, True])
async def test_upstream_http_failures_are_classified_without_secrets(
    registry, monkeypatch, caplog, status, nested
):
    request = httpx.Request(
        "POST", "https://example.test/private-url-secret", headers={"Authorization": "secret"}
    )
    error = httpx.HTTPStatusError(
        "private-exception-secret",
        request=request,
        response=httpx.Response(status, request=request, text="private-response-secret"),
    )
    if nested:
        error = ExceptionGroup("private-group-secret", [ExceptionGroup("nested", [error])])
    attempts = []

    @asynccontextmanager
    async def denied_transport(*args, **kwargs):
        attempts.append(True)
        raise error
        yield  # pragma: no cover

    monkeypatch.setattr("omnigent.server.mcp_registry.streamable_http_client", denied_transport)
    monkeypatch.setattr(registry, "token", AsyncMock(return_value="private-token-secret"))
    with pytest.raises(McpUpstreamError, match=f"HTTP {status}") as failure:
        await registry.execute("tracker", "alice", SimpleNamespace(), tool="read_ticket")
    assert failure.value.kind == "http"
    assert failure.value.status_code == status
    assert "not retried" in str(failure.value)
    assert len(attempts) == 1
    assert f"http_status={status}" in caplog.text
    assert "service=tracker tool=read_ticket" in caplog.text
    assert "private-" not in caplog.text + str(failure.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (TimeoutError("private-secret"), "timeout"),
        (httpx.ReadTimeout("private-secret"), "timeout"),
        (httpx.ConnectError("private-secret"), "transport"),
        (RuntimeError("private-secret"), "unexpected"),
    ],
)
async def test_non_http_failures_are_safe(registry, monkeypatch, caplog, error, kind):
    @asynccontextmanager
    async def failed_transport(*args, **kwargs):
        raise ExceptionGroup("private-group-secret", [error])
        yield  # pragma: no cover

    monkeypatch.setattr("omnigent.server.mcp_registry.streamable_http_client", failed_transport)
    monkeypatch.setattr(registry, "token", AsyncMock(return_value="private-token-secret"))
    with pytest.raises(McpUpstreamError) as failure:
        await registry.execute("tracker", "alice", SimpleNamespace(), tool="read_ticket")
    assert failure.value.kind == kind
    assert failure.value.status_code is None
    assert "private-" not in caplog.text + str(failure.value)


@pytest.mark.asyncio
async def test_connection_test_preserves_safe_upstream_diagnosis(registry, monkeypatch):
    app = FastAPI()
    app.include_router(create_mcp_registry_router(registry, TestIdentity()), prefix="/v1")
    monkeypatch.setattr(
        registry,
        "execute",
        AsyncMock(
            side_effect=McpUpstreamError(
                "MCP service denied access (HTTP 403).", kind="http", status_code=403
            )
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"x-test-user": "alice"},
    ) as client:
        response = await client.post("/v1/mcp-registry/services/tracker/test")
    assert response.status_code == 502
    assert "HTTP 403" in response.json()["detail"]
