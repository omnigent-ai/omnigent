---
name: track-issues
description: Set up (or tear down) hourly GitHub-issue tracking for a repo — polly pulls issues labeled `polly` and delegates them. Use when the user says /track-issues [owner/repo], "track issues for <repo>", or wants a repo's tickets auto-processed.
---

# track-issues — GitHub Issues as polly's task queue

Turns a GitHub repo's issues into a ticket queue that a scheduled polly run
pulls from hourly. The human files issues labeled `polly` (plus a normal type
label like `bug`/`feature`); polly claims, delegates, cross-reviews, opens
PRs, and hands back `polly-ready` tickets. The human always merges PRs and
closes issues.

## Invocation

`/track-issues <owner>/<repo>` — set up tracking for that repo.
`/track-issues <owner>/<repo> --project <name>` — same, but file each
scheduled run's session into the named Omnigent project (UI grouping).
`/track-issues remove <owner>/<repo>` — pause/delete the schedule.

Requirements before setup: the repo must have a local clone on this host
(ask the human for the path if you can't find one under `~/projects`), and
`gh` must be authenticated for it.

## Label protocol (create any that are missing via `gh label create`)

- `polly` (#5319e7) — queued for polly. The human applies this.
- `polly-in-progress` (#fbca04) — claimed by a run; prevents double-claiming.
- `polly-ready` (#0e8a16) — PR open + cross-reviewed; awaiting human merge.

Candidate = open, has `polly`, has NEITHER state label. Blocked tickets get
the state label removed plus a comment explaining the blocker, so the human
can amend and requeue.

## Setup procedure

1. Verify the local clone path and `gh` auth (`gh label list --repo <repo>`).
2. Create missing labels per the protocol above.
3. Get this session's `host_id` from `sys_session_get_info`, and use the
   deployed builtin polly's `agent_id`.
4. `sys_scheduled_task_create` with:
   - `name`: `track-issues: <repo-short-name>`
   - `rrule`: `FREQ=HOURLY;BYMINUTE=5` (stagger BYMINUTE per repo if several)
   - `timezone`: the host's local timezone
   - `workspace`: the local clone path; `host_id`: from step 3
   - `prompt`: the sweep prompt below, with `<repo>` substituted.
   - `project_id` (optional): if the human passed `--project <name>` — or an
     Omnigent project named exactly like the repo short name exists — resolve
     the project's id and pass it so every run's session files into that
     project. If the server rejects the field (older build without
     scheduled-task project filing), retry the create without it and tell the
     human the server needs updating for project filing.
5. Report the schedule id, first-fire time, and (if set) the project the
   runs will file into.

## The sweep prompt (template for each scheduled run)

> Issue-tracker sweep for github.com/<repo> (workspace is a clone of it).
> Follow this loop exactly; if there is nothing to do, end the session
> quietly without dispatching any workers.
>
> 1. QUERY: `gh issue list --repo <repo> --label polly --state open --json
>    number,title,labels,body`. Candidates have `polly` and neither
>    `polly-in-progress` nor `polly-ready`. If none, stop.
> 2. CLAIM per issue before any work: re-check the state label is still
>    absent, add `polly-in-progress`, comment that this run claimed it.
> 3. TRIAGE with `scout` (purpose: explore) for any repo lookups; read
>    `.polly/registry.json` and any handoff docs for established patterns.
> 4. DELEGATE per roster rules: `codex` for narrow scoped changes,
>    `claude_code` for multi-file/refactor/test-heavy work; own worktree,
>    fresh branch off origin/main, all four gates green, own PR with
>    "Closes #<issue>" in the body.
> 5. VERIFY gates yourself at the PR head, then cross-review with the
>    OPPOSITE vendor (diff + contract only); loop fixes until clean.
> 6. CLOSE OUT: comment PR link + gate results + verdict on the issue;
>    swap `polly-in-progress` → `polly-ready`. NEVER merge the PR, NEVER
>    close the issue.
> 7. Blocked/ambiguous/repeatedly-failing ticket: comment the blocker,
>    remove `polly-in-progress` (keep `polly`), move on.
>
> Budget: max 3 issues per run, oldest first. Record state in
> `.polly/registry.json`.

## Teardown

`sys_scheduled_task_list`, find `track-issues: <repo-short-name>`, then
`sys_scheduled_task_update(state="paused")` to suspend or
`sys_scheduled_task_delete` to remove. Leave the labels in place.

## Notes

- Each scheduled run is a fresh polly session; continuity lives in the issue
  comments, labels, and `.polly/registry.json` — the claim protocol is what
  makes concurrent runs safe.
- Hourly is the platform's minimum interval; don't try to go faster.
