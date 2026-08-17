"""
Persistent store for per-user external-service credentials.

Rows are written by the web UI's Settings → Credentials flow (e.g.
"Connect GitHub"), and read by the managed-sandbox launch path, which
injects the decrypted token into the session's sandbox (``GIT_TOKEN``).

Tokens are Fernet-encrypted at rest with
``OMNIGENT_CREDENTIAL_ENCRYPTION_KEY`` (urlsafe-base64, 32 bytes — a
``cryptography.fernet.Fernet.generate_key()`` value). Without a valid
key the credentials feature is disabled: writes raise, reads decrypt to
``None``, and callers fall back to launching without a credential.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Engine, select
from sqlalchemy import delete as sql_delete

from omnigent.db.db_models import SqlUserCredential, current_workspace_id
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker, now_epoch

logger = logging.getLogger(__name__)

_KEY_ENV = "OMNIGENT_CREDENTIAL_ENCRYPTION_KEY"


def _fernet() -> Fernet | None:
    """Build the Fernet codec from the env key, or ``None`` when absent/invalid."""
    key = os.environ.get(_KEY_ENV, "").strip()
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        return None


def credential_encryption_enabled() -> bool:
    """Whether the encryption key is configured and valid (feature gate)."""
    return _fernet() is not None


@dataclass
class UserCredential:
    """
    A user's connected external-service credential.

    :param user_id: Owning user id, e.g. ``"alice@example.com"``.
    :param provider: External service, e.g. ``"github"``.
    :param token_encrypted: Fernet ciphertext — decrypt via
        :meth:`CredentialStore.decrypt_token`, never stored or logged raw.
    :param login: Provider-side account name for display, e.g. ``"alice"``.
    :param scopes: Granted scopes for display, e.g. ``"repo"``.
    :param created_at: Unix epoch seconds of first connect.
    :param updated_at: Unix epoch seconds of last (re)connect.
    """

    user_id: str
    provider: str
    token_encrypted: str
    login: str
    scopes: str
    created_at: int
    updated_at: int


class CredentialStore:
    """
    Persistent store for user credentials backed by SQLAlchemy.

    :param storage_location: SQLAlchemy database URI, e.g.
        ``"sqlite:///creds.db"``.
    """

    def __init__(self, storage_location: str) -> None:
        self._engine: Engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    def get(self, user_id: str, provider: str) -> UserCredential | None:
        """Return the user's credential for *provider*, or ``None``."""
        with self._session() as session:
            row = session.execute(
                select(SqlUserCredential).where(
                    SqlUserCredential.workspace_id == current_workspace_id(),
                    SqlUserCredential.user_id == user_id,
                    SqlUserCredential.provider == provider,
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return UserCredential(
                user_id=row.user_id,
                provider=row.provider,
                token_encrypted=row.token_encrypted,
                login=row.login,
                scopes=row.scopes,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )

    def upsert(self, user_id: str, provider: str, *, token: str, login: str, scopes: str) -> None:
        """
        Store (or replace) the user's credential for *provider*.

        :raises RuntimeError: When the encryption key is not configured —
            storing a plaintext token is never acceptable.
        """
        codec = _fernet()
        if codec is None:
            raise RuntimeError(f"{_KEY_ENV} is not configured; refusing to store a credential")
        token_encrypted = codec.encrypt(token.encode()).decode()
        now = now_epoch()
        with self._session() as session:
            row = session.execute(
                select(SqlUserCredential).where(
                    SqlUserCredential.workspace_id == current_workspace_id(),
                    SqlUserCredential.user_id == user_id,
                    SqlUserCredential.provider == provider,
                )
            ).scalar_one_or_none()
            if row is None:
                session.add(
                    SqlUserCredential(
                        user_id=user_id,
                        provider=provider,
                        token_encrypted=token_encrypted,
                        login=login,
                        scopes=scopes,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                row.token_encrypted = token_encrypted
                row.login = login
                row.scopes = scopes
                row.updated_at = now
            session.commit()

    def delete(self, user_id: str, provider: str) -> bool:
        """Remove the credential; ``True`` when a row was deleted."""
        with self._session() as session:
            result = session.execute(
                sql_delete(SqlUserCredential).where(
                    SqlUserCredential.workspace_id == current_workspace_id(),
                    SqlUserCredential.user_id == user_id,
                    SqlUserCredential.provider == provider,
                )
            )
            session.commit()
            return bool(result.rowcount)

    def decrypt_token(self, cred: UserCredential) -> str | None:
        """
        Decrypt a credential's token.

        :returns: The raw token, or ``None`` when the key is missing,
            invalid, or was rotated after this row was written (callers
            log + launch without the credential; the UI shows Reconnect).
        """
        codec = _fernet()
        if codec is None:
            return None
        try:
            return codec.decrypt(cred.token_encrypted.encode()).decode()
        except InvalidToken:
            return None
