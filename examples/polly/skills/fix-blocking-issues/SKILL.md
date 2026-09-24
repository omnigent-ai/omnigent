---
name: fix-blocking-issues
description: Identify blocking issues in a PR diff, fix them in isolated worktrees via implementer sub-agents, cross-review each fix, and open fix PRs for the human to merge.
---

# fix-blocking-issues — automated fix dispatch

Use when a human triggers `/fix` on a PR. Your job: identify the blocking
issues in the diff and description, fix them in isolated worktrees, and open
fix PRs. You never merge; the human does.

## What counts as blocking

Only issues that are actually present in the changed code AND fall into one
of these categories:
- Correctness bug (wrong logic, off-by-one, missed case)
- Broken contract (violates an explicit API or behaviour guarantee)
- Real security risk (injection, auth bypass, secret exposure, path traversal,
  etc.)

Do NOT fix non-blocking notes, style issues, or speculative concerns.
If no blocking issues exist in the diff, skip to step 5 (emit done sentinel).

## Procedure

1. **Identify blocking issues** — read the full diff and PR description.
   List only genuine blocking issues with file:line evidence. Double-check
   each: does the problem actually exist in the diff, not just nearby or in
   the surrounding context?

2. **Plan fix tasks** — group related issues into coherent tasks. Prefer one
   task covering all issues unless they touch completely disjoint areas (no
   shared files, no ordering dependency). One worktree per task.

3. **Dispatch implementers** — for each task:
   `sys_os_shell("git worktree add .worktrees/<task_id> -b polly/fix/<task_id>")`,
   then `sys_session_send(agent="claude_code"|"codex", title="fix-<slug>",
   args={purpose: "implement", input: "<diff slice> + <acceptance contract> +
   worktree path. Stay strictly within .worktrees/<task_id>. Drive to green
   (tests/lint/typecheck), push your branch, open a PR."})`.
   Emit all worktree creates and dispatches in the SAME turn — never announce
   then yield. Record each `conversation_id` in the registry.

4. **Cross-review each fix PR** via the `cross-review` skill: a different-vendor
   reviewer judges the fix diff against its contract. Blocking issues in the
   fix loop back to the implementer in the same worktree/branch.

5. **Emit the done sentinel** when all fix PRs are open and cross-review is
   clean — or immediately if there were no blocking issues:

   ```
   <!-- POLLY_FIX_DONE -->
   <one fix PR URL per line, or "No blocking issues found — nothing to fix.">
   ```

   Nothing before the sentinel will be shown in the PR comment.

## Notes
- Use short, task-based worktree names: `fix-null-check`, `fix-auth-bypass`.
  Never `claude_code`, `codex`, `pi`, or other vendor names.
- Every commit the implementer authors must end with a blank line followed by
  the co-sign trailer as its final line:
  `Co-authored-by: omnigent <noreply@omnigent.ai>`
- Never run `git merge` or `gh pr merge` — PRs are the deliverable.
- Remove a finished worktree only after its PR is open and cross-review is
  clean: `git worktree remove .worktrees/<task_id>`.
