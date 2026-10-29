# Automations (scheduled tasks)

An automation is a saved instruction that fires an agent session on a recurring schedule. Each firing creates a real session, bound to an agent, that runs a stored prompt -- so "triage new issues every weekday at 9" or "summarize yesterday's alerts each morning" runs without anyone at the keyboard.

"Automations" is the user-facing name in the web UI. Everywhere else the feature is called a **scheduled task**: the REST paths (`/v1/scheduled-tasks`), the agent tools (`sys_scheduled_task_*`), the database tables (`scheduled_tasks`, `scheduled_task_runs`), and the code. This guide uses whichever name matches the surface being described.

## Creating an automation

There are three surfaces, all writing the same rows.

**Web UI.** Open the `/tasks` page (sidebar: *Automations*, or the command palette: *Go to Automations*). The create dialog takes the schedule, the agent, the prompt, and optionally a model, a reasoning effort, and a project to file each run's session into (pick one or type a new name to create it). When the server supports managed sandboxes, the host picker also offers a new sandbox for each run. Each row shows the schedule and, when armed, a live relative next-run time. The API exposes the last run's status as `last_run_status`; the row does not display it.

**REST API.** `POST /v1/scheduled-tasks`. Required: `name`, `prompt`, `rrule`, `agent_id`. Optional: `timezone` (defaults to `UTC`), `model_override`, `reasoning_effort`, `permission_mode`, `max_cost_usd`, `session_mode` (`new` or `continue`, defaults to `new`), `session_id`, `workspace`, `host_id`, `execution_target` (defaults to `connected_host`), and `project_id`. Unknown fields are rejected. For `connected_host`, the two optional targeting fields are not independent: sending a `workspace` without a `host_id` is a 400 (`host_id required when workspace is set`), because only a host can resolve a path. Sending neither is fine -- the fire resolves both. Existing managed sandbox hosts cannot be pinned or automatically selected for connected-host runs.

To file every fired session into a project, send `project_id`, one of your projects; another user's is a 404. To use a project by name, look it up or create it with `GET` / `POST /v1/projects` first, as the automation dialog does. Omit `project_id` to leave fired sessions unfiled.

To provision a fresh sandbox per firing, set `execution_target: "managed_sandbox"` and omit `host_id` and `workspace`. This requires managed sandboxes to be configured on the server; otherwise creation is rejected. Each sandbox uses the server's normal runner-idle and provider keepalive settings.

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

`max_cost_usd` is a positive per-firing budget. Each fired session gets its own cost-budget policy, which blocks subsequent requests and tool calls once recorded spend reaches the cap. It does not interrupt an in-progress model turn: spend can overshoot before the next check, and denying a native tool call does not terminate the turn. Omit the field to attach no automation-specific cap; other applicable policies still apply.

**Continuing one session.** By default every firing starts a new session (`session_mode: "new"`). With `session_mode: "continue"`, every firing sends its prompt into one session, recorded as the task's `session_id`, so the agent keeps the context of earlier runs. Leave `session_id` unset and the first firing creates the session as usual; or set it to an existing top-level session you own, which must run the task's `agent_id` and match any pinned `host_id` and `workspace`. A session can't move, so a connected-host task without a pinned host is pinned to the session's host, whether you pick the session or the first firing creates it. A picked session keeps its own agent, so the task takes no copy of it. A `managed_sandbox` task keeps one sandbox host of its own for every session it runs, so it cannot pick a session in another sandbox. After the session is reset, the next session starts on that same sandbox, in the earlier session's workspace, or on a new sandbox under the same host id if the earlier session is unknown. In the dialog, pick **Continue one session** under **Session**; the field appears when the server advertises `scheduled_task_session_modes` in `/v1/info`. A continued session keeps the model and permission mode it was created with. `max_cost_usd` stays per firing: each firing caps the session at its spend so far plus the budget. Each automation only changes its own cap: clearing `max_cost_usd` removes only the cap this automation set, and another automation's cap on the same session stays in force for every run in it. A firing sends its prompt even while the session is busy with an earlier turn, the way a message you send mid-turn goes in, and its run is `succeeded` once the prompt is delivered, without waiting for the turn. If firings can overlap, write the prompt so each firing's work happens in a sub-agent. A firing never silently starts a replacement session:

