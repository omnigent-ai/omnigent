# Persistent task dispatch

`PersistentTaskScheduler` finds due tasks in the database, claims each occurrence
through the store, and supervises the executor while it prepares the run.

1. Initialize a future `next_run_at` for active tasks that have none.
2. Find due tasks and check whether this server owns the occurrence's host.
3. Atomically create a `starting` run and advance the task to its next future time.
4. Call `on_fire(task, lease)` and renew the startup lease while it works.
5. Stop renewal when the executor records `running`, `failed`, or `skipped`.

```mermaid
flowchart TD
    A["Poll for work"] --> B["Initialize missing next_run_at values"]
    B --> C["Find due tasks"]
    C --> D["Check host ownership and startup capacity"]
    D --> E["Claim occurrence: create starting run<br/>and advance next_run_at atomically"]
    E --> F["Call executor: on_fire(task, lease)"]
    E --> G["Renew lease while startup is in progress"]
    F --> H["Executor records running, failed, or skipped"]
    H --> I["Stop lease renewal"]
    G --> J["If lease is lost: cancel local startup"]
    J --> K["Keep unfinished run for recovery"]
```

`poll_once()` initializes missing schedules and finds due tasks. `_claim_due()`
uses the store's atomic claim to decide which replica starts each occurrence.
`_execute()` runs the executor callback alongside the heartbeat. The executor
uses `StartupLease` to check its lease, attach the session, and record the startup
outcome; the lease covers startup, not the entire agent turn.

The executor checks its lease before external actions, attaches the session before
sending the prompt, and records `running` after delivery. Lease loss stops local
startup work. Interrupted runs remain recorded for recovery; the scheduler never
resends a prompt automatically. After an outage it dispatches one overdue
occurrence per task, then advances into the future instead of replaying every
missed occurrence.

Defaults are 30-second polls and renewals, 120-second leases, 100 rows per page,
and 100 concurrent startups per server. `ScheduledTaskSchedulerConfig` validates
these settings; renewal must occur more frequently than lease expiry.
