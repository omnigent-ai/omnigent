# Azure Key Vault Secret Cipher Design

## Goal

Add `azure` as a third `SecretCipher` backend so Azure deployments reach Key
Vault or Managed HSM with a managed identity, the way AWS deployments reach KMS
with an IAM role. Today `build_secret_cipher()` offers `kms` and `vault` only,
so an Azure operator must stand up and run HashiCorp Vault to use the
CredentialStore and every "Connect …" integration built on it.

This closes the Azure half of the `SecretCipher` docstring's own promise — "the
KMS implementation can be swapped (e.g. for GCP/Azure KMS or Vault Transit)
without a schema change" — and resolves omnigent-ai/omnigent#8302.

## Binding: AES-GCM with the row identity as AAD

The row identity — `(workspace_id, user_id, provider, account_id)` — is passed
as AES-GCM additional authenticated data. This is the direct analogue of KMS's
`EncryptionContext`: the same primitive, enforced by the backend, with no
global-key path and no envelope/data-key layer. Decryption requires the same
AAD, so a wrong row identity fails authentication and reads as "reconnect".

The Python SDK exposes AAD directly on both operations:

    encrypt(algorithm, plaintext, *, iv=None,
            additional_authenticated_data=None) -> EncryptResult
    decrypt(algorithm, ciphertext, *, iv=None, authentication_tag=None,
            additional_authenticated_data=None) -> DecryptResult

`A256GCM` is used, the GCM nonce is service-generated (no `iv` is passed on
encrypt), and `EncryptResult` returns the `iv` and `authentication_tag` that
decryption requires.

AAD bytes are the same canonical serialization the Vault backend already uses
for its derived context: empty values dropped, then sorted-key JSON with
compact separators. A value containing `=` or a newline therefore cannot
collide two distinct identities, matching the field-by-field binding KMS gets
from its structured `EncryptionContext`.

To keep one implementation of that binding rather than two that can drift,
`vault_cipher._context_b64`'s canonicalization moves to `secret_cipher.py` as
`canonical_context_json(context) -> bytes`. `_context_b64` becomes a base64 of
that, unchanged in behavior, and the Azure backend passes the bytes straight
through as AAD. If a reviewer prefers no shared change, the fallback is a
private `_aad()` in `azure_cipher.py` producing byte-identical output, with the
duplication called out in both docstrings.

## Required Azure resource

AES keys, and therefore AAD, exist only on:

- **Managed HSM** — `oct` keys, GA.
- **Key Vault Premium** — `oct-HSM` keys, public preview.

Key Vault **Standard** is RSA/EC only and has no AAD parameter, so it cannot
satisfy the Protocol's required-`context` contract. It is unsupported, and the
backend fails loudly rather than silently dropping the binding.

Both supported resources use the same client, the same algorithm and the same
code; only the key identifier's host differs (`<vault>.vault.azure.net` versus
`<hsm>.managedhsm.azure.net`). The choice is a deployment decision with no
effect on this design.

## Configuration

- `OMNIGENT_CREDENTIAL_AZURE_KEY_ID` — the full key identifier,
  `https://<vault>.vault.azure.net/keys/<name>/<version>`. The version is
  required; an unversioned identifier is rejected at construction. Unset ⇒ the
  builder returns `None` and the backend is simply not selected, matching both
  existing builders' disabled-when-unset contract.
- `OMNIGENT_CREDENTIAL_CIPHER=azure` — explicit selection, required only when
  more than one backend is configured.

Credentials come from `azure-identity`'s `DefaultAzureCredential`, so AKS
Workload Identity, a user-assigned managed identity, or a service principal all
work with no omnigent-specific configuration — the counterpart of KMS-via-IRSA
and Transit-via-`VAULT_TOKEN`.

Permissions needed are `keys/encrypt` and `keys/decrypt` only (Key Vault Crypto
User, or the Managed HSM equivalent role). `keys/get` is **not** required:
`CryptographyClient` attempts to fetch key material to operate locally and
falls back to service-side operations when it cannot, and an `oct-HSM` key is
non-exportable in any case, so operations are always remote.

Deployments should set `AZURE_CLIENT_ID` explicitly. `DefaultAzureCredential`
walks several credential sources in order, and on a cluster with more than one
usable identity an unset client id can silently bind to the wrong one.

## Ciphertext format

The AAD is reconstructible from the row, but `iv` and `authentication_tag` are
not, so they must be stored. An Azure Key Vault ciphertext — unlike a KMS blob
— also does not embed the key identifier, so the envelope carries it:

    osakv:1:<b64url(key_id)>:<b64url(iv)>:<b64url(tag)>:<b64url(ciphertext)>

Components use padded URL-safe base64, as `vault_cipher._wrap` already does;
the `=` padding character cannot collide with the `:` delimiter. Parsed with
`split(":", 5)`; a string that is not six parts with the `osakv` prefix is a
per-row failure (corrupt or foreign). The `1` is a format version,
reserved so a future variant — an RSA-wrap path for Key Vault Standard, say —
can coexist with rows already written rather than requiring a migration.

