# Session journal

Every Omnigent session has a **journal**: an append-only record of the plan its
agent follows, where each step stands, and the notes the agent keeps. The agent
reads it to pick up where it left off, after a restart, a context compaction or
the next automation run. People follow its plan in the session's **Tasks** tab.

## What's in a journal

Each entry has an ID, a position (0, 1, 2, … within the session), the time it
was appended, the automation run the session was serving (if any), and an
optional plan step. Entries are never edited, and are deleted only with their
session. A fork carries the journal its history wrote: all of it, or up to where a fork
from an earlier message cuts the history. A side chat starts with none.

| Kind     | Written by | Fields                                                                       |
| -------- | ---------- | ---------------------------------------------------------------------------- |
| `plan`   | agent      | `name`, `steps` (the whole graph), `version` (set by Omnigent), `source`     |
| `status` | agent      | `status` (`started`, `done`, `needs_input`, `blocked` or `failed`) and `text`, for one step |
| `note`   | agent      | `body`: Markdown whose first line is the summary                             |

The newest `plan` entry is the session's current plan; earlier ones stay as
history, numbered v1, v2, …. A plan has a name that says its goal in a few
words, such as "Cut GET /orders p95 by 20%", and a graph of steps, each with a
short, stable ID such as `profile`, a name, an optional goal, and the IDs of the
steps it depends on. A single goal is a one-step plan.

The newest status for a step is its current one:

- `started`: the agent has begun the step itself. It shows as Running while the
  session is working, and as "Started 3h ago" once it's idle.
- `done`: the step achieved its goal. Only `done` lets later steps start.
- `needs_input`: the step waits for the session's user: an approval, a
  decision, or a go-ahead. The agent also asks with its question tool, so the
  question reaches the user's Inbox. It shows as Waiting for you, in pink.
- `blocked`: something outside the session stops it, such as CI being down or
  another team's review.
- `failed`: the step's own work failed.

The workflow skill keeps the agent working while any step is ready or started:
it ends its turn only once everything left is done, failed, blocked or waiting
for the user, unless told to stop sooner.

A status belongs to a step ID, not to a plan version. When the agent replans, a
step that keeps its ID keeps its status, so finished work stays finished.

Limits: a plan has at most 100 steps and 32 KiB of JSON; status text is at most
80 characters (required except for `started`); a note is at most 4 KiB. Every
stored entry is at most 16,383 bytes after compression.

## Agent tools

