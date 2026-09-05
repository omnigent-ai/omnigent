"""Provider-agnostic per-user integration credential store.

Backs every "Connect …" integration (GitHub App today, MCP connectors later):
one encrypted secret blob + non-secret metadata per
``(workspace_id, user_id, provider, account_id)``. The secret material is the
only thing encrypted (via a :class:`~omnigent.stores.credential_store.secret_cipher.SecretCipher`);
it is the sole place ciphertext ⇄ plaintext crosses, so callers work with plain
:class:`~omnigent.entities.ProviderConnection` entities. Provider-specific
typed façades (e.g. :class:`~omnigent.connections.github.GithubConnectionStore`)
sit on top of this. See ``designs/CREDENTIAL_STORE.md``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError

from omnigent.db.db_models import SqlConnection, current_workspace_id
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker, now_epoch
from omnigent.entities import ProviderConnection
from omnigent.stores.credential_store.secret_cipher import SecretCipher

_logger = logging.getLogger(__name__)


def _enc_context(
    workspace_id: int, user_id: str, provider: str, account_id: str
) -> dict[str, str]:
    """The row's identity, passed to the cipher so each row encrypts under its
    own derived key (per-user encryption). Decryption must use the same
    identity, so it is always taken from the row being read."""
    return {
        "workspace_id": str(workspace_id),
        "user_id": user_id,
        "provider": provider,
        "account_id": account_id,
    }


def _safe_json_obj(raw: str | None) -> dict[str, Any] | None:
    """Parse a JSON object column, returning ``None`` on empty/corrupt/non-object
    data instead of raising.

    The vend path is deliberately soft-fail: :meth:`SecretCipher.decrypt`
    already degrades a wrong key to ``None`` (⇒ reconnect), so the JSON parse
    that immediately follows must not re-introduce a 500 on a truncated or
    malformed column.
    """
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


class CredentialStore:
    """SQLAlchemy-backed persistence for per-user integration credentials.

    :param storage_location: SQLAlchemy database URI (shares the pool with the
        other stores via :func:`get_or_create_engine`).
    :param secret_cipher: Cipher for the secret blob at rest.
    """

    def __init__(self, storage_location: str, secret_cipher: SecretCipher) -> None:
        self.storage_location = storage_location
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.credential_store"
        )
        self._cipher = secret_cipher

    def _build_entity(
        self, row: SqlConnection, secret: dict[str, Any] | None
    ) -> ProviderConnection:
        """Build an entity from a row and an already-resolved *secret*.

        Metadata is parsed soft-fail (a corrupt column reads as ``{}`` rather
        than 500-ing the caller).
        """
        return ProviderConnection(
            user_id=row.user_id,
            provider=row.provider,
            account_id=row.account_id,
            secret=secret,
            metadata=_safe_json_obj(row.metadata_json) or {},
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def _to_entity(self, row: SqlConnection, *, with_secret: bool) -> ProviderConnection:
        """Convert an ORM row to an entity, optionally decrypting the secret."""
        secret: dict[str, Any] | None = None
        if with_secret:
            context = _enc_context(row.workspace_id, row.user_id, row.provider, row.account_id)
            plaintext = self._cipher.decrypt(row.secret_enc, context=context)
            # ``None`` (wrong key/context or corrupt) degrades to "reconnect",
            # not a 500; the JSON parse is soft-fail for the same reason.
            secret = _safe_json_obj(plaintext) if plaintext is not None else None
        return self._build_entity(row, secret)

    def upsert(
        self,
        user_id: str,
        provider: str,
        *,
        secret: dict[str, Any],
        metadata: dict[str, Any],
        account_id: str = "",
    ) -> ProviderConnection:
        """Create or replace a user's connection for *provider*.

        Idempotent on ``(user_id, provider, account_id)``: reconnecting
        overwrites the secret and metadata in place, preserving ``created_at``.
        Concurrency-safe — two simultaneous connects (a double-clicked
        "Connect") don't 500 on a primary-key violation; the loser retries as an
        update.
        """
        now = now_epoch()
        workspace_id = current_workspace_id()
        pk = (workspace_id, user_id, provider, account_id)
        context = _enc_context(workspace_id, user_id, provider, account_id)
        secret_enc = self._cipher.encrypt(json.dumps(secret), context=context)
        metadata_json = json.dumps(metadata)

        def _apply(session: Any, row: SqlConnection | None) -> SqlConnection:
            if row is None:
                row = SqlConnection(
                    user_id=user_id,
                    provider=provider,
                    account_id=account_id,
                    secret_enc=secret_enc,
                    metadata_json=metadata_json,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.secret_enc = secret_enc
                row.metadata_json = metadata_json
                row.updated_at = now
            return row

        with self._session("upsert") as session:
            row = _apply(session, session.get(SqlConnection, pk))
            try:
                session.flush()
            except IntegrityError:
                # A concurrent reconnect inserted the same PK between our get
                # and flush. Roll back and retry as an update below.
                session.rollback()
            else:
                # Return the entity from the secret we just wrote — no wasted
                # decrypt of the ciphertext we produced a few lines up.
                return self._build_entity(row, secret)
        with self._session("upsert_retry") as session:
            row = _apply(session, session.get(SqlConnection, pk))
            session.flush()
            return self._build_entity(row, secret)

    def update_secret(
        self,
        user_id: str,
        provider: str,
        *,
        secret: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        account_id: str = "",
    ) -> bool:
        """Persist a refreshed secret (and optional metadata) for an existing row.

        Returns ``True`` when a row was updated, ``False`` when the connection
        was removed between read and refresh. The caller must not treat
        ``False`` as success: providers that rotate refresh tokens (GitHub does)
        have already spent the old one, so a silently-dropped refresh wedges the
        user until they reconnect — worth surfacing, not swallowing.
        """
        workspace_id = current_workspace_id()
        with self._session("update_secret") as session:
            row = session.get(SqlConnection, (workspace_id, user_id, provider, account_id))
            if row is None:
                _logger.warning(
                    "credential_store: update_secret found no %s connection for the user "
                    "(removed mid-refresh); the refreshed secret was discarded",
                    provider,
                )
                return False
            context = _enc_context(workspace_id, user_id, provider, account_id)
            row.secret_enc = self._cipher.encrypt(json.dumps(secret), context=context)
            if metadata is not None:
                row.metadata_json = json.dumps(metadata)
            row.updated_at = now_epoch()
            return True

    def get(
        self,
        user_id: str,
        provider: str,
        *,
        account_id: str = "",
        with_secret: bool = False,
    ) -> ProviderConnection | None:
        """Look up a user's connection for *provider*.

        :param with_secret: When ``True``, decrypt the secret onto the returned
            entity (vend path). When ``False`` (default), ``secret`` is ``None``
            — the safe shape for status endpoints that must not surface secrets.
        :returns: ``None`` iff there is no connection row. A returned entity
            always exists; disambiguate its ``secret``: with ``with_secret=False``
            it is ``None`` by construction, while with ``with_secret=True`` a
            ``None`` secret means the ciphertext could not be decrypted (wrong
            key/context or corrupt) — treat that as "reconnect", not "no row".
        """
        with self._session("get") as session:
            row = session.get(
                SqlConnection, (current_workspace_id(), user_id, provider, account_id)
            )
            return self._to_entity(row, with_secret=with_secret) if row is not None else None

    def delete(self, user_id: str, provider: str, *, account_id: str = "") -> bool:
        """Remove a user's connection for *provider*. Returns ``True`` if a row went."""
        with self._session("delete") as session:
            result = cast(
                CursorResult[tuple[object]],
                session.execute(
                    delete(SqlConnection).where(
                        SqlConnection.workspace_id == current_workspace_id(),
                        SqlConnection.user_id == user_id,
                        SqlConnection.provider == provider,
                        SqlConnection.account_id == account_id,
                    )
                ),
            )
            return result.rowcount > 0

    def list_for_user(self, user_id: str) -> list[ProviderConnection]:
        """All of a user's connections (metadata only), across providers."""
        with self._session("list_for_user") as session:
            rows = (
                session.execute(
                    select(SqlConnection).where(
                        SqlConnection.workspace_id == current_workspace_id(),
                        SqlConnection.user_id == user_id,
                    )
                )
                .scalars()
                .all()
            )
            return [self._to_entity(r, with_secret=False) for r in rows]

    def list_all(self, *, provider: str | None = None) -> list[ProviderConnection]:
        """All connections (metadata only), optionally filtered by *provider* — tests/admin."""
        with self._session("list_all") as session:
            stmt = select(SqlConnection).where(
                SqlConnection.workspace_id == current_workspace_id()
            )
            if provider is not None:
                stmt = stmt.where(SqlConnection.provider == provider)
            rows = session.execute(stmt).scalars().all()
            return [self._to_entity(r, with_secret=False) for r in rows]
