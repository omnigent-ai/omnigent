"""Resolve a user's Databricks access token for the credential broker.

Bridges the connection store and the Databricks OAuth client: reads the stored
connection and transparently refreshes an expired access token, so the broker
always vends a currently-valid token together with its workspace host (needed to
build the AI Gateway MCP URL). Mirrors :mod:`omnigent.server.github_identity`.
See ``designs/DATABRICKS_CONNECT.md``.
"""

from __future__ import annotations

import logging

from omnigent.connections.databricks import DatabricksConnectionStore
from omnigent.db.utils import now_epoch
from omnigent.server.databricks_app import DatabricksAppError
from omnigent.server.databricks_app_client import DatabricksAppClient

_logger = logging.getLogger(__name__)

# Refresh a token that expires within this margin so it does not lapse
# mid-launch (or shortly after) inside the sandbox.
_REFRESH_MARGIN_S = 300


async def resolve_databricks_token(
    user_id: str,
    *,
    store: DatabricksConnectionStore,
    client: DatabricksAppClient,
) -> tuple[str, str] | None:
    """Resolve a valid ``(access_token, workspace_host)`` for *user_id*, or ``None``.

    Reads the stored connection and transparently refreshes a token at/near
    expiry (persisting the refresh, workspace-scoped). Best-effort: any failure
    (no connection, no refresh token, refresh rejected) returns ``None``.
    """
    connection = await _run_sync(store.get, user_id, with_tokens=True)
    if connection is None or not connection.access_token or not connection.workspace_host:
        return None
    access_token = connection.access_token
    workspace_host = connection.workspace_host
    expires_at = connection.token_expires_at
    if expires_at is not None and expires_at <= now_epoch() + _REFRESH_MARGIN_S:
        if not connection.refresh_token:
            return None
        try:
            refreshed = await client.refresh_token(workspace_host, connection.refresh_token)
        except DatabricksAppError as exc:
            _logger.warning("Databricks token refresh failed for %s: %s", user_id, exc)
            return None
        await _run_sync(store.update_tokens, user_id, refreshed)
        access_token = refreshed.access_token
    return access_token, workspace_host


async def resolve_databricks_credential(
    user_id: str,
    *,
    store: DatabricksConnectionStore,
    client: DatabricksAppClient,
) -> dict[str, object] | None:
    """Resolve the Databricks broker payload for *user_id*, or ``None``.

    The provider adapter the generic credential broker
    (:mod:`omnigent.server.routes.host_credentials`) calls: returns the vended
    (server-refreshed) access token plus the ``workspace_host`` the sandbox
    needs to target the AI Gateway, or ``None`` when the owner has not linked
    Databricks. Mirrors
    :func:`omnigent.server.github_identity.resolve_github_credential`.
    """
    resolved = await resolve_databricks_token(user_id, store=store, client=client)
    if resolved is None:
        return None
    access_token, workspace_host = resolved
    return {"token": access_token, "workspace_host": workspace_host}


async def _run_sync(func, /, *args, **kwargs):
    """Run a synchronous store call off the event loop."""
    import asyncio

    return await asyncio.to_thread(lambda: func(*args, **kwargs))
