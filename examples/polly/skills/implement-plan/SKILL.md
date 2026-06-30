---
name: implement-plan
description: Worker-side implementation discipline for Polly implement tasks. Follow when dispatched with purpose "implement", especially for plan-backed or U-unit work.
---

# implement-plan

Use this inside a Polly implementation worker. Polly handles orchestration,
worktrees, registry state, PR routing, and cross-review. Your job is to ship
the assigned implementation slice inside the worktree Polly gave you.

## 1. Lock The Work Source

Resolve the task source before editing:

- If Polly supplied a plan path, read it and treat it as the authority.
- If Polly supplied a task packet with acceptance criteria, treat that packet as
  the authority.
- If the packet references implementation units, preserve their IDs in your
  task notes and final report.
- If scope is ambiguous enough to change behavior, stop and report the blocking
  question to Polly instead of guessing.

Completion criterion: you can state the behavior to ship, what is out of
scope, and how you will verify it.

## 2. Read The Contract

For plan-driven work, read only the sections needed for execution:

- product contract, requirements, and acceptance criteria
- non-goals and scope boundaries
- implementation units and file hints
- verification commands
- risks, open questions, and deferred decisions

Do not rewrite the plan during implementation unless Polly explicitly assigned
that as part of the task. Progress belongs in commits, the PR, and your final
worker report.

## 3. Confirm Worktree State

Before editing:

1. Run `git branch --show-current`.
2. Run `git status --short`.
3. Confirm you are in Polly's assigned worktree and branch.
4. Do not touch files outside the assigned scope unless the plan requires it.
5. Do not revert unrelated changes unless Polly explicitly told you they are
   yours to replace.

Completion criterion: the branch and dirty state are understood before edits.

## 4. Slice The Work

For non-trivial work, keep a short local checklist in your own reasoning or
native task tracker:

- derive tasks from implementation units when present
- include test discovery and verification tasks
- keep one slice in progress at a time
- mark slices complete only after focused verification passes

Completion criterion: your work can be traced back to the task packet or plan.

## 5. Implement In Thin Slices

For each slice:

1. Read the relevant files before editing.
2. Search for existing helpers, tests, and local patterns.
3. If the requested behavior already exists, verify it and report that.
4. Add or update tests for behavior changes.
5. Make the smallest coherent change that satisfies the slice.
6. Run focused verification for the touched area.
7. Fix failures before moving to the next slice.

Use diagnosis discipline when the task becomes an unknown failure investigation
instead of straightforward implementation.

## 6. Discover Tests Before Editing

Before changing behavior in a file, find relevant tests:

- tests named in the plan or task packet
- adjacent tests
- tests importing or referencing the changed module/component
- integration tests when behavior crosses UI, API, store, hook, CLI, or
  persistence boundaries

If you do not add a test for behavior-bearing work, explain why in the final
worker report and PR body.

## 7. Commit And PR Rules

Polly implementers are expected to create the deliverable branch and PR.

- Stage only files for this task.
- Do not use `git add .`.
- Commit only after relevant checks pass.
- Use a meaningful commit message, never `WIP`.
- End every commit message with a blank line followed by:
  `Co-authored-by: omnigent <noreply@omnigent.ai>`
- Push only your task branch.
- Open a PR with what changed and how it was verified.
- Never merge the PR.

## 8. Quality Tail

Before reporting done:

1. Run the plan's verification commands, or the nearest focused repo checks.
2. Broaden to build, typecheck, lint, or wider tests when the slice touches
   shared behavior or user-facing flows.
3. Re-check `git status --short`.
4. Make sure untracked files that belong to the task are included.

Completion criterion: the diff is reviewable, verification is recorded, and
remaining risks are explicit.

## 9. Worker Report

Return a concise structured report to Polly:

- work source: plan path, issue, or task packet
- shipped behavior, tied to implementation unit IDs when present
- changed files
- PR URL
- verification run and result
- tests not run, with reason
- remaining risks or follow-ups

Do not claim the whole plan is complete unless every acceptance criterion
assigned to you is satisfied.
