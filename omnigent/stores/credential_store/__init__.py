"""Provider-agnostic per-user credential store.

The SQLAlchemy backend (:class:`CredentialStore`) behind the
:class:`SecretCipher` port (AWS-KMS implementation). Provider façades live in
:mod:`omnigent.connections`. See ``designs/CREDENTIAL_STORE.md``.
"""

from omnigent.stores.credential_store.secret_cipher import (
    CREDENTIAL_KMS_KEY_ENV_VAR,
    KmsSecretCipher,
    SecretCipher,
    SecretContext,
    build_secret_cipher,
)
from omnigent.stores.credential_store.sqlalchemy_store import CredentialStore

__all__ = [
    "CREDENTIAL_KMS_KEY_ENV_VAR",
    "CredentialStore",
    "KmsSecretCipher",
    "SecretCipher",
    "SecretContext",
    "build_secret_cipher",
]
