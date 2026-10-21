# Durable preference client

`PreferenceClient` adds a local SQLite cache/outbox above the opt-in
[server API](shared-preferences-api.md). It is an explicit Python integration
point; no host, launcher, browser, or Arclet automatically opts in. The caller
supplies its authenticated `httpx.AsyncClient`, expected identity, private cache
path, and installation ID. Credentials are never persisted in the cache.

```python
from pathlib import Path
from omnigent.preference_client import PreferenceClient
from omnigent.preference_sync import PreferenceNamespace, SharedPreferenceKey

local = PreferenceClient(
    Path("/private/client-data/workspace-preferences.db"),
    identity=PreferenceNamespace(
        authority_id="my-deployment", user_id="my-user", workspace_id="17"
    ),
    installation_id="my-laptop",
)
# http is the caller's authenticated AsyncClient, with base_url and timeout set.
await local.sync(http)  # Verify identity before allowing offline edits.
local.enqueue(SharedPreferenceKey(kind="harness", name="default"), "pi-native")
snapshot = await local.sync(http)
```

`enqueue` commits to local disk before returning an operation ID. It makes no
network request. `pending()` reports unacknowledged edits; `cached()` returns
the last complete server snapshot, keeping unsaved edits distinct. All server
preference types, explicit `false`/`[]`, removals, and `resolves` IDs are supported.
The cache does not write YAML or mark import/adoption complete.

Each `sync` first checks the server identity and refreshes state, delivers at
most 100 queued operations by default, then refreshes again if it sent anything.
Each refresh follows revision-bound pages of at most 100 combined values/conflicts.
An empty outbox costs one GET for a small snapshot; larger snapshots need one GET
per page. One edit costs a complete read, one POST, then another complete read. Pass
`max_operations=0` to refresh without sending. A bounded batch may leave work
pending; the caller decides its next retry/reconnect schedule. There is no timer
or background worker in this slice.

Pages are held in memory until the traversal completes at one revision. A 409
revision change, identity mismatch, repeated cursor, duplicate entry, malformed
page, or network failure leaves the previous complete cache intact. A failed
initial read sends no queued edits; retry starts at the first page. The caller
owns retry/backoff. `page_size` can request smaller pages; a partial page never
becomes the authoritative cache. `read_preference_snapshot(http, identity=...)`
provides the same validated traversal without a disk cache; benchmarks reuse it.

## Persistence and recovery

The cache file is bound to deployment, user, workspace, and installation. A
different identity cannot reopen it. A changed authenticated user is detected
by the first GET before any queued edit is sent. Use a different file for each
binding; changing a URL does not change the binding.

Local transactions atomically update the outbox and acknowledged versions. A
separate process lock permits only one synchronizer per cache, while local
enqueue remains available during HTTP waits. No SQLite transaction spans a
network request. SQLite's normal durable commit settings are retained.

The device cache deliberately uses local SQLite, rather than opening the server's
application database: offline hosts cannot reach that database or carry its
credentials. Server stores and their Alembic lifecycle require application
bootstrap; using those tables for an outbox would also mix local, unsaved intent
with authoritative server state. A device-local transactional queue is needed to
commit edits before network access and survive process death. This separate
persistence path is proposed for maintainer design review in this PR; approval
has not been assumed.

Schema version 1 uses `PRAGMA user_version`. Unknown versions and earlier
unversioned prototype files fail visibly; preserve those files for recovery,
rather than deleting pending edits. Future migrations must preserve operation
IDs, bodies, ordering and binding atomically, with reopen/rollback tests.
Records are bounded binary JSON (at most 16,000 bytes each), UUID references are
16 bytes, and sequence numbers are supplied explicitly. Entries and conflicts
are individual records, not one duplicated snapshot blob. A newer receipt is
held separately on its entry until a complete refresh replaces it; this keeps
the last complete offline snapshot distinct from acknowledged versions.

New cache directories are private (0700), cache files are 0600, and read-only
transactions do not request a write lock. Caller-owned existing directories are
not chmodded. Synchronization holds its process lock until any disk worker has
finished, including cancellation. A busy sync/discard raises `filelock.Timeout`
immediately; SQLite contention raises `sqlite3.OperationalError` after five
seconds. Callers should retry without dropping pending edits.

Consecutive offline edits to one key form a chain. After an applied/unchanged
receipt, only its unattempted successor inherits that acknowledged version.
If the predecessor conflicts, its successor keeps the stale base and also
surfaces a conflict instead of silently overriding the remote choice. The
operation ID/body are fixed before the first send and survive uncertain delivery.

HTTP failures, cancellation, malformed receipts, and process termination leave
uncertain edits queued. A retry returns the server's durable receipt. Old
receipts cannot replace newer cached values; snapshots older than acknowledged
state are rejected. Corrupt local state fails visibly instead of resetting and
discarding pending work. A final refresh can fail after delivery: acknowledged
edits remain acknowledged, and the next sync refreshes authoritative state.

Permanent rejections, such as resolving a conflict that another machine already
resolved, remain visible as HTTP errors with the edit still queued. After reviewing
the error and `pending()`, the caller can explicitly `discard(operation_id)` for
the first queued edit and retry sync. This never undoes a server commit or changes
successors' expected versions; an uncertain discarded edit may already have
applied. Discard and sync share the process lock. No rejection is silently dropped.

At most 512 local pending operations are allowed. The server still enforces its
separate history/conflict/key limits. There is no automatic history compaction
or cache reset. Protocol v2 bounds server retry history; edits whose receipt has expired require
explicit review. Unattempted edits may refresh only their observed namespace
revision before first delivery; their reviewed key version and value stay fixed.
Attempted edits retain their exact body. Cache schema v4 stores the negotiated
protocol and upgrades v1–v3 caches without losing pending work.

## Verify

The [feature map](../feature-map/preferences.md) defines the `shared-client`
entry point and real-process recipe. To reproduce offline/restart recovery:

```sh
uv run --no-sync pytest tests/e2e/omnigent/test_shared_preferences_e2e.py --reruns 0 -q -rs
```

The scenario uses isolated client processes and two initial server replicas plus a replacement server; a lost reply
is injected after a real committed HTTP request. It proves the client/API/SQL
path, not UI, native plugin application, live vendor models, or managed startup.
