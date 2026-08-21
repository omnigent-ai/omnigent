"""Per-user Databricks connection store.

A thin typed façade over the provider-agnostic
:class:`~omnigent.server.credential_store.IntegrationCredentialStore`
(``provider="databricks"``): it maps a :class:`DatabricksConnection` to/from the
generic ``(secret, metadata)`` shape. Reuses the same table and cipher as every
other provider — no new schema. Mirrors
:class:`~omnigent.server.github_store.GithubConnectionStore`. See
``designs/DATABRICKS_CONNECT.md``.
"""

from __future__ import annotations

from typing import Any

from omnigent.entities import DatabricksConnection, IntegrationConnection
from omnigent.server.credential_store import IntegrationCredentialStore
from omnigent.server.databricks_app import DatabricksTokenSet
from omnigent.server.secretbox import SecretCipher

#: Provider key for Databricks rows in the shared credential store.
_PROVIDER = "databricks"


class DatabricksConnectionStore:
    """Databricks-typed façade over :class:`IntegrationCredentialStore`.

    :param storage_location: SQLAlchemy database URI.
    :param secret_cipher: Cipher for the secret blob at rest.
    """

    def __init__(self, storage_location: str, secret_cipher: SecretCipher) -> None:
        self.storage_location = storage_location
        self._store = IntegrationCredentialStore(storage_location, secret_cipher)

    @staticmethod
    def _to_databricks(conn: IntegrationConnection) -> DatabricksConnection:
        secret = conn.secret or {}
        meta = conn.metadata
        return DatabricksConnection(
            user_id=conn.user_id,
            workspace_host=str(meta.get("workspace_host") or ""),
            databricks_user=str(meta.get("databricks_user") or ""),
            databricks_user_id=str(meta.get("databricks_user_id") or ""),
            access_token=secret.get("access_token") if conn.secret is not None else None,
            refresh_token=secret.get("refresh_token") if conn.secret is not None else None,
            token_expires_at=meta.get("token_expires_at"),
            refresh_token_expires_at=meta.get("refresh_token_expires_at"),
            scopes=str(meta.get("scopes") or ""),
            created_at=conn.created_at,
            updated_at=conn.updated_at,
        )

    @staticmethod
    def _secret(tokens: DatabricksTokenSet) -> dict[str, Any]:
        return {"access_token": tokens.access_token, "refresh_token": tokens.refresh_token}

    @staticmethod
    def _metadata(
        tokens: DatabricksTokenSet,
        *,
        workspace_host: str,
        databricks_user: str,
        databricks_user_id: str,
    ) -> dict[str, Any]:
        return {
            "workspace_host": workspace_host,
            "databricks_user": databricks_user,
            "databricks_user_id": databricks_user_id,
            "token_expires_at": tokens.expires_at,
            "refresh_token_expires_at": tokens.refresh_token_expires_at,
            "scopes": tokens.scopes,
        }

    def upsert(
        self,
        user_id: str,
        *,
        workspace_host: str,
        databricks_user: str,
        databricks_user_id: str,
        tokens: DatabricksTokenSet,
    ) -> DatabricksConnection:
        """Create or replace a user's Databricks connection (idempotent on ``user_id``)."""
        conn = self._store.upsert(
            user_id,
            _PROVIDER,
            secret=self._secret(tokens),
            metadata=self._metadata(
                tokens,
                workspace_host=workspace_host,
                databricks_user=databricks_user,
                databricks_user_id=databricks_user_id,
            ),
        )
        return self._to_databricks(conn)

    def update_tokens(self, user_id: str, tokens: DatabricksTokenSet) -> bool:
        """Persist a refreshed token set, preserving the connected workspace/user.

        Returns ``True`` if a row was present to update, ``False`` if it was
        removed between read and refresh.
        """
        existing = self._store.get(user_id, _PROVIDER)
        if existing is None:
            return False
        meta = dict(existing.metadata)
        meta["token_expires_at"] = tokens.expires_at
        meta["refresh_token_expires_at"] = tokens.refresh_token_expires_at
        if tokens.scopes:
            meta["scopes"] = tokens.scopes
        self._store.update_secret(user_id, _PROVIDER, secret=self._secret(tokens), metadata=meta)
        return True

    def get(self, user_id: str, *, with_tokens: bool = False) -> DatabricksConnection | None:
        """Look up a user's connection; decrypt tokens only when *with_tokens*."""
        conn = self._store.get(user_id, _PROVIDER, with_secret=with_tokens)
        return self._to_databricks(conn) if conn is not None else None

    def delete(self, user_id: str) -> bool:
        """Remove a user's Databricks connection. Returns ``True`` if a row went."""
        return self._store.delete(user_id, _PROVIDER)

    def list_all(self) -> list[DatabricksConnection]:
        """All Databricks connections (metadata only) — tests/admin use."""
        return [self._to_databricks(c) for c in self._store.list_all(provider=_PROVIDER)]
