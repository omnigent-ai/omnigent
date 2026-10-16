# Sandbox attachment schema rollout

`hosts.sandbox_attached` records an externally owned, reusable sandbox. It defaults
to false for existing rows and for inserts from older servers. This migration
alone enables no behavior and adds no table or index.

Deploy the schema first, then the attachment-aware application to **all** servers
and cleanup workers. Only then allow attachment requests. Older workers ignore
the ownership marker and must never process attached hosts.

Before rolling back the application, disable attachment requests and remove all
attached host registrations through the attachment-aware application's host-delete
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
