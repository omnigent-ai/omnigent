---
name: fanout
description: Run independent subtasks in parallel — one git worktree and one implementation sub-agent per task, each opening its own PR — then cross-review every PR and integrate each reviewed task into polly's own worktree. polly never merges a PR into main; the human does.
---

# fanout — safe parallel execution

Use ONLY for subtasks that are parallel-safe (no shared files, no ordering
dependency).

## Procedure
1. Per task, create an isolated worktree:
   `sys_worktree_create(branch_name="polly/<task_id>")`. Never
   `git worktree add` — the tool puts the tree where this project configures
   worktrees to live and runs the project's setup script (dependency install,
   `.env` copy) in it, so the worker starts in a usable tree. Each task tree is
   forked from YOUR OWN session's branch, so anything you already committed in
   your worktree is the base every worker builds on — don't pass `base_branch`
   unless a task must fork from somewhere else. Record the returned
   `worktree_path`, `branch`, and `base_branch` in the registry
   (`.polly/registry.json`).
   If the response's `setup.ok` is false, the tree exists but is NOT prepared —
   don't dispatch a worker into it; report `setup.output_tail` to the user and
   stop. (`setup: null` just means the project configures no setup script.)
2. Dispatch one implementation sub-agent per task, scoped to its worktree:
   `sys_session_send(agent="claude_code"|"codex"|"opencode"|"cursor"|"hermes"|"agy", title="<task_slug>",
   args={purpose: "implement", input: "<task + acceptance contract +
   worktree path>"})`. Use a short task-based title such as `auth-refactor` or
   `fix-sse-error`, never the raw vendor name. State the scope and that it must
   work only inside the `worktree_path` from step 1. The worker drives the task to green
   and opens its OWN PR for the branch. Every commit the worker authors must
   end with a blank line followed by the exact co-sign trailer as its final
   line — `Co-authored-by: omnigent <noreply@omnigent.ai>`.
   For a long-running `claude_code` or `codex` implementation with an explicit
   completion condition, the `input` may instead be one standalone
   `/goal <condition>` command containing that same task, worktree, acceptance
   contract, green gates, and PR requirement.
   Do not use child goal mode for other workers or non-implementation purposes.
   Record each handle's `conversation_id`
   in the registry. Emit the worktree + `sys_session_send` tool calls in THIS
   turn — never end a turn having only said you will dispatch; the dispatch
   calls and their announcement go in the same turn. Dispatch the whole
   parallel-safe set, THEN (and only then) END YOUR TURN. Do not poll.
3. Each sub-agent runs autonomously and notifies you through the inbox when it
   finishes. Collect its structured result with `sys_read_inbox` and record the
   PR URL in the registry. If the inbox result is empty/unclear, inspect that
   worker conversation with `sys_session_get_history` before deciding what to do
   next.
4. Send each finished task's PR through `cross-review`.
5. Integrate the reviewed task into YOUR OWN worktree, so the finished fan-out
   is present in one tree instead of scattered across branches. As each task
   passes cross-review, in your own workspace:
   `sys_os_shell("git merge --no-ff --no-edit polly/<task_id>")`. One merge per
   task, in registry order, each a distinct revertible commit. Then mark the
   task ready in the registry with its PR URL.
   This is NOT merging the PR: you never merge into `main` and never run
   `gh pr merge` — the PR stays open for the human. Your session branch is an
   integration branch, and you do NOT push it.
   On a merge conflict, STOP: `git merge --abort`, leave the tree clean, and
   report the conflicting paths plus both task ids. Do not resolve it — that is
   code work, so it goes to a sub-agent (scoped to the integration) or to the
   human. Conflicts here mean the task slices were not disjoint.
6. Remove a finished worktree with
   `sys_worktree_remove(worktree_path="<path>")` — not `git worktree remove`,
   which skips the project's teardown script (stop a dev server, drop a
   database). Only once its PR is open, review is clean, and step 5 integrated
   it: the branch lives on the remote and in your tree, so the worktree is
   disposable, but leave `delete_branch` off so the PR keeps its branch. Don't
   remove a worktree that still has open fix-tasks.
7. When the whole set is done, report: every task's PR URL, that each one is
   integrated into your branch, and `git log --oneline` of the merges. If any
   task changed a dependency manifest, say so — your tree's installed
   dependencies predate the merge and someone must re-run the project's setup.

## Notes
- A fork carries COMMITS, not your working tree. If the tasks depend on
  groundwork you did in your own worktree, commit it before step 1 — otherwise
  the workers fork from your branch's last commit and won't see it.
- Respect the per-turn dispatch cap (enforced by policy). More tasks than the
  cap → dispatch in waves (let the running batch finish before dispatching more).
- The human can open any sub-agent in the UI's Subagents panel and read its
  conversation while it runs.
- If a running worker is wrong, runaway, superseded, or no longer useful, call
  `sys_cancel_task` with `task_id` set to the recorded `conversation_id` before
  dispatching a replacement. `claude_code` is hard-stopped; `codex` cancellation
  is best-effort until its runner-side hard-stop exists.
- A sub-agent that returns a dark or failing result: don't re-prompt it in a
  loop — re-dispatch a fresh implementation sub-agent in a clean worktree, or
  escalate to the user.
- Integration (step 5) is where non-disjoint task slices bite: two tasks that
  touched the same files conflict in YOUR tree, before the human ever sees the
  PRs. That is the point — it is the early warning. Keeping each parallel task's
  file scope disjoint is what keeps it rare; honor it.
- When `codegraph__*` tools are available and the repo has a `.codegraph/`
  index, check disjointness BEFORE dispatching: run
  `codegraph__codegraph_impact` on each task's core symbols — two slices whose
  impact sets share files should be split differently or serialized, and the
  affected-tests output belongs in each task's acceptance contract.
