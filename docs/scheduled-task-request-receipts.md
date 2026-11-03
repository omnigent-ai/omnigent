# Scheduled-task request receipts rollout

Deploy migration `b4e2a8d06f19` before enabling the application that accepts
scheduled-task request IDs. It adds an independent table; existing task rows,
writers, schedulers and run statuses are unchanged. The application/ORM change
ships separately. Existing binaries can run against the expanded schema.

The primary key is `(workspace_id, owner_key, request_id)`. `owner_key` is a
binary UUID derived from the authenticated owner and account generation. A
32-byte digest binds the request ID to its method, target and normalized input.
The task ID and creation timestamp are the only other values; no prompts,
credentials or response bodies are stored. Every value has a fixed bound.

The application must insert the receipt in the same transaction as the task
mutation. Receipt existence proves that mutation committed. Absence means the
outcome is unknown: a request may still be waiting for approval or processing.
Every lookup is an exact primary-key lookup; no list or scan API is planned.
Receipts are retained after task deletion so a retry cannot recreate a deleted
task. IDs are never recycled or expired automatically. A different request ID
is a different operation and is not deduplicated by this contract.

Rollback the application first and retain the table. Migration downgrade is
allowed only while the table is empty; it refuses to discard used receipts.
The table does not add foreign keys, indexes, database defaults or new task/run
states. No existing data needs a backfill.
