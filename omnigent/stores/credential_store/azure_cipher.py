"""Azure Key Vault / Managed HSM-backed :class:`SecretCipher` — the managed
Azure counterpart to AWS KMS, for deployments that don't run on AWS.

A third implementation of the same ``SecretCipher`` port that
:class:`KmsSecretCipher` and :class:`VaultSecretCipher` implement. The row
identity — ``(workspace_id, user_id, provider, account_id)`` — is passed as
AES-GCM *additional authenticated data*, the direct analogue of KMS's
*encryption context*: the same primitive, enforced by the backend, with no
global-key path. Decryption must present the same AAD, so a wrong row identity
fails authentication and degrades to "reconnect".

AES keys, and therefore AAD, exist only on Managed HSM (``oct``) and Key Vault
Premium (``oct-HSM``). Key Vault Standard is RSA/EC and has no AAD parameter at
all; a non-AES key raises store-wide rather than silently dropping the binding.

Selected with ``OMNIGENT_CREDENTIAL_AZURE_KEY_ID``; credentials come from
``DefaultAzureCredential``, so AKS Workload Identity, a user-assigned managed
identity or a service principal all work unconfigured. Only ``keys/encrypt``
and ``keys/decrypt`` are needed.
"""

from __future__ import annotations

import base64
import logging
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from omnigent.stores.credential_store.secret_cipher import (
    SecretContext,
    canonical_context_json,
)

if TYPE_CHECKING:
    from azure.core.exceptions import (
        ClientAuthenticationError,
        HttpResponseError,
        ResourceNotFoundError,
    )

_logger = logging.getLogger(__name__)

_MISSING_SDK = (
    "The Azure credential backend needs the Azure SDKs. Install them "
    "with `pip install 'omnigent[azure]'`."
)

#: Names the Key Vault / Managed HSM key used to encrypt integration secrets:
#: the full identifier, ``https://<vault>.vault.azure.net/keys/<name>/<version>``.
#: Unset ⇒ the Azure cipher is not selected.
CREDENTIAL_AZURE_KEY_ENV_VAR = "OMNIGENT_CREDENTIAL_AZURE_KEY_ID"

#: AES-256-GCM. The only algorithm this backend uses: AAD exists for GCM alone.
_ALGORITHM = "A256GCM"

#: Envelope marker. A Key Vault ciphertext embeds neither the key identifier nor
#: the GCM nonce and tag, all three of which decryption needs, so they ride with
#: it. The version field reserves room for a second format without migrating
#: rows already written.
_ENVELOPE_PREFIX = "osakv"
_ENVELOPE_VERSION = "1"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    """Strictly decode URL-safe base64; ``urlsafe_b64decode`` would silently drop
    stray characters and accept garbage."""
    return base64.b64decode(text.encode("ascii"), altchars=b"-_", validate=True)


def _wrap(key_id: str, iv: bytes, tag: bytes, ciphertext: bytes) -> str:
    """Envelope a ciphertext with the key identifier, GCM nonce and tag."""
    return ":".join(
        (
            _ENVELOPE_PREFIX,
            _ENVELOPE_VERSION,
            _b64(key_id.encode("utf-8")),
            _b64(iv),
            _b64(tag),
            _b64(ciphertext),
        )
    )


def _unwrap(stored: str) -> tuple[str, bytes, bytes, bytes] | None:
    """Return ``(key_id, iv, tag, ciphertext)``, or ``None`` when *stored* is not
    one of our envelopes (corrupt, or written by another backend)."""
    parts = stored.split(":", 5)
    if len(parts) != 6:
        return None
    prefix, version, key_b64, iv_b64, tag_b64, ct_b64 = parts
    if prefix != _ENVELOPE_PREFIX or version != _ENVELOPE_VERSION:
        return None
    try:
        key_id = _unb64(key_b64).decode("utf-8")
        return (
            key_id,
            _unb64(iv_b64),
            _unb64(tag_b64),
            _unb64(ct_b64),
        )
    except (ValueError, UnicodeDecodeError):
        return None


