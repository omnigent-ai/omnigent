---
name: plan-work
description: Select, slice, and track implementation from repo plans, especially docs/plans. Use when the user asks Polly to pick up an existing plan, work from docs/plans, or execute planned work.
---

# plan-work

Use this when Polly is asked to execute existing planned work. Polly owns plan
selection, task slicing, registry tracking, dispatch, and plan status updates.
Workers receive scoped task packets and follow `implement-plan`.

## 1. Resolve The Plan

Select exactly one source of work before dispatching:

- If the user named a plan path, read it and treat it as the authority.
- If the request is blank or says to pick up planned work, scan `docs/plans/`
  for candidate `.md` plans and choose the newest plausible implementation
  plan. If multiple candidates are plausible, ask the human which one to use.
- Skip plans marked complete, superseded, archived, or not implementation work.
- If no implementation-ready plan exists, stop and ask whether to write a plan
  first.

Completion criterion: one plan path is selected, or the human has the exact
blocking choice.

## 2. Read The Contract

Read only the plan sections needed to create task packets:

- goal, scope, non-goals, and assumptions
- requirements, product contract, or acceptance criteria
- implementation units and stable IDs
- dependencies and ordering constraints
- verification commands and test scenarios
- risks, blockers, and deferred decisions

Do not change product scope while preparing execution. If the plan has product
ambiguity that changes behavior, stop at the plan gate and ask the human.

Completion criterion: Polly can name the implementable units, sequencing, and
verification gates without inventing scope.

## 3. Track In The Registry

Create or update `.polly/registry.json` before dispatching.

For each implementation unit, record:

- plan path
- unit ID
- task slug
- acceptance contract
- dependencies
- status: `queued`, `running`, `reviewing`, `fixing`, `ready`, `blocked`
- worktree path and branch
- worker agent and conversation ID after dispatch
- PR URL when available
- verification summary when available
- blocking review findings
- follow-ups

The registry is the live task tracker. Do not put per-worker transient state in
the plan document.

Completion criterion: every planned worker task has a registry entry before it
is dispatched.

## 4. Update Plan Status

Plans carry durable high-level status only. Update the plan at these points:

- If Polly initializes tracking without dispatching yet, leave or mark it
  `not_started` and record the registry path.
- When execution starts, mark it `in_progress` and record the registry path.
- When Polly hits a hard blocker that needs the human, mark it `blocked` with a
  short blocker note.
- When every in-scope task has an open PR, deterministic gates are green, and
  cross-review has no blocking findings, mark it `complete` and list the PRs.

If the plan has YAML frontmatter, use these fields:

```yaml
polly_status: not_started
polly_registry: .polly/registry.json
polly_prs: []
polly_blocker: null
```

If the plan has no frontmatter, add or update a `## Polly Status` section near
the top with status, registry path, PRs, and blocker notes. Do not rewrite the
rest of the plan while tracking execution.

Completion criterion: the plan tells a reader whether execution has not started,
is running, is blocked, or is complete, while details remain in the registry.

Allowed plan status values are `not_started`, `in_progress`, `blocked`, and
`complete`. Do not use the plan as a task tracker. It stays portable and
reviewable, while `.polly/registry.json` owns execution progress.

## 5. Dispatch Work

For each parallel-safe unit:

1. Create a dedicated worktree and branch.
2. Dispatch an implement worker with `purpose: "implement"`.
3. Include the plan path, unit ID, acceptance contract, worktree path, and the
   exact instruction `follow implement-plan`.
4. Record the returned conversation ID in the registry.

Use `fanout` for independent units. Keep ordered or shared-file units in later
waves.

Completion criterion: every running worker has a scoped task packet, worktree,
branch, registry entry, and conversation ID.

## 6. Review And Close

When workers return:

1. Record PR URLs and verification summaries in the registry.
2. Run deterministic gates before model review.
3. Send each PR through `cross-review`.
4. Route blocking findings back to the original implementer conversation.
5. Mark registry entries `ready` only after gates and cross-review pass.
6. Update the plan to `complete` only when every in-scope unit is ready.

Do not merge PRs. The human merges.

Completion criterion: registry state, plan status, and PR readiness agree.
