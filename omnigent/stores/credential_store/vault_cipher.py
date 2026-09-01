"""HashiCorp Vault Transit-backed :class:`SecretCipher` — the provider-agnostic
alternative to AWS KMS, for deployments that don't run on AWS.

A parallel implementation of the same ``SecretCipher`` port that
:class:`KmsSecretCipher` implements, proving the port is genuinely cloud-agnostic
(not KMS-shaped). It backs onto a Vault Transit key created with ``derived=true``:
the row identity — ``(workspace_id, user_id, provider, account_id)`` — is passed as
Transit's per-operation ``context``, so a ciphertext only decrypts under the *same*
identity. That is the direct analogue of KMS's *encryption context*: KMS binds the
identity as AAD; Vault Transit uses it to derive the per-context key. Same guarantee
— per-user binding enforced by the backend, no global-key path — via a different
primitive.

Two subtleties this backend handles that a naive Transit wrapper would not:

- **Unambiguous context.** The identity is serialized as sorted-key JSON (not a
  ``k=v``-joined string) before base64, so a field value containing ``=`` or a
  newline (an ``account_id`` label, say) cannot collide two distinct identities
  onto the same derived context — matching the field-by-field binding KMS gets from
  its structured ``EncryptionContext``.
- **Key-scoped ciphertext.** Transit reports a wrong *context* (per-row) and a wrong
  *key* (store-wide) as the same ``message authentication failed`` 400, so a
  substring check alone cannot tell them apart. Each ciphertext therefore carries
  the encrypting key's name (:func:`_wrap`); :meth:`decrypt` rejects a name mismatch
  as a store-wide misconfiguration *before* calling Transit, so a repointed
  ``OMNIGENT_CREDENTIAL_VAULT_KEY`` propagates instead of masquerading as a per-user
  disconnect (which would re-encrypt under the wrong key on reconnect, overwriting
  still-recoverable ciphertext). KMS gets this for free — its ciphertext embeds the
  key ARN. Residual gap: a key deleted and recreated under the *same* name still
  reads as a per-row failure.

Wrong context / corrupt ciphertext ⇒ :meth:`decrypt` returns ``None`` (reconnect),
mirroring the KMS soft-fail; store-wide / operational failures (repointed key, bad
token, sealed Vault) raise. Selected with ``OMNIGENT_CREDENTIAL_VAULT_KEY`` (see
:func:`~omnigent.stores.credential_store.secret_cipher.build_secret_cipher`), plus
``VAULT_ADDR`` / ``VAULT_TOKEN`` from the standard Vault env.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

from omnigent.stores.credential_store.secret_cipher import SecretContext

_logger = logging.getLogger(__name__)

#: Names the Vault Transit key used to encrypt integration secrets. Unset ⇒ the
#: Vault cipher is not selected (the store falls back to its configured cipher).
CREDENTIAL_VAULT_KEY_ENV_VAR = "OMNIGENT_CREDENTIAL_VAULT_KEY"
CREDENTIAL_VAULT_MOUNT_ENV_VAR = "OMNIGENT_CREDENTIAL_VAULT_MOUNT"

_DEFAULT_TRANSIT_MOUNT = "transit"

#: Envelope marker for our ciphertext format: ``osvault:<b64url(key_name)>:<transit>``.
#: The key name rides with the ciphertext so :meth:`VaultSecretCipher.decrypt` can
#: distinguish a store-wide key repoint from a per-row context mismatch (Transit
#: reports both as the same 400). base64url keeps the key name delimiter-free.
_ENVELOPE_PREFIX = "osvault"

# After the key-name check, a Transit 400 (hvac ``InvalidRequest``) matching one of
# these is a genuine PER-ROW failure — wrong derived context or a corrupt blob — and
# degrades to "reconnect" (``decrypt`` → ``None``). Any other 400 (e.g. the key was
# deleted → "encryption key not found") is store-wide and propagates.
_PER_ROW_DECRYPT_MARKERS: tuple[str, ...] = (
    "message authentication failed",
    "invalid ciphertext",
)


def build_vault_secret_cipher() -> VaultSecretCipher | None:
    """Construct the Vault Transit cipher from deployment config, or ``None``.

    Reads ``OMNIGENT_CREDENTIAL_VAULT_KEY`` (the derived Transit key name) and the
    optional ``OMNIGENT_CREDENTIAL_VAULT_MOUNT`` (default ``transit``); the Vault
    address and token come from the standard ``VAULT_ADDR`` / ``VAULT_TOKEN`` env.
    Unset key ⇒ ``None`` — the Vault backend is simply not selected, mirroring the
    KMS builder's disabled-when-unset contract.
    """
    key = os.environ.get(CREDENTIAL_VAULT_KEY_ENV_VAR, "").strip()
    if not key:
        return None
    mount = os.environ.get(CREDENTIAL_VAULT_MOUNT_ENV_VAR, "").strip() or _DEFAULT_TRANSIT_MOUNT
    return VaultSecretCipher(key, mount_point=mount)


def _context_b64(context: SecretContext) -> str:
    """Serialize the row identity into Vault Transit's base64 ``context``.

    Empty values are dropped — consistently on encrypt and decrypt, exactly as the
    KMS cipher drops them — so ``account_id=""`` and an absent account bind
    identically, while a named account yields a distinct (still-decryptable)
    context. Serialized as sorted-key JSON so the encoding is both stable
    (order-independent) and unambiguous: a value containing ``=`` or a newline
    cannot collide two distinct identities onto the same context, unlike a
    ``k=v``-joined string. Matches the field-by-field binding KMS gets from its
    structured ``EncryptionContext``.
    """
    items = {k: v for k, v in context.items() if v != ""}
    canonical = json.dumps(items, sort_keys=True, separators=(",", ":"))
    return base64.b64encode(canonical.encode("utf-8")).decode("ascii")


def _wrap(key_name: str, transit_ciphertext: str) -> str:
    """Envelope a Transit ciphertext with the encrypting key's name (base64url)."""
    key_b64 = base64.urlsafe_b64encode(key_name.encode("utf-8")).decode("ascii")
    return f"{_ENVELOPE_PREFIX}:{key_b64}:{transit_ciphertext}"


