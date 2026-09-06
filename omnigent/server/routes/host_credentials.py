"""Host-facing endpoint that vends a session's per-user credential.

Generic over the connection providers. A managed sandbox authenticates back to
the server with its launch token (:data:`~omnigent.host.identity.MANAGED_HOST_TOKEN_HEADER`)
— the same channel the host tunnel uses. This route resolves that token to the
session **owner** and returns the owner's credential for the requested provider,
so the sandbox obtains it *over the existing authenticated channel* instead of
having each executor inject it into the environment.

One route serves every provider that registers a ``credential_resolver`` on
:mod:`omnigent.server.connections_registry`; the resolver owns the
provider-specific secret lookup + attribution metadata. Consumers (all inside
the sandbox/runner, never the agent's own process) pass ``provider`` in the
path — e.g. the git credential helper and the GitHub MCP proxy pass ``github``.

The credential is fetched on demand and never persisted in the sandbox: the
server re-resolves it on each request and stops vending the moment the launch
token expires or the host row is deleted (session teardown). Responses are
``Cache-Control: no-store`` so no intermediary retains it.

Threat model (unchanged from the original GitHub-only broker): what this vends
is the owner's provider credential — for GitHub, their **full-scope user
token**. Teardown stops *future* vends, but a token already handed out stays
valid at the provider for its own lifetime; this endpoint cannot revoke it. Any
in-sandbox process that can reach this endpoint (it authenticates with the
launch token baked into the sandbox) can obtain that credential for its TTL. So
the trust boundary is the sandbox itself, not this endpoint. See
``designs/CREDENTIAL_STORE.md``.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Header, HTTPException, Request, Response

from omnigent.server.connections_registry import connection_providers
from omnigent.stores.host_store import HostStore

_logger = logging.getLogger(__name__)


def create_host_credentials_router(host_store: HostStore) -> APIRouter:
    """Build the host-facing, provider-generic credential router.

    :param host_store: Resolves a launch token + host id to the session owner.
    :returns: A router exposing ``GET /hosts/{host_id}/credentials/{provider}``.
    """
    resolvers = {
        provider.name: provider.credential_resolver
        for provider in connection_providers()
        if provider.credential_resolver is not None
    }

    router = APIRouter()

    @router.get("/hosts/{host_id}/credentials/{provider}")
    async def host_credential(
        host_id: str,
        provider: str,
        request: Request,
        response: Response,
        x_omnigent_host_token: str | None = Header(default=None),
    ) -> dict[str, object]:
        """Return the session owner's *provider* credential for *host_id*.

        Authenticated by the launch token (constant-time, expiry-aware, and
        bound to *host_id*), exactly like the host tunnel. ``401`` when the
        token doesn't resolve; ``404`` for a provider with no broker resolver or
        not configured on this server; ``{"connected": false}`` when the owner
        hasn't linked it, so a caller can fall back cleanly.
        """
        # Never let a proxy/browser cache a vended credential.
        response.headers["Cache-Control"] = "no-store"
        if not x_omnigent_host_token:
            raise HTTPException(status_code=401, detail="missing host token")
        managed = await asyncio.to_thread(
            host_store.resolve_launch_token, host_id, x_omnigent_host_token
        )
        if managed is None:
            raise HTTPException(status_code=401, detail="unauthenticated")
        # Resolve provider only after auth so the endpoint reveals nothing to an
        # unauthenticated caller. A resolver with no configured store on this
        # server (or an unknown provider) is a 404 — nothing to vend.
        resolver = resolvers.get(provider)
        store = getattr(request.app.state, f"{provider}_store", None)
        client = getattr(request.app.state, f"{provider}_client", None)
        if resolver is None or store is None:
            raise HTTPException(status_code=404, detail="unknown credential provider")
        try:
            payload = await resolver(managed.user_id, store=store, client=client)
        except Exception:  # noqa: BLE001 - a provider resolver fault must degrade, not 500
            _logger.warning("credential resolve failed for provider %r", provider, exc_info=True)
            return {"connected": False}
        if payload is None:
            return {"connected": False}
        return {"connected": True, "owner": managed.user_id, **payload}

    return router
