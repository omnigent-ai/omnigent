"""AWS KMS-backed encryption for integration secrets stored at rest.

Integration credentials (GitHub App tokens today; MCP connector secrets later)
are encrypted before they touch the database. Encryption is delegated to AWS
KMS: the key material never leaves KMS, and the server holds only an IAM
permission to call ``Encrypt``/``Decrypt``, plus ``GenerateDataKey`` if it
stores secrets over 4096 bytes (granted via IRSA — no key or master secret
lives in the process or its environment). Each secret is bound to its
row's identity — ``(workspace_id, user_id, provider, account_id)`` — passed as
the KMS *encryption context*, so a ciphertext can only be decrypted by
presenting the same identity. That is per-user encryption enforced by KMS, not
by a key the server derives, and there is no global-key path.

A deployment selects the key with ``OMNIGENT_CREDENTIAL_KMS_KEY_ID``; unset ⇒
the credential store, and every integration built on it, is disabled. There is
no non-KMS fallback. See ``designs/CREDENTIAL_STORE.md``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

_logger = logging.getLogger(__name__)

#: A row's identity, passed as the encryption ``context`` so each
#: ``(workspace, user, provider, account)`` is bound to its own ciphertext.
SecretContext = Mapping[str, str]

#: Names the AWS KMS key (id, ARN, or ``alias/…``) used to encrypt integration
#: secrets. Owned by the credential store, not any one provider, so every
#: "Connect …" integration shares it. Unset ⇒ the store is disabled.
CREDENTIAL_KMS_KEY_ENV_VAR = "OMNIGENT_CREDENTIAL_KMS_KEY_ID"

#: Optional explicit backend selector for the credential store's cipher
#: (``kms`` | ``vault``). Set it to pick a backend per server; its key env var is
#: then required. Unset ⇒ the single configured backend is auto-detected, and
#: configuring more than one without this selector is an error.
CREDENTIAL_CIPHER_ENV_VAR = "OMNIGENT_CREDENTIAL_CIPHER"

#: KMS error codes that mean "this ciphertext can't be read under this identity"
#: — a wrong/rotated key, a mismatched encryption context, or a corrupt blob.
#: These degrade to "reconnect" (``decrypt`` → ``None``); anything else
#: (AccessDenied, disabled key, throttling) is operational and propagates.
_SOFT_FAIL_KMS_CODES = frozenset({"InvalidCiphertextException", "IncorrectKeyException"})

#: Largest plaintext ``kms:Encrypt`` accepts. Secrets up to this size keep the
#: direct single-call path; larger ones are envelope-encrypted.
_KMS_DIRECT_ENCRYPT_LIMIT = 4096

#: Marker on envelope ciphertexts. Direct ciphertexts are plain base64, which
#: never contains ``:``, so the two formats can't be confused on decrypt.
_ENVELOPE_PREFIX = "kms-envelope-v1:"

#: AES-GCM nonce length (bytes), as recommended for GCM.
_ENVELOPE_NONCE_BYTES = 12


class SecretTooLargeError(ValueError):
    """The serialized secret exceeds the configured encryption backend limit."""


@runtime_checkable
class SecretCipher(Protocol):
    """Port for encrypting integration secrets at rest.

    The credential store depends on this, not on a concrete backend, so the KMS
    implementation can be swapped (e.g. for GCP/Azure KMS or Vault Transit)
    without a schema change. See ``designs/CREDENTIAL_STORE.md``.

    ``context`` is the row's identity (``workspace``/``user``/``provider``/
    ``account``) and is **required** — every secret is bound to a user, there is
    no unbound/global-key path. Decryption must be given the *same* context; a
    mismatch yields ``None`` (⇒ reconnect), never a wrong plaintext.
    """

    def encrypt(self, plaintext: str, *, context: SecretContext) -> str:
        """Return the ciphertext for *plaintext*, bound to *context*."""
        ...

    def decrypt(self, ciphertext: str, *, context: SecretContext) -> str | None:
        """Return the plaintext, or ``None`` when the ciphertext/context is unusable."""
        ...


def build_kms_secret_cipher() -> KmsSecretCipher | None:
    """Build the AWS KMS cipher from ``OMNIGENT_CREDENTIAL_KMS_KEY_ID``, or ``None``.

    ``None`` when the key is unset — the KMS backend is simply not configured.
    boto3 stays lazy: this only reads env and constructs (no SDK import and no AWS
    call until the first encrypt/decrypt).
    """
    key_id = os.environ.get(CREDENTIAL_KMS_KEY_ENV_VAR, "").strip()
    return KmsSecretCipher(key_id) if key_id else None


def build_secret_cipher() -> SecretCipher | None:
    """Construct the credential store's cipher from deployment config, or ``None``.

    The backend is configurable per server. Set ``OMNIGENT_CREDENTIAL_CIPHER`` to
    choose one explicitly (``kms`` or ``vault``); its key env var
    (``OMNIGENT_CREDENTIAL_KMS_KEY_ID`` / ``OMNIGENT_CREDENTIAL_VAULT_KEY``) is then
    required, and a mismatch raises rather than silently disabling the store. When
    the selector is unset, the single configured backend is auto-detected as a
    zero-config convenience; configuring more than one without a selector is an
    error (no silent precedence), and configuring none disables the store
    (``None`` — the caller decides how to surface that). Adding a backend is a new
    registry entry, not a new branch. See ``designs/CREDENTIAL_STORE.md``.
    """
    # hvac / boto3 stay optional: each builder only reads env and constructs, so
    # importing and calling them here pulls no SDK and makes no network call.
    from omnigent.stores.credential_store.vault_cipher import build_vault_secret_cipher

    builders: dict[str, Callable[[], SecretCipher | None]] = {
        "kms": build_kms_secret_cipher,
        "vault": build_vault_secret_cipher,
    }

    selected = os.environ.get(CREDENTIAL_CIPHER_ENV_VAR, "").strip().lower()
    if selected:
        builder = builders.get(selected)
        if builder is None:
            raise ValueError(
                f"{CREDENTIAL_CIPHER_ENV_VAR}={selected!r} is not a known credential "
                f"backend; expected one of {sorted(builders)}."
            )
        cipher = builder()
        if cipher is None:
            raise ValueError(
                f"{CREDENTIAL_CIPHER_ENV_VAR}={selected!r} but its key is unset — "
                "set the backend's key env var."
            )
        return cipher

    # No explicit selector: use the single configured backend, or None if none.
    configured = {name: c for name, build in builders.items() if (c := build()) is not None}
    if len(configured) > 1:
        raise ValueError(
            f"multiple credential backends are configured ({sorted(configured)}); "
            f"set {CREDENTIAL_CIPHER_ENV_VAR} to choose one."
        )
    return next(iter(configured.values()), None)


def _kms_context(context: SecretContext) -> dict[str, str]:
    """Encryption context for a KMS call.

    KMS rejects empty-string encryption-context values, so drop them — done
    consistently on encrypt and decrypt, the binding is unchanged, and the
    remaining fields (workspace/user/provider, plus ``account_id`` only when a
    named account is used) still uniquely identify the row.
    """
    return {k: v for k, v in context.items() if v != ""}


def _envelope_aad(context: SecretContext) -> bytes:
    """AES-GCM associated data binding an envelope to its row identity.

    The data key is already bound to the identity by the KMS encryption
    context; repeating the (canonical) context here also binds the local
    ciphertext, so an envelope can't be re-paired with another row's data key.
    """
    canonical = json.dumps(_kms_context(context), sort_keys=True, separators=(",", ":"))
    return _ENVELOPE_PREFIX.encode("ascii") + canonical.encode("utf-8")


class KmsSecretCipher:
    """:class:`SecretCipher` backed by AWS KMS.

    A secret up to 4096 serialized bytes is a single ``kms:Encrypt`` call under
    ``key_id`` with the row identity as the encryption context; the ciphertext
    blob is stored base64. A larger secret (for example an OAuth access and
    refresh token pair) is envelope-encrypted instead: ``kms:GenerateDataKey``
    under the same key and context returns a 256-bit data key, the secret is
    sealed locally with AES-256-GCM using the context as associated data, and
    the KMS-encrypted data key is stored alongside it as
    ``kms-envelope-v1:<b64 data key>:<b64 nonce>:<b64 ciphertext>``. Decrypt
    recognises both formats, so existing rows keep working, and deployments that
    never store a large secret never need ``kms:GenerateDataKey``. The KMS
    client is created lazily so importing this module never requires boto3 or
    AWS credentials — only constructing the cipher (which only happens when a
    key is configured) does.

    :param key_id: KMS key id, ARN, or ``alias/…``.
    :param client: Optional pre-built boto3 KMS client (tests inject a fake).
    """

    def __init__(self, key_id: str, *, client: Any | None = None) -> None:
        self._key_id = key_id
        self._client = client

    @property
    def _kms(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - import guard
                raise ImportError(
                    "The AWS KMS credential backend needs boto3. Install it with "
                    "`pip install 'omnigent[kms]'`."
                ) from exc

            self._client = boto3.client("kms")
        return self._client

    def encrypt(self, plaintext: str, *, context: SecretContext) -> str:
        encoded = plaintext.encode("utf-8")
        if len(encoded) > _KMS_DIRECT_ENCRYPT_LIMIT:
            return self._encrypt_envelope(encoded, context=context)
        response = self._kms.encrypt(
            KeyId=self._key_id,
            Plaintext=encoded,
            EncryptionContext=_kms_context(context),
        )
        return base64.b64encode(response["CiphertextBlob"]).decode("ascii")

    def decrypt(self, ciphertext: str, *, context: SecretContext) -> str | None:
        """Return the plaintext for *ciphertext*, or ``None`` if unreadable.

        Returns ``None`` (rather than raising) when the ciphertext was written
        under a different key/context or is corrupt, so a rotated key degrades
        to "reconnect" instead of a 500. Operational failures (access denied,
        disabled key, throttling) propagate — telling the user to reconnect
        wouldn't help and would hide a misconfiguration.
        """
        from botocore.exceptions import ClientError

        if ciphertext.startswith(_ENVELOPE_PREFIX):
            return self._decrypt_envelope(ciphertext, context=context)
        try:
            blob = base64.b64decode(ciphertext.encode("ascii"), validate=True)
        except ValueError:
            _logger.warning(
                "secretbox: stored ciphertext is not valid base64 (corrupt) — "
                "the affected integration will read as disconnected until reconnected"
            )
            return None
        try:
            response = self._kms.decrypt(
                CiphertextBlob=blob,
                EncryptionContext=_kms_context(context),
                KeyId=self._key_id,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _SOFT_FAIL_KMS_CODES:
                _logger.warning(
                    "secretbox: KMS could not decrypt (code=%s: wrong key/context or "
                    "corrupt) — the affected integration will read as disconnected "
                    "until reconnected",
                    code,
                )
                return None
            raise
        return response["Plaintext"].decode("utf-8")

    def _encrypt_envelope(self, encoded: bytes, *, context: SecretContext) -> str:
        """Seal *encoded* under a fresh KMS data key bound to *context*."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        response = self._kms.generate_data_key(
            KeyId=self._key_id,
            KeySpec="AES_256",
            EncryptionContext=_kms_context(context),
        )
        nonce = os.urandom(_ENVELOPE_NONCE_BYTES)
        sealed = AESGCM(response["Plaintext"]).encrypt(nonce, encoded, _envelope_aad(context))
        parts = (response["CiphertextBlob"], nonce, sealed)
        return _ENVELOPE_PREFIX + ":".join(base64.b64encode(p).decode("ascii") for p in parts)

    def _decrypt_envelope(self, ciphertext: str, *, context: SecretContext) -> str | None:
        """Open an envelope written by :meth:`_encrypt_envelope`, or ``None``.

        Same soft-fail contract as :meth:`decrypt`: a malformed envelope, a data
        key KMS won't release under this identity, or a failed GCM tag check
        reads as "reconnect"; operational KMS errors propagate.
        """
        from botocore.exceptions import ClientError
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        fields = ciphertext[len(_ENVELOPE_PREFIX) :].split(":")
        try:
            if len(fields) != 3:
                raise ValueError("expected three envelope fields")
            data_key_blob, nonce, sealed = (
                base64.b64decode(f.encode("ascii"), validate=True) for f in fields
            )
            if len(nonce) != _ENVELOPE_NONCE_BYTES:
                raise ValueError("unexpected nonce length")
        except ValueError:
            _logger.warning(
                "secretbox: stored KMS envelope is malformed (corrupt) — the affected "
                "integration will read as disconnected until reconnected"
            )
            return None
        try:
            response = self._kms.decrypt(
                CiphertextBlob=data_key_blob,
                EncryptionContext=_kms_context(context),
                KeyId=self._key_id,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _SOFT_FAIL_KMS_CODES:
                _logger.warning(
                    "secretbox: KMS could not decrypt the envelope data key (code=%s: "
                    "wrong key/context or corrupt) — the affected integration will read "
                    "as disconnected until reconnected",
                    code,
                )
                return None
            raise
        try:
            plaintext = AESGCM(response["Plaintext"]).decrypt(
                nonce, sealed, _envelope_aad(context)
            )
        except InvalidTag:
            _logger.warning(
                "secretbox: KMS envelope failed authentication (tampered, or another "
                "row's) — the affected integration will read as disconnected until "
                "reconnected"
            )
            return None
        return plaintext.decode("utf-8")
