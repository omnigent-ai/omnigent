# Automations (scheduled tasks)

An automation is a saved instruction that fires an agent session on a recurring schedule. Each firing creates a real session, bound to an agent, that runs a stored prompt -- so "triage new issues every weekday at 9" or "summarize yesterday's alerts each morning" runs without anyone at the keyboard.

"Automations" is the user-facing name in the web UI. Everywhere else the feature is called a **scheduled task**: the REST paths (`/v1/scheduled-tasks`), the agent tools (`sys_scheduled_task_*`), the database tables (`scheduled_tasks`, `scheduled_task_runs`), and the code. This guide uses whichever name matches the surface being described.

## Creating an automation

There are three surfaces, all writing the same rows.

**Web UI.** Open the `/tasks` page (sidebar: *Automations*, or the command palette: *Go to Automations*). The create dialog takes the schedule, the agent, the prompt, and optionally a model and reasoning effort. Each row shows a live relative next-run time and the status of the last run.

**REST API.** `POST /v1/scheduled-tasks`. Required: `name`, `prompt`, `rrule`, `agent_id`. Optional: `timezone` (defaults to `UTC`), `model_override`, `reasoning_effort`, `permission_mode`, `max_cost_usd`, `workspace`, `host_id`. Unknown fields are rejected. The two optional targeting fields are not independent: sending a `workspace` without a `host_id` is a 400 (`host_id required when workspace is set`), because only a host can resolve a path. Sending neither is fine -- the fire resolves both.

```jsonc
{
  "name": "nightly triage",
  "prompt": "Triage issues opened since yesterday and post a summary.",
  "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
  "agent_id": "ag_...",
  "timezone": "Asia/Tokyo",
  "permission_mode": "acceptEdits",
  "max_cost_usd": 2.00
}
```

`permission_mode` is available only for Claude Code (`claude-native`) agents. It accepts `default`, `auto`, `acceptEdits`, `plan`, `dontAsk`, or `bypassPermissions`; omitting it uses the agent default. Automations are unattended, so prompting modes such as `default` and `plan` can leave a run waiting for approval. Choose an auto-running mode only when its permission level is appropriate for the task.

`max_cost_usd` is a positive per-firing budget. Each fired session gets its own cap, and model calls are blocked after that session reaches it. Omit the field for no cap.

**Agent tools.** An agent can manage automations itself with `sys_scheduled_task_create`, `sys_scheduled_task_list`, `sys_scheduled_task_update`, and `sys_scheduled_task_delete` -- so an agent can schedule its own follow-up work. The create and update tools expose the same `permission_mode` and `max_cost_usd` controls.

## Schedules are RRULEs, not cron