def _unwrap(stored: str) -> tuple[str, str] | None:
    """Return ``(key_name, transit_ciphertext)`` from our envelope, or ``None`` when
    *stored* is not one (corrupt / foreign — a per-row failure)."""
    parts = stored.split(":", 2)
    if len(parts) != 3 or parts[0] != _ENVELOPE_PREFIX:
        return None
    try:
        key_name = base64.urlsafe_b64decode(parts[1].encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return key_name, parts[2]


class VaultSecretCipher:
    """:class:`SecretCipher` backed by Vault Transit encrypt/decrypt on a derived key.

    :param key_name: Transit key name (must be created with ``derived=true``).
    :param mount_point: Transit mount path (default ``transit``).
    :param client: Optional pre-built ``hvac.Client`` (tests / explicit wiring
        inject one); otherwise built lazily from ``VAULT_ADDR`` / ``VAULT_TOKEN``
        so importing this module never requires hvac or a Vault connection.
    """

    def __init__(
        self, key_name: str, *, mount_point: str = "transit", client: Any | None = None
    ) -> None:
        self._key = key_name
        self._mount = mount_point
        self._client = client

    @property
    def _vault(self) -> Any:
        if self._client is None:
            try:
                import hvac
            except ImportError as exc:  # pragma: no cover - import guard
                raise ImportError(
                    "The Vault credential backend needs hvac. Install it with "
                    "`pip install 'omnigent[vault]'`."
                ) from exc

            self._client = hvac.Client(
                url=os.environ.get("VAULT_ADDR", "http://127.0.0.1:8200"),
                token=os.environ.get("VAULT_TOKEN", ""),
            )
        return self._client

    def encrypt(self, plaintext: str, *, context: SecretContext) -> str:
        resp = self._vault.secrets.transit.encrypt_data(
            name=self._key,
            mount_point=self._mount,
            plaintext=base64.b64encode(plaintext.encode("utf-8")).decode("ascii"),
            context=_context_b64(context),
        )
        # Envelope with the key name so decrypt can detect a store-wide key repoint.
        return _wrap(self._key, resp["data"]["ciphertext"])

    def decrypt(self, ciphertext: str, *, context: SecretContext) -> str | None:
        """Return the plaintext, or ``None`` when the ciphertext/context is unusable.

        Degrades to ``None`` (⇒ reconnect, never a wrong plaintext) only for genuine
        per-row failures: an unrecognized envelope (corrupt/foreign), or a Transit
        ``message authentication failed`` / ``invalid ciphertext`` once the key name
        matches (a wrong derived context). Everything store-wide propagates instead:
        a key repoint (the envelope's key name ≠ the configured key) raises *before*
        Transit is called, and other Transit errors (e.g. the key was deleted →
        "encryption key not found") plus auth/seal/server errors raise too — masking
        any of those as a per-user reconnect would re-encrypt under the wrong key and
        overwrite recoverable ciphertext. Residual gap: a key deleted and recreated
        under the *same* name reads as per-row.
        """
        client = self._vault  # trigger the friendly hvac ImportError before importing exceptions
        import hvac.exceptions as vexc

        unwrapped = _unwrap(ciphertext)
        if unwrapped is None:
            _logger.warning(
                "vault transit: ciphertext is not a recognized envelope (corrupt/foreign) — "
                "the affected integration will read as disconnected until reconnected"
            )
            return None
        stored_key, transit_ciphertext = unwrapped
        if stored_key != self._key:
            raise ValueError(
                f"vault transit: ciphertext was written under key {stored_key!r} but the "
                f"configured key is {self._key!r} — refusing to read "
                "(OMNIGENT_CREDENTIAL_VAULT_KEY repointed?)"
            )
        try:
            resp = client.secrets.transit.decrypt_data(
                name=self._key,
                mount_point=self._mount,
                ciphertext=transit_ciphertext,
                context=_context_b64(context),
            )
        except vexc.InvalidRequest as exc:
            # Key name already matched, so a MAC failure here is a genuine per-row
            # condition (wrong derived context / corrupt). Anything else (e.g. the
            # key was deleted → "encryption key not found") is store-wide → propagate.
            if not any(m in str(exc).lower() for m in _PER_ROW_DECRYPT_MARKERS):
                raise
            _logger.warning(
                "vault transit: could not decrypt (wrong context or corrupt) — the "
                "affected integration will read as disconnected until reconnected: %s",
                exc,
            )
            return None
        return base64.b64decode(resp["data"]["plaintext"].encode("ascii")).decode("utf-8")
