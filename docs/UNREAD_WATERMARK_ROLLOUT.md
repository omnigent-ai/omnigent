# Unread watermark rollout

Unread indicators use one nullable column, `conversations.last_message_at`,
maintained only by visible-message writes. Reader activation is a deployment
decision, controlled by the server-only `unread_message_watermark` release
feature. There is no per-conversation freshness or backfill-progress column.

## Deploy in stages

1. **Schema:** deploy the schema-only release containing revision
   `mn1a2b3c4d5e`. It adds nullable integer `last_message_at` without a default,
   data scan, or backfill. Existing rows and old-style inserts remain null.
   Upgrade and downgrade can be re-entered safely.
2. **Writers, readers off:** deploy the application release everywhere with
   `unread_message_watermark` absent from `OMNIGENT_FEATURES`. New writers
   maintain the timestamp for visible messages, independent of the flag.
   Readers omit it from REST and send WebSocket null, retaining the
   `updated_at` fallback even when the stored timestamp is populated.
3. **Drain and backfill:** confirm every writer is running the new code and
   all old writer processes have stopped. Run the bounded maintenance command
   to completion for every workspace using the deployment's actual decoder.
4. **Readers on:** only after every workspace completes, add
   `unread_message_watermark` to the deployment's existing comma-separated
   `OMNIGENT_FEATURES` value and restart/redeploy the serving processes.
   Flags are captured at process construction; editing the environment alone
   does not change an already-running server.

The schema PR must merge before the application PR. While under review, the
application PR targets the schema branch so its diff excludes schema changes.
After the schema PR merges, retarget the application PR to `main`; do not
merge application changes into the schema-preparation branch.

The gate defaults off, including in single-user installations. Before activation,
metadata-driven unread indicators retain legacy behavior. After activation,
a null stored timestamp means no visible messages and is exposed as zero.
Session sorting continues to use `updated_at`.

This design relies on the deployment order: an old writer returning after
activation can leave a non-null timestamp stale, and there is no per-row marker
to detect it. Prevent that operationally. The temporary reader gate is scheduled
for review in release `0.18.0`; do not remove it before the compatibility window
has closed.

## Bounded backfill

Run `scripts/reconcile_message_watermarks.py` from the application release while
readers are off and old writers have drained. Set `UNREAD_STORAGE_LOCATION` to
the intended database URI. For the standard SQLAlchemy store:

```sh
uv run --no-sync python scripts/reconcile_message_watermarks.py \
  --storage-location "$UNREAD_STORAGE_LOCATION" \
  --workspace-id 0 \
  --item-batch-limit 1000 \
  --max-pages 100
```

Supply `--conversation-storage-location` when conversations use a separate
database. For encrypted or custom stores, supply
`--store-factory package.module:factory`; the factory receives the two storage
locations and must return the deployment's configured `ConversationStore`,
including its actual decoder. Do not use the plaintext decoder on encrypted data.

Each call seeks one conversation by primary key and decodes at most one bounded
message page through the existing type/position index. Progress and the running
maximum timestamp are returned in a job cursor, not stored on the conversation.
Partial scans do not publish a partial timestamp. A completed scan updates
`last_message_at` under the same row lock used by appends; new-writer appends
between pages are included as the scan advances. Reconciliation never advances
`updated_at`.

The command prints `{"complete": false, "next_cursor": {...}}` after each
committed call. Exit `2` means the page budget ended before completion; exit
`0` means the workspace completed. Pass the exact returned `next_cursor`
object back as `--cursor` to resume:

```sh
uv run --no-sync python scripts/reconcile_message_watermarks.py \
  --storage-location "$UNREAD_STORAGE_LOCATION" \
  --workspace-id 0 \
  --cursor "$UNREAD_RESUME_CURSOR" \
  --item-batch-limit 1000 \
  --max-pages 100
```

Keep the cursor intact and scoped to the same database/workspace. Without a
saved cursor, restart from the beginning; rescanning is safe. Malformed payloads
fail without publishing an incomplete result. The scan recomputes null and
non-null timestamps, so repeat it after any old-writer rollback and re-upgrade.

## Rollback

First remove `unread_message_watermark` from every serving process's feature
configuration and restart/redeploy all readers. Only then roll writers back.
Keep the additive column and use the schema-preparation release as the rollback
target; older-than-preparation binaries can reject the schema at startup.

To re-enable readers, upgrade all writers again, drain the old ones, and rerun
backfill from the beginning for every workspace. A previously completed cursor
does not prove timestamps stayed current during an old-writer rollback.

Prefer retaining the column. A schema downgrade is only safe after all
schema-aware binaries are stopped, including the preparation release whose ORM
selects the new column. Earlier development versions of this unmerged PR may
have left an unused extra column in preview databases; this update does not
drop that development data automatically.

## Verify

Run the migration suite against each supported isolated database:

```sh
uv run --no-sync pytest tests/db/test_migration_last_message_at.py -q
```

With readers off, REST must omit `last_message_at` and WebSocket rows must send
null even when writers have populated it. After backfill and reader activation,
read a chat, leave for Inbox, and rename it from another tab: no unread dot.
Add a visible reply while away: the dot appears and clears when reopened.
Explicit Mark as unread survives reload and clears after leaving and reopening.
