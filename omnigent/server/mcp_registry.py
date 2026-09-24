"""Administrator-configured MCP services using the shared per-user credential store."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import secrets
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

import httpx
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnigent.entities import ProviderConnection
from omnigent.server.routes.connections_base import ConnectionError, ConnectStart
from omnigent.stores.credential_store.sqlalchemy_store import CredentialStore


class OAuthConfig(BaseModel):
    """Explicit OAuth configuration for an approved MCP resource."""

    model_config = ConfigDict(extra="forbid")
    authorize_url: str
    token_url: str
    client_id: str
    client_secret_env: str | None = None
    scopes: list[str] = Field(default_factory=list)
    resource: str | None = None


class McpService(BaseModel):
    """A catalog entry. Only the server operator can choose upstream destinations."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    title: str
    description: str = ""
    url: str
    auth: Literal["none", "bearer", "oauth", "github", "databricks"] = "bearer"
    oauth: OAuthConfig | None = None
    allowed_users: list[str] | None = None
    tools: list[str] = Field(min_length=1)
    allow_http: bool = False
    timeout: int = Field(default=60, ge=1, le=300)

    @model_validator(mode="after")
    def validate_service(self) -> McpService:
        if "__" in self.id:
            raise ValueError("MCP service ids cannot contain the tool namespace separator '__'")
        if (self.auth == "oauth") != (self.oauth is not None):
            raise ValueError("oauth configuration is required only for auth=oauth")
        urls = [self.url]
        if self.oauth:
            urls += [self.oauth.authorize_url, self.oauth.token_url]
        for url in urls:
            parsed = urlsplit(url)
            if (
                parsed.scheme not in ({"http", "https"} if self.allow_http else {"https"})
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
            ):
                raise ValueError(
                    "MCP/OAuth URLs require HTTPS (allow_http is for local development)"
                )
        return self

    def permits(self, user_id: str) -> bool:
        return self.allowed_users is None or user_id in self.allowed_users


class McpRegistryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    public_url: str | None = None
    services: list[McpService]

    @model_validator(mode="after")
    def validate_catalog(self) -> McpRegistryConfig:
        if len({s.id for s in self.services}) != len(self.services):
            raise ValueError("MCP service ids must be unique")
        if any(s.oauth for s in self.services) and not self.public_url:
            raise ValueError("public_url is required for OAuth callbacks")
        if self.public_url:
            parsed = urlsplit(self.public_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"})
            ):
                raise ValueError("public_url must be an HTTPS base URL (HTTP only on localhost)")
        return self

    @classmethod
    def from_env(cls) -> McpRegistryConfig | None:
        path = os.environ.get("OMNIGENT_MCP_REGISTRY")
        return cls.model_validate(yaml.safe_load(Path(path).read_text())) if path else None


class McpConnectionStore:
    """Typed facade sharing the existing encrypted connections table."""

    def __init__(self, store: CredentialStore, service_id: str) -> None:
        self._store = store
        self.provider = f"mcp:{service_id}"

    def get(self, user_id: str, *, with_tokens: bool = False) -> ProviderConnection | None:
        return self._store.get(user_id, self.provider, with_secret=with_tokens)

    def delete(self, user_id: str) -> bool:
        return self._store.delete(user_id, self.provider)


