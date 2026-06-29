"""Per-user secret vault store (#5) — encrypted credentials, scoped by user.

Persists only ciphertext (the REST/vault layer encrypts before ``upsert`` and
decrypts after ``get_encrypted``). Every method is keyed by ``user_id`` so a
caller can only ever touch its own rows; listings return metadata (never the
secret value).
"""

from abc import ABC, abstractmethod

from omnigent.entities.user_credential import UserCredential


class UserCredentialStore(ABC):
    """Abstract base for the per-user encrypted credential vault."""

    def __init__(self, storage_location: str) -> None:
        """
        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///chat.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    @abstractmethod
    def list_for_user(self, user_id: str) -> list[UserCredential]:
        """
        List a user's stored credential metadata (no secret values).

        :param user_id: The owning user.
        :returns: The user's :class:`UserCredential` rows (possibly empty).
        """
        ...

    @abstractmethod
    def upsert(
        self,
        credential_id: str,
        user_id: str,
        name: str,
        secret_encrypted: str,
    ) -> UserCredential:
        """
        Store or overwrite a user's secret, keyed by ``(user_id, name)``.

        :param credential_id: Id to use if creating (ignored on overwrite).
        :param user_id: The owning user.
        :param name: Logical credential name (unique per user).
        :param secret_encrypted: The already-encrypted secret token.
        :returns: The stored :class:`UserCredential` (metadata).
        """
        ...

    @abstractmethod
    def get_encrypted(self, user_id: str, name: str) -> str | None:
        """
        Return the encrypted secret for ``(user_id, name)``, or ``None``.

        :param user_id: The owning user (scopes the lookup).
        :param name: The credential name.
        :returns: The ciphertext token, or ``None`` if absent.
        """
        ...

    @abstractmethod
    def delete(self, user_id: str, name: str) -> bool:
        """
        Delete a user's credential. Idempotent.

        :param user_id: The owning user.
        :param name: The credential name.
        :returns: ``True`` if a row was removed; ``False`` if none existed.
        """
        ...
