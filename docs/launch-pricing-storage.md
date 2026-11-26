# Launch pricing storage

Running sessions must retain the provider identity they actually launched with
when an agent bundle is republished. A provider name currently has no length
limit. Storing it directly in the 512-character session overrides document can
also prevent a later model switch from fitting.

The schema proposal adds nullable `launch_pricing_context_id` to
`omnigent_conversation_metadata`, using the existing `Uuid16` type. It adds no
pricing reader or writer. The canonical metadata schema declares the column as
deferred: metadata-based database initialization includes it, while ordinary
session reads leave it unloaded. The reference occupies 16 bytes, with
`BINARY(16)` on MySQL and the existing binary representation on other engines.
The complete provider name belongs in the existing artifact store, alongside
the harness needed to select the correct pricing family. This preserves names
without truncation or a new configuration limit. The application follow-up
must define and validate that artifact's versioned representation.

Read and write the reference by the existing `(workspace_id, id)` primary key.
There is no provider lookup or list query on this column, so no secondary
index or foreign key is needed. This migration performs no data backfill;
existing and older-writer rows retain `NULL` and their existing pricing fallback.
It leaves the AP conversation schema and `session_overrides` unchanged.

## Deployment and rollback

This is a standalone schema proposal. Database-owner approval and rollout
review are required before merge. Application changes ship afterward.

An already-running older server can keep using its known columns after this
addition. **An older build cannot restart against the new Alembic revision**:
the existing database initialization guard rejects revisions it does not know.
Deploy a build containing this migration before applying it, including every
replica or job that can reopen the database. Confirm that deployments that
apply migrations on first connection have completed this compatibility step.
This proposal does not change or bypass that guard.

Normal application rollback leaves the extra column in place and uses a build
that still knows this revision. A downgrade to an earlier build requires
stopping all database clients, using a migration-aware build to downgrade,
then restarting the earlier build. The downgrade removes the reference; it
does not restore launch pricing. After activation, any removal must follow
the database guide's staged deprecation and retention requirements. The
isolated downgrade test is evidence for pre-activation recovery only.

## Application follow-up

The schema alone does not fix session pricing. Before activating writes:

- Capture the provider identity from the actual launch decision, including
  runner and native launch paths; retain the matching harness. Republishing a
  bundle must not replace a live session's identity.
- Store nonsecret identity metadata through the workspace's artifact store,
  then publish its reference through the existing conversation store. Define
  ownership, runner-generation checks, resume/fork behavior and artifact
  retirement before enabling this path. Never store credentials in the record.
- Preserve existing behavior for legacy rows and older clients that report no
  launch identity. Pricing failures must remain observable without interrupting
  session accounting.
- Use this context for relay, native cumulative and Claude transcript budget
  estimates, including server-bound contexts. Keep detected-provider caching
  scoped to the workspace and invalidate it when configuration changes.

Validate the migration on an isolated database with
`pytest tests/db/test_migration_launch_pricing_context.py --reruns 0 -n 0`.
The checks render upgrade DDL for SQLite, PostgreSQL and MySQL. SQLite lifecycle
checks cover existing rows and overrides, older SQL writes, reference round trips
and pre-activation downgrade. A metadata-create/stamp/downgrade scenario also
covers the initialization boundary used by CockroachDB tests, including ordinary
ORM reads after downgrade. They do not replace live-engine deployment validation
of the revision guard or application acceptance.
