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


## Retry and reconciliation contract

For `sys_scheduled_task_create` and `sys_scheduled_task_update`, choose a UUID
`request_id` before the first call. Keep that ID and the same inputs for retries.
If omitted, the native relay or runner allocates one and includes it in the
result. Native relay allocation happens before approval/dispatch and before its
HTTP transport, so timeout responses retain the submitted ID.

After an uncertain result, call `sys_scheduled_task_list` with only that
`request_id`. It returns `applied` with the task ID when a receipt exists, or
`unknown` when no committed receipt is visible. A missing receipt cannot rule
out pending approval or a request still in flight. An `applied` receipt records
historical completion, not the task's current settings or whether it still exists.

REST callers send `Idempotency-Key: <UUID>` with POST `/v1/scheduled-tasks` or
PATCH `/v1/scheduled-tasks/<task-id>`. GET
`/v1/scheduled-task-requests/<request-id>` reconciles the current owner's request
in the current workspace. Reusing a key for different inputs or another target
returns 409 without applying the new mutation. Replays return `replayed: true`
and a warning; task fields reflect current state, preserving subsequent edits.
Retrying a create after deletion returns the receipt with `deleted: true` and
does not recreate the task.

Runner and relay failures contain a non-empty error and request identity.
Pre-dispatch refusals and server 4xx responses mean the current attempt was
`not_applied`; transport loss and server 5xx responses are `unknown`. The relay
requests cancellation on its 300-second dispatch timeout, including a pending
approval wait. Cancellation does not undo committed writes. Interactive approval
requirements remain in force, including on a retry.

New runners refuse mutations against servers that do not advertise
`scheduled_task_request_ids`. Upgrade the schema and server before the runners.
Existing REST callers remain compatible, but calls without the header have no
retry guarantee. In particular, the web create/update UI does not yet supply
these IDs. Similar requests with fresh IDs are separate intents: content-based
duplicate detection is not implemented. The receipt warning covers same-ID
replays only. The historical hours-long delay has not been reproduced or
attributed by these bounded tests.

## Verify locally

Run the focused tests from this checkout; all use isolated databases or loopback
transports, and none needs a live task or sends email:

```sh
uv run --no-sync pytest tests/server/integration/test_scheduled_tasks_routes.py -k 'request or receipt or lost_mutation or replay' --reruns 0 -q
uv run --no-sync pytest tests/stores/test_scheduled_task_store.py -k 'receipt or request_identity' --reruns 0 -q
uv run --no-sync pytest tests/runtime/policies/test_scheduled_task_approval_gate.py --reruns 0 -q
uv run --no-sync pytest tests/harnesses/claude_native/test_claude_native_bridge.py -k 'call_relay_tool_returns_mcp_error_on_read_timeout' --reruns 0 -q
```

For a human check, use an isolated local server and an interactive agent session.
Ask it to create a harmless daily task with an explicit UUID and approve the
request. Ask it to repeat the exact call with that UUID: there should be one
task, and the result should show `replayed: true`. Ask it to look up that UUID
through `sys_scheduled_task_list`: expect `applied` and the same task ID. Change
the prompt but reuse the UUID: expect a conflict and unchanged task settings.
Delete the disposable task when finished. A different, unused UUID should return
`unknown` from status lookup. Do not use a production automation for this check.

Feature-map scope: this is a nonvisual tool/API change. Scheduled-task CRUD is
currently listed as unmapped in `feature-map/README.md`. The native-harness and
tool-call maps identify approval and raw-result surfaces; their layouts are
unchanged. Backend and shared relay boundaries are covered here; vendor CLI,
browser/Inbox presentation, mobile, and Managed deployment are not claimed as
verified by these component tests.
