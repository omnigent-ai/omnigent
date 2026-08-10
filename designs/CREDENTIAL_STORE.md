# Per-user integration credential store

## Motivation

The GitHub App integration needs to persist a per-user OAuth token set and vend
it to managed sandboxes on demand (see the credential broker below). The first
cut stored this in a GitHub-specific table (`github_connections`) encrypted with
a local Fernet key.

Two forces push toward generalizing that store *before* it accumulates real
data:

1. **More providers.** Connecting MCP integrations (Datadog, Slack, a hosted
   GitHub MCP, …) needs the same shape: a per-user secret + some non-secret
   metadata, connected once and re-vended to sandboxes. We do not want one
   bespoke table per provider.
2. **More secret backends.** A local Fernet key baked from an operator secret is
   fine for a single-tenant OSS deployment, but some deployments will want the
   secret material in a managed KMS/secret store — AWS Secrets Manager (KMS
   backed), Databricks secret scopes, HashiCorp Vault — for rotation, audit, and
   "the app process never holds the raw key."

Getting the schema generic now is cheap; reshaping `github_connections` after
users have connected means migrating encrypted rows. Hence this store lands at
the base of the integration stack, with GitHub as its first (and, today, only)
provider.

## Two independent axes

The design deliberately separates two concerns that are easy to conflate:

| Axis | What varies | This design |
| --- | --- | --- |
| **Data model** | "a credential for *any* provider, per user" | one generic table + typed façades per provider |
| **Secret backend** | *where* the secret lives and *who* encrypts it | a `SecretCipher` port; Fernet-in-DB is the default adapter |

Keeping them orthogonal means a new provider is a façade (no schema change) and
a new backend is a cipher adapter (no schema change).

## Data model

One table, `integration_connections`:

| column | type | notes |
| --- | --- | --- |
| `workspace_id` | bigint | tenant partition, part of PK (`0` = default) |
| `user_id` | str(128) | omnigent user, part of PK |
| `provider` | str(64) | e.g. `"github"`, part of PK |
| `account_id` | str(128) | provider account discriminator, part of PK; `""` = the user's single account for that provider |
| `secret_enc` | text | ciphertext of a JSON blob holding *all* secret material (access token, refresh token, …) |
| `metadata_json` | text | non-secret provider metadata (login, ids, scopes, expiries, …) as JSON |
| `created_at` / `updated_at` | bigint | unix epoch seconds |

Primary key: `(workspace_id, user_id, provider, account_id)`.

Why a JSON secret blob rather than typed token columns: any provider's secret
shape (single API key, OAuth access+refresh, client-cert, …) fits one encrypted
column, and the ciphertext is opaque to SQL. Non-secret fields that a provider
needs at refresh time (token expiries, granted scopes, the connected login/id)
live in `metadata_json` — queryable enough for our access patterns (we always
load the whole connection) without widening the schema per provider.

The `account_id` column is `""` today (one account per provider per user) but is
in the PK so a future "connect two GitHub orgs" needs no migration.

## `SecretCipher` port

```python
class SecretCipher(Protocol):
    def encrypt(self, plaintext: str) -> str: ...
    def decrypt(self, ciphertext: str) -> str | None: ...   # None on wrong-key/corrupt
```

`SecretBox` (Fernet, key derived from an operator secret) is the default
implementation and the only one shipped now. A `build_secret_cipher()` factory
is the single seam where a deployment selects a backend; it reads the
**store-level** key `OMNIGENT_CREDENTIAL_ENC_KEY` and returns the cipher, or
`None` when unset (the store — and every integration on it — is then disabled).

The key belongs to the **credential store**, not to any one provider: every
provider façade shares the one cipher, so connecting an MCP server or Datadog
account needs no provider-specific key and does not depend on GitHub being
configured. A deployment sets `OMNIGENT_CREDENTIAL_ENC_KEY` to a dedicated
high-entropy secret; rotating it invalidates all stored secrets (they decrypt to
`None` ⇒ reconnect), which is why it is deliberately dedicated rather than
borrowed from another secret. Provider config gates that provider's routes, not
the store's ability to encrypt.

Planned adapters (not built until a deployment needs them, so the interface is
designed against a real second backend rather than speculatively):

- **AWS**: envelope encryption — a KMS data key wraps the blob, ciphertext stays
  in `secret_enc`; or push the blob into Secrets Manager and store only its ARN
  in `secret_enc`. The former keeps the store shape; the latter moves the secret
  out of our DB entirely.
- **Databricks**: a secret scope holds the material; `secret_enc` holds the
  scope/key reference.
- **Vault**: transit-encrypt (envelope) or a KV path reference.

`decrypt` returning `None` (not raising) on a key mismatch is load-bearing: a
rotated key degrades to "reconnect this integration," never a 500 on the vend
path.

## Provider façades

Callers never touch the generic store directly. Each provider gets a thin typed
façade that maps its domain object ⇄ `(secret dict, metadata dict)`:

- `GithubConnectionStore` — `provider="github"`; secret = `{access_token,
  refresh_token}`; metadata = `{github_login, github_user_id, token_expires_at,
  refresh_token_expires_at, scopes}`. Its public surface (`upsert`,
  `update_tokens`, `get`, `delete`, `list_all`) is unchanged, so the routes, the
  broker endpoint, and the launch path keep working verbatim.

A future MCP-credential façade is the same pattern with `provider="mcp:<name>"`.

## Migration

The integration stack is unmerged, so the existing `add_github_connections`
migration is rewritten in place to create `integration_connections` with the
generic schema (no second migration, single Alembic head preserved). No
production DB has the old shape yet; the demo's dev DB is reset so the reshaped
migration runs fresh.

If this had already shipped, the migration would instead `CREATE` the new table
and copy `github_connections` rows across (`provider='github'`,
`account_id=''`, secret blob = `{access_token, refresh_token}` re-encrypted or
carried as-is since the cipher is unchanged), then drop the old table.

## Broker endpoint (future generalization)

The credential broker currently vends only GitHub:
`GET /v1/hosts/{host_id}/github-credential`. When a second provider needs
on-demand delivery to a sandbox, this becomes
`GET /v1/hosts/{host_id}/credentials/{provider}` returning the provider's secret
+ attribution metadata, with the git credential helper and the GitHub MCP proxy
passing `provider=github`. Kept GitHub-specific for now to limit blast radius;
the store change is the prerequisite that makes it a small follow-up.

## Revocation & audit note

Today a vended GitHub token's blast radius is bounded by the launch token: the
broker resolves `(host_id, launch_token)` server-side and stops vending the
moment the launch token expires or the host row is deleted, and the raw token
never touches sandbox disk. Moving secret material into an external backend
(Secrets Manager/Databricks/Vault) shifts *rotation and audit* to that backend
and must not weaken this: "stops vending when the session ends" has to remain a
server-side check on the broker path, independent of where the ciphertext lives.
Design each backend adapter so revocation stays a property of the broker, not of
the storage.
