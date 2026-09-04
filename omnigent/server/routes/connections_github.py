"""GitHub App integration routes: connect / callback / status / disconnect.

Mounted under ``/v1`` so paths are ``/v1/connections/github/...``. Only mounted
when a :class:`GitHubAppConfig` is configured. Lets a signed-in user connect
their GitHub account so their managed sandboxes authenticate ``gh`` / git as
them. The shared OAuth flow (signed state, CSRF/replay binding, redirect
sanitising, the four endpoints) lives in
:mod:`omnigent.server.routes.connections_base`; this module is just the GitHub
adapter. See ``docs/GITHUB_APP_SETUP.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from typing import Any

from fastapi import Request
from httpx import HTTPError

from omnigent.connections.github import GithubConnectionStore
from omnigent.server.auth import AuthProvider
from omnigent.server.github_app import (
    GitHubAppConfig,
    GitHubAppError,
    build_authorize_url,
)
from omnigent.server.github_app_client import GitHubAppClient
from omnigent.server.routes.connections_base import (
    ConnectionError,
    ConnectStart,
    create_connection_router,
)

_logger = logging.getLogger(__name__)

# Context string binding the derived key to the state-signing purpose, so the
# same input secret used elsewhere never produces the same HMAC key.
_STATE_KEY_INFO = b"omnigent.connections.github.oauth-state.v1"


def _derive_state_signing_key(client_secret: str) -> bytes:
    """Derive a dedicated HMAC key for OAuth-state JWTs from the client secret.

    The App's OAuth client secret is used only as input keying material, run
    through an HMAC-based KDF with a fixed context string. The value that
    actually signs state tokens is therefore an independent subkey, not the
    client secret itself — separating the state-signing purpose from every
    other use of that secret so exposure of one never trivially yields the
    other.
    """
    return hmac.new(client_secret.encode(), _STATE_KEY_INFO, hashlib.sha256).digest()


class GithubConnectionHooks:
    """GitHub half of the shared connection flow: a derived signing key, the
    GitHub authorize redirect, the code→token exchange, and the non-secret
    status fields (login, scopes, install URL)."""

    provider = "github"

    def __init__(
        self,
        config: GitHubAppConfig,
        store: GithubConnectionStore,
        client: GitHubAppClient | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self._api = client if client is not None else GitHubAppClient(config)

    def signing_key(self) -> bytes:
        return _derive_state_signing_key(self.config.client_secret)

    def status_fields(self, connection: Any | None) -> dict[str, Any]:
        return {
            "login": connection.github_login if connection is not None else None,
            "scopes": connection.scopes if connection is not None else None,
            "install_url": self.config.install_url,
        }

    def begin(self, request: Request, build_state: Any) -> ConnectStart | None:
        # GitHub takes no per-connection input, so no extra state claims.
        del request
        return ConnectStart(build_authorize_url(self.config, state=build_state({})))

    async def complete(self, user_id: str, code: str, claims: dict[str, Any]) -> None:
        del claims  # GitHub carries no extra state claims.
        try:
            tokens = await self._api.exchange_code(code)
            login, github_user_id = await self._api.fetch_login(tokens.access_token)
        except (GitHubAppError, HTTPError, ValueError) as exc:
            raise ConnectionError(str(exc)) from exc
        await asyncio.to_thread(
            self.store.upsert,
            user_id,
            github_login=login,
            github_user_id=github_user_id,
            tokens=tokens,
        )
        _logger.info("GitHub account %s connected for %s", login, user_id)


def create_connections_github_router(
    config: GitHubAppConfig,
    store: GithubConnectionStore,
    *,
    auth_provider: AuthProvider | None = None,
    client: GitHubAppClient | None = None,
):
    """Build the GitHub App integration router — the GitHub adapter over the
    shared ``/connections/{provider}/*`` flow."""
    return create_connection_router(
        GithubConnectionHooks(config, store, client),
        auth_provider=auth_provider,
    )