A schedule is an [RFC 5545](https://datatracker.ietf.org/doc/html/rfc5545) recurrence rule evaluated in the task's IANA timezone.

| Intent | `rrule` |
| --- | --- |
| Every hour | `FREQ=HOURLY` |
| Daily at 09:00 local | `FREQ=DAILY;BYHOUR=9;BYMINUTE=0` |
| Weekdays at 09:00 local | `FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0` |

The rule is anchored at midnight of the local day, which gives occurrences a deterministic wall-clock phase: an hourly rule fires on the hour, a daily rule at its `BYHOUR`/`BYMINUTE`.

Three rules are rejected at create and update time with a 400:

- **Faster than hourly.** One hour is the tightest allowed cadence, checked across every consecutive pair of fires (an irregular rule cannot hide a tight pair mid-window). Every firing spawns a real session, so the floor is a cost bound.
- **Never fires.**
- **Fires only once.** A schedule that cannot recur is not a schedule.

Two timing caveats are worth knowing:

- **DST.** A wall-clock time that does not exist on a spring-forward day maps to whichever instant the timezone database resolves it to, and a time that occurs twice on a fall-back day picks the earlier one. Either way a schedule slips by at most an hour across a DST edge.
- **`INTERVAL>1` rules** (biweekly, interval-monthly) tie their phase to the day the timer last re-armed, because the anchor is midnight of the query day rather than a stored start date. A server restart on a different weekday can slip such a rule by one period. `INTERVAL=1` rules are unaffected.

`next_run_at` in an API response is computed by the live scheduler and is authoritative. Do not recompute it client-side; a client cannot reproduce the server's anchor for `INTERVAL>1` rules.

## What happens when a task fires

1. **The row is re-read.** The armed timer is never trusted. A task deleted or paused between arming and firing is a no-op.
2. **The launch target is resolved.** A task with no pinned `host_id` uses the owner's most-recently-active live host, chosen at fire time. A task with no pinned `workspace` starts the runner in that host's home directory, which is what makes chat-only, research, and MCP-only automations possible. A pinned host that is missing or offline -- or an owner with no live host at all -- records a **failed** run rather than a running one (`error_code` `host_offline`, `host_not_found`, or `no_online_host`).
3. **A session is created**, bound to the task's agent and carrying the resolved workspace and host plus any `model_override`, `reasoning_effort`, and supported `permission_mode`. If `max_cost_usd` is set, a per-session cost-budget policy is attached before the prompt is dispatched.
4. **Ownership is granted.** The new session gets a `LEVEL_OWNER` grant for the task's owner (a reserved local user in single-user and OSS deployments). Without the grant the run would be invisible.
5. **The runner launches and the prompt is dispatched**, so the agent actually works. A seeded prompt with no launched runner would just sit in history.
6. **The run is recorded** in `scheduled_task_runs`, and `last_run_at` and `last_run_conversation_id` are stamped on the task.

Firing is fire-and-forget: the guard steps run synchronously so a dead fire costs nothing, then session creation and launch move to a background task and the scheduler re-arms immediately. A failure in that background work is logged and never crashes the scheduler.

## Run history

`GET /v1/scheduled-tasks/{id}/runs` returns the history. Each run carries `status`, `scheduled_at`, `fired_at`, `finished_at`, `conversation_id`, and `error_code`. The free-text `error` blob is deliberately not returned; `error_code` is the queryable classification.

| `status` | Meaning |
| --- | --- |
| `running` | Dispatched; the turn is in flight. |
| `succeeded` | The dispatched turn finished. |
| `failed` | The firing or the turn failed; see `error_code`. |
| `skipped` | The firing was deliberately not attempted -- for example an unsupported `execution_target`. |

`ScheduledTaskRun` also declares a `scheduled` status, but no code path writes it: a run row is created at dispatch, so `running` is the earliest state a client can observe.

## Lifecycle

A task is `active`, `paused`, or `deleted`. `PATCH /v1/scheduled-tasks/{id}` changes any stored field and can move a task between `active` and `paused`; it cannot set `deleted` (use `DELETE`). Omitting a field leaves it unchanged, with one exception: moving a task to a different `agent_id` clears `model_override`, `reasoning_effort`, and `permission_mode` unless the same request resends them, because a harness switch invalidates the settings stored beside the agent -- so re-pin an automation's execution controls in the request that switches its agent, or it fires the new agent on defaults. `max_cost_usd` is not agent-bound and survives the switch. Sending `permission_mode: null` clears the permission override, and `max_cost_usd: null` clears the cost cap. An explicit `null` for `workspace` or `host_id` is rejected -- the rejection is on the presence of the key, so it applies even to a task where that field is currently unset; there is no way to clear either one.

`POST /v1/scheduled-tasks/{id}/run` fires a task immediately and returns 202. It does not disturb the recurring schedule, which makes it the way to test a new automation without waiting for its next occurrence.

## API reference

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/v1/scheduled-tasks` | Create a task. |
| `GET` | `/v1/scheduled-tasks` | List tasks. |
| `GET` | `/v1/scheduled-tasks/{id}` | Read one task. |
| `PATCH` | `/v1/scheduled-tasks/{id}` | Update fields, or pause/resume. |
| `DELETE` | `/v1/scheduled-tasks/{id}` | Delete a task. |
| `POST` | `/v1/scheduled-tasks/{id}/run` | Run now (202). |
| `GET` | `/v1/scheduled-tasks/{id}/runs` | Run history. |

A task response carries `id`, `name`, `prompt`, `rrule`, `owner_user_id`, `agent_id`, `timezone`, `created_at`, `model_override`, `reasoning_effort`, `permission_mode`, `max_cost_usd`, `workspace`, `host_id`, `state`, `last_run_at`, `last_run_status`, `last_run_conversation_id`, `next_run_at`, and `updated_at`.

## Current limits

The scheduler is the timing engine only, and its behavior is deliberately simple:

- **Missed fires are not replayed.** On startup the scheduler arms the next future occurrence of every active task; occurrences that passed while the server was down are gone. There is no backfill or replay.
- **No automatic retry.** A failed run is recorded and visible in history; the retry policy is that the next occurrence fires normally.
- **Overlapping fires are skipped.** If the previous firing of the same task is still being dispatched when the next tick arrives, the tick is dropped. No run row is recorded for it, so a dropped tick leaves no trace in history beyond the server log.
- **Late ticks are skipped.** A tick arriving more than 30 seconds after its scheduled time (a blocked event loop, for instance) is skipped rather than fired late.
- **One scheduler per server process.** Timers are in-process with no distributed leasing, so a multi-replica deployment would fire each task once per replica. Shared session-create orchestration is the intended path for that.
- **Connected-host runs only.** Firing targets a connected host with an existing workspace. Managed sandboxes and git branch selection at fire time are not available: neither `base_branch` nor `execution_target` can be set through the API (both are rejected as unknown fields), and a row whose `execution_target` is anything other than `connected_host` records a `skipped` run with `error_code` `unsupported_target` rather than firing.

## Related

- [Agent YAML spec](AGENT_YAML_SPEC.md) -- defining the agent an automation binds to.
- [Policies](POLICIES.md) -- gates and spend caps that apply to sessions an automation creates.