def _azure_errors() -> tuple[
    type[ClientAuthenticationError], type[HttpResponseError], type[ResourceNotFoundError]
]:
    """Import the SDK's exception types, failing with the install hint if absent."""
    try:
        from azure.core.exceptions import (
            ClientAuthenticationError,
            HttpResponseError,
            ResourceNotFoundError,
        )
    except ImportError as exc:
        raise ImportError(_MISSING_SDK) from exc
    return ClientAuthenticationError, HttpResponseError, ResourceNotFoundError


def _split_key_id(key_id: str) -> tuple[str, str, str | None]:
    """Split a key identifier into ``(vault_url, key_name, version)``.

    The vault URL is lowercased and any trailing slash dropped so a pasted or
    differently-cased identifier is not mistaken for a different key. The version
    is ``None`` only when the identifier has none at all; empty or extra path
    segments are a typo and raise.
    """
    parts = key_id.strip().rstrip("/").split("/")
    if (
        len(parts) not in (5, 6)
        or parts[0].lower() not in ("https:", "http:")
        or parts[1] != ""
        or parts[3] != "keys"
        or not all(parts[2:])
    ):
        raise ValueError(
            f"{CREDENTIAL_AZURE_KEY_ENV_VAR} must be a full Key Vault key identifier "
            f"like https://<vault>.vault.azure.net/keys/<name>/<version>, got {key_id!r}"
        )
    vault_url = "/".join(parts[:3]).lower()
    return vault_url, parts[4], parts[5] if len(parts) == 6 else None


