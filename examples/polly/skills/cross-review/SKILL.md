---
name: cross-review
description: Verify an implementer's diff with an INDEPENDENT, different-vendor sub-agent (diff plus contract only); turn blocking issues into fix-tasks and loop until clean.
---

# cross-review — independent verification

The implementer never signs off on its own work — a different model does, and
review is a sub-agent that returns a structured report, not a transcript
anyone needs to read through.

## Procedure
1. Reuse the supplied diff file, or save the task's diff directly with a shell
   redirect (`gh pr diff <pr> > <absolute shared diff path>` or
   `git -C .worktrees/<task_id> diff <base-sha>...<head-sha> > <absolute shared diff path>`).
   Use a unique snapshot outside the implementer's worktree, record its base and
   head commit SHAs, and leave it unchanged until review finishes. The reviewer
   must be able to read that path. For a different host or sandbox, transfer the
   snapshot first or arrange access to both commits so it can compute the diff;
   never regenerate the diff through model output.
2. Run the deterministic gates first — tests / lint / typecheck via
   `sys_os_shell`. If red, re-dispatch the implementer to drive it green first;
   don't involve the reviewer yet.
   If a pytest result's count must be recorded or reconciled, collect ground
   truth with `python -m pytest --collect-only -q <same files>` against the
   exact file set/command/commit the implementer reported. Never use
   `grep -c 'def test_'` as a pytest count: it counts functions, not collected
   cases, and misses parametrized case expansion.
3. Dispatch a DIFFERENT-vendor sub-agent as reviewer: pick any AVAILABLE worker
   whose vendor differs from the implementer's — `claude_code`, `codex`,
   `opencode`, `cursor`, `hermes`, `agy`, or `pi` (e.g. Claude built it → any of
   `codex` / `opencode` / `cursor` / `hermes` / `agy` / `pi`, and so on). Use a
   task-based title such as `review-auth-refactor`, never the raw vendor name:
   `sys_session_send(agent="claude_code"|"codex"|"opencode"|"cursor"|"hermes"|"agy"|"pi", title="review-<task_slug>",
   args={purpose: "review", input: "Read the diff at <absolute shared diff path>
   (base <base-sha>, head <head-sha>). <The acceptance contract>.
   Review ONLY against the contract. Report blocking / non-blocking /
   suggestions. Do not edit code. If the snapshot is inaccessible, report that
   failure rather than reviewing from a summary."})`. Pass the snapshot path,
   not its contents; do NOT point the reviewer at the implementer's worktree.
   Prepare the snapshot and emit the
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
- Cross-review requires a reviewer from a DIFFERENT vendor than the implementer,
  so it needs at least two AVAILABLE workers (per polly's roster preflight). If
  only one worker — or only one vendor that can review this implementer's PR —
  is available on the machine, you CANNOT run independent cross-vendor review:
  don't dispatch a reviewer that can't boot, say so explicitly, and pull in the
  human at the plan gate.
- Give the reviewer ONLY the diff snapshot + contract — never the implementer's
  transcript or worktree. The cross-vendor independence is the whole point.
- Review is a coding sub-agent (`claude_code`/`codex`/`opencode`/`cursor`/`hermes`/`agy`/`pi`) dispatched with
  `purpose: "review"` — a DIFFERENT vendor from the one that built the diff. It
  reports issues and never edits; only the implementer opens a PR, so a stray
  reviewer edit never reaches the deliverable.
- Non-blocking issues / suggestions go in the registry as follow-ups; they
  don't block the PR.