class McpRegistry:
    """Resolve credentials and execute approved catalog tools on the server."""

    def __init__(self, config: McpRegistryConfig, store: CredentialStore | None) -> None:
        self.config = config
        self.services = {service.id: service for service in config.services}
        self.store = store
        self._locks: dict[tuple[int, str, str], asyncio.Lock] = {}
        if any(s.auth in {"oauth", "bearer"} for s in config.services) and store is None:
            raise ValueError("MCP account connections require the existing KMS or Vault cipher")
        self.state_secret = os.environ.get("OMNIGENT_MCP_OAUTH_STATE_SECRET", "")
        if any(s.oauth for s in config.services) and len(self.state_secret) < 32:
            raise ValueError(
                "Set OMNIGENT_MCP_OAUTH_STATE_SECRET to at least 32 random characters"
            )

    def service(self, service_id: str, user_id: str) -> McpService:
        service = self.services.get(service_id)
        if service is None or not service.permits(user_id):
            raise ConnectionError("MCP service is unavailable for this user")
        return service

    async def token(self, service: McpService, user_id: str, app_state: Any) -> str | None:
        if service.auth == "none":
            return None
        if service.auth in {"github", "databricks"}:
            from omnigent.server.connections_registry import connection_providers

            provider = next(p for p in connection_providers() if p.name == service.auth)
            store = getattr(app_state, f"{provider.name}_store", None)
            client = getattr(app_state, f"{provider.name}_client", None)
            if store is None or provider.credential_resolver is None:
                raise ConnectionError(f"Connect {provider.name} in Sandbox Integrations")
            payload = await provider.credential_resolver(user_id, store=store, client=client)
            if not payload or not isinstance(payload.get("token"), str):
                raise ConnectionError(f"Reconnect {provider.name} in Sandbox Integrations")
            return payload["token"]
        from omnigent.db.db_models import current_workspace_id

        assert self.store is not None
        key = (current_workspace_id(), user_id, service.id)
        async with self._locks.setdefault(key, asyncio.Lock()):
            conn = await asyncio.to_thread(
                self.store.get, user_id, f"mcp:{service.id}", with_secret=True
            )
            if conn is None or not conn.secret or not conn.secret.get("access_token"):
                raise ConnectionError("Connect this MCP account in Sandbox Integrations")
            expiry = conn.metadata.get("expires_at")
            if service.oauth and expiry is not None and float(expiry) <= time.time() + 30:
                refresh = conn.secret.get("refresh_token")
                if not refresh:
                    raise ConnectionError("Reconnect this MCP account in Sandbox Integrations")
                tokens = await self.exchange(
                    service, {"grant_type": "refresh_token", "refresh_token": refresh}
                )
                tokens.setdefault("refresh_token", refresh)
                updated = await asyncio.to_thread(
                    self.store.update_secret,
                    user_id,
                    f"mcp:{service.id}",
                    secret=tokens,
                    metadata=self.metadata(tokens),
                )
                if not updated:
                    raise ConnectionError("MCP account disconnected")
                return str(tokens["access_token"])
            return str(conn.secret["access_token"])

    @staticmethod
    def metadata(tokens: dict[str, Any]) -> dict[str, Any]:
        return {
            "expires_at": time.time() + float(tokens["expires_in"])
            if tokens.get("expires_in") is not None
            else None
        }

    async def exchange(self, service: McpService, data: dict[str, Any]) -> dict[str, Any]:
        oauth = service.oauth
        assert oauth is not None
        data = {**data, "client_id": oauth.client_id}
        if oauth.client_secret_env:
            secret = os.environ.get(oauth.client_secret_env)
            if not secret:
                raise ConnectionError("MCP OAuth client secret is not configured")
            data["client_secret"] = secret
        if oauth.resource:
            data["resource"] = oauth.resource
        async with httpx.AsyncClient(follow_redirects=False, timeout=20) as client:
            response = await client.post(oauth.token_url, data=data)
        if response.status_code != 200:
            raise ConnectionError("MCP authorization failed; reconnect the account")
        tokens = response.json()
        if (
            not isinstance(tokens.get("access_token"), str)
            or tokens.get("token_type", "Bearer").lower() != "bearer"
        ):
            raise ConnectionError("MCP provider did not return a bearer access token")
        return tokens

    async def execute(
        self,
        service_id: str,
        user_id: str,
        app_state: Any,
        *,
        tool: str | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        service = self.service(service_id, user_id)
        if tool is not None and tool not in service.tools:
            raise ConnectionError("Tool is not allowed by the MCP registry")
        token = await self.token(service, user_id, app_state)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with asyncio.timeout(service.timeout):
            async with httpx.AsyncClient(
                headers=headers, follow_redirects=False, timeout=service.timeout
            ) as client:
                async with streamable_http_client(service.url, http_client=client) as (
                    read,
                    write,
                    _,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        if tool is not None:
                            return await session.call_tool(tool, arguments or {})
                        tools = []
                        cursor = None
                        for _ in range(100):
                            result = await session.list_tools(cursor=cursor)
                            tools.extend(t for t in result.tools if t.name in service.tools)
                            cursor = result.nextCursor
                            if not cursor:
                                return tools
                        raise ConnectionError("MCP tool catalog exceeded the pagination limit")


class McpOAuthHooks:
    """Reuse the existing user-bound OAuth connect/callback/disconnect routes."""

    def __init__(self, registry: McpRegistry, service: McpService) -> None:
        assert registry.store is not None
        self.registry = registry
        self.service = service
        self.provider = f"mcp-{service.id}"
        self.store = McpConnectionStore(registry.store, service.id)

    def signing_key(self) -> str:
        return self.registry.state_secret

    def status_fields(self, connection: ProviderConnection | None) -> dict[str, Any]:  # noqa: ARG002
        return {}

    def begin(self, request: Any, build_state: Any) -> ConnectStart:
        oauth = self.service.oauth
        assert oauth is not None
        from omnigent.server.auth import RESERVED_USER_LOCAL
        from omnigent.server.routes._auth_helpers import require_user

        user_id = require_user(request, request.app.state.mcp_registry_auth) or RESERVED_USER_LOCAL
        self.registry.service(self.service.id, user_id)
        verifier = secrets.token_urlsafe(48)
        nonce = secrets.token_urlsafe(32)
        assert self.registry.store is not None
        self.registry.store.upsert(
            user_id,
            f"mcp-pending:{self.service.id}",
            secret={"verifier": verifier, "nonce": nonce},
            metadata={},
        )
        params = {
            "client_id": oauth.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri(),
            "scope": " ".join(oauth.scopes),
            "state": build_state({"nonce": nonce, "service": self.service.id}),
            "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode(),
            "code_challenge_method": "S256",
        }
        if oauth.resource:
            params["resource"] = oauth.resource
        separator = "&" if "?" in oauth.authorize_url else "?"
        return ConnectStart(authorize_url=oauth.authorize_url + separator + urlencode(params))

    def redirect_uri(self) -> str:
        base = str(self.registry.config.public_url).rstrip("/")
        return f"{base}/v1/connections/{self.provider}/callback"

    async def complete(self, user_id: str, code: str, claims: dict[str, Any]) -> None:
        self.registry.service(self.service.id, user_id)
        store = self.registry.store
        assert store is not None
        pending = await asyncio.to_thread(
            store.get, user_id, f"mcp-pending:{self.service.id}", with_secret=True
        )
        if (
            not pending
            or not pending.secret
            or claims.get("service") != self.service.id
            or not secrets.compare_digest(
                str(pending.secret.get("nonce", "")), str(claims.get("nonce", ""))
            )
        ):
            raise ConnectionError("MCP authorization expired; try connecting again")
        await asyncio.to_thread(store.delete, user_id, f"mcp-pending:{self.service.id}")
        tokens = await self.registry.exchange(
            self.service,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri(),
                "code_verifier": pending.secret["verifier"],
            },
        )
        await asyncio.to_thread(
            store.upsert,
            user_id,
            f"mcp:{self.service.id}",
            secret=tokens,
            metadata=self.registry.metadata(tokens),
        )
