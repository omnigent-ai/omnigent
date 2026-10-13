# Sandbox profile schema deployment

Migration `nn1a2b3c4d5e`, following `mm1a2b3c4d5e`, adds two tables to the
Omnigent metadata database. It changes no existing tables or rows.

| Table | Primary key | Purpose |
| --- | --- | --- |
| `sandbox_profiles` | `(workspace_id, id)` | Administrator-defined profile metadata, revision, enabled state, and JSON configuration. |
| `sandbox_profile_bindings` | `(workspace_id, session_id)` | Accepted profile snapshot and optional host association. |

The enabled-profile index supports workspace-scoped keyset pagination. The
host-binding index includes workspace, host, and session IDs for a bounded,
deterministic lookup. Session and host IDs use the existing binary UUID type.
Both JSON fields use binary storage; the application must reject serialized
values larger than 16,383 bytes before writing. Database BLOB types alone do not
enforce that application limit consistently across supported engines.

Deploy this additive migration before the profile catalog, selection, and launch
code. Older application versions ignore the new tables and can continue running
throughout the schema rollout. New code keeps accepted snapshots when profiles
are edited or disabled; sessions retain only the explicit profile overrides,
while omitted settings use current deployment defaults.

Roll back the application independently, leaving the tables and accepted
snapshots in place. Migration downgrade drops the new tables and their data; use
it only before the feature has stored profiles or after separately preserving
that data. No backfill or destructive production cleanup is required.

The schema PR contains this migration, the two ORM table declarations, this
deployment note, and `tests/db/test_migration_sandbox_profiles.py`. The profile
stores, APIs, provider integration, and UI ship in a separate application PR.
