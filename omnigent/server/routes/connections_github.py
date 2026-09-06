"""GitHub App integration routes: connect / callback / status / disconnect, plus
the repo/branch listing that backs the New Chat repo picker.

Mounted under ``/v1`` so paths are ``/v1/connections/github/...``. Only mounted
when a :class:`GitHubAppConfig` is configured. Lets a signed-in user connect
their GitHub account so their managed sandboxes authenticate ``gh`` / git as
them. The shared OAuth flow (signed state, CSRF/replay binding, redirect
sanitising, the four endpoints) lives in
:mod:`omnigent.server.routes.connections_base`; this module is the GitHub
adapter plus the GitHub-only repo/branch endpoints. See
``docs/GITHUB_APP_SETUP.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from httpx import HTTPError

from omnigent.connections.github import GithubConnectionStore
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.github_app import (
    GitHubAppConfig,
    GitHubAppError,
    build_authorize_url,
)
from omnigent.server.github_app_client import GitHubAppClient
from omnigent.server.github_identity import resolve_access_token
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes.connections_base import (
    ConnectionError,
    ConnectStart,
    create_connection_router,
)

_logger = logging.getLogger(__name__)

# Context string binding the derived key to the state-signing purpose, so the
# same input secret used elsewhere never produces the same HMAC key.
_STATE_KEY_INFO = b"omnigent.connections.github.oauth-state.v1"

# GitHub owner / repo name charset, enforced before either reaches the branches
# URL so a caller can never smuggle a path or query. A dot is allowed but a
# dot-run (``..``) is not, so ``owner``/``repo`` can never be a traversal segment.
_GITHUB_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _valid_github_name(name: str) -> bool:
    """Whether *name* is a safe GitHub owner/repo segment (no ``..``)."""
    return bool(_GITHUB_NAME_RE.match(name)) and ".." not in name


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
        self.api = client if client is not None else GitHubAppClient(config)

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
            tokens = await self.api.exchange_code(code)
            login, github_user_id = await self.api.fetch_login(tokens.access_token)
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
    shared ``/connections/{provider}/*`` flow, plus the GitHub-only repo/branch
    endpoints the New Chat picker reads."""
    hooks = GithubConnectionHooks(config, store, client)
    api = hooks.api

    def _current_user(request: Request) -> str:
        user_id = require_user(request, auth_provider)
        return user_id if user_id is not None else RESERVED_USER_LOCAL

    def _repo_routes(router: APIRouter) -> None:
        @router.get("/connections/github/repos")
        async def repos(request: Request) -> dict[str, object]:
            """List repos the connected user can access, for the new-chat picker.

            ``connected: false`` (with an empty list) when the caller hasn't
            linked GitHub, so the UI can fall back to a free-text repo URL.
            ``truncated: true`` when the page cap was hit and more repos exist
            than are returned, so the UI can say the list is partial.
            """
            user_id = _current_user(request)
            token = await resolve_access_token(user_id, store=store, client=api)
            if token is None:
                return {"connected": False, "repos": [], "truncated": False}
            try:
                repo_list, truncated = await api.list_repos(token)
            except (GitHubAppError, HTTPError, ValueError) as exc:
                _logger.warning("GitHub repo list failed for %s: %s", user_id, exc)
                raise HTTPException(
                    status_code=502, detail="Failed to list GitHub repositories"
                ) from exc
            return {"connected": True, "repos": repo_list, "truncated": truncated}

        @router.get("/connections/github/repos/{owner}/{repo}/branches")
        async def repo_branches(request: Request, owner: str, repo: str) -> dict[str, object]:
            """List branch names for ``owner/repo``, for the per-repo branch picker.

            ``connected: false`` (empty list) when the caller hasn't linked
            GitHub. Owner/repo are charset-validated before they reach the
            GitHub URL so a caller cannot smuggle a path.
            """
            user_id = _current_user(request)
            if not _valid_github_name(owner) or not _valid_github_name(repo):
                raise HTTPException(status_code=400, detail="Invalid repository name")
            token = await resolve_access_token(user_id, store=store, client=api)
            if token is None:
                return {"connected": False, "branches": []}
            try:
                branches = await api.list_branches(token, f"{owner}/{repo}")
            except (GitHubAppError, HTTPError, ValueError) as exc:
                _logger.warning("GitHub branch list failed for %s/%s: %s", owner, repo, exc)
                raise HTTPException(
                    status_code=502, detail="Failed to list GitHub branches"
                ) from exc
            return {"connected": True, "branches": branches}

    return create_connection_router(hooks, auth_provider=auth_provider, extra_routes=_repo_routes)
