---
name: cross-review
description: Verify an implementer's diff with an INDEPENDENT, different-vendor sub-agent (diff plus contract only); turn blocking issues into fix-tasks and loop until clean.
---

# cross-review — independent verification

The implementer never signs off on its own work — a different model does, and
review is a sub-agent that returns a structured report, not a transcript
anyone needs to read through.

## Procedure
1. Get the task's diff — `sys_os_shell("gh pr diff <pr>")` (or
   `git -C .worktrees/<task_id> diff main...HEAD`).
2. Run the deterministic gates first — tests / lint / typecheck via
   `sys_os_shell`. If red, re-dispatch the implementer to drive it green first;
   don't involve the reviewer yet.
3. Dispatch `agy` as the DEDICATED INDEPENDENT REVIEWER — it reviews ALL
   PRs regardless of which vendor implemented. Use a task-based title such as
   `review-auth-refactor`, never the raw vendor name:
   `sys_session_send(agent="agy", title="review-<task_slug>",
   args={purpose: "review", input: "<the diff> + <the acceptance contract>.
   Review ONLY against the contract. Report blocking / non-blocking /
   suggestions. Do not edit code."})`. Give it the diff as text — do NOT point
   it at the implementer's worktree. Fetch the diff and emit the
   `sys_session_send` call in the SAME turn you decide to review — never end a
   turn having only announced "I'll load cross-review and fetch the diff" with
   no tool call (that dropped turn stalls the run; nothing dispatches and no
   inbox wake arrives). Once the reviewer dispatch is in flight, end your turn;
   collect the inbox-delivered structured report with `sys_read_inbox` when it
   returns. Use `sys_session_get_history` only to debug an empty or unclear
   review result.
4. The reviewer SURFACES issues; it does not fix them.
5. For each **blocking** issue: add a fix-task to the registry scoped to the
   same worktree, and send the concrete fixes back to the SAME implementer
   conversation via `sys_session_send` — reuse the original implementer's
   `agent` + `title` (or address it by `session_id`) with
   `purpose: "implement"`, so the worker keeps its worktree/branch context and
   updates its existing PR. A new title would spawn a fresh worker with no
   memory of the task. Then loop to step 1.
6. When gates are green AND there are zero blocking issues, the PR passes
   review — mark it ready in the registry (with its PR URL) and leave it for
   the human to merge. polly does NOT merge it.
7. If the contract can't be satisfied after a few loops, stop and escalate to
   the user with specifics.

## Notes
- Cross-review uses `agy` as the DEDICATED INDEPENDENT REVIEWER for ALL PRs,
  regardless of which vendor implemented. This ensures consistent, independent
  verification from a single trusted reviewer.
- If `agy` is not available (per polly's roster preflight), you CANNOT run
  independent cross-vendor review: say so explicitly and pull in the human at
  the plan gate.
- Give the reviewer ONLY the diff + contract — never the implementer's
  transcript or worktree. The cross-vendor independence is the whole point.
- Review is `agy` dispatched with `purpose: "review"`. It reports issues and
  never edits; only the implementer opens a PR, so a stray reviewer edit never
  reaches the deliverable.
- Non-blocking issues / suggestions go in the registry as follow-ups; they
  don't block the PR.
