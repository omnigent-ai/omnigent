"""Envelope and key-identifier handling for the Azure Key Vault cipher."""

from __future__ import annotations

import os
import sys
from typing import Any

import pytest
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
)
from azure.keyvault.keys.crypto import DecryptResult, EncryptionAlgorithm, EncryptResult
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from omnigent.stores.credential_store.azure_cipher import (
    CREDENTIAL_AZURE_KEY_ENV_VAR,
    AzureKeyVaultSecretCipher,
    _split_key_id,
    _unwrap,
    _wrap,
)

KEY_ID = "https://kv.vault.azure.net/keys/omnigent-credentials/9f8e7d"


def test_envelope_roundtrip() -> None:
    stored = _wrap(KEY_ID, b"\x00iv-bytes", b"tag\xff", b"ct\x01\x02")
    assert stored.startswith("osakv:1:")
    assert _unwrap(stored) == (KEY_ID, b"\x00iv-bytes", b"tag\xff", b"ct\x01\x02")


def test_unwrap_rejects_foreign_and_corrupt() -> None:
    # A ciphertext from another backend, read after an operator
    # switches OMNIGENT_CREDENTIAL_CIPHER, must be reported as unrecognized
    # rather than raising.
    assert _unwrap("osvault:b3RoZXI=:vault:v1:abc") is None
    assert _unwrap("AQICAHhwbG9i") is None  # raw base64 KMS blob
    assert _unwrap("") is None
    assert _unwrap("osakv:1:only:three") is None
    assert _unwrap("osakv:2:" + "a:" * 3 + "a") is None  # unknown format version
    assert _unwrap("osakv:1:!!!:!!!:!!!:!!!") is None  # undecodable base64


def test_split_key_id_parses_versioned_and_unversioned() -> None:
    assert _split_key_id(KEY_ID) == (
        "https://kv.vault.azure.net",
        "omnigent-credentials",
        "9f8e7d",
    )
    assert _split_key_id("https://kv.vault.azure.net/keys/k") == (
        "https://kv.vault.azure.net",
        "k",
        None,
    )
    assert _split_key_id("https://h.managedhsm.azure.net/keys/k/v")[0] == (
        "https://h.managedhsm.azure.net"
    )


def test_split_key_id_normalizes_case_and_trailing_slash() -> None:
    # DNS is case-insensitive and operators paste trailing
    # slashes. Neither may read as a different key.
    assert _split_key_id("HTTPS://KV.Vault.Azure.Net/keys/k/") == _split_key_id(
        "https://kv.vault.azure.net/keys/k"
    )


def test_split_key_id_rejects_malformed() -> None:
    for bad in (
        "",
        "not-a-url",
        "https://kv.vault.azure.net",
        "https://kv.vault.azure.net/secrets/s",
    ):
        with pytest.raises(ValueError):
            _split_key_id(bad)


@pytest.mark.parametrize(
    "bad",
    [
        "https://kv.vault.azure.net/keys/omnigent-credentials//9f8e7d",  # empty segment
        "https://kv.vault.azure.net/keys//9f8e7d",  # empty key name
        "https://kv.vault.azure.net/keys/omnigent-credentials/9f8e7d/extra",
    ],
)
def test_split_key_id_rejects_empty_and_extra_segments(bad: str) -> None:
    # A typo must fail at startup, not be read as some other key or version.
    with pytest.raises(ValueError, match=CREDENTIAL_AZURE_KEY_ENV_VAR):
        _split_key_id(bad)
    with pytest.raises(ValueError, match=CREDENTIAL_AZURE_KEY_ENV_VAR):
        AzureKeyVaultSecretCipher(bad, client_factory=_FakeVault().factory)


def test_split_key_id_error_shows_the_version_as_required() -> None:
    with pytest.raises(ValueError, match="keys/<name>/<version>,"):
        _split_key_id("not-a-url")


def test_stored_key_id_with_extra_segments_reads_as_disconnected() -> None:
    # Inside decrypt the parser guards a stored id, so a malformed one is per-row.
    vault = _FakeVault()
    cipher = AzureKeyVaultSecretCipher(KEY_ID, client_factory=vault.factory)
    unwrapped = _unwrap(cipher.encrypt("t", context=CTX_ALICE))
    assert unwrapped is not None
    _, iv, tag, blob = unwrapped
    corrupt = _wrap(KEY_ID + "/extra", iv, tag, blob)
    assert cipher.decrypt(corrupt, context=CTX_ALICE) is None


