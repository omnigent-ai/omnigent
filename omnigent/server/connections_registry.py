"""Registry of per-user connection providers (git providers, Databricks).

Each git provider whose ``connection`` facet loads contributes one entry (see
:mod:`omnigent.server.git_providers`), in provider registration order;
Databricks follows. ``create_app`` iterates :func:`connection_providers` to set
``app.state.<name>_{config,store,client}`` and mount the provider's router
whenever it is configured (both its config and its connection store are present).
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
    """

    name: str
    client_factory: Callable[[Any], Any]
    router_factory: Callable[..., Any]
    credential_resolver: Callable[..., Awaitable[dict[str, Any] | None]] | None = None


def connection_providers() -> list[ConnectionProvider]:
    """The connection providers this build knows how to wire, in a stable order.

    Git provider connection facets come first, in provider registration order,
    then Databricks. Imports are deferred so importing this module stays cheap
    and free of import cycles through the route modules.
    """
    from omnigent.server.databricks_app_client import DatabricksAppClient
    from omnigent.server.databricks_identity import resolve_databricks_credential
    from omnigent.server.git_providers import connection_facets
    from omnigent.server.routes.connections_databricks import (
        create_connections_databricks_router,
    )

    git_connections = [
        ConnectionProvider(
            name=provider_id,
            client_factory=facet.make_client,
            router_factory=facet.make_router,
            credential_resolver=facet.resolve_credential,
        )
        for provider_id, facet in connection_facets()
    ]
    return [
        *git_connections,
        ConnectionProvider(
            name="databricks",
            client_factory=DatabricksAppClient,
            router_factory=create_connections_databricks_router,
            credential_resolver=resolve_databricks_credential,
        ),
    ]
