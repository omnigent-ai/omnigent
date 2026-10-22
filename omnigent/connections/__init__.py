"""Provider-façade base over the generic credential store.

:class:`ConnectionStore` is the per-provider surface the routes talk to: it owns
a :class:`~omnigent.stores.credential_store.CredentialStore` and maps the
generic :class:`~omnigent.entities.ProviderConnection` rows into a typed,
provider-specific entity. Concrete façades (``GithubConnectionStore``,
``DatabricksConnectionStore``) subtype this, set ``_PROVIDER``, implement
``_to_entity``, and add the provider-specific writes (``upsert`` /
``update_tokens``); the uniform reads (``get`` / ``delete`` / ``list_all``) are
shared here. See ``designs/CREDENTIAL_STORE.md``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Generic, TypeVar

from omnigent.entities import ProviderConnection
from omnigent.stores.credential_store.secret_cipher import SecretCipher
from omnigent.stores.credential_store.sqlalchemy_store import CredentialStore

EntityT = TypeVar("EntityT")


class ConnectionStore(ABC, Generic[EntityT]):
    """Typed per-provider façade over the shared :class:`CredentialStore`.

    :param storage_location: SQLAlchemy database URI (shares the pool).
    :param secret_cipher: Cipher for the secret blob at rest.
    """

    #: Provider key this façade owns, e.g. ``"github"``. Set by each subclass.
    _PROVIDER: ClassVar[str]

    def __init__(self, storage_location: str, secret_cipher: SecretCipher) -> None:
        self._store = CredentialStore(storage_location, secret_cipher)

    @staticmethod
    @abstractmethod
    def _to_entity(conn: ProviderConnection) -> EntityT:
        """Map a generic connection row into this provider's typed entity."""

    def get(self, user_id: str, *, with_tokens: bool = False) -> EntityT | None:
        """Return the user's connection for this provider, or ``None``.

        ``with_tokens=True`` decrypts the secret onto the entity (vend path);
        the default metadata-only view is the safe shape for status endpoints.
        """
        conn = self._store.get(user_id, self._PROVIDER, with_secret=with_tokens)
        return self._to_entity(conn) if conn is not None else None

    def delete(self, user_id: str) -> bool:
        """Remove the user's connection for this provider. ``True`` if a row went."""
        return self._store.delete(user_id, self._PROVIDER)

    def list_all(self) -> list[EntityT]:
        """All connections for this provider (metadata only) — tests/admin."""
        return [self._to_entity(c) for c in self._store.list_all(provider=self._PROVIDER)]
