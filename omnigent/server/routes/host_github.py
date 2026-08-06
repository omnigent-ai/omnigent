"""Host-facing endpoint that vends a session's GitHub credential.

A managed sandbox authenticates back to the server with its launch token
(:data:`~omnigent.host.identity.MANAGED_HOST_TOKEN_HEADER`) — the same channel
the host tunnel uses. This route resolves that token to the session **owner**
and returns the owner's connected GitHub token, so the sandbox can obtain
credentials *over the existing authenticated channel* instead of having each
executor inject them into the environment.

Consumers (all inside the sandbox/runner, never the agent's own process):
- the git credential helper (``omnigent git-credential-github``), per git op;
- the GitHub MCP proxy, when it connects to GitHub's hosted MCP.

The token is fetched on demand and never persisted in the sandbox: the server
re-resolves it (:func:`resolve_access_token`) on each request and stops vending
the moment the launch token expires or the host row is deleted (session
teardown). Responses are ``Cache-Control: no-store`` so no intermediary retains
it.

Threat model — be precise about what teardown does and does not do. What this
vends is the session owner's **full-scope user token** (whatever scopes they
granted, typically ``repo`` = all their repos), not a repo-scoped one. Teardown
stops *future* vends, but a token already handed out stays valid at GitHub for
its own lifetime (~8h) regardless — this endpoint cannot revoke it. And any
process in the sandbox that can reach this endpoint (it authenticates with the
launch token baked into the sandbox's git config) can obtain that token for its
TTL. So the trust boundary is the sandbox itself, not this endpoint: it removes
the token from disk/env, not the ability of in-sandbox code to request one. See
``designs/CREDENTIAL_STORE.md``.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Header, HTTPException, Response

from omnigent.connections.github import GithubConnectionStore
from omnigent.server.github_app_client import GitHubAppClient
from omnigent.server.github_identity import resolve_access_token
from omnigent.stores.host_store import HostStore

_logger = logging.getLogger(__name__)

# The username git uses with a token over HTTPS (``https://<user>:<token>@…``);
# GitHub ignores the value but requires a non-empty one.
_GIT_TOKEN_USERNAME = "x-access-token"


def create_host_github_router(
    host_store: HostStore,
    github_store: GithubConnectionStore,
    github_client: GitHubAppClient,
) -> APIRouter:
    """Build the host-facing GitHub-credential router.

    :param host_store: Resolves a launch token + host id to the session owner.
    :param github_store: The per-user GitHub connection store.
    :param github_client: GitHub App client, for refreshing user tokens.
    :returns: A router exposing ``GET /hosts/{host_id}/github-credential``.
    """
    router = APIRouter()

    @router.get("/hosts/{host_id}/github-credential")
    async def github_credential(
        host_id: str,
        response: Response,
        x_omnigent_host_token: str | None = Header(default=None),
    ) -> dict[str, object]:
        """Return the session owner's GitHub credential for *host_id*.

        Authenticated by the launch token (constant-time, expiry-aware, and
        bound to *host_id*), exactly like the host tunnel. Returns
        ``{"connected": false}`` when the owner hasn't linked GitHub (or the
        feature is off), so a caller can fall back cleanly; ``401`` when the
        token doesn't resolve.
        """
        # Never let a proxy/browser cache a vended token.
        response.headers["Cache-Control"] = "no-store"
        if not x_omnigent_host_token:
            raise HTTPException(status_code=401, detail="missing host token")
        managed = await asyncio.to_thread(
            host_store.resolve_launch_token, host_id, x_omnigent_host_token
        )
        if managed is None:
            raise HTTPException(status_code=401, detail="unauthenticated")
        token = await resolve_access_token(
            managed.user_id, store=github_store, client=github_client
        )
        if token is None:
            return {"connected": False}
        # ``owner``/``login`` let the host attribute commits to the human
        # (author identity), decoupled from the push credential.
        connection = await asyncio.to_thread(github_store.get, managed.user_id)
        return {
            "connected": True,
            "username": _GIT_TOKEN_USERNAME,
            "token": token,
            "owner": managed.user_id,
            "login": connection.github_login if connection is not None else None,
        }

    return router
