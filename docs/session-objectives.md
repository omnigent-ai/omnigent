# Session objectives and outcomes

A session's status says whether its turns finished, not whether the agent did
what it set out to do. An agent records that itself, as a short log:

- an **objective**: one line on what it sets out to do, recorded with
  `sys_record_objective`. Each call adds one, and the latest is the session's
  current objective.
- an **outcome** for an objective: whether the agent reached it (`success`),
  one emoji, and a few words, like a status message. `sys_record_outcome`
  records it; calling it again for the same objective replaces it.

`sys_record_outcome` names the objective with `objective_id`. Without it, the
outcome resolves the latest objective: in a session an automation is running,
the latest one recorded during the current run. When there is no objective to
resolve, the outcome gets an entry of its own, without objective text, so an
agent that skips `sys_record_objective` still records how things went.

Recording never changes the session's or a run's status: a run can be
`succeeded` while its agent reports `success: false`.

## When an agent has the tools

The tools belong to the `objectives` tool group. It is off by default, except in
sessions an automation serves: one an automation created, or one an automation
has sent a run's prompt to. There the runner turns it on, and the note that
follows every automation prompt asks the agent to record the goal as an
objective and, before its final reply, whether it reached it. A session that an
automation starts continuing gets the tools from that automation's first prompt
on: the prompt changes the session's agent revision, and the runner rebuilds
the session's tools the way it does after an MCP selection change.

An agent spec decides for itself with `tool_groups.objectives`: `true` gives
every session the tools, and `false` withholds them even from automations. See
[Tool groups](AGENT_YAML_SPEC.md#tool-groups).

## Automation runs

Before a run's prompt is sent, the server stamps the session's
`omnigent.scheduled_task_run` label with the run's ID: a new session gets it at
creation, and a continued session just before the prompt goes in. Objectives
and outcomes recorded afterwards carry that run's ID. The label is stored with
the session, so attribution holds across server restarts and replicas, and a
continued session's earlier runs keep their own outcomes. If the label cannot
be written, the run fails with `launch_failed` and no prompt is sent.

Attribution follows the session's latest run: if a continued session gets the
next run's prompt while still working on the previous one, what it records from
then on goes to the newer run. What a session records between runs, after a
person has stepped in, goes to the latest run too.

Run history (`GET /v1/scheduled-tasks/{id}/runs`) shows, for each run, the five
newest objectives recorded during it, oldest first, as `objectives`, with
`more_objectives` set when there are more. The automation's page shows each
outcome's emoji and words on the run's row, and an objective without an outcome
muted.

Clients cannot set or change the run label; a fork drops it.

## API

| Method | Path | Access | Purpose |
| --- | --- | --- | --- |
| `GET` | `/v1/sessions/{id}/objectives` | read | List objectives, oldest first. `limit` 1–100 (default 50); pass `next_cursor` as `cursor` for the next page. |
| `POST` | `/v1/sessions/{id}/objectives` | edit | Record an objective. Body: `objective`. |
| `PUT` | `/v1/sessions/{id}/objectives/outcome` | edit | Record an outcome. Body: `success`, `emoji`, `text`, optional `objective_id`. |

An objective is returned as:

```json
{
  "id": "…",
  "session_id": "…",
  "objective": "Get the nightly build green",
  "created_at": 1730700000,
  "scheduled_task_run_id": "…",
  "outcome": {"success": false, "emoji": "🚧", "text": "Blocked: CI is down", "recorded_at": 1730700300}
}
```

`objective` is `null` for an outcome recorded on its own, `scheduled_task_run_id`
is `null` outside automation runs, and `outcome` is `null` until one is
recorded. Times are Unix epoch seconds.

The objective is at most 200 characters and the outcome's text at most 80;
runs of whitespace collapse to single spaces. The emoji must be exactly one
emoji (a keycap, flag, skin-tone or ZWJ sequence counts as one); the check
follows Unicode's Extended_Pictographic characters, so a few symbols that
rarely display as emoji, such as `☉`, also pass. Invalid values get a 422,
an `objective_id` that names no objective of the session a 404, and a session
already holding 500 objectives a 409.

The runner sends the tools for the calling session only, with the
`X-Omnigent-Orchestration-Source` header; a call relayed for one session's agent
cannot record for another session (403).

## Storage

Objectives are rows of `session_objectives`, keyed by workspace, session and
objective ID, and deleted with their session. `outcome_success` is a typed
column. The objective and outcome words can repeat task content, so they are
stored in `objective_payload` and `outcome_payload`, envelopes private to
`SessionObjectiveStore` (`{"plaintext": {...}}`, with `ciphertext` reserved for
an encrypting store) that CHECK constraints cap at 2,048 bytes. Writes lock the
session's metadata row, so a session's objective cap holds under concurrent
calls.
