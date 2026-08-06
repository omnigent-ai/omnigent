"""Persistence for per-user GitHub App connections.

Sibling to :class:`omnigent.server.accounts_store.SqlAlchemyAccountStore`
— same database, server-only surface. Only the integration routes and
the managed-sandbox launch path touch it. See
``designs/GITHUB_APP_SANDBOX_AUTH.md``.

Token columns are encrypted at rest with a :class:`SecretBox`; the
store is the only place ciphertext ⇄ plaintext crosses, so callers work
with plain :class:`GithubConnection` entities and never see the
ciphertext.
"""

from __future__ import annotations

from sqlalchemy import delete, select

from omnigent.db.db_models import SqlGithubConnection, current_workspace_id
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker, now_epoch
from omnigent.entities import GithubConnection
from omnigent.server.github_app import GitHubTokenSet
from omnigent.server.secretbox import SecretBox


class GithubConnectionStore:
    """SQLAlchemy-backed persistence for GitHub App connections.

    :param storage_location: SQLAlchemy database URI. Shares the
        connection pool with the other stores via
        :func:`get_or_create_engine`.
    :param secret_box: Encryptor for token columns at rest.
    """

    def __init__(self, storage_location: str, secret_box: SecretBox) -> None:
        self.storage_location = storage_location
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)
        self._box = secret_box

    def _to_entity(self, row: SqlGithubConnection, *, with_tokens: bool) -> GithubConnection:
        """Convert an ORM row to an entity, optionally decrypting tokens."""
        access = self._box.decrypt(row.access_token_enc) if with_tokens else None
        refresh = (
            self._box.decrypt(row.refresh_token_enc)
            if with_tokens and row.refresh_token_enc is not None
            else None
        )
        return GithubConnection(
            user_id=row.user_id,
            github_login=row.github_login,
            github_user_id=row.github_user_id,
            access_token=access,
            refresh_token=refresh,
            token_expires_at=row.token_expires_at,
            refresh_token_expires_at=row.refresh_token_expires_at,
            scopes=row.scopes,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def upsert(
        self,
        user_id: str,
        *,
        github_login: str,
        github_user_id: int,
        tokens: GitHubTokenSet,
    ) -> GithubConnection:
        """Create or replace a user's GitHub connection.

        Idempotent on ``user_id``: reconnecting overwrites the tokens and
        login in place, preserving the original ``created_at``.

        :param user_id: The omnigent user connecting their account.
        :param github_login: The connected GitHub login.
        :param github_user_id: The connected GitHub numeric id.
        :param tokens: The freshly-exchanged token set.
        :returns: The stored connection (tokens decrypted).
        """
        now = now_epoch()
        access_enc = self._box.encrypt(tokens.access_token)
        refresh_enc = (
            self._box.encrypt(tokens.refresh_token) if tokens.refresh_token is not None else None
        )
        with self._session() as session:
            row = session.get(SqlGithubConnection, (current_workspace_id(), user_id))
            if row is None:
                row = SqlGithubConnection(
                    user_id=user_id,
                    github_login=github_login,
                    github_user_id=github_user_id,
                    access_token_enc=access_enc,
                    refresh_token_enc=refresh_enc,
                    token_expires_at=tokens.expires_at,
                    refresh_token_expires_at=tokens.refresh_token_expires_at,
                    scopes=tokens.scopes,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.github_login = github_login
                row.github_user_id = github_user_id
                row.access_token_enc = access_enc
                row.refresh_token_enc = refresh_enc
                row.token_expires_at = tokens.expires_at
                row.refresh_token_expires_at = tokens.refresh_token_expires_at
                row.scopes = tokens.scopes
                row.updated_at = now
            session.flush()
            return self._to_entity(row, with_tokens=True)

    def update_tokens(self, user_id: str, tokens: GitHubTokenSet) -> None:
        """Persist a refreshed token set for an existing connection.

        No-op if the connection was removed between read and refresh.

        :param user_id: The connection's owner.
        :param tokens: The refreshed token set.
        """
        with self._session() as session:
            row = session.get(SqlGithubConnection, (current_workspace_id(), user_id))
            if row is None:
                return
            row.access_token_enc = self._box.encrypt(tokens.access_token)
            row.refresh_token_enc = (
                self._box.encrypt(tokens.refresh_token)
                if tokens.refresh_token is not None
                else None
            )
            row.token_expires_at = tokens.expires_at
            row.refresh_token_expires_at = tokens.refresh_token_expires_at
            if tokens.scopes:
                row.scopes = tokens.scopes
            row.updated_at = now_epoch()

    def get(self, user_id: str, *, with_tokens: bool = False) -> GithubConnection | None:
        """Look up a user's connection.

        :param user_id: The connection's owner.
        :param with_tokens: When ``True``, decrypt the token columns onto
            the returned entity (launch path). When ``False`` (default),
            the token fields are ``None`` — the safe shape for status
            endpoints that must not surface secrets.
        :returns: The connection, or ``None`` when the user has none.
        """
        with self._session() as session:
            row = session.get(SqlGithubConnection, (current_workspace_id(), user_id))
            return self._to_entity(row, with_tokens=with_tokens) if row is not None else None

    def delete(self, user_id: str) -> bool:
        """Remove a user's connection.

        :param user_id: The connection's owner.
        :returns: ``True`` when a row was deleted.
        """
        with self._session() as session:
            result = session.execute(
                delete(SqlGithubConnection).where(
                    SqlGithubConnection.workspace_id == current_workspace_id(),
                    SqlGithubConnection.user_id == user_id,
                )
            )
            return result.rowcount > 0

    def list_all(self) -> list[GithubConnection]:
        """Return all connections (metadata only) — for tests/admin use."""
        with self._session() as session:
            rows = (
                session.execute(
                    select(SqlGithubConnection).where(
                        SqlGithubConnection.workspace_id == current_workspace_id()
                    )
                )
                .scalars()
                .all()
            )
            return [self._to_entity(r, with_tokens=False) for r in rows]
