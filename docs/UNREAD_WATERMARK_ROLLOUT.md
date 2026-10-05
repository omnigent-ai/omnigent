# Unread watermark rollout

Unread indicators distinguish visible messages from metadata updates using
`conversations.last_message_at`. Deploy its schema separately from the
application behavior, and retain the additive columns when rolling back.

## Schema preparation

Deploy the schema-only release containing revision `mn1a2b3c4d5e` first. It adds
two nullable integer columns, with no data scan, backfill, or reader activation:

- `last_message_at`: the latest visible-message timestamp in the observed prefix.
- `last_message_observed_position`: the exclusive item-position boundary covered
  by that timestamp. A null boundary means the prefix is unknown.

Existing rows and rows created by old writers keep null markers. Already-running
old binaries can continue using the existing columns. The preparation release
declares the new ORM fields but otherwise retains the old read/write behavior.
It is the supported application rollback target after the schema is installed;
older-than-preparation binaries can reject the newer schema at startup.

The schema PR must merge before its dependent application PR. While under review,
the application PR can target the schema branch to keep its diff application-only.
Retarget it to `main` after the schema PR merges; do not merge the application
into the schema-preparation branch.

## Application rollout

Deploy the application release only after schema preparation succeeds. New empty
sessions start with both the observed boundary and `next_position` at zero. New
writers advance the boundary with the item allocator, but advance the timestamp
only for visible, non-meta messages.

A reader trusts the timestamp only when the observed boundary is non-null and
equals the non-null `next_position`. A fresh empty session is exposed as timestamp
zero. An unknown or stale row omits the REST field or sends WebSocket null, so
clients use the legacy `updated_at` fallback. Full updates from old servers also
clear any newer cached watermark. Sorting continues to use `updated_at`.

An old writer advances `next_position` without advancing the observed boundary.
This invalidates even a previously repaired timestamp. New appends to an unknown
prefix leave it untrusted until reconciliation observes the missing history.
There is no global readiness flag that can mask a late old-writer append.

During compatibility mode, unreconciled sessions can still show metadata-driven
unread indicators. The fallback deliberately preserves detection of real activity
instead of treating an unknown timestamp as an authoritative empty session.

## Bounded reconciliation

After all old writers have drained, run the application release's
`scripts/reconcile_message_watermarks.py` once per workspace. Supply the same
storage locations and decoder configuration as the deployment. The script is
introduced by the application PR, not by the schema-preparation release.

For the standard SQLAlchemy store, set `UNREAD_STORAGE_LOCATION` to the intended
database URI and start with conservative bounds:

```sh
uv run --no-sync python scripts/reconcile_message_watermarks.py \
  --storage-location "$UNREAD_STORAGE_LOCATION" \
  --workspace-id 0 \
  --conversation-batch-limit 100 \
  --item-batch-limit 1000 \
  --max-pages 100
```

If conversation data uses a separate database, supply
`--conversation-storage-location`. For encrypted or custom stores, supply
`--store-factory package.module:factory`; the factory receives the storage and
conversation-storage locations and must return the deployment's configured
`ConversationStore`, including its actual decoder. Do not run the default
plaintext decoder against encrypted data.

Each invocation of the store operation inspects a bounded primary-key page and
repairs at most one conversation's bounded message page in its own transaction.
Message reads use the existing workspace/conversation/type/position index. The
store decodes payloads in application code; the database never inspects JSON or
ciphertext. Row locking and a compare-and-set on the old allocator and boundary
protect concurrent appends. Null and stale watermarks are both repaired.

The command prints JSON progress after each committed call:

```json
{"complete":false,"next_after":[0,"conversation-id"]}
```

Exit status `2` means `--max-pages` stopped an unfinished run; `0` means the
workspace scan completed. Resume using both `--after-workspace-id` and
`--after-conversation-id` from the last returned `next_after`. When it is null,
omit both options. The cursor advances only past completed conversations; a
partial message page persists its own prefix boundary and resumes the same
conversation. Restarting from the beginning is also safe and skips fresh rows.
A decode error rolls back that page without advertising its prefix as complete.

Run a final pass from the beginning after old writers are definitely gone. A
writer can invalidate a row already passed by an earlier scan, including a scan
run during deployment. Later old writes remain detectable and use the fallback;
repeat reconciliation after any application rollback and re-upgrade.

## Rollback and verification

Roll back the application to the schema-preparation release while keeping both
columns. Its legacy behavior does not depend on either watermark. Re-upgrading
the application and rerunning reconciliation restores authoritative prefixes.

Prefer retaining these additive columns. Use the migration's schema downgrade
only in a controlled rollback after **all schema-aware binaries are stopped**,
including the preparation release whose ORM selects the new fields. Start the
older schema-compatible release only after downgrade completes. Do not drop the
columns beneath a running preparation or application release.

Migration tests cover upgrade, nullable existing rows across workspaces,
idempotent re-entry, and downgrade row preservation using the shared database
fixture. Run them for each supported engine:

```sh
uv run --no-sync pytest tests/db/test_migration_last_message_at.py -q
```

For a deployed application smoke check, read a session, leave for Inbox, and
rename it from another tab: it should remain read once reconciled. Add a visible
reply while away: it should become unread, then clear when reopened. An explicit
Mark as unread must survive reload and clear only after leaving and reopening.
