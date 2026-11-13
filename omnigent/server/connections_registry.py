"""Registry of per-user connection providers (GitHub, Databricks, ...).

Wiring a new provider into the server is a single entry here plus its
:class:`~omnigent.server.routes.connections_base.ConnectionHooks` adapter —
not another hand-copied block in :func:`create_app`. ``create_app`` iterates
:func:`connection_providers` to set ``app.state.<name>_{config,store,client}``
and mount the provider's router whenever it is configured (both its config and
its connection store are present).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ConnectionProvider:
    """One provider's server-side factories.

    :param name: URL/state segment and ``app.state`` prefix, e.g. ``"github"``.
    :param client_factory: ``(config) -> client`` for the OAuth/API client.
    :param router_factory: ``(config, store, *, auth_provider, client) ->
        APIRouter`` building the provider's ``/connections/{name}/*`` routes.
    :param credential_resolver: ``(user_id, *, store, client) -> {"token": …,
        **attribution metadata} | None`` — the adapter the generic host
        credential broker (:mod:`omnigent.server.routes.host_credentials`) calls
        to vend this provider's secret to a sandbox. ``None`` when the provider
        has no broker endpoint (connect-only, or on-demand delivery not built
        yet), in which case ``/hosts/{id}/credentials/{name}`` returns ``404``.
    :param owner_identity_resolver: Same shape as *credential_resolver*, used
        when the SERVER acts as the user against the provider's API — a
        managed-sandbox launcher whose ``owner_credential_provider`` is this
        provider (e.g. Databricks Sandboxes created and driven as their owner).
        ``None`` when the provider cannot back an owner identity.
    """

    name: str
    client_factory: Callable[[Any], Any]
    router_factory: Callable[..., Any]
    credential_resolver: Callable[..., Awaitable[dict[str, Any] | None]] | None = None
    owner_identity_resolver: Callable[..., Awaitable[dict[str, Any] | None]] | None = None


def connection_providers() -> list[ConnectionProvider]:
    """The connection providers this build knows how to wire, in a stable order.

    Imports are deferred so importing this module stays cheap and free of import
    cycles through the route modules.
    """
    from functools import partial

    from omnigent.server.databricks_app_client import DatabricksAppClient
    from omnigent.server.databricks_identity import (
        OWNER_IDENTITY_REFRESH_MARGIN_S,
        resolve_databricks_credential,
    )
    from omnigent.server.github_app_client import GitHubAppClient
    from omnigent.server.github_identity import resolve_github_credential
    from omnigent.server.routes.connections_databricks import (
        create_connections_databricks_router,
    )
    from omnigent.server.routes.connections_github import (
        create_connections_github_router,
    )

    return [
        ConnectionProvider(
            name="github",
            client_factory=GitHubAppClient,
            router_factory=create_connections_github_router,
            credential_resolver=resolve_github_credential,
        ),
        ConnectionProvider(
            name="databricks",
            client_factory=DatabricksAppClient,
            router_factory=create_connections_databricks_router,
            credential_resolver=resolve_databricks_credential,
            owner_identity_resolver=partial(
                resolve_databricks_credential,
                refresh_margin_s=OWNER_IDENTITY_REFRESH_MARGIN_S,
            ),
        ),
    ]


def blocking_owner_resolver(
    resolver: Callable[..., Awaitable[dict[str, Any] | None]],
    *,
    store: Any,
    client: Any,
) -> Callable[[str], dict[str, Any] | None]:
    """Adapt an async owner-identity resolver to the blocking ``(user_id) -> payload``
    shape managed-sandbox launchers call from worker threads.

    Each call runs the resolver on a private event loop in the calling thread
    (the store read and any token refresh are per-call, so nothing is shared
    with the server loop). The caller's contextvars — notably the workspace
    scope — carry into it. Never call this from an event-loop thread.

    :param resolver: The provider's ``owner_identity_resolver``.
    :param store: The provider's connection store.
    :param client: The provider's OAuth/API client.
    :returns: ``resolve(user_id)`` returning the payload or ``None``.
    """
    import asyncio

    async def _resolve(user_id: str) -> dict[str, Any] | None:
        return await resolver(user_id, store=store, client=client)

    def resolve(user_id: str) -> dict[str, Any] | None:
        return asyncio.run(_resolve(user_id))

    return resolve
