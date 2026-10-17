# Session observation schema rollout

The additive `d42f66e0a5b1` and `e53a77f1b6c2` migrations prepare metadata for
binding epochs, ordered live observations, offline decisions and independent
terminal-startup claims. They do not introduce application writers or change
existing session status. Legacy rows retain their runner, status and history;
epoch/revision default to zero, startup pending defaults to false and optional
payloads remain null.

Deploy this schema release before the separate ownership application release.
It includes matching ORM declarations because fresh CockroachDB bootstrap
creates ORM metadata and stamps the current migration head. Existing store
responses ignore the added fields and keep their existing behavior.

The new metadata types enforce at most 16,383 final framed bytes for binary
payloads and 64 characters for observation/error identifiers on typed writes.
Existing compression columns retain their behavior. Lifecycle writers and
ownership-aware response behavior remain in the separate application release.
No backfill or data conversion is performed by these migrations.

Roll back the application first while retaining the additive schema. The old
application remains compatible with the additional columns. Keep the columns
through the mixed-version window; dropping them before all new writers stop
would break the new application. Before downgrading, return every process to binaries predating the new ORM
mappings; disabling writers alone is insufficient because the newer readers
select mapped columns. Downgrades remove only the new ownership metadata.
Existing session rows, runner bindings, status and history remain intact.

The migration tests cover upgrade, downgrade and upgrade again for default and
non-default workspaces, preserving existing rows and already-owned state. The
existing historical migration tests remain scoped to their original revision
before they exercise the current ORM schema.