`key_id` is the versioned identifier from configuration. The original design
took it from `EncryptResult.key_id` as the service's resolved `kid`; measured
against `azure-keyvault-keys` 4.11.2, `CryptographyClient.encrypt()` returns
`EncryptResult(key_id=self.key_id, ...)`, the identifier passed to the
constructor, and discards the `kid` in the response. Only a successful
`get_key()` (the `keys/get` permission, deliberately not required here) replaces
it with the versioned id, and the SDK swallows the resulting 403. An unversioned
configured identifier would therefore be recorded unversioned in every envelope.
The design requires a versioned identifier in configuration, which is the
fallback this paragraph originally named; an unversioned one is rejected at
construction. The envelope records the configured identifier itself rather
than `EncryptResult.key_id`, which the SDK respells to the service's `kid`
whenever `get_key()` succeeds. Note too that `EncryptResult` takes the GCM tag
as its `authentication_tag` keyword but stores it as `.tag`. Key Vault decryption must
use the exact key version that encrypted the data, and the ciphertext does not
carry it, so the envelope records it. Rotating means pinning a new version in
configuration: new rows use it, and existing rows keep decrypting under their
original version, as the stored identifier is what decryption uses. This
preserves the rotation behavior KMS gets from its key-embedding blobs, at the
cost of a config change. Decryption builds a client for the stored
identifier, cached per version.

On decrypt, the stored identifier's vault host and key name are compared
against the configured key. A mismatch is store-wide misconfiguration — a
repointed `OMNIGENT_CREDENTIAL_AZURE_KEY_ID` — and raises *before* any service
call, exactly as `vault_cipher` does, so it cannot masquerade as a per-user
disconnect and overwrite still-recoverable ciphertext on reconnect. Only the
version component is allowed to differ.

## Error taxonomy

Per-row failures degrade to `decrypt` → `None` ("reconnect", never a wrong
plaintext):

- The stored string is not a recognized `osakv` envelope.
- AES-GCM authentication fails — a wrong row identity, or a corrupt ciphertext,
  `iv` or tag.

Store-wide and operational failures propagate, because telling a user to
reconnect would not help and would hide a misconfiguration:

- A stored key identifier whose vault or key name differs from the configured
  key (raised before the service call).
- `ClientAuthenticationError` and HTTP 403 — the identity cannot use the key.
- `ResourceNotFoundError` — the key or that version was deleted.
- Throttling (429) and 5xx.
- A non-AES key, see below.