CTX_ALICE = {"workspace_id": "0", "user_id": "alice", "provider": "github", "account_id": ""}
CTX_BOB = {"workspace_id": "0", "user_id": "bob", "provider": "github", "account_id": ""}

#: Stand-in for the service's single-block encrypt limit. The real value is not
#: hardcoded anywhere in the implementation; the fake only needs *a* threshold
#: to prove an oversized payload raises instead of being truncated.
FAKE_BLOCK_LIMIT = 4096


def _http_error(status: int, message: str) -> HttpResponseError:
    err = HttpResponseError(message=message)
    err.status_code = status
    return err


class _FakeVault:
    """In-memory Key Vault holding one real AES-256 key per key version.

    Keys are created on first use, so distinct versions have distinct keys and a
    row encrypted under one version decrypts only under that version.
    """

    def __init__(self, *, key_type: str = "oct-HSM") -> None:
        self._key_type = key_type
        self._keys: dict[tuple[str, str, str | None], bytes] = {}
        #: Every key id a client was created for, in order.
        self.requested: list[str] = []
        #: Raised by every decrypt when set: an operational fault or a tripwire.
        self.raise_on_decrypt: BaseException | None = None

    def key_for(self, key_id: str) -> bytes:
        # Normalize like the service: host case and trailing slashes don't matter.
        return self._keys.setdefault(_split_key_id(key_id), AESGCM.generate_key(bit_length=256))

    def factory(self, key_id: str) -> _FakeCryptoClient:
        self.requested.append(key_id)
        return _FakeCryptoClient(key_id, self, key_type=self._key_type)


class _FakeCryptoClient:
    """CryptographyClient stand-in that performs real AES-256-GCM."""

    def __init__(self, key_id: str, vault: _FakeVault, *, key_type: str = "oct-HSM") -> None:
        self.key_id = key_id
        self._vault = vault
        self._key_type = key_type

    def encrypt(
        self,
        algorithm: str,
        plaintext: bytes,
        *,
        iv: bytes | None = None,
        additional_authenticated_data: bytes | None = None,
    ) -> Any:
        assert algorithm == "A256GCM"
        assert iv is None, "the service generates the GCM nonce"
        if self._key_type not in ("oct", "oct-HSM"):
            raise _http_error(400, f"algorithm {algorithm} is not supported for key type")
        if len(plaintext) > FAKE_BLOCK_LIMIT:
            raise _http_error(400, "the request payload exceeds the maximum block size")
        nonce = os.urandom(12)
        sealed = AESGCM(self._vault.key_for(self.key_id)).encrypt(
            nonce, plaintext, additional_authenticated_data
        )
        return EncryptResult(
            key_id=self.key_id,
            algorithm=EncryptionAlgorithm.a256_gcm,
            ciphertext=sealed[:-16],
            iv=nonce,
            authentication_tag=sealed[-16:],
        )

    def decrypt(
        self,
        algorithm: str,
        ciphertext: bytes,
        *,
        iv: bytes | None = None,
        authentication_tag: bytes | None = None,
        additional_authenticated_data: bytes | None = None,
    ) -> Any:
        assert algorithm == "A256GCM"
        if self._vault.raise_on_decrypt is not None:
            raise self._vault.raise_on_decrypt
        assert iv is not None and authentication_tag is not None
        try:
            plaintext = AESGCM(self._vault.key_for(self.key_id)).decrypt(
                iv, ciphertext + authentication_tag, additional_authenticated_data
            )
        except InvalidTag:
            raise _http_error(400, "the authentication tag could not be verified") from None
        return DecryptResult(self.key_id, EncryptionAlgorithm.a256_gcm, plaintext)


def _make_azure(*, key_type: str = "oct-HSM") -> AzureKeyVaultSecretCipher:
    return AzureKeyVaultSecretCipher(KEY_ID, client_factory=_FakeVault(key_type=key_type).factory)


def test_roundtrip_and_binding() -> None:
    cipher = _make_azure()
    stored = cipher.encrypt("ghu_secret_token", context=CTX_ALICE)
    assert stored.startswith("osakv:1:")
    assert cipher.decrypt(stored, context=CTX_ALICE) == "ghu_secret_token"
    assert cipher.decrypt(stored, context=CTX_BOB) is None


def test_non_ascii_roundtrips() -> None:
    # account_id is a user-facing label and tokens are not guaranteed ASCII;
    # both must survive as UTF-8.
    ctx = {**CTX_ALICE, "account_id": "orgé-日本"}
    cipher = _make_azure()
    assert cipher.decrypt(cipher.encrypt("tøken-✓", context=ctx), context=ctx) == "tøken-✓"


