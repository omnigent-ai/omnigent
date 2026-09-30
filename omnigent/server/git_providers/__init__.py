"""Server-side git provider facets: a provider's per-user connection.

A provider's ``connection`` facet module (see
:func:`omnigent.git_providers.load_facet`) exposes one :class:`ConnectionFacet`
as ``CONNECTION``. The server reads the facet's config from the environment,
builds its connection store and API client, mounts its router under ``/v1``, and
vends its credential to sandboxes through ``/v1/hosts/{host_id}/credentials/<id>``.
The wired objects live on ``app.state.<id>_config``, ``<id>_store``, and
``<id>_client``.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from omnigent.git_providers import load_facet, provider, providers

if TYPE_CHECKING:
    from fastapi import APIRouter

    from omnigent.server.auth import AuthProvider
    from omnigent.stores.credential_store import SecretCipher

_logger = logging.getLogger(__name__)


@runtime_checkable
class ConnectionFacet(Protocol):
    """The server half of one provider's per-user connection.

    The config, store, and client are the provider's own types. The server only
    keeps them on ``app.state`` and passes them back to the facet.
    """

    @property
    def repo_browser(self) -> bool:
        """Whether the router lists the user's repositories for the new-chat picker."""
        ...

    def config_from_env(self) -> Any | None:
        """Return the provider's config from the environment, or ``None`` when unset.

        Raise when the environment configures the provider incorrectly; the
        server then fails to start.
        """
        ...

    def make_store(self, db_uri: str, cipher: SecretCipher) -> Any:
        """Return the per-user connection store over the shared credential store.

        An exception stops server startup.
        """
        ...

    def make_client(self, config: Any) -> Any:
        """Return the provider API client that the router and the resolver share."""
        ...

    def make_router(
        self, config: Any, store: Any, *, auth_provider: AuthProvider | None, client: Any
    ) -> APIRouter:
        """Return the provider's ``/connections/<id>/*`` routes, mounted under ``/v1``."""
        ...

    async def resolve_credential(
        self, user_id: str, *, store: Any, client: Any
    ) -> dict[str, object] | None:
        """Return the credential to vend for *user_id*, or ``None`` when not linked.

        The payload has ``token`` and may have ``username``, ``expires_at`` (epoch
        seconds or null), and ``hosts`` (lower-cased git host names with no scheme
        or port).
        """
        ...


def connection_facets() -> Iterator[tuple[str, ConnectionFacet]]:
    """Yield ``(provider_id, facet)`` for each provider whose connection facet loads.

    Providers come in registration order. A facet that fails to import, or that
    does not implement :class:`ConnectionFacet`, is logged and skipped.
    """
    for descriptor in providers():
        provider_id = descriptor.id
        try:
            facet = load_facet(provider_id, "connection")
        except Exception as exc:  # noqa: BLE001 - a broken facet must not stop the server
            # The exception text can quote configuration, so log only its type.
            _logger.warning(
                "Git provider %s connection facet failed to import (%s); skipping it",
                provider_id,
                type(exc).__name__,
            )
            continue
        if facet is None:
            continue
        if not isinstance(facet, ConnectionFacet):
            _logger.warning(
                "Git provider %s connection facet does not implement ConnectionFacet; skipping it",
                provider_id,
            )
            continue
        yield provider_id, facet


def _build_secret_cipher() -> SecretCipher | None:
    """Build the credential store cipher from the deployment config."""
    from omnigent.stores.credential_store import build_secret_cipher

    return build_secret_cipher()


def connections_from_env(
    db_uri: str,
    *,
    cipher_factory: Callable[[], SecretCipher | None] | None = None,
) -> dict[str, tuple[Any, Any]]:
    """Build ``{provider_id: (config, store)}`` for the connections set in the environment.

    A provider is included when its ``config_from_env`` returns a config. Its
    store is ``None`` when the credential store has no cipher, which keeps the
    connection disabled. A facet module that fails to import is logged and
    skipped (see :func:`connection_facets`).

    :param db_uri: SQLAlchemy database URI that the connection stores share.
    :param cipher_factory: Builds the credential store cipher. It is called at
        most once, and only when a provider is configured. Defaults to
        :func:`omnigent.stores.credential_store.build_secret_cipher`.
    :returns: The ``connections`` mapping for :func:`omnigent.server.app.create_app`.
    :raises Exception: Whatever a facet's ``config_from_env`` or ``make_store``
        raises for a misconfigured provider, so the server fails to start.
    """
    cipher = functools.cache(cipher_factory or _build_secret_cipher)
    connections: dict[str, tuple[Any, Any]] = {}
    for provider_id, facet in connection_facets():
        config = facet.config_from_env()
        if config is None:
            continue
        secret_cipher = cipher()
        if secret_cipher is None:
            descriptor = provider(provider_id)
            _logger.error(
                "%s is configured but disabled: set OMNIGENT_CREDENTIAL_ENC_KEY "
                "(the credential store's encryption key) to enable it.",
                descriptor.display_name if descriptor is not None else provider_id,
            )
            connections[provider_id] = (config, None)
            continue
        connections[provider_id] = (config, facet.make_store(db_uri, secret_cipher))
    return connections
