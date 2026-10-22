# Persistent task dispatch

`PersistentTaskScheduler` finds due tasks in the database, claims each occurrence
through the store, and supervises the executor while it prepares the run.

1. Initialize a future `next_run_at` for active tasks that have none.
2. Find due tasks and check whether this server owns the occurrence's host.
3. Atomically create a `starting` run and advance the task to its next future time.
4. Call `on_fire(task, lease)` and renew the startup lease while it works.
5. Stop renewal when the executor records `running`, `failed`, or `skipped`.

The executor checks its lease before external actions, attaches the session before
sending the prompt, and records `running` after delivery. Lease loss stops local
startup work. Interrupted runs remain recorded for recovery; the scheduler never
resends a prompt automatically. After an outage it dispatches one overdue
occurrence per task, then advances into the future instead of replaying every
missed occurrence.

Defaults are 30-second polls and renewals, 120-second leases, 100 rows per page,
and 100 concurrent startups per server. `ScheduledTaskSchedulerConfig` validates
these settings; renewal must occur more frequently than lease expiry.

This component is not wired into server startup. Recovery and the application
integration must be supplied before enabling persistent scheduling. The existing
server continues to use its in-memory scheduler.

Start reviewing at `PersistentTaskScheduler.poll_once()` in
[dispatch_scheduler.py](dispatch_scheduler.py), then follow `_claim_due()` and
`_execute()`. `StartupLease` handles the executor's database checkpoints.

Run dispatch and configuration tests with:

```sh
.venv/bin/pytest tests/server/scheduled/test_dispatch_scheduler.py \
  tests/server/scheduled/test_config.py -q --reruns 0
```

Tests use the real store with controlled time and fake execution callbacks;
they do not launch real runners or sandboxes.