def test_oversized_plaintext_raises() -> None:
    # An over-limit secret must fail loudly, never be truncated.
    cipher = _make_azure()
    with pytest.raises(ValueError, match="single-block"):
        cipher.encrypt("x" * (FAKE_BLOCK_LIMIT + 1), context=CTX_ALICE)


def test_unversioned_key_id_is_rejected() -> None:
    # Without keys/get the SDK reports the configured id, not the version that
    # encrypted, so an unversioned id would orphan every row on rotation.
    with pytest.raises(ValueError, match=CREDENTIAL_AZURE_KEY_ENV_VAR):
        AzureKeyVaultSecretCipher(
            "https://kv.vault.azure.net/keys/omnigent-credentials",
            client_factory=_FakeVault().factory,
        )


OTHER_KEY_ID = "https://kv.vault.azure.net/keys/someone-elses-key/1a2b"
ROTATED_KEY_ID = "https://kv.vault.azure.net/keys/omnigent-credentials/NEWVERSION"


def test_key_repoint_raises_before_any_service_call() -> None:
    """A store-wide key repoint must RAISE, not soft-fail to None.

    The envelope carries its encrypting key, so decrypting under a differently
    named configured key is detected as store-wide and raises before the
    service is reached — it can never mask as a per-user reconnect, which would
    re-encrypt under the wrong key and overwrite recoverable ciphertext.
    """
    written = _make_azure().encrypt("t", context=CTX_ALICE)
    vault = _FakeVault()
    vault.raise_on_decrypt = AssertionError("the service must not be called")
    repointed = AzureKeyVaultSecretCipher(OTHER_KEY_ID, client_factory=vault.factory)
    with pytest.raises(ValueError, match="repointed"):
        repointed.decrypt(written, context=CTX_ALICE)
    assert vault.requested == []


def test_key_version_difference_still_decrypts() -> None:
    # Rotation must not orphan existing rows: only the version differs, so the
    # stored identifier is honoured rather than treated as a repoint.
    vault = _FakeVault()
    written = AzureKeyVaultSecretCipher(KEY_ID, client_factory=vault.factory).encrypt(
        "t", context=CTX_ALICE
    )
    rotated = AzureKeyVaultSecretCipher(ROTATED_KEY_ID, client_factory=vault.factory)
    assert rotated.decrypt(written, context=CTX_ALICE) == "t"


def test_configured_key_case_and_slash_are_not_a_repoint() -> None:
    # A pasted or differently-cased identifier must not raise.
    vault = _FakeVault()
    written = AzureKeyVaultSecretCipher(KEY_ID, client_factory=vault.factory).encrypt(
        "t", context=CTX_ALICE
    )
    sloppy = AzureKeyVaultSecretCipher(
        "HTTPS://KV.Vault.Azure.Net/keys/omnigent-credentials/9f8e7d/",
        client_factory=vault.factory,
    )
    assert sloppy.decrypt(written, context=CTX_ALICE) == "t"


@pytest.mark.parametrize(
    "error",
    [
        ClientAuthenticationError(message="the identity cannot use this key"),
        ResourceNotFoundError(message="key or version not found"),
        _http_error(403, "forbidden"),
        _http_error(429, "throttled"),
        _http_error(503, "service unavailable"),
    ],
)
def test_operational_errors_propagate(error: Exception) -> None:
    # Telling a user to reconnect would not help and would hide the fault.
    vault = _FakeVault()
    cipher = AzureKeyVaultSecretCipher(KEY_ID, client_factory=vault.factory)
    written = cipher.encrypt("t", context=CTX_ALICE)
    vault.raise_on_decrypt = error
    with pytest.raises(type(error)):
        cipher.decrypt(written, context=CTX_ALICE)


def test_non_aes_key_raises_loudly_on_first_encrypt() -> None:
    # RSA/EC has no AAD parameter, so it cannot carry the binding. Never soft-fail.
    cipher = _make_azure(key_type="RSA-HSM")
    with pytest.raises(ValueError, match="AES"):
        cipher.encrypt("t", context=CTX_ALICE)


def test_foreign_backend_ciphertext_reconnects() -> None:
    # The vault→azure migration path. Rows written by another
    # backend read as disconnected, never as a crash.
    cipher = _make_azure()
    assert cipher.decrypt("osvault:b3RoZXI=:vault:v1:abcd", context=CTX_ALICE) is None
    assert cipher.decrypt("AQICAHhwbG9iCg==", context=CTX_ALICE) is None
    assert cipher.decrypt("", context=CTX_ALICE) is None


