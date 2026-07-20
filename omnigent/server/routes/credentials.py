"""
Per-user external-service credential routes (Settings → Credentials).

``GET /v1/credentials`` lists the caller's connected credentials (masked —
never the token). ``POST /v1/credentials/github/connect`` starts the GitHub
OAuth authorize flow; ``GET /auth/github/credential-callback`` finishes it
(code → token exchange, identity fetch, encrypted upsert);
``DELETE /v1/credentials/github`` disconnects.

The GitHub OAuth App is configured via
``OMNIGENT_GITHUB_CREDENTIAL_CLIENT_ID`` /
``OMNIGENT_GITHUB_CREDENTIAL_CLIENT_SECRET``. The feature also requires the
credential store's encryption key; with either missing, the connect route
returns 409 ``credentials_disabled`` and the rest of the app is unaffected.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores.credential_store import (
    CredentialStore,
    credential_encryption_enabled,
)

logger = logging.getLogger(__name__)

_CLIENT_ID_ENV = "OMNIGENT_GITHUB_CREDENTIAL_CLIENT_ID"
_CLIENT_SECRET_ENV = "OMNIGENT_GITHUB_CREDENTIAL_CLIENT_SECRET"
_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_TOKEN_URL = "https://github.com/login/oauth/access_token"
_USER_URL = "https://api.github.com/user"
_SCOPES = "repo"
_STATE_TTL_S = 600
# Single-user deployments (no auth provider) store under this sentinel.
_LOCAL_USER = "local"
_SETTINGS_PATH = "/settings/credentials"


def _client_id() -> str:
    return os.environ.get(_CLIENT_ID_ENV, "").strip()


def _client_secret() -> str:
    return os.environ.get(_CLIENT_SECRET_ENV, "").strip()


def _feature_enabled() -> bool:
    return bool(_client_id()) and bool(_client_secret()) and credential_encryption_enabled()


def _state_key() -> bytes:
    # Reuse the credential encryption key as the state-HMAC key; the feature
    # gate guarantees it exists whenever a flow can start.
    return os.environ.get("OMNIGENT_CREDENTIAL_ENCRYPTION_KEY", "").strip().encode()


def _mint_state(user_id: str) -> str:
    ts = str(int(time.time()))
    mac = hmac.new(_state_key(), f"{user_id}:{ts}".encode(), hashlib.sha256).hexdigest()
    raw = f"{user_id}:{ts}:{mac}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _check_state(state: str) -> str | None:
    """Validate a callback state; returns the user id, or ``None``."""
    try:
        user_id, ts, mac = base64.urlsafe_b64decode(state.encode()).decode().rsplit(":", 2)
    except (ValueError, UnicodeDecodeError):
        return None
    expected = hmac.new(_state_key(), f"{user_id}:{ts}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return None
    if time.time() - int(ts) > _STATE_TTL_S:
        return None
    return user_id


async def _exchange_code(code: str) -> dict[str, Any]:
    """Exchange the authorize code for a token payload (raises on HTTP error)."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            _TOKEN_URL,
            headers={"Accept": "application/json"},
            data={
                "client_id": _client_id(),
                "client_secret": _client_secret(),
                "code": code,
            },
        )
        resp.raise_for_status()
        return resp.json()


async def _fetch_github_user(token: str) -> dict[str, Any]:
    """Fetch the token owner's GitHub profile (raises on HTTP error)."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            _USER_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
        )
        resp.raise_for_status()
        return resp.json()


def create_credentials_router(
    credential_store: CredentialStore,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the credentials router (mounted at the app root)."""
    router = APIRouter()

    def _user(request: Request) -> str:
        user_id = require_user(request, auth_provider)
        return user_id if user_id is not None else _LOCAL_USER

    @router.get("/v1/credentials")
    async def list_credentials(request: Request) -> dict[str, Any]:
        """The caller's connected credentials — masked, never the token."""
        user_id = _user(request)
        creds = []
        cred = credential_store.get(user_id, "github")
        if cred is not None:
            creds.append(
                {
                    "provider": "github",
                    "login": cred.login,
                    "scopes": cred.scopes,
                    "connected_at": cred.updated_at,
                }
            )
        return {"credentials": creds, "enabled": _feature_enabled()}

    @router.post("/v1/credentials/github/connect")
    async def connect_github(request: Request) -> dict[str, str]:
        """Start the OAuth flow; the client navigates to ``authorize_url``."""
        user_id = _user(request)
        if not _feature_enabled():
            raise OmnigentError(
                "credentials_disabled",
                code=ErrorCode.CONFLICT,
            )
        query = urlencode(
            {
                "client_id": _client_id(),
                "scope": _SCOPES,
                "state": _mint_state(user_id),
            }
        )
        return {"authorize_url": f"{_AUTHORIZE_URL}?{query}"}

    @router.get("/auth/github/credential-callback")
    async def credential_callback(
        code: str = "", state: str = "", error: str = ""
    ) -> RedirectResponse:
        """Finish the OAuth flow and land back on Settings → Credentials."""
        if error:
            return RedirectResponse(f"{_SETTINGS_PATH}?error=github_denied", status_code=302)
        if not _feature_enabled():
            return RedirectResponse(f"{_SETTINGS_PATH}?error=disabled", status_code=302)
        user_id = _check_state(state) if state else None
        if user_id is None or not code:
            return RedirectResponse(f"{_SETTINGS_PATH}?error=state_mismatch", status_code=302)
        try:
            payload = await _exchange_code(code)
            token = str(payload.get("access_token") or "")
            if not token:
                raise ValueError("no access_token in exchange response")
            profile = await _fetch_github_user(token)
        except (httpx.HTTPError, ValueError, KeyError):
            logger.warning("github credential exchange failed for %s", user_id, exc_info=True)
            return RedirectResponse(f"{_SETTINGS_PATH}?error=exchange_failed", status_code=302)
        credential_store.upsert(
            user_id,
            "github",
            token=token,
            login=str(profile.get("login") or "unknown"),
            scopes=str(payload.get("scope") or _SCOPES),
        )
        return RedirectResponse(f"{_SETTINGS_PATH}?connected=github", status_code=302)

    @router.delete("/v1/credentials/github")
    async def disconnect_github(request: Request) -> dict[str, bool]:
        """Remove the caller's GitHub credential."""
        user_id = _user(request)
        credential_store.delete(user_id, "github")
        return {"ok": True}

    return router
