# Comment anchor schema rollout

Revision `f9b3e6c2a401`, following `c7cd88f24c1c`, adds nullable
`comments.anchor_metadata`. It stores compressed provider-specific coordinates
with a database-enforced 16,383-byte stored limit. Existing rows remain NULL. There is no default,
backfill, index, or application consumer in this change.

The table metadata also includes this column and its CHECK without an ORM
property. Fresh CockroachDB databases bootstrap from table metadata and stamp
the current migration head; omitting it would silently create an incomplete
schema. Ordinary ORM comment queries still omit the column in this release.

## Deployment order

1. Merge and release this schema-only change first. Run the normal database
   migration deployment (`omnigent debug db-upgrade <database-uri>`) and verify
   the database reached `f9b3e6c2a401`. Database startup also runs this chain.
2. Deploy the local PR-review feature only after all databases it uses have
   the column. That release adds the ORM mapping, API capability, and readers
   and writers. Its client requires explicit server support before writing.

Existing application readers and writers ignore the column and may continue
using the old fields. In particular, inserting an ordinary comment without
anchor metadata remains valid.

## Rollback

Roll the feature application back to the schema-only release and leave the
column and stored anchors in place. That release knows the migration revision
but does not consume the new field. A binary predating this schema release may
reject a database whose revision it does not recognize, so it is not the
restart/rollback baseline after migration.

The migration's downgrade removes the column and all stored anchors; it is
tested for development recovery, not the normal production rollback. Do not
downgrade a database while feature readers or writers are deployed. Any later
production removal requires the staged deprecation procedure in
[Database Best Practices](DATABASE_BEST_PRACTICES.md#deployment-practices).

## Verification

`uv run --no-sync pytest tests/db/test_migration_comment_anchor_metadata.py
tests/db/test_migration_connections.py` verifies the migration chain has one
head, preserves existing comments, accepts old writers, round-trips compressed
anchors, rejects raw and reflected writes of 16,384 bytes, and preserves comment
bodies on downgrade. SQLite/D1 use a byte-length CHECK in ADD COLUMN; PostgreSQL,
CockroachDB, and MySQL use an octet-length CHECK. The lifecycle runs on SQLite;
DDL tests cover all five dialect names. Deployment CI exercises the migration on
the other configured database engines.