**Measured against a live vault.** On Key Vault Premium with an `oct-HSM`
AES-256 key, called by an identity holding only `keys/encrypt` and
`keys/decrypt`, an AES-GCM decrypt whose AAD does not match fails with
`HttpResponseError` status 400, service error code `BadParameter` ("Unable to
decrypt specified value with this key"). A tampered tag, a tampered
ciphertext, a wrong nonce, and a swapped nonce and tag all fail identically.
Every 400 this decrypt can produce is therefore a per-row condition, and the
per-row signal stays keyed on `HttpResponseError.status_code == 400`.
Narrowing it to `BadParameter` would add nothing: the non-AES-key 400 the
spec worried about arises on encrypt, which never soft-fails. Matching on an
error-message substring remains rejected as the discriminator — `vault_cipher`
documents that exact fragility, where a wrong context and a wrong key arrive as
the same 400 and a substring check cannot separate them.

## Non-AES keys

The existing builders are network-free: they read environment variables and
construct, with no SDK import and no service call. `build_azure_secret_cipher`
keeps that contract, so a non-AES key cannot be rejected at build time without
either a `keys/get` permission this design deliberately does not require or a
startup round trip the other backends do not make.

Detection is therefore first-use. The first `encrypt` or `decrypt` against an
RSA or EC key is rejected by the service for `A256GCM`; that error is caught
and re-raised as a store-wide `ValueError` naming
`OMNIGENT_CREDENTIAL_AZURE_KEY_ID` and the required key type (`oct` on Managed
HSM, `oct-HSM` on Key Vault Premium). It is never soft-failed — a silently
unbound secret is the one outcome the Protocol forbids.

## Registry entry

`azure_cipher.py` sits beside `vault_cipher.py` and exports
`AzureKeyVaultSecretCipher` and `build_azure_secret_cipher`.
`build_secret_cipher()` gains one line:

    builders: dict[str, Callable[[], SecretCipher | None]] = {
        "kms": build_kms_secret_cipher,
        "vault": build_vault_secret_cipher,
        "azure": build_azure_secret_cipher,
    }

Per that function's own docstring, "adding a backend is a new registry entry,
not a new branch". The lazy import joins the existing one, keeping the Azure
SDKs optional on the same terms as `hvac` and `boto3`. Selector docstrings
listing `kms` | `vault` widen to include `azure`; the unknown-selector error
message already enumerates `sorted(builders)` and needs no change.

## Packaging

A new extra, following the convention and comment style of the existing
per-backend extras:

    azure = ["azure-keyvault-keys>=4.9,<5", "azure-identity>=1.17,<2"]

Both are imported lazily inside the client property, exactly as `vault_cipher`
does with `hvac` and `KmsSecretCipher` does with `boto3`, with an `ImportError`
naming `pip install 'omnigent[azure]'`.

These floors are deliberately conservative rather than minimal: both are
releases that certainly carry what is needed —
`additional_authenticated_data` on `CryptographyClient.encrypt`/`decrypt`, and
Workload Identity support in `DefaultAzureCredential`. They may be lowered if
broader compatibility is wanted, but not below the release that introduced the
AAD keyword, which is checked against the changelog rather than guessed.

## Delivery: one class, two vehicles

The implementation is identical whether it ships upstream or locally, so it is
written once.

**Upstream PR** — `azure_cipher.py`, the registry entry, the extra, tests and
docs, against omnigent-ai/omnigent#8302.

**Local shim**, to take the reporting deployment off Vault without waiting on
review or a release, and without forking. A separate small package installed
into the server image ships the *same* `azure_cipher.py` and a
`sitecustomize.py` that rebinds
`omnigent.stores.credential_store.build_secret_cipher`. This works because both
call sites — `omnigent/cli.py` and `deploy/docker/entrypoint.py` — import the
name *inside the function body*, so it resolves at call time, after interpreter
startup. The shim asserts its patch took effect and refuses to start if not:
the dangerous failure is an upgrade that restructures the call site, leaving the
patch inert and the store quietly disabled. The file is copied from the PR
branch, never reimplemented, so the two cannot diverge.

Once the PR merges, the shim package is uninstalled and
`OMNIGENT_CREDENTIAL_CIPHER=azure` selects the in-tree backend. Rows written by
the shim are readable by the merged code, because the envelope and AAD are the
same bytes.

## Testing

`tests/server/test_vault_cipher.py` already parametrizes a `cipher` fixture
over `_make_kms()` and `_make_vault()` and drives the shared port contract
through it. Adding `_make_azure()` inherits that suite unchanged:
`test_satisfies_port`, `test_roundtrip`, `test_wrong_context_returns_none`,
`test_context_is_required`, `test_named_account_is_distinct` and
`test_corrupt_ciphertext_returns_none`.

The injected fake mirrors `_FakeKmsClient` but performs real AES-GCM via
`cryptography`'s `AESGCM`, so a wrong AAD genuinely fails authentication
instead of simulating the failure. Whether `cryptography` is already an
installed test dependency is confirmed before relying on it; if not, the fake
compares AAD explicitly and raises the service's error shape.

Azure-specific tests will cover:

- A repointed key — different vault or key name in the stored envelope — raises
  store-wide, and does so before any service call.
- A key *version* difference decrypts successfully, proving rotation does not
  orphan existing rows.
- A non-AES key raises loudly on first use and is never soft-failed.
- Authentication, not-found and throttling errors propagate; the per-row
  authentication failure returns `None`.
- `build_secret_cipher()` selection across three backends: explicit selector,
  selector without its key, autodetect of a single configured backend, and the
  ambiguity error when two are configured.

The file now hosts three backends under a Vault-specific name; renaming it to
`test_secret_cipher_backends.py` is left to the maintainer's preference, as it
is cosmetic and would obscure the diff.

## Documentation

`designs/CREDENTIAL_STORE.md` gains an Azure subsection under `## SecretCipher
port` and a "Pod → Key Vault auth (Workload Identity)" counterpart to its
existing `### Pod → KMS auth (IRSA)` section, including the Premium-preview
caveat and the Standard-tier exclusion.

Two stale AWS-only statements are corrected in passing, both already noted on
the issue. `secret_cipher.py`'s module docstring still opens "AWS KMS-backed
encryption for integration secrets stored at rest" and still asserts "There is
no non-KMS fallback", though the module has hosted a pluggable registry since
the Vault backend landed. `credential_store/__init__.py` likewise describes the
port as "(AWS-KMS implementation)".

## Risks

**Preview status.** `oct-HSM` on Key Vault Premium is public preview: no SLA,
and Microsoft states it is not recommended for production workloads. Accepted
deliberately. The exposure is bounded because the code is identical on Managed
HSM, so the fallback is a data migration rather than a redesign — and what is
encrypted is refresh tokens, whose loss means users reconnect, which is the
store's designed soft-fail path.

**The 400 discriminator.** Flagged above as the one item requiring live
verification. Getting it wrong in the permissive direction turns an operational
fault into a silent mass disconnect; in the strict direction it turns a
per-user reconnect into a 500.

**Per-operation latency.** Every encrypt and decrypt is one service round trip,
the same shape as KMS and Transit, and subject to Key Vault request throttling.
No new caching is introduced; this matches the existing backends rather than
diverging from them.
