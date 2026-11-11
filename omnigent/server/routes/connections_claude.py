"""Claude subscription routes: status / token / disconnect.

Mounted under ``/v1`` so paths are ``/v1/connections/claude/...``. Only mounted
when :class:`~omnigent.server.claude_connection.ClaudeConnectConfig` is enabled.
Claude has no OAuth redirect for third-party servers, so the user pastes the
token ``claude setup-token`` prints instead of following a connect/callback flow;
status and disconnect are the shared connection endpoints.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from omnigent.connections.claude import ClaudeConnectionStore
from omnigent.server.auth import AuthProvider
from omnigent.server.claude_connection import (
    ClaudeConnectConfig,
    InvalidSetupTokenError,
    normalize_setup_token,
)
from omnigent.server.routes.connections_base import (
    add_status_and_disconnect_routes,
    connection_status,
    connection_user_resolver,
)

_logger = logging.getLogger(__name__)

_PROVIDER = "claude"


class ClaudeTokenRequest(BaseModel):
    """Body of ``PUT /connections/claude/token``."""

    token: str


def _status_fields(connection: Any | None) -> dict[str, Any]:
    del connection
    return {}


def create_connections_claude_router(
    config: ClaudeConnectConfig,
    store: ClaudeConnectionStore,
    *,
    auth_provider: AuthProvider | None = None,
    client: Any = None,
) -> APIRouter:
    """Build the Claude subscription router over the shared connection endpoints."""
    del config, client
    router = APIRouter()
    current_user = connection_user_resolver(auth_provider)
    add_status_and_disconnect_routes(
        router,
        provider=_PROVIDER,
        store=store,
        status_fields=_status_fields,
        current_user=current_user,
    )

    @router.put(f"/connections/{_PROVIDER}/token")
    async def save_token(request: Request, body: ClaudeTokenRequest) -> dict[str, object]:
        """Store the caller's setup token, replacing any earlier one. Never echoes it."""
        user_id = current_user(request)
        try:
            token = normalize_setup_token(body.token)
        except InvalidSetupTokenError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        connection = await asyncio.to_thread(store.upsert, user_id, oauth_token=token)
        _logger.info("Claude subscription connected for %s", user_id)
        return connection_status(connection, _status_fields(connection))

    return router