The four tools make up the `journal` tool group, which every agent has. An agent
spec can drop it with `tool_groups.journal: false` (see
[Tool groups](AGENT_YAML_SPEC.md#tool-groups)).

| Tool                                                               | Does                                                                                                                                                                                                                                                                            |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sys_journal_read(conversation_id?, kind?, step?, cursor?, limit?)` | Returns entries as compact lines, oldest first within the page, and `page.next_cursor` for older ones. With no arguments it reads the caller's newest entries. `kind: "plan"` returns plans with their full graphs. Another session's journal needs read access to that session. |
| `sys_journal_write_plan(name, steps)`                               | Sets the session's plan: its name and the whole graph; returns `{id, position, version}`.                                                                                                                                                                                       |
| `sys_journal_write_status(status, step?, text?)`                    | Records a step's status. `step` may be left out when the plan has one step. `started` and `done` need every step it depends on to be done first; otherwise the write fails with `step_not_ready`, naming them.                                                                  |
| `sys_journal_write_note(body, step?)`                               | Adds a note.                                                                                                                                                                                                                                                                    |

Errors come back as `{"error": …}`: `invalid_entry` (a field failed
validation), `entry_too_large` (with the size and the limit),
`session_not_found` and `session_out_of_tree` (for reads of another session).
A write's retry, for example while the runner's connection to the server
reconnects, returns the entry the first attempt wrote instead of adding another.

## Running a workflow from a session

Type `/workflow` and a description in the composer, or pick **Run a workflow**
from the composer's **+** menu, for example:

```
/workflow Cut p95 latency of GET /orders 20%; one change per step; open a PR.
```

`/workflow` is a skill Omnigent ships to every agent with the journal tools. The
agent turns the description into a plan, works through it, and records each
step's status as it goes; the Tasks tab shows the plan's steps the moment it's
written. The agent asks first only if the description is ambiguous, and you can
steer or stop it in chat at any time. An agent's own skill named `workflow`
takes precedence over Omnigent's.

## Automations

An automation's prompt asks the agent to read its journal first, write a plan if
the session has none, and write a status for each step it worked on before its
final reply. Before each run's prompt, Omnigent labels the session with the run
(`omnigent.scheduled_task_run`), so the entries written during the run carry its
ID. See [Automations](AUTOMATIONS.md).

## Tasks

A session's work shows as one list of **tasks**, in two places that always
agree: a one-line preview pinned above the chat ("Tasks 3/7 · Profiling the
request"), and the **Tasks** tab in the session's Workspace rail (a drawer on
phones). Click the preview to open the tab. Both count a failed task as
finished, since its work is over, and say how many failed: a list that ended
with one failure reads "Tasks 9/9 · Finished, 1 failed".

The tasks are the current plan's steps when the session has a plan. Otherwise
they're the agent's own to-do list, which Claude and Codex keep as they work,
so a session shows its tasks without a workflow. Once a session has a plan, the
plan replaces the agent's own list in both places.

In the Tasks tab, the plan's name heads the list (the chat preview shows it on
hover), and each task appears once, in order, with its state (not started,
ready, running, stopped, waiting for you, done, blocked or failed), its newest
result, how long it took and its newest note. A started task is running while
the session's turn, a background task it left running or a sub-agent is still
going, and stopped once nothing is. It waits for you while a question or
permission prompt in the chat waits for your answer. A task that starts again
shows only its new attempt's note, and its history marks a restart after the
task was done or failed. Tasks waiting for you are pink, and an idle list
with tasks the agent could still run says "Stopped" ("Run ended" after an
automation run), so it never looks about to run. A summary line and a strip
count the tasks in each state, and stay at the top as you scroll: hover a
segment of the strip for that task's status, or click it to jump to the task. When the agent replans, the new
tasks simply appear. Click a plan's task for what it waits on, its goal and its
history: each status and note, oldest first. Tasks from the agent's own list
show only their name and state.

Notes that aren't about one task appear under Notes. With no tasks yet, the
tab links to starting a workflow, the same as **Run a workflow** in the
composer's **+** menu. On servers that don't keep journals, the tab appears once
the agent makes a to-do list.

New entries arrive live over the session's event stream. After a reconnect the
tab fetches whatever it missed, and while the stream is down it checks every
30 seconds.

## API

| Method | Path                       | Notes                                                                                                                                                                                                           |
| ------ | -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET`  | `/v1/sessions/{id}/journal` | Read access. `limit` (1–1000), `order` (`asc` or `desc`), `after` and `before` (entry IDs, as for conversation items), `kind`, `step`. Returns a list page. `kind=plan&order=desc&limit=1` is the current plan. |
| `POST` | `/v1/sessions/{id}/journal` | Written by the session's runner for its agent's tools; the call must carry `X-Omnigent-Orchestration-Source` naming the session.                                                                                |

Each new entry is also published as `session.journal.appended` on
`GET /v1/sessions/{id}/stream`. `GET /v1/info` reports `session_journal: true` on
servers that keep journals.

## Storage

Entries live in `session_journal_entries`, beside the session metadata, keyed by
workspace, session, position and creation time; `session_journal_heads` holds
each session's next position. An append locks only its session's heads row, so
one session's appends run in order without slowing other sessions or the
session's own metadata updates. Payloads are compressed JSON in an envelope that
also has room for an encrypted variant.
