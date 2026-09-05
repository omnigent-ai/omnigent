"""Per-user GitHub App connection store.

A GitHub-typed :class:`~omnigent.connections.ConnectionStore` façade
(``provider="github"``): it maps a :class:`GithubConnection` to/from the generic
``(secret, metadata)`` shape so the integration routes, the credential broker,
and the launch path keep a GitHub-typed API. Only server-side code touches it;
token material is encrypted at rest by the underlying store's cipher, so callers
never see ciphertext. See ``designs/CREDENTIAL_STORE.md``.
"""

from __future__ import annotations

from typing import Any, ClassVar

from omnigent.connections import ConnectionStore
from omnigent.entities import GithubConnection, ProviderConnection
from omnigent.server.github_app import GitHubTokenSet


class GithubConnectionStore(ConnectionStore[GithubConnection]):
    """GitHub-typed façade over the shared credential store.

    Inherits the uniform ``get`` / ``delete`` / ``list_all`` from
    :class:`ConnectionStore`; adds the GitHub-specific ``upsert`` /
    ``update_tokens`` writes and the row → :class:`GithubConnection` mapping.
    """

    _PROVIDER: ClassVar[str] = "github"

    @staticmethod
    def _to_entity(conn: ProviderConnection) -> GithubConnection:
        """Rebuild a :class:`GithubConnection` from a generic connection."""
        secret = conn.secret or {}
        meta = conn.metadata
        return GithubConnection(
            user_id=conn.user_id,
            github_login=str(meta.get("github_login") or ""),
            github_user_id=int(meta.get("github_user_id") or 0),
            access_token=secret.get("access_token") if conn.secret is not None else None,
            refresh_token=secret.get("refresh_token") if conn.secret is not None else None,
            token_expires_at=meta.get("token_expires_at"),
            refresh_token_expires_at=meta.get("refresh_token_expires_at"),
            scopes=str(meta.get("scopes") or ""),
            created_at=conn.created_at,
            updated_at=conn.updated_at,
        )

    @staticmethod
    def _secret(tokens: GitHubTokenSet) -> dict[str, Any]:
        return {"access_token": tokens.access_token, "refresh_token": tokens.refresh_token}

    @staticmethod
    def _metadata(
        tokens: GitHubTokenSet, *, github_login: str, github_user_id: int
    ) -> dict[str, Any]:
        return {
            "github_login": github_login,
            "github_user_id": github_user_id,
            "token_expires_at": tokens.expires_at,
            "refresh_token_expires_at": tokens.refresh_token_expires_at,
            "scopes": tokens.scopes,
        }

    def upsert(
        self,
        user_id: str,
        *,
        github_login: str,
        github_user_id: int,
        tokens: GitHubTokenSet,
    ) -> GithubConnection:
        """Create or replace a user's GitHub connection (idempotent on ``user_id``)."""
        conn = self._store.upsert(
            user_id,
            self._PROVIDER,
            secret=self._secret(tokens),
            metadata=self._metadata(
                tokens, github_login=github_login, github_user_id=github_user_id
            ),
        )
        return self._to_entity(conn)

    def update_tokens(self, user_id: str, tokens: GitHubTokenSet) -> None:
        """Persist a refreshed token set, preserving the connected login/id.

        No-op if the connection was removed between read and refresh.
        """
        existing = self._store.get(user_id, self._PROVIDER)
        if existing is None:
            return
        meta = dict(existing.metadata)
        meta["token_expires_at"] = tokens.expires_at
        meta["refresh_token_expires_at"] = tokens.refresh_token_expires_at
        if tokens.scopes:
            meta["scopes"] = tokens.scopes
        self._store.update_secret(
            user_id, self._PROVIDER, secret=self._secret(tokens), metadata=meta
        )
