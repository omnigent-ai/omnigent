# Shared preference operations

This opt-in API stores model choice (`model` and `opencode_model`), the default
harness, ordered arguments per harness, and plugin enable/disable choices. It
provides durable values, versions, conflict alternatives, and retry receipts.
It does not import settings, enable automatic client sync, or change launches.

## Enable an isolated server

In the server configuration file, set `shared_preferences_authority_id` to a
stable, nonempty identifier for this deployment. The CLI mounts the API only
when that setting is present. Replicas sharing a database must use the same ID;
separate deployments must use different IDs. Keep the ID across restarts and
URL changes. Existing stored state rejects an inconsistent authority.

Embedded deployments opt in by passing a `PreferenceStore` to `create_app`.
With no auth provider, requests use the reserved `local` user namespace.
With a provider, requests require its authenticated user identity. Workspace
identity comes from the trusted workspace context. An installation ID describes
the source of an edit; it is not an authorization grant.

## Read and edit

`GET /v1/preferences/shared` returns protocol version `1`, the authenticated
`identity`, a namespace `revision`, `entries`, unresolved `conflicts`, and
`next_cursor`. Pass `limit` (1–100, default 100) and the opaque `cursor` from the
previous page until `next_cursor` is null. Conflicts precede values; each range
uses deterministic key order. The combined entry/conflict count is at most
`limit`; receipt history is excluded.

Every cursor binds the authenticated namespace and revision. If any edit commits
between pages, the server returns 409: discard all collected pages and restart
from page one. Each page is internally consistent. Only publish a complete
snapshot from one revision; never adopt a partial page. Malformed or foreign
cursors return 400; cursors over 4,096 characters return 422. A cursor is not an
authorization credential. Authenticated user IDs that do not fit the namespace
name rules return 400.

Reads do not create records or initialize adoption. Each entry includes its
`key`, `version`, `value`, and originating `installation_id`. A missing key has
version `0`. A deleted key remains present with `value: null` and its version.

Copy the returned identity into each explicit edit. Submit one edit with
`POST /v1/preferences/shared/operations`:

```json
{
  "protocol_version": 1,
  "identity": {
    "authority_id": "isolated-preference-test",
    "user_id": "local",
    "workspace_id": "0"
  },
  "operation_id": "2e7e7332-ffb9-45a4-aa0c-4c6bbf6883e1",
  "installation_id": "laptop",
  "key": {"kind": "harness", "name": "default"},
  "expected_version": 0,
  "value": "pi-native",
  "resolves": []
}
```

Use a new UUID for each deliberate edit and retain it for retries. Read the
entry's version before editing it. The supplied identity must match the server's
authenticated context; it does not select another user or workspace.

| Key | Value |
| --- | --- |
| `{"kind":"model","name":"model"}` | Nonempty model name |
| `{"kind":"model","name":"opencode_model"}` | Separate OpenCode model name |
| `{"kind":"harness","name":"default"}` | Nonempty harness name |
| `{"kind":"harness_args","name":"claude-sdk"}` | Ordered string array; `[]` explicitly clears inherited arguments |
| `{"kind":"plugin","name":{"ecosystem":"claude","source":"catalog","name":"tools"}}` | Boolean; `false` explicitly disables the plugin |

For every key, `null` removes the shared choice. Harness aliases use the existing
canonicalization rules. Credentials, executable commands, plugin installation,
and arbitrary plugin configuration are not supported preference keys.

## Concurrent edits and retries

Every consumed operation returns HTTP 200 with a `status`:

- `applied`: the expected version matched and the value changed; the key version
  advances. The first removal creates a versioned deletion record.
- `unchanged`: the requested value is already current, even if the caller's
  version is older. The key version stays unchanged.
- `conflict`: another edit changed this key. The current value stays active;
  the submitted operation becomes a durable alternative in `conflicts`.

Each new consumed operation advances the namespace revision, including conflicts.
Independent keys compare their own versions, so unrelated edits do not conflict.
Retrying an identical operation returns its original result, including its old
revision. Clients must not replace newer state with an old receipt. Reusing an
operation ID with a different body returns HTTP 409 without changing anything.

To resolve a conflict, read the latest snapshot and send a new operation for that
key, using its current version, the chosen value, and the conflict operation IDs
in `resolves`. Only the named alternatives are removed. A stale resolution
creates another conflict and retains the originals. Missing or wrong-key conflict
IDs, a future version, and exhausted limits return HTTP 409 without consuming the
operation. Foreign identities return 403; malformed payloads return 422.
If the complete stored record exceeds 16,000 bytes, the response is 400
(`invalid_input`), even when the value alone fits. Nothing is committed and
the operation ID remains reusable with a smaller request.

## Storage and limits

Values, conflicts and retry receipts commit atomically in the existing
`preferences` table. See the [store](../omnigent/stores/preference_store/sqlalchemy_store.py)
for locking and bounded-query details.

Per user/workspace, version 1 allows 512 distinct keys including deletions,
128 unresolved conflicts, and 10,000 consumed operations. Each JSON-encoded
`{"value": ...}` is limited to 8,192 UTF-8 bytes. Names are at most 128 characters;
the full-record cap above includes JSON escaping and request metadata.

Receipts and deletions do not expire. At 10,000 consumed operations, all new IDs
are permanently blocked, including unchanged edits and conflict resolutions.
Reads and identical retries still work; open conflicts remain visible but cannot
be resolved. Retention/compaction and bounded account-deletion batches are
prerequisites for broad activation; neither is implemented yet.

The version-1 key encoding is explicit and pinned by digest tests. Changing its
encoding, harness aliases, or stored-record validation requires a migration or a
new storage version. Invalid stored rows fail the read; they are never silently
discarded. This unmerged format is not a migration for databases created from
earlier development revisions.

## Verify the implemented path

The [feature map](../feature-map/preferences.md) owns entry-point coverage.
The isolated server test drives the real CLI, HTTP routes, authentication,
database, two replicas, and a restart. It calls no model provider and applies no
native plugin settings:

```sh
uv run --no-sync pytest tests/e2e/omnigent/test_shared_preferences_e2e.py --reruns 0 -q -rs
```

Both the store tests and this server test can use an isolated PostgreSQL, MySQL,
or CockroachDB instance when `OMNIGENT_TEST_DB_URI` is configured. CI's external
database lanes run the store tests; running the real-process scenario on those
backends is an explicit verification step. An empty API snapshot is not an
adoption marker.
