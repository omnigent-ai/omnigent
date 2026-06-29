"""Per-user secret vault crypto (#5) — encryption-at-rest for collaborators'
credentials in a shared session.

In a shared session the runner executes on the *owner's* host, so a
collaborator's own git/aws/databricks secrets aren't there. The vault lets each
user store their own secrets server-side; on a tool action the *acting* user's
secret is resolved (and, in a follow-up, injected into that execution). Because
that means other people's secrets live on the server, they are encrypted at
rest with a server-held key (Fernet: AES-128-CBC + HMAC, authenticated).

This module is the crypto + key-management half (pure/testable); the store
persists only ciphertext, and the REST layer scopes every operation to the
acting user so one user can never read another's.
"""

from __future__ import annotations

import logging
from pathlib import Path

from cryptography.fernet import Fernet

_logger = logging.getLogger(__name__)


def load_or_create_vault_key(key_path: Path) -> bytes:
    """Load the server's secret-vault key, generating + persisting if absent.

    The key must be stable: if it changes, every stored secret becomes
    undecryptable (users would have to re-add them). Persisted ``0600`` next to
    the server's data.

    :param key_path: Where the Fernet key lives, e.g.
        ``<data>/secret_vault.key``.
    :returns: The 32-byte url-safe base64 Fernet key.
    """
    if key_path.exists():
        return key_path.read_bytes()
    key = Fernet.generate_key()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key)
    try:
        key_path.chmod(0o600)
    except OSError:  # best-effort on platforms without POSIX modes
        _logger.debug("could not chmod vault key %s", key_path, exc_info=True)
    _logger.info("generated a new secret-vault key at %s", key_path)
    return key


def encrypt_secret(key: bytes, plaintext: str) -> str:
    """Encrypt a secret for storage.

    :param key: The vault key from :func:`load_or_create_vault_key`.
    :param plaintext: The secret value (e.g. a git token).
    :returns: An opaque, authenticated ciphertext token (safe to persist).
    """
    return Fernet(key).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(key: bytes, token: str) -> str:
    """Decrypt a stored secret.

    :param key: The vault key (must match the one used to encrypt).
    :param token: The ciphertext from :func:`encrypt_secret`.
    :returns: The original plaintext secret.
    :raises cryptography.fernet.InvalidToken: If the token is corrupt or was
        encrypted under a different key.
    """
    return Fernet(key).decrypt(token.encode("ascii")).decode("utf-8")
