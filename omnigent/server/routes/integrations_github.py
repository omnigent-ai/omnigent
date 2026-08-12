"""GitHub App integration routes: connect / callback / status / disconnect.

Mounted under ``/v1`` so paths are ``/v1/integrations/github/...``.
Only mounted when a :class:`GitHubAppConfig` is configured. Lets a
signed-in user connect their GitHub account so their managed sandboxes
authenticate ``gh`` / git as them and receive their public SSH keys.
See ``designs/GITHUB_APP_SANDBOX_AUTH.md``.
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
from omnigent.server.github_app import (
    GitHubAppConfig,
    GitHubAppError,
    build_authorize_url,
)
from omnigent.server.github_app_client import GitHubAppClient
from omnigent.server.github_store import GithubConnectionStore
from omnigent.server.routes._auth_helpers import require_user

_logger = logging.getLogger(__name__)

# The OAuth state JWT is short-lived: it only has to survive the user's
# round trip to GitHub's consent screen.
_STATE_TTL_S = 600
_STATE_ALG = "HS256"

# Fallback landing after connect/disconnect when no (safe) return_to is
# supplied. The SPA renders the integrations panel in Settings.
_DEFAULT_RETURN_TO = "/settings"


def _sanitize_return_to(raw: str | None) -> str:
    """Clamp a caller-supplied return path to a safe same-origin path.

    Only relative paths beginning with a single ``/`` are accepted, so a
    redirect can never be pointed at an external origin
    (``//evil.com``, ``https://evil.com``).

    A leading ``/`` alone is not sufficient: a browser reads ``/\\evil.com``
    (backslash normalized to ``/``) and ``/%09//evil.com`` (control char
    stripped) as protocol-relative and navigates off-origin. So reject any
    backslash or control/whitespace char outright.

    :param raw: The caller-supplied ``return_to``, or ``None``.
    :returns: A safe relative path.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return _DEFAULT_RETURN_TO
    if "\\" in raw or any(ord(c) < 0x20 or c == "\x7f" for c in raw):
        return _DEFAULT_RETURN_TO
    return raw


def _redirect_with_status(return_to: str, status: str) -> RedirectResponse:
    """Redirect back to *return_to* with a ``?github=<status>`` marker."""
    sep = "&" if "?" in return_to else "?"
    return RedirectResponse(
        url=f"{return_to}{sep}{urlencode({'github': status})}", status_code=302
    )


def create_integrations_github_router(
    config: GitHubAppConfig,
    store: GithubConnectionStore,
    *,
    auth_provider: AuthProvider | None = None,
    client: GitHubAppClient | None = None,
) -> APIRouter:
    """Build the GitHub App integration router.

    :param config: Validated GitHub App config (feature is enabled).
    :param store: Connection persistence.
    :param auth_provider: Auth provider for identity resolution, or
        ``None`` when auth is disabled (single-user/local).
    :param client: GitHub App client. Defaults to one built from
        *config*; injectable for tests.
    :returns: A FastAPI router with the integration endpoints.
    """
    router = APIRouter()
    api = client if client is not None else GitHubAppClient(config)

    def _current_user(request: Request) -> str:
        """Return the caller's id, mapping the disabled case to ``local``."""
        user_id = require_user(request, auth_provider)
        return user_id if user_id is not None else RESERVED_USER_LOCAL

    # The OAuth state is HMAC-signed with the App's own client secret — a
    # required, high-entropy secret owned by this flow. State is short-lived
    # (``_STATE_TTL_S``), so a client-secret rotation only invalidates
    # in-flight connect attempts, never stored connections.
    def _sign_state(user_id: str, return_to: str) -> str:
        payload = {
            "sub": user_id,
            "return_to": return_to,
            "nonce": secrets.token_urlsafe(16),
            "exp": int(time.time()) + _STATE_TTL_S,
        }
        return jwt.encode(payload, config.client_secret, algorithm=_STATE_ALG)

    def _verify_state(state: str) -> dict:
        return jwt.decode(state, config.client_secret, algorithms=[_STATE_ALG])

    @router.get("/integrations/github/status")
    async def status(request: Request) -> dict[str, object]:
        """Return the caller's GitHub connection status.

        Never surfaces tokens — only the connected login, scopes, and
        the App's install URL.
        """
        user_id = _current_user(request)
        connection = await asyncio.to_thread(store.get, user_id)
        return {
            "enabled": True,
            "connected": connection is not None,
            "login": connection.github_login if connection is not None else None,
            "scopes": connection.scopes if connection is not None else None,
            "connected_at": connection.created_at if connection is not None else None,
            "install_url": config.install_url,
        }

    @router.get("/integrations/github/connect")
    async def connect(request: Request, return_to: str | None = None) -> RedirectResponse:
        """Redirect the user into the GitHub authorization flow."""
        user_id = _current_user(request)
        state = _sign_state(user_id, _sanitize_return_to(return_to))
        return RedirectResponse(url=build_authorize_url(config, state=state), status_code=302)

    @router.get("/integrations/github/callback")
    async def callback(
        request: Request, code: str | None = None, state: str | None = None
    ) -> RedirectResponse:
        """Handle the GitHub redirect: exchange the code and store tokens.

        Validates the signed state and binds it to the authenticated
        caller so the callback cannot be replayed or cross-bound to
        another user. Redirects back to the state's ``return_to`` with a
        ``?github=connected|error`` marker.
        """
        user_id = _current_user(request)
        if not code or not state:
            return _redirect_with_status(_DEFAULT_RETURN_TO, "error")
        try:
            claims = _verify_state(state)
        except jwt.PyJWTError:
            _logger.warning("GitHub callback with invalid state")
            return _redirect_with_status(_DEFAULT_RETURN_TO, "error")
        return_to = _sanitize_return_to(claims.get("return_to"))
        if claims.get("sub") != user_id:
            _logger.warning("GitHub callback state/user mismatch")
            return _redirect_with_status(return_to, "error")
        try:
            tokens = await api.exchange_code(code)
            login, github_user_id = await api.fetch_login(tokens.access_token)
        except (GitHubAppError, HTTPError, ValueError) as exc:
            # A transient network error or malformed token response must land on
            # the graceful ?github=error redirect, not a raw 500.
            _logger.warning("GitHub connect failed for %s: %s", user_id, exc)
            return _redirect_with_status(return_to, "error")
        await asyncio.to_thread(
            store.upsert,
            user_id,
            github_login=login,
            github_user_id=github_user_id,
            tokens=tokens,
        )
        _logger.info("GitHub account %s connected for %s", login, user_id)
        return _redirect_with_status(return_to, "connected")

    @router.post("/integrations/github/disconnect")
    async def disconnect(request: Request) -> dict[str, bool]:
        """Remove the caller's GitHub connection."""
        user_id = _current_user(request)
        removed = await asyncio.to_thread(store.delete, user_id)
        return {"disconnected": removed}

    return router
