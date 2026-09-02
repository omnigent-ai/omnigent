"""Registry of per-user connection providers (GitHub, Databricks, ...).

Wiring a new provider into the server is a single entry here plus its
:class:`~omnigent.server.routes.connections_base.ConnectionHooks` adapter —
not another hand-copied block in :func:`create_app`. ``create_app`` iterates
:func:`connection_providers` to set ``app.state.<name>_{config,store,client}``
and mount the provider's router whenever it is configured (both its config and
its connection store are present).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ConnectionProvider:
    """One provider's server-side factories.

    :param name: URL/state segment and ``app.state`` prefix, e.g. ``"github"``.
    :param client_factory: ``(config) -> client`` for the OAuth/API client.
    :param router_factory: ``(config, store, *, auth_provider, client) ->
        APIRouter`` building the provider's ``/connections/{name}/*`` routes.
    """

    name: str
    client_factory: Callable[[Any], Any]
    router_factory: Callable[..., Any]


def connection_providers() -> list[ConnectionProvider]:
    """The connection providers this build knows how to wire, in a stable order.

    Imports are deferred so importing this module stays cheap and free of import
    cycles through the route modules.
    """
    from omnigent.server.github_app_client import GitHubAppClient
    from omnigent.server.routes.connections_github import (
        create_connections_github_router,
    )

    return [
        ConnectionProvider(
            name="github",
            client_factory=GitHubAppClient,
            router_factory=create_connections_github_router,
        ),
    ]