V1_KEY_ID = "https://kv.vault.azure.net/keys/omnigent-credentials/v1"
V2_KEY_ID = "https://kv.vault.azure.net/keys/omnigent-credentials/v2"


def test_rotation_decrypts_under_the_stored_version() -> None:
    # After rotating to v2, a row written under v1 must be decrypted with the v1
    # key; v1 and v2 hold different keys, so routing to v2 cannot succeed.
    vault = _FakeVault()
    written = AzureKeyVaultSecretCipher(V1_KEY_ID, client_factory=vault.factory).encrypt(
        "ghu_v1_token", context=CTX_ALICE
    )
    rotated = AzureKeyVaultSecretCipher(V2_KEY_ID, client_factory=vault.factory)
    before = len(vault.requested)
    assert rotated.decrypt(written, context=CTX_ALICE) == "ghu_v1_token"
    assert vault.requested[before:] == [V1_KEY_ID]
    # New rows go to the rotated version.
    assert _unwrap(rotated.encrypt("ghu_v2_token", context=CTX_ALICE)) is not None
    assert vault.requested[-1] == V2_KEY_ID


def test_envelope_records_the_configured_versioned_key_id() -> None:
    # Decryption must use the exact encrypting version, so the envelope keeps it.
    cipher = AzureKeyVaultSecretCipher(V1_KEY_ID, client_factory=_FakeVault().factory)
    unwrapped = _unwrap(cipher.encrypt("t", context=CTX_ALICE))
    assert unwrapped is not None
    assert unwrapped[0] == V1_KEY_ID


def test_envelope_ignores_the_sdk_reported_key_id() -> None:
    # With keys/get the SDK may report the service's spelling of the key id; the
    # envelope must still carry the configured one so the repoint gate stays quiet.
    vault = _FakeVault()

    class _RespellingClient(_FakeCryptoClient):
        def encrypt(self, *args: Any, **kwargs: Any) -> Any:
            result = super().encrypt(*args, **kwargs)
            result.key_id = "https://kv.vault.azure.net:443/keys/Omnigent-Credentials/v1"
            return result

    def respelling_factory(key_id: str) -> _FakeCryptoClient:
        return _RespellingClient(key_id, vault)

    cipher = AzureKeyVaultSecretCipher(V1_KEY_ID, client_factory=respelling_factory)
    unwrapped = _unwrap(cipher.encrypt("t", context=CTX_ALICE))
    assert unwrapped is not None
    assert unwrapped[0] == V1_KEY_ID


def test_one_client_per_key_version() -> None:
    vault = _FakeVault()
    written = AzureKeyVaultSecretCipher(V1_KEY_ID, client_factory=vault.factory).encrypt(
        "t", context=CTX_ALICE
    )
    reader = AzureKeyVaultSecretCipher(V2_KEY_ID, client_factory=vault.factory)
    before = len(vault.requested)
    assert reader.decrypt(written, context=CTX_ALICE) == "t"
    assert reader.decrypt(written, context=CTX_ALICE) == "t"
    assert vault.requested[before:] == [V1_KEY_ID]


def test_real_client_path_is_cached_per_version(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without a factory the cipher builds real SDK clients, also one per version.
    import azure.identity
    import azure.keyvault.keys.crypto

    built: list[str] = []

    class _StubClient:
        def __init__(self, key_id: str, *, credential: Any) -> None:
            built.append(key_id)

    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", lambda: object())
    monkeypatch.setattr(azure.keyvault.keys.crypto, "CryptographyClient", _StubClient)
    cipher = AzureKeyVaultSecretCipher(V2_KEY_ID)
    first = cipher._crypto_for(V1_KEY_ID)
    assert cipher._crypto_for(V1_KEY_ID) is first
    cipher._crypto_for(V2_KEY_ID)
    assert built == [V1_KEY_ID, V2_KEY_ID]


def test_missing_sdk_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without omnigent[azure], both paths must say what to install, not "no module azure".
    cipher = _make_azure()
    written = cipher.encrypt("t", context=CTX_ALICE)
    monkeypatch.setitem(sys.modules, "azure.core.exceptions", None)
    with pytest.raises(ImportError, match=r"omnigent\[azure\]"):
        cipher.encrypt("t", context=CTX_ALICE)
    with pytest.raises(ImportError, match=r"omnigent\[azure\]"):
        cipher.decrypt(written, context=CTX_ALICE)
