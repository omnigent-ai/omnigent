"""Host-facing endpoint that vends a session's Databricks credential.

A managed sandbox authenticates back with its launch token (the same channel the
host tunnel uses); this route resolves it to the session **owner** and returns
the owner's Databricks OAuth token plus their workspace host, so the sandbox's
AI-Gateway MCP proxy can connect to ``https://<workspace>/…`` as the user. The
token is fetched on demand, refreshed server-side, never persisted in the
sandbox, and stops resolving the moment the launch token expires or the host row
is deleted. Responses are ``Cache-Control: no-store``. Mirrors
:mod:`omnigent.server.routes.host_github`. See ``designs/DATABRICKS_CONNECT.md``.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Header, HTTPException, Response

from omnigent.server.databricks_app_client import DatabricksAppClient
from omnigent.server.databricks_identity import resolve_databricks_token
from omnigent.server.databricks_store import DatabricksConnectionStore
from omnigent.stores.host_store import HostStore

_logger = logging.getLogger(__name__)


def create_host_databricks_router(
    host_store: HostStore,
    databricks_store: DatabricksConnectionStore,
    databricks_client: DatabricksAppClient,
) -> APIRouter:
    """Build the host-facing Databricks-credential router.

    :param host_store: Resolves a launch token + host id to the session owner.
    :param databricks_store: The per-user Databricks connection store.
    :param databricks_client: Databricks OAuth client, for refreshing tokens.
    :returns: A router exposing ``GET /hosts/{host_id}/databricks-credential``.
    """
    router = APIRouter()

    @router.get("/hosts/{host_id}/databricks-credential")
    async def databricks_credential(
        host_id: str,
        response: Response,
        x_omnigent_host_token: str | None = Header(default=None),
    ) -> dict[str, object]:
        """Return the session owner's Databricks credential for *host_id*.

        Authenticated by the launch token (constant-time, expiry-aware, bound to
        *host_id*). Returns ``{"connected": false}`` when the owner hasn't linked
        Databricks; ``401`` when the token doesn't resolve.
        """
        response.headers["Cache-Control"] = "no-store"
        if not x_omnigent_host_token:
            raise HTTPException(status_code=401, detail="missing host token")
        managed = await asyncio.to_thread(
            host_store.resolve_launch_token, host_id, x_omnigent_host_token
        )
        if managed is None:
            raise HTTPException(status_code=401, detail="unauthenticated")
        resolved = await resolve_databricks_token(
            managed.user_id, store=databricks_store, client=databricks_client
        )
        if resolved is None:
            return {"connected": False}
        token, workspace_host = resolved
        return {
            "connected": True,
            "token": token,
            "workspace_host": workspace_host,
            "owner": managed.user_id,
        }

    return router