class AzureKeyVaultSecretCipher:
    """:class:`SecretCipher` backed by Key Vault / Managed HSM AES-GCM.

    :param key_id: Full key identifier, **including the version**. Without
        ``keys/get`` the SDK reports the configured identifier rather than the
        version that actually encrypted, so the version must be pinned in config.
        Rotating means updating the configured version; rows written under
        earlier versions keep decrypting because each envelope records its own.
    :param client_factory: Optional callable taking a key identifier and
        returning a ``CryptographyClient``-shaped object (tests inject a fake).
        Otherwise a real ``CryptographyClient`` is built lazily. Either way one
        client is created per key version and cached, so importing this module
        never requires the Azure SDKs or a credential.
    """

    def __init__(self, key_id: str, *, client_factory: Callable[[str], Any] | None = None) -> None:
        self._key_id = key_id.strip().rstrip("/")
        self._vault_url, self._key_name, version = _split_key_id(self._key_id)
        if version is None:
            raise ValueError(
                f"{CREDENTIAL_AZURE_KEY_ENV_VAR} must include the key version, like "
                "https://<vault>.vault.azure.net/keys/<name>/<version>, so each row "
                f"records the version that encrypted it; got {key_id!r}"
            )
        self._client_factory = client_factory
        self._clients: dict[str, Any] = {}
        self._credential: Any | None = None

    def _crypto_for(self, key_id: str) -> Any:
        """Return a client for *key_id*, one per version, created on first use."""
        cached = self._clients.get(key_id)
        if cached is not None:
            return cached
        if self._client_factory is not None:
            client = self._client_factory(key_id)
            self._clients[key_id] = client
            return client
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.keys.crypto import CryptographyClient
        except ImportError as exc:  # pragma: no cover - import guard
            raise ImportError(_MISSING_SDK) from exc

        if self._credential is None:
            self._credential = DefaultAzureCredential()
        client = CryptographyClient(key_id, credential=self._credential)
        self._clients[key_id] = client
        return client

    def encrypt(self, plaintext: str, *, context: SecretContext) -> str:
        ClientAuthenticationError, HttpResponseError, ResourceNotFoundError = _azure_errors()

        client = self._crypto_for(self._key_id)
        try:
            result = client.encrypt(
                _ALGORITHM,
                plaintext.encode("utf-8"),
                additional_authenticated_data=canonical_context_json(context),
            )
        except (ClientAuthenticationError, ResourceNotFoundError):
            raise
        except HttpResponseError as exc:
            # Encrypt has no per-row soft-fail path, so a 400 always raises. The
            # two plausible causes are a non-AES key (RSA/EC cannot carry the
            # binding) and an over-limit payload; name both and keep the
            # service's own message.
            if exc.status_code == 400:
                raise ValueError(
                    f"azure key vault: the key named by {CREDENTIAL_AZURE_KEY_ENV_VAR} "
                    f"rejected {_ALGORITHM}. It must be an AES key (oct on Managed HSM, "
                    "oct-HSM on Key Vault Premium), and the secret must fit the "
                    f"service's single-block limit. Service said: {exc}"
                ) from exc
            raise
        # Record the configured versioned id: the SDK may respell result.key_id.
        # EncryptResult stores the GCM tag as `.tag`; `authentication_tag` is only its kwarg.
        return _wrap(self._key_id, result.iv, result.tag, result.ciphertext)

    def decrypt(self, ciphertext: str, *, context: SecretContext) -> str | None:
        """Return the plaintext, or ``None`` when the ciphertext/context is unusable.

        Degrades to ``None`` (⇒ reconnect, never a wrong plaintext) only for
        per-row failures: an unrecognized envelope (corrupt, or written by
        another backend), and a 400 from the service once the key name matches,
        which is a failed authentication tag — a wrong row identity or a
        corrupt blob. Store-wide failures propagate instead: a repointed key
        raises *before* the service is called, and auth, not-found, throttling
        and server errors raise too, because masking any of them as a per-user
        reconnect would re-encrypt under the wrong key and overwrite
        recoverable ciphertext. Residual gap: a store that only decrypts, never
        writing a new secret, would read a non-AES key's 400 as per-row, since
        only the encrypt path can tell the two apart.
        """
        ClientAuthenticationError, HttpResponseError, ResourceNotFoundError = _azure_errors()

        unwrapped = _unwrap(ciphertext)
        if unwrapped is None:
            _logger.warning(
                "azure key vault: ciphertext is not a recognized envelope "
                "(corrupt, or written by another backend) — the affected integration "
                "will read as disconnected until reconnected"
            )
            return None
        stored_key_id, iv, tag, blob = unwrapped
        try:
            stored_vault, stored_name, _ = _split_key_id(stored_key_id)
        except ValueError:
            _logger.warning(
                "azure key vault: ciphertext carries an unparseable key identifier "
                "(corrupt) — the affected integration will read as disconnected"
            )
            return None
        if (stored_vault, stored_name) != (self._vault_url, self._key_name):
            raise ValueError(
                f"azure key vault: ciphertext was written under key {stored_key_id!r} "
                f"but the configured key is {self._key_id!r} — refusing to read "
                f"({CREDENTIAL_AZURE_KEY_ENV_VAR} repointed?)"
            )
        try:
            result = self._crypto_for(stored_key_id).decrypt(
                _ALGORITHM,
                blob,
                iv=iv,
                authentication_tag=tag,
                additional_authenticated_data=canonical_context_json(context),
            )
        except (ClientAuthenticationError, ResourceNotFoundError):
            raise
        except HttpResponseError as exc:
            if exc.status_code != 400:
                raise
            _logger.warning(
                "azure key vault: could not decrypt (wrong row identity or corrupt) — "
                "the affected integration will read as disconnected until "
                "reconnected: %s",
                exc,
            )
            return None
        return result.plaintext.decode("utf-8")


def build_azure_secret_cipher() -> AzureKeyVaultSecretCipher | None:
    """Construct the Azure Key Vault cipher from deployment config, or ``None``.

    Reads ``OMNIGENT_CREDENTIAL_AZURE_KEY_ID``; unset ⇒ ``None``, mirroring the
    other builders' disabled-when-unset contract. A malformed identifier raises
    here rather than on the first encrypt, so a config typo surfaces at startup.
    The Azure SDKs stay lazy: this reads env and constructs only.
    """
    key_id = os.environ.get(CREDENTIAL_AZURE_KEY_ENV_VAR, "").strip()
    return AzureKeyVaultSecretCipher(key_id) if key_id else None
