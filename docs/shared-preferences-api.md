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
Universe wiring and managed verification remain separate. The server derives
user identity from its configured authentication provider and workspace identity
from the existing trusted workspace context. An installation ID describes the
source of an edit; it is not proof of machine identity or an authorization grant.

## Read and edit

`GET /v1/preferences/shared` returns protocol version `1`, the authenticated
`identity`, a namespace `revision`, `entries`, unresolved `conflicts`, and
`next_cursor`. Pass `limit` (1–100, default 100) and the opaque `cursor` from the
previous page until `next_cursor` is null. Conflicts precede values; each range
uses deterministic key order. The combined entry/conflict count is at most
`limit`. Metadata is a point lookup, and indexed key ranges exclude receipt
history; work per page is O(limit), independent of receipt count.

Every cursor binds the authenticated namespace and revision. If any edit commits
between pages, the server returns 409: discard all collected pages and restart
from page one. Each page is one SQL statement combining a metadata point lookup with two
independently limited index ranges, so its rows come from one consistent view. Only publish a complete
snapshot from one revision; never adopt a partial page. Malformed or foreign
cursors return 400. A cursor is not an authorization credential.

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

## Storage and limits

The existing `preferences` table holds separate records for current values,
operation receipts, conflicts, and namespace counters under `shared.v1.*` keys.
An account lock (where applicable) precedes the namespace lock. SQLite uses an
immediate write transaction; the other backends lock the namespace row. The
existing transaction helper retries supported database serialization/deadlock
failures. Value, conflict, counter, and receipt updates commit together.

Per user/workspace, version 1 allows 512 distinct keys including deletions,
128 unresolved conflicts, and 10,000 consumed operations. Each JSON-encoded
`{"value": ...}` is limited to 8,192 UTF-8 bytes. Names are at most 128 characters;
individual stored records fit the existing 65,535-byte BLOB contract.

Receipts and deletions do not expire. Hitting a limit rejects further work rather
than discarding history; existing reads and receipt retries still work. Resolve
conflicts before reaching the operation cap. Raising limits or compacting history
requires a migration/protocol plan before broad activation. Account deletion
uses the existing cleanup of all user preference rows.

## Verify the implemented path

The [feature map](../feature-map/preferences.md) owns entry-point coverage.
The isolated server test drives the real CLI, HTTP routes, authentication,
database, two replicas, and a restart. It calls no model provider and applies no
native plugin settings:

```sh
uv run --no-sync pytest tests/e2e/omnigent/test_shared_preferences_e2e.py --reruns 0 -q -rs
```

Both the store tests and this server test can use an isolated PostgreSQL, MySQL,
or CockroachDB instance via the existing `OMNIGENT_TEST_DB_URI` fixture. Managed deployment, Arclet startup, automatic
outbox/reconnect handling, import/rollback, and native plugin isolation remain
unverified follow-ups. An empty API snapshot is not an adoption marker.
