"""Symmetric encryption for secrets stored at rest.

A thin wrapper over :class:`cryptography.fernet.Fernet` used to encrypt
per-user credentials (currently GitHub user access / refresh tokens)
before they land in the database. The plaintext only exists in the
server process; the column holds the Fernet ciphertext.

The Fernet key is *derived* from an operator-supplied secret via SHA-256 so the
operator can pass any high-entropy string (e.g. ``openssl rand -hex 32``) rather
than a base64 32-byte Fernet key. Use a **dedicated** secret — never one already
serving another purpose — so rotating that other secret can't silently
invalidate stored credentials. Two processes that share the same input secret
derive the same key and can decrypt each other's rows, which is what a
multi-replica deployment needs.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import Protocol, runtime_checkable

from cryptography.fernet import Fernet, InvalidToken

_logger = logging.getLogger(__name__)

#: Store-level key material for encrypting integration secrets at rest. Owned by
#: the credential store (not by any one provider), so every "Connect …"
#: integration shares one cipher. Unset ⇒ the store is disabled.
CREDENTIAL_ENC_KEY_ENV_VAR = "OMNIGENT_CREDENTIAL_ENC_KEY"


@runtime_checkable
class SecretCipher(Protocol):
    """Port for encrypting integration secrets at rest.

    The credential store depends on this, not on a concrete backend, so a
    deployment can swap the default local-Fernet :class:`SecretBox` for a
    KMS/Secrets-Manager/Databricks/Vault adapter without a schema change. See
    ``designs/CREDENTIAL_STORE.md``.
    """

    def encrypt(self, plaintext: str) -> str:
        """Return the ciphertext for *plaintext*."""
        ...

    def decrypt(self, ciphertext: str) -> str | None:
        """Return the plaintext, or ``None`` when the ciphertext is unusable."""
        ...


def build_secret_cipher() -> SecretCipher | None:
    """Construct the credential store's cipher from deployment config.

    Single seam where a deployment selects the secret backend. Reads the
    store-level key ``OMNIGENT_CREDENTIAL_ENC_KEY`` and returns the local Fernet
    :class:`SecretBox`; returns ``None`` when it is unset — the credential store,
    and every integration built on it, is then disabled (the caller decides how
    to surface that). A KMS/Databricks/Vault adapter slots in here behind a
    backend selector when a deployment needs one (see
    ``designs/CREDENTIAL_STORE.md``).

    The key belongs to the store, not to any provider: rotating it invalidates
    all stored secrets (they decrypt to ``None`` ⇒ reconnect), so it is
    deliberately dedicated rather than borrowed from another secret.
    """
    secret = os.environ.get(CREDENTIAL_ENC_KEY_ENV_VAR, "").strip()
    if not secret:
        return None
    return SecretBox(secret)


class SecretBox:
    """Encrypt/decrypt short secrets with a key derived from *secret*.

    :param secret: Operator-supplied key material of arbitrary length.
        Its SHA-256 digest is base64url-encoded into a 32-byte Fernet
        key, so the caller need not supply a Fernet-formatted key.
    """

    def __init__(self, secret: str | bytes) -> None:
        material = secret.encode("utf-8") if isinstance(secret, str) else secret
        key = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        """Return the URL-safe base64 ciphertext for *plaintext*."""
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str | None:
        """Return the plaintext for *ciphertext*, or ``None`` if invalid.

        Returns ``None`` (rather than raising) when the ciphertext was
        written under a different key or is corrupt, so a rotated
        encryption secret degrades to "connection needs reconnecting"
        instead of a 500 at launch time.
        """
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            # Fernet is authenticated, so this is a wrong key (rotated secret) or
            # corrupt/tampered ciphertext — never a silently-wrong plaintext.
            # Warn (no secret material) so an operator can tell "everyone must
            # reconnect after a key rotation" from a quiet, unexplained logout.
            _logger.warning(
                "secretbox: ciphertext failed to decrypt (wrong key or corrupt) — "
                "the affected integration will read as disconnected until reconnected"
            )
            return None
