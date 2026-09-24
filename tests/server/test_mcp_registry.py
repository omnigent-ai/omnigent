"""Credential reuse and account/operation boundaries for the MCP registry."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from omnigent.server.auth import AuthProvider
from omnigent.server.mcp_registry import McpRegistry, McpRegistryConfig, McpService
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
