# Conditional session failure recovery

This schema prerequisite reserves two nullable columns on
`omnigent_conversation_metadata`. It does not change session status behavior or
resolve failure replay across polling cycles. Runtime consumers belong in a
separate application change under the [database guide](DATABASE_BEST_PRACTICES.md).

| Column | Purpose | Storage bound |
| --- | --- | --- |
| `live_status_version` | Application-generated UUID identifying the current status/error/binding generation | Exactly 16 bytes when present |
| `live_error_state` | One opaque snapshot containing current failure ownership and its displayed error | At most 16,383 bytes, including encoding overhead |

Both columns start as `NULL`; existing live status, runner binding, and error
labels are untouched. A snapshot requires a version. A non-null version with a
null snapshot means a compatible writer has explicitly cleared the current
error. A null version means ownership is unknown and recovery must be refused.
The existing primary key `(workspace_id, id)` serves every lookup; no secondary
index or duplicate status column is added. No runtime model or codec consumes
these columns in this prerequisite.

## Required application contract

The snapshot must keep the exact failure ID, native session ID, captured runner
binding, failure source, and the displayed error together. Source IDs remain
opaque strings; only the application-generated status version is a UUID. Do not
infer ownership by matching an error message or runner ID alone. The follow-up
must define a versioned storage envelope, with an explicit plaintext/encrypted
variant as required by the database guide, and reject unknown encodings. Enforce
the stored-byte bound before writing, including envelope/encryption overhead.

Use `run_write_transaction` with the metadata session factory. Lock the metadata
row before reading it (`BEGIN IMMEDIATE` on SQLite, `SELECT FOR UPDATE` elsewhere),
decode the snapshot in application code, and compare its exact owner with the
recovery proof. Do not filter or inspect the opaque snapshot in SQL. A successful
recovery changes `live_status`, `live_status_version`, and `live_error_state` in
the same transaction. Its conditional update must include the captured version
and current runner binding. A changed version or binding must leave all three
fields unchanged. Publish the committed result only if the local cache still
belongs to that generation.

Every status/error writer, runner rebind, and session rotation must replace the
version and ownership together. That includes failures with no structured
detail, unchanged `failed` statuses, best-effort queued writes, relay events,
disconnects, and retry/clear paths. A delayed write must carry a generation
precondition so it cannot restore an obsolete failure after recovery. Flush or
fence existing ordered worker writes before acknowledging a recovery. Repeated
recovery requests must be idempotent without treating an unrelated `idle` as
evidence of success.

Read the canonical error snapshot from the same metadata result as its status.
Legacy labels live in a separately transacted conversation database. They cannot
be part of this atomic update. Until all readers use the snapshot, any label
projection needs its own generation check and retry semantics. Do not clear
labels unconditionally before or after the metadata transaction.

The runner must durably retain the failed hook identity, native parent identity,
and a later parent submit until that parent's authoritative completion can name
the superseded failure. A partial record, subagent event, session rotation, or
unknown source is insufficient. Advance the recovery cursor only after a durable
acknowledgment; restart and file rewind must preserve causality.

Recovery changes current display state only. Historical error items, diagnostic
observations, and completed failed scheduled-run outcomes remain intact. An idle
display recovery must not complete a different active scheduled run.

## Deployment and rollback

1. Deploy this additive schema revision with recovery disabled. Existing rows
   need no backfill. On SQLite the added checks rebuild the metadata table; allow
   for its write lock and temporary disk space. Other databases perform their
   ordinary column/constraint DDL.
2. Deploy compatible writers, readers, and durable runner proof with recovery
   still disabled. Include every replica, job, and old queued write. Merely
   checking that a recovery request names a current runner is insufficient.
3. Enable recovery only after legacy writers and their in-flight work have
   drained and the acceptance tests below pass. Mixed-version writes can retain
   stale ownership, because old binaries know only `live_status` and labels.
4. Disable recovery before rolling application consumers back. Retain the
   additive columns and a binary that recognizes the schema revision. Once any
   version has been written, downgrade refuses to erase the state. Before any
   consumer runs, stop database writers before downgrading. The downgrade checks
   unused storage in primary-key batches and can remove the columns without
   modifying existing conversation data.

The migration resumes after partially committed column/constraint additions.
Changing a populated schema later requires a separate reviewed migration and
the database guide's deprecation/retention process.

## Acceptance tests for the follow-up

Use synthetic hook records with one failure, a later parent submit, and a
successful stop. Compare all records in one poll with a stop split over two
polls, and repeat with a forwarder/server restart. The later successful turn
should display idle and its earlier error should remain in history.

Also cover newer failures (including identical error text), replica handoff,
runner/native-session replacement, subagents, unknown ownership, legacy peers,
unknown snapshot encodings, storage failures, retry acknowledgments, and delayed
status/label writes against separate physical metadata and conversation
databases. Test that recovery cannot alter another scheduled run's outcome.