| Situation | Run | `error_code` |
| --- | --- | --- |
| Another first run of the automation recorded its session first; this run's unused session is deleted | `skipped` | `continued_session_busy` |
| The session was deleted | `failed` | `continued_session_missing` |
| The session is archived | `failed` | `continued_session_archived` |
| The owner no longer owns the session | `failed` | `continued_session_forbidden` |
| The automation has a budget, and the session already has a cost cap it did not set, such as another automation's or one from before this release | `failed` | `continued_session_budget_conflict` |
| The automation's cost cap could not be set on the session; any cap it already had stays | `failed` | `continued_session_budget_failed` |
| A first run could not record its new session on the automation | `failed` | `session_record_failed` |
| The session runs a different agent, on a different host or workspace than the task pins, in a sandbox when the task targets a connected host (or the reverse), or, for a `managed_sandbox` task, outside the sandbox the task started | `failed` | `continued_session_incompatible` |

Send `session_id: null` in a `PATCH` (**Start a new session on the next run** in the edit dialog) so the next firing creates a new session that later firings continue. A `PATCH` that changes `agent_id`, `host_id`, `workspace`, or `execution_target` without sending `session_id` does the same, since the old session cannot follow the change, and switching to `session_mode: "new"` clears `session_id`.

**Agent tools.** An agent can manage automations itself with `sys_scheduled_task_create`, `sys_scheduled_task_list`, `sys_scheduled_task_update`, and `sys_scheduled_task_delete` -- so an agent can schedule its own follow-up work. The create and update tools expose the same `permission_mode`, `max_cost_usd`, `session_mode`, `session_id`, `execution_target`, and `project_id` controls, plus `project_name`: the tool looks up your project with that exact name, or creates it, and sends its id. With `session_mode` and `session_id`, an agent can schedule follow-ups into its own session. On a server that doesn't advertise `scheduled_task_projects`, a call that sets either project field is refused before anything is created; likewise a call that sets a session field on a server that doesn't advertise `scheduled_task_session_modes`. An update by name checks the task exists before creating a project; if a create fails after making a new project, the project is kept and a retry reuses it. Because they persist or change unattended work, every agent call to `sys_scheduled_task_create` or `sys_scheduled_task_update` parks on an approval prompt in the session, even when no other guardrails apply. The prompt lists the requested settings (agent, schedule, timezone, host, execution target, project, session mode and session, permission mode, cost cap) and the prompt's length, and the card's argument preview shows the request itself, truncated after 1,024 characters; review them before approving. This also holds inside an automation's own run: if its agent creates or updates another automation, that call waits for approval too and is denied when nobody answers before the approval timeout. `sys_scheduled_task_list` and `sys_scheduled_task_delete` are not gated this way, and changes you make directly in the UI or REST API keep their existing authorization rules.

Agent calls to `sys_scheduled_task_create` and `sys_scheduled_task_update`
require interactive approval before persisting future work. Review the agent,
prompt, host and permission settings in that request. This also applies when
the call inherits defaults. Direct changes you make in the UI or REST API keep
their existing authorization rules.

## Schedules are RRULEs, not cron

Persisted RRULEs have a 512-character storage limit. The parser also applies a
4096-character safety ceiling; that ceiling does not increase storage capacity.
Use a single RRULE with at most 24 combinations of
`BYHOUR`, `BYMINUTE`, and `BYSECOND`. `INTERVAL` is limited to 1–366 periods;
for minute and second frequencies the equivalent maximum is 366 hours.
Hourly-equivalent rules such as `FREQ=MINUTELY;INTERVAL=60` and
`FREQ=SECONDLY;INTERVAL=3600` remain valid, including when loaded from storage.
Validation and next-fire lookup have an eight-year occurrence horizon and a
100,000-step search budget that counts calendar candidates, position selections,
and inner time-filter searches. Each search also has a 250 ms monotonic deadline.
Every `BY*` list is deduplicated and limited to 24 distinct values before parsing.
Create and update validation runs off the HTTP event loop. Exhausting
the budget raises a validation error. If this happens while re-arming a task,
the scheduler logs the reason and clears its next-run time instead of leaving
an expired timestamp. Update the rule to resume scheduling.
An empty search stops at the horizon even when filters yield no candidates.
Schedules with no occurrence in the window are not armed; validation requires
at least two occurrences and an hourly minimum.

