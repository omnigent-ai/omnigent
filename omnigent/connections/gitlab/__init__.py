"""Encrypted, typed store for per-user GitLab OAuth connections."""

from __future__ import annotations

from typing import Any, ClassVar

from omnigent.connections import ConnectionStore
from omnigent.entities import GitlabConnection, ProviderConnection
from omnigent.server.gitlab_app import GitLabTokenSet


class GitlabConnectionStore(ConnectionStore[GitlabConnection]):
    """GitLab facade over the shared encrypted credential store."""

    _PROVIDER: ClassVar[str] = "gitlab"

    @staticmethod
    def _to_entity(conn: ProviderConnection) -> GitlabConnection:
        secret = conn.secret or {}
        meta = conn.metadata
        return GitlabConnection(
            user_id=conn.user_id,
            gitlab_login=str(meta.get("gitlab_login") or ""),
            gitlab_user_id=int(meta.get("gitlab_user_id") or 0),
            gitlab_host=str(meta.get("gitlab_host") or ""),
            access_token=secret.get("access_token") if conn.secret is not None else None,
            refresh_token=secret.get("refresh_token") if conn.secret is not None else None,
            token_expires_at=meta.get("token_expires_at"),
            scopes=str(meta.get("scopes") or ""),
            created_at=conn.created_at,
            updated_at=conn.updated_at,
        )

    @staticmethod
    def _secret(tokens: GitLabTokenSet) -> dict[str, Any]:
        return {"access_token": tokens.access_token, "refresh_token": tokens.refresh_token}

    def upsert(
        self,
        user_id: str,
        *,
        gitlab_login: str,
        gitlab_user_id: int,
        gitlab_host: str,
        tokens: GitLabTokenSet,
    ) -> GitlabConnection:
        conn = self._store.upsert(
            user_id,
            self._PROVIDER,
            secret=self._secret(tokens),
            metadata={
                "gitlab_login": gitlab_login,
                "gitlab_user_id": gitlab_user_id,
                "gitlab_host": gitlab_host,
                "token_expires_at": tokens.expires_at,
                "scopes": tokens.scopes,
            },
        )
        return self._to_entity(conn)

    def update_tokens(self, user_id: str, tokens: GitLabTokenSet) -> None:
        existing = self._store.get(user_id, self._PROVIDER)
        if existing is None:
            return
        metadata = dict(existing.metadata)
        metadata["token_expires_at"] = tokens.expires_at
        if tokens.scopes:
            metadata["scopes"] = tokens.scopes
        self._store.update_secret(
            user_id, self._PROVIDER, secret=self._secret(tokens), metadata=metadata
        )
