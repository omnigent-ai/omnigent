"""Databricks integration routes: connect / callback / status / disconnect.

Mounted under ``/v1`` so paths are ``/v1/connections/databricks/...``. Only
mounted when a :class:`DatabricksConfig` is configured. Lets a signed-in user
authorize their Databricks workspace (OAuth U2M + PKCE) so their managed
sandboxes reach the Databricks AI Gateway MCP as them. The shared OAuth flow
lives in :mod:`omnigent.server.routes.connections_base`; this module is the
Databricks adapter (per-workspace host + PKCE). See
``designs/DATABRICKS_CONNECT.md``.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

from fastapi import Request
from httpx import HTTPError

from omnigent.connections.databricks import DatabricksConnectionStore
from omnigent.server.auth import AuthProvider
from omnigent.server.databricks_app import (
    DatabricksAppError,
    DatabricksConfig,
    authorize_url,
    derive_pkce,
    normalize_workspace_host,
)
from omnigent.server.databricks_app_client import DatabricksAppClient
from omnigent.server.routes.connections_base import (
    ConnectionError,
    ConnectStart,
    create_connection_router,
)

_logger = logging.getLogger(__name__)


class DatabricksConnectionHooks:
    """Databricks half of the shared connection flow: OAuth U2M + PKCE against a
    per-user workspace host, and the non-secret status fields (workspace, user).

    Unlike GitHub, Databricks is multi-workspace, so ``begin`` reads a
    ``workspace`` query param and binds the workspace host + PKCE nonce into the
    signed state; ``complete`` recomputes the PKCE verifier from that nonce (the
    verifier never rides in the browser)."""

    provider = "databricks"

    def __init__(
        self,
        config: DatabricksConfig,
        store: DatabricksConnectionStore,
        client: DatabricksAppClient | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.api = client if client is not None else DatabricksAppClient(config)

    def signing_key(self) -> str:
        return self.config.client_secret

    def status_fields(self, connection: Any | None) -> dict[str, Any]:
        return {
            "workspace_host": connection.workspace_host if connection is not None else None,
            "databricks_user": connection.databricks_user if connection is not None else None,
        }

    def begin(self, request: Request, build_state: Any) -> ConnectStart | None:
        workspace_host = normalize_workspace_host(request.query_params.get("workspace") or "")
        if workspace_host is None:
            return None
        nonce = secrets.token_urlsafe(32)
        _verifier, challenge = derive_pkce(self.config.client_secret, nonce)
        state = build_state({"workspace_host": workspace_host, "nonce": nonce})
        return ConnectStart(
            authorize_url(
                self.config,
                workspace_host=workspace_host,
                state=state,
                code_challenge=challenge,
            )
        )

    async def complete(self, user_id: str, code: str, claims: dict[str, Any]) -> None:
        workspace_host = normalize_workspace_host(str(claims.get("workspace_host") or ""))
        nonce = claims.get("nonce")
        if workspace_host is None or not nonce:
            raise ConnectionError("missing workspace host or PKCE nonce in state")
        verifier, _challenge = derive_pkce(self.config.client_secret, str(nonce))
        try:
            tokens = await self.api.exchange_code(workspace_host, code, code_verifier=verifier)
            user_name, databricks_user_id = await self.api.fetch_user(
                workspace_host, tokens.access_token
            )
        except (DatabricksAppError, HTTPError, ValueError) as exc:
            raise ConnectionError(str(exc)) from exc
        await asyncio.to_thread(
            self.store.upsert,
            user_id,
            workspace_host=workspace_host,
            databricks_user=user_name,
            databricks_user_id=databricks_user_id,
            tokens=tokens,
        )
        _logger.info("Databricks workspace %s connected for %s", workspace_host, user_id)


def create_connections_databricks_router(
    config: DatabricksConfig,
    store: DatabricksConnectionStore,
    *,
    auth_provider: AuthProvider | None = None,
    client: DatabricksAppClient | None = None,
):
    """Build the Databricks integration router — the Databricks adapter over the
    shared ``/connections/{provider}/*`` flow."""
    return create_connection_router(
        DatabricksConnectionHooks(config, store, client),
        auth_provider=auth_provider,
    )