A schedule is an [RFC 5545](https://datatracker.ietf.org/doc/html/rfc5545) recurrence rule evaluated in the task's IANA timezone.

| Intent | `rrule` |
| --- | --- |
| Every hour | `FREQ=HOURLY` |
| Daily at 09:00 local | `FREQ=DAILY;BYHOUR=9;BYMINUTE=0` |
| Weekdays at 09:00 local | `FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0` |

The rule is anchored at midnight of the local day, which gives occurrences a deterministic wall-clock phase: an hourly rule fires on the hour, a daily rule at its `BYHOUR`/`BYMINUTE`.

Beyond syntax checks, create and update reject these cases with a 400:

- **Sub-hour gaps in the validation sample.** The validator checks consecutive occurrences from a fixed UTC anchor, stopping when it reaches an occurrence at least 25 hours after the first or samples 2,000 occurrences. It rejects gaps below one hour within that sample. Irregular recurrence sets can contain shorter gaps later, so passing validation does not guarantee an hourly minimum for every future firing or a bound on cost.
- **Never fires.**
- **Fires only once.** A schedule that cannot recur is not a schedule.

Two timing caveats are worth knowing:

- **DST.** A wall-clock time that does not exist on a spring-forward day maps to whichever instant the timezone database resolves it to, and a time that occurs twice on a fall-back day picks the earlier one. Either way a schedule slips by at most an hour across a DST edge.
- **`INTERVAL>1` rules** (biweekly, interval-monthly) tie their phase to the day the timer last re-armed, because the anchor is midnight of the query day rather than a stored start date. A server restart on a different weekday can slip such a rule by one period. `INTERVAL=1` rules are unaffected.

`next_run_at` in an API response is computed by the live scheduler and is authoritative. Do not recompute it client-side; a client cannot reproduce the server's anchor for `INTERVAL>1` rules.

## What happens when a task fires

1. **The row is re-read.** The armed timer is never trusted. A task deleted or paused between arming and firing is a no-op.
2. **The launch target is resolved.** A `managed_sandbox` task checks that managed launches are available; its fresh sandbox is provisioned during launch. For `connected_host`, a task with no pinned `host_id` uses the owner's most-recently-active live host, chosen at fire time. A task with no pinned `workspace` starts the runner in that host's home directory, which is what makes chat-only, research, and MCP-only automations possible. A pinned host that is missing or offline -- or an owner with no live host at all -- records a **failed** run rather than a running one (`error_code` `host_offline`, `host_not_found`, or `no_online_host`).
3. **A session is created**, bound to the task's agent and carrying any `model_override`, `reasoning_effort`, and supported `permission_mode`. Connected-host sessions carry the resolved workspace and host; managed-sandbox sessions are bound to their new sandbox during launch. If the task has a project, the session is filed into it; a project deleted since the task was saved leaves the session unfiled instead of failing the run, and is cleared from the task. If `max_cost_usd` is set, a per-session cost-budget policy is attached before the prompt is dispatched.
4. **Ownership is granted.** The new session gets a `LEVEL_OWNER` grant for the task's owner (a reserved local user in single-user and OSS deployments). Without the grant the run would be invisible.
5. **The runner launches and the prompt is dispatched**, so the agent actually works. A seeded prompt with no launched runner would just sit in history. A `managed_sandbox` fire saves the prompt in the session first, records the run, and then provisions the sandbox in the background. The prompt is delivered when the sandbox host connects, even after a server restart or on another replica. A launch failure fails the run with `error_code` `launch_failed`.
6. **The run is recorded** in `scheduled_task_runs`, and `last_run_at` and `last_run_conversation_id` are stamped on the task.

For a `continue` task with a `session_id`, steps 2 to 4 are replaced: the session is checked as described in [Continuing one session](#creating-an-automation), its runner is woken the way a user message would wake it, and the prompt is dispatched into it. The run is recorded as `succeeded` once the prompt is delivered, so its status says the prompt went in, not that the turn finished. A `continue` task's first firing runs every step and records the new session as the task's `session_id` once it exists and its owner is granted. If the task was edited while that first firing ran, the firing keeps its session to itself and the next firing starts the one later firings continue. With `automations_v2`, a `continue` task's runs are routed by its pinned connected host, or for `managed_sandbox` by one host id derived from the task, so every run reaches the server holding that sandbox's connection. A dead sandbox is relaunched under the same host id, the way a user message relaunches it.

Firing is fire-and-forget: the guard steps run synchronously so a dead fire costs nothing, then session creation and launch move to a background task and the scheduler re-arms immediately. A failure in that background work is logged and never crashes the scheduler.

## Run history

`GET /v1/scheduled-tasks/{id}/runs` returns history in descending `scheduled_at` order as `{"runs": [...], "next_cursor": ...}`. The `limit` query parameter defaults to 100 and accepts 1–1,000. Pass the returned `next_cursor` as `after` to fetch the next page; a null cursor marks the end. For example, request `/runs?limit=1`, then `/runs?limit=1&after=<next_cursor>`.

Each run carries `status`, `scheduled_at`, `fired_at`, `finished_at`, `conversation_id`, and `error_code`. The free-text `error` blob is deliberately not returned; `error_code` is the queryable classification.

| `status` | Meaning |
| --- | --- |
| `running` | Dispatched; the turn is in flight. |
| `succeeded` | The dispatched turn finished, or, for a continued session, the prompt was delivered. |
| `failed` | The firing or the turn failed; see `error_code`. |
| `skipped` | The firing was deliberately not attempted -- for example an unsupported `execution_target`. |

`ScheduledTaskRun` also declares `scheduled` and `starting` statuses. The default scheduler creates runs at dispatch with status `running`. With `automations_v2` enabled, Run now records `scheduled` until a server claims the request. The persistent scheduler then records `starting` while it prepares a run, and `running` once the prompt is sent, or saved with its sandbox's host bound. A continued session's run goes straight to `succeeded` once its prompt is delivered.

## Lifecycle

Clients see tasks as `active` or `paused`. Deleted tasks are hidden immediately; their database rows may remain until unfinished claimed runs complete or are reconciled. `PATCH /v1/scheduled-tasks/{id}` changes supported mutable fields and can move a task between `active` and `paused`; it cannot set `deleted` (use `DELETE`). Omitting a field normally leaves it unchanged. Moving a task to a different `agent_id` clears `model_override`, `reasoning_effort`, and `permission_mode` unless the same request resends them, because a harness switch invalidates the settings stored beside the agent -- so re-pin an automation's execution controls in the request that switches its agent, or it fires the new agent on defaults. `max_cost_usd` is not agent-bound and survives the switch. Sending `permission_mode: null` clears the permission override, and `max_cost_usd: null` clears the cost cap. `project_id` moves future runs to another project, and `project_id: null` stops filing them into one; sessions from earlier runs stay where they are. An explicit `null` for `workspace` or `host_id` is rejected -- the rejection is on the presence of the key, so it applies even to a task where that field is currently unset; there is no direct null-based way to clear either one.

Switching to `execution_target: "managed_sandbox"` clears both stored pins. Omit the `host_id` and `workspace` keys entirely in that PATCH, even if their values would be null. Switching back to `connected_host` leaves both unset unless the request supplies new pins, so the next fire resolves the owner's live host and its home directory.

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

A task response carries `id`, `name`, `prompt`, `rrule`, `owner_user_id`, `agent_id`, `timezone`, `created_at`, `model_override`, `reasoning_effort`, `permission_mode`, `max_cost_usd`, `session_mode`, `session_id`, `workspace`, `host_id`, `execution_target`, `project_id`, `state`, `last_run_at`, `last_run_status`, `last_run_conversation_id`, `next_run_at`, and `updated_at`.

## Current limits

The scheduler is the timing engine only, and its behavior is deliberately simple:

- **Missed fires are not replayed.** On startup the scheduler arms the next future occurrence of every active task; occurrences that passed while the server was down are gone. There is no backfill or replay.
- **No automatic retry.** A failed run is recorded and visible in history; the retry policy is that the next occurrence fires normally.
- **Overlapping fires are skipped.** If the previous firing of the same task is still being dispatched when the next tick arrives, the tick is dropped. A `managed_sandbox` firing stops counting once its prompt is saved and its sandbox has started provisioning, so the next tick runs in its own new sandbox. No run row is recorded for it, so a dropped tick leaves no trace in history beyond the server log.
- **Late ticks are skipped.** A tick arriving more than 30 seconds after its scheduled time (a blocked event loop, for instance) is skipped rather than fired late.
- **One scheduler per server process.** Timers are in-process with no distributed leasing, so a multi-replica deployment would fire each task once per replica. Shared session-create orchestration is the intended path for that.
- **Execution targets.** Runs use either a connected host with an existing workspace or a fresh managed sandbox. Reusing an existing managed sandbox is not supported. Git branch selection at fire time is not available: `base_branch` is rejected as an unknown API field. A stored row with an unsupported `execution_target` records a `skipped` run with `error_code` `unsupported_target`.

## Related

- [Agent YAML spec](AGENT_YAML_SPEC.md) -- defining the agent an automation binds to.
- [Policies](POLICIES.md) -- gates and spend caps that apply to sessions an automation creates.
