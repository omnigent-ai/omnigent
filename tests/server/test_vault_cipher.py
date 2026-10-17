"""Verify the ``SecretCipher`` PORT works for AWS KMS, Vault Transit and Azure Key Vault.

One behavioral contract, parametrized over three backends:
  - ``KmsSecretCipher`` with an in-memory fake KMS client (no AWS),
  - ``VaultSecretCipher`` against a live Vault dev server (localhost:8200), and
  - ``AzureKeyVaultSecretCipher`` with an in-memory fake crypto client (no Azure).

If all three pass the same assertions, the port abstraction genuinely
generalizes rather than being KMS-shaped. The Vault params skip cleanly when no
dev server is reachable. Requires a Transit key created with ``derived=true``.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from botocore.exceptions import ClientError

from omnigent.stores.credential_store.azure_cipher import (
    CREDENTIAL_AZURE_KEY_ENV_VAR,
    AzureKeyVaultSecretCipher,
)
from omnigent.stores.credential_store.secret_cipher import (
    CREDENTIAL_CIPHER_ENV_VAR,
    CREDENTIAL_KMS_KEY_ENV_VAR,
    KmsSecretCipher,
    SecretCipher,
    build_secret_cipher,
    canonical_context_json,
)
from omnigent.stores.credential_store.vault_cipher import (
    CREDENTIAL_VAULT_KEY_ENV_VAR,
    VaultSecretCipher,
)
from tests.server.test_azure_cipher import _make_azure

CTX_ALICE = {"workspace_id": "0", "user_id": "alice", "provider": "github", "account_id": ""}
CTX_BOB = {"workspace_id": "0", "user_id": "bob", "provider": "github", "account_id": ""}

VAULT_ADDR = os.environ.get("VAULT_ADDR", "http://127.0.0.1:8200")
VAULT_TOKEN = os.environ.get("VAULT_TOKEN", "devroot")
VAULT_KEY = os.environ.get("OMNIGENT_CREDENTIAL_VAULT_KEY", "omnigent-cred")
AZURE_KEY_ID = "https://kv.vault.azure.net/keys/omnigent-credentials/9f8e7d"


class _FakeKmsClient:
    """In-memory KMS stand-in: decrypt succeeds only under the same context."""

    def __init__(self) -> None:
        self.store: dict[bytes, tuple[str, dict[str, str], bytes]] = {}
        self._n = 0

    def encrypt(self, *, KeyId: str, Plaintext: bytes, EncryptionContext: dict[str, str]) -> Any:
        self._n += 1
        blob = f"ct-{self._n}".encode()
        self.store[blob] = (KeyId, dict(EncryptionContext), Plaintext)
        return {"CiphertextBlob": blob, "KeyId": KeyId}

    def decrypt(
        self, *, CiphertextBlob: bytes, EncryptionContext: dict[str, str], KeyId: str | None = None
    ) -> Any:
        entry = self.store.get(CiphertextBlob)
        if entry is None or entry[1] != dict(EncryptionContext):
            raise ClientError(
                {"Error": {"Code": "InvalidCiphertextException", "Message": "mismatch"}}, "Decrypt"
            )
        return {"Plaintext": entry[2], "KeyId": entry[0]}


def _make_kms() -> SecretCipher:
    return KmsSecretCipher("alias/omnigent-credentials", client=_FakeKmsClient())


def _make_vault() -> SecretCipher:
    hvac = pytest.importorskip("hvac")
    client = hvac.Client(url=VAULT_ADDR, token=VAULT_TOKEN)
    try:
        ok = client.is_authenticated()
    except Exception:
        ok = False
    if not ok:
        pytest.skip("no live Vault dev server at VAULT_ADDR")
    return VaultSecretCipher(VAULT_KEY, client=client)


@pytest.fixture(params=["kms", "vault", "azure"])
def cipher(request: pytest.FixtureRequest) -> SecretCipher:
    if request.param == "kms":
        return _make_kms()
    if request.param == "azure":
        return _make_azure()
    return _make_vault()


def test_satisfies_port(cipher: SecretCipher) -> None:
    # The whole point: both backends satisfy the SAME runtime-checkable port.
    assert isinstance(cipher, SecretCipher)


def test_roundtrip(cipher: SecretCipher) -> None:
    ct = cipher.encrypt("ghu_secret_token", context=CTX_ALICE)
    assert ct != "ghu_secret_token"
    assert cipher.decrypt(ct, context=CTX_ALICE) == "ghu_secret_token"


def test_wrong_context_returns_none(cipher: SecretCipher) -> None:
    # A secret encrypted for one identity can't be read under another's.
    ct = cipher.encrypt("ghu_alice", context=CTX_ALICE)
    assert cipher.decrypt(ct, context=CTX_BOB) is None


def test_context_is_required(cipher: SecretCipher) -> None:
    with pytest.raises(TypeError):
        cipher.encrypt("x")  # type: ignore[call-arg]


def test_named_account_is_distinct(cipher: SecretCipher) -> None:
    # account_id="" is dropped; a named account is a different, still-usable context.
    named = {**CTX_ALICE, "account_id": "org1"}
    assert cipher.decrypt(cipher.encrypt("y", context=named), context=named) == "y"
    ct_default = cipher.encrypt("x", context=CTX_ALICE)
    assert cipher.decrypt(ct_default, context=named) is None


def test_corrupt_ciphertext_returns_none(cipher: SecretCipher) -> None:
    assert cipher.decrypt("not-a-real-ciphertext!!", context=CTX_ALICE) is None


def test_vault_key_repoint_propagates() -> None:
    """A store-wide key repoint must RAISE, not soft-fail to None.

    The ciphertext carries its encrypting key's name, so decrypting under a
    different configured key (``OMNIGENT_CREDENTIAL_VAULT_KEY`` repointed — whether
    or not the new key exists) is detected as store-wide and raises *before* Transit
    is called, so it never masks as a per-user reconnect (which would re-encrypt
    under the wrong key). This is the repointed-existing-key case a substring-only
    heuristic could not catch — both it and a wrong context surface as Transit's
    ``message authentication failed``.
    """
    client = pytest.importorskip("hvac").Client(url=VAULT_ADDR, token=VAULT_TOKEN)
    try:
        ok = client.is_authenticated()
    except Exception:
        ok = False
    if not ok:
        pytest.skip("no live Vault dev server at VAULT_ADDR")
    ct = VaultSecretCipher(VAULT_KEY, client=client).encrypt("t", context=CTX_ALICE)
    repointed = VaultSecretCipher(f"{VAULT_KEY}-other", client=client)
    with pytest.raises(ValueError):
        repointed.decrypt(ct, context=CTX_ALICE)


# ── build_secret_cipher: per-server backend selection ──────────────────────
# These exercise the dispatch only; constructing a cipher reads env (no SDK call
# and no network), so no live Vault or AWS is needed.


def _clear_backend_env(mp: pytest.MonkeyPatch) -> None:
    for var in (
        CREDENTIAL_CIPHER_ENV_VAR,
        CREDENTIAL_KMS_KEY_ENV_VAR,
        CREDENTIAL_VAULT_KEY_ENV_VAR,
        CREDENTIAL_AZURE_KEY_ENV_VAR,
    ):
        mp.delenv(var, raising=False)


def test_explicit_selector_picks_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv(CREDENTIAL_CIPHER_ENV_VAR, "vault")
    monkeypatch.setenv(CREDENTIAL_VAULT_KEY_ENV_VAR, "omnigent-cred")
    assert isinstance(build_secret_cipher(), VaultSecretCipher)


def test_explicit_selector_requires_its_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv(CREDENTIAL_CIPHER_ENV_VAR, "vault")  # selected, but no vault key
    with pytest.raises(ValueError):
        build_secret_cipher()


def test_unknown_selector_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv(CREDENTIAL_CIPHER_ENV_VAR, "bogus")
    with pytest.raises(ValueError):
        build_secret_cipher()


def test_autodetect_single_then_ambiguous(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_backend_env(monkeypatch)
    assert build_secret_cipher() is None  # none configured → disabled
    monkeypatch.setenv(CREDENTIAL_VAULT_KEY_ENV_VAR, "omnigent-cred")
    assert isinstance(build_secret_cipher(), VaultSecretCipher)  # single → that one
    monkeypatch.setenv(CREDENTIAL_KMS_KEY_ENV_VAR, "alias/x")
    with pytest.raises(ValueError):  # both + no selector → ambiguous, no silent precedence
        build_secret_cipher()


def test_azure_selector_picks_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv(CREDENTIAL_CIPHER_ENV_VAR, "azure")
    monkeypatch.setenv(CREDENTIAL_AZURE_KEY_ENV_VAR, AZURE_KEY_ID)
    assert isinstance(build_secret_cipher(), AzureKeyVaultSecretCipher)


def test_azure_selector_requires_its_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv(CREDENTIAL_CIPHER_ENV_VAR, "azure")
    with pytest.raises(ValueError, match="key is unset") as excinfo:
        build_secret_cipher()
    # A known selector with no key, not an unregistered backend.
    assert "not a known credential" not in str(excinfo.value)


def test_azure_autodetects_and_conflicts(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv(CREDENTIAL_AZURE_KEY_ENV_VAR, AZURE_KEY_ID)
    assert isinstance(build_secret_cipher(), AzureKeyVaultSecretCipher)  # single → that one
    monkeypatch.setenv(CREDENTIAL_VAULT_KEY_ENV_VAR, "omnigent-cred")
    with pytest.raises(ValueError):  # two configured, no selector → ambiguous
        build_secret_cipher()


def test_azure_malformed_key_id_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # A typo in config must fail at build with a message naming the env var,
    # not produce a confusing SDK error on the first encrypt.
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv(CREDENTIAL_AZURE_KEY_ENV_VAR, "kv.vault.azure.net/omnigent-cred")
    with pytest.raises(ValueError, match=CREDENTIAL_AZURE_KEY_ENV_VAR):
        build_secret_cipher()


def test_canonical_context_is_order_independent() -> None:
    a = canonical_context_json({"user_id": "alice", "workspace_id": "0"})
    b = canonical_context_json({"workspace_id": "0", "user_id": "alice"})
    assert a == b
    assert isinstance(a, bytes)


def test_canonical_context_drops_empty_values() -> None:
    assert canonical_context_json({"a": "1", "b": ""}) == canonical_context_json({"a": "1"})


def test_canonical_context_golden_bytes() -> None:
    ctx = {"workspace_id": "0", "user_id": "alice", "provider": "github", "account_id": ""}
    assert (
        canonical_context_json(ctx)
        == b'{"provider":"github","user_id":"alice","workspace_id":"0"}'
    )


def test_canonical_context_cannot_collide_distinct_identities() -> None:
    # A value containing a delimiter must not collide two distinct identities
    # onto the same AAD.
    left = canonical_context_json({"provider": "github", "account_id": "a=b"})
    right = canonical_context_json({"provider": "github", "account_id": "a", "b": ""})
    assert left != right
    nl = canonical_context_json({"account_id": "a\nprovider=x"})
    assert nl != canonical_context_json({"account_id": "a", "provider": "x"})


def test_vault_context_b64_matches_canonical() -> None:
    import base64

    from omnigent.stores.credential_store.vault_cipher import _context_b64

    ctx = {"workspace_id": "0", "user_id": "alice", "provider": "github", "account_id": ""}
    assert base64.b64decode(_context_b64(ctx)) == canonical_context_json(ctx)
