# Sandbox attachment schema rollout

`hosts.sandbox_attached` records an externally owned, reusable sandbox. It defaults
to false for existing rows and for inserts from older servers. This migration
alone enables no behavior and adds no table or index.

Deploy the schema first, then the attachment-aware application to **all** servers
and cleanup workers. Only then allow attachment requests or enable a provider's
`reusable_hosts` capability. Older workers ignore the ownership marker and must
never process static hosts.

Before rolling back the application, disable attachment requests and static
creation, then remove all static host registrations through the application's host-delete
API. This revokes Omnigent access without deleting the boxes. Drain outstanding
attachment/resume requests before checking that no attached rows remain. Then
roll back the application; the additive column can remain. Downgrading the schema
refuses to drop the column while attachments exist.

Verify migration defaults and rollback with:

```sh
uv run pytest tests/db/test_migration_sandbox_attached.py
```

The application prototype must separately verify that teardown, account deletion,
failed wakes, reaping and replacement respect this marker. Schema deployment does
not by itself protect attached sandboxes from old application code.

## Optional friendly names

The follow-up `no1a2b3c4d5e` migration adds nullable `hosts.display_name`.
`np1a2b3c4d5e` merges it with main's conversation host index migration,
preserving databases that already applied either branch. The subsequent
`nq1a2b3c4d5e` merge joins that head with main's scheduled-task dispatch migration
(`c8e4f2a6b901`), including databases that already applied `np1a2b3c4d5e`.
Existing rows and inserts by older application versions keep it null. There is
no backfill, new index, or change to the unique registration `name`. Ship this
migration with the schema prerequisite in the native PR stack, before deploying
the naming UI/API. Keep schema and application changes independently deployable.

An older application can continue reading/writing hosts after this migration;
it ignores labels and displays the registration name. Rolling back only the
naming application code may leave the column in place. Dropping the column
loses saved labels, so retain it for rollback unless that loss is intended.
The static-host ownership rollout restrictions above still apply: nullable
labels do not make old cleanup workers safe for new static hosts.
