"""Databricks integration routes: connect / callback / status / disconnect.

Mounted under ``/v1`` so paths are ``/v1/integrations/databricks/...``. Only
mounted when a :class:`DatabricksConfig` is configured. Lets a signed-in user
authorize their Databricks workspace (OAuth U2M + PKCE) so their managed
sandboxes reach the Databricks AI Gateway MCP as them. Mirrors
:mod:`omnigent.server.routes.integrations_github`. See
``designs/DATABRICKS_CONNECT.md``.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from urllib.parse import urlencode

import jwt
from fastapi import APIRouter, Request
from httpx import HTTPError
from starlette.responses import RedirectResponse

from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.databricks_app import (
    DatabricksAppError,
    DatabricksConfig,
    authorize_url,
    derive_pkce,
    normalize_workspace_host,
)
from omnigent.server.databricks_app_client import DatabricksAppClient
from omnigent.server.databricks_store import DatabricksConnectionStore
from omnigent.server.routes._auth_helpers import require_user

_logger = logging.getLogger(__name__)

# The OAuth state JWT only has to survive the user's round trip to the workspace
# consent screen.
_STATE_TTL_S = 600
_STATE_ALG = "HS256"

_DEFAULT_RETURN_TO = "/settings"


def _sanitize_return_to(raw: str | None) -> str:
    """Clamp a caller-supplied return path to a safe same-origin path.

    Only relative paths beginning with a single ``/`` are accepted; any
    backslash or control/whitespace char (which a browser can read as
    protocol-relative) is rejected — see the GitHub routes for the rationale.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return _DEFAULT_RETURN_TO
    if "\\" in raw or any(ord(c) < 0x20 or c == "\x7f" for c in raw):
        return _DEFAULT_RETURN_TO
    return raw


def _redirect_with_status(return_to: str, status: str) -> RedirectResponse:
    """Redirect back to *return_to* with a ``?databricks=<status>`` marker."""
    sep = "&" if "?" in return_to else "?"
    return RedirectResponse(
        url=f"{return_to}{sep}{urlencode({'databricks': status})}", status_code=302
    )


def create_integrations_databricks_router(
    config: DatabricksConfig,
    store: DatabricksConnectionStore,
    *,
    auth_provider: AuthProvider | None = None,
    client: DatabricksAppClient | None = None,
) -> APIRouter:
    """Build the Databricks integration router.

    :param config: Validated Databricks OAuth config (feature is enabled).
    :param store: Connection persistence.
    :param auth_provider: Auth provider for identity resolution, or ``None`` when
        auth is disabled (single-user/local).
    :param client: Databricks OAuth client. Defaults to one built from *config*;
        injectable for tests.
    """
    router = APIRouter()
    api = client if client is not None else DatabricksAppClient(config)

    def _current_user(request: Request) -> str:
        user_id = require_user(request, auth_provider)
        return user_id if user_id is not None else RESERVED_USER_LOCAL

    # The OAuth state is HMAC-signed with the app's client secret. It carries the
    # workspace host (per-connection) and the PKCE nonce; the PKCE verifier is
    # derived from (client_secret, nonce) at callback, so it never rides in the
    # browser. State is short-lived, so a secret rotation only invalidates
    # in-flight connect attempts.
    def _sign_state(user_id: str, workspace_host: str, return_to: str, nonce: str) -> str:
        payload = {
            "sub": user_id,
            "workspace_host": workspace_host,
            "return_to": return_to,
            "nonce": nonce,
            "exp": int(time.time()) + _STATE_TTL_S,
        }
        return jwt.encode(payload, config.client_secret, algorithm=_STATE_ALG)

    def _verify_state(state: str) -> dict:
        return jwt.decode(state, config.client_secret, algorithms=[_STATE_ALG])

    @router.get("/integrations/databricks/status")
    async def status(request: Request) -> dict[str, object]:
        """Return the caller's Databricks connection status (never tokens)."""
        user_id = _current_user(request)
        connection = await asyncio.to_thread(store.get, user_id)
        return {
            "enabled": True,
            "connected": connection is not None,
            "workspace_host": connection.workspace_host if connection is not None else None,
            "databricks_user": connection.databricks_user if connection is not None else None,
            "connected_at": connection.created_at if connection is not None else None,
        }

    @router.get("/integrations/databricks/connect")
    async def connect(
        request: Request, workspace: str | None = None, return_to: str | None = None
    ) -> RedirectResponse:
        """Redirect the user into their workspace's authorization flow.

        Requires a ``workspace`` URL (Databricks is multi-workspace). Generates a
        PKCE challenge and a signed state carrying the workspace host + nonce.
        """
        user_id = _current_user(request)
        return_to = _sanitize_return_to(return_to)
        workspace_host = normalize_workspace_host(workspace or "")
        if workspace_host is None:
            return _redirect_with_status(return_to, "error")
        nonce = secrets.token_urlsafe(32)
        _verifier, challenge = derive_pkce(config.client_secret, nonce)
        state = _sign_state(user_id, workspace_host, return_to, nonce)
        return RedirectResponse(
            url=authorize_url(
                config, workspace_host=workspace_host, state=state, code_challenge=challenge
            ),
            status_code=302,
        )

    @router.get("/integrations/databricks/callback")
    async def callback(
        request: Request, code: str | None = None, state: str | None = None
    ) -> RedirectResponse:
        """Handle the workspace redirect: exchange the code and store tokens.

        Validates the signed state, rebinds it to the authenticated caller,
        recomputes the PKCE verifier from the state's nonce, and exchanges the
        code against the workspace token endpoint.
        """
        user_id = _current_user(request)
        if not code or not state:
            return _redirect_with_status(_DEFAULT_RETURN_TO, "error")
        try:
            claims = _verify_state(state)
        except jwt.PyJWTError:
            _logger.warning("Databricks callback with invalid state")
            return _redirect_with_status(_DEFAULT_RETURN_TO, "error")
        return_to = _sanitize_return_to(claims.get("return_to"))
        workspace_host = normalize_workspace_host(str(claims.get("workspace_host") or ""))
        nonce = claims.get("nonce")
        if claims.get("sub") != user_id or workspace_host is None or not nonce:
            _logger.warning("Databricks callback state/user mismatch")
            return _redirect_with_status(return_to, "error")
        verifier, _challenge = derive_pkce(config.client_secret, str(nonce))
        try:
            tokens = await api.exchange_code(workspace_host, code, code_verifier=verifier)
            user_name, databricks_user_id = await api.fetch_user(
                workspace_host, tokens.access_token
            )
        except (DatabricksAppError, HTTPError, ValueError) as exc:
            _logger.warning("Databricks connect failed for %s: %s", user_id, exc)
            return _redirect_with_status(return_to, "error")
        await asyncio.to_thread(
            store.upsert,
            user_id,
            workspace_host=workspace_host,
            databricks_user=user_name,
            databricks_user_id=databricks_user_id,
            tokens=tokens,
        )
        _logger.info("Databricks workspace %s connected for %s", workspace_host, user_id)
        return _redirect_with_status(return_to, "connected")

    @router.post("/integrations/databricks/disconnect")
    async def disconnect(request: Request) -> dict[str, bool]:
        """Remove the caller's Databricks connection."""
        user_id = _current_user(request)
        removed = await asyncio.to_thread(store.delete, user_id)
        return {"disconnected": removed}

    return router
