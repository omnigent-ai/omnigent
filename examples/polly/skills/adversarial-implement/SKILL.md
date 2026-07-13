---
name: adversarial-implement
description: Build a static-DAG workflow that, for each unit of work, implements the change and then has 2+ INDEPENDENT refuter agents try to break it before it counts as done. Use for multi-unit coding where correctness matters more than speed.
---

# adversarial-implement — implement, then refute

Use when a coding task splits into several independent units (bugs to fix,
files to port, callsites to migrate, findings in a report) and each fix must
survive scrutiny before it counts as done. This is the "do the work → have
other agents attack it → apply" loop, expressed as a static DAG so the
scheduler runs the units in parallel and the refuters get their own clean
context windows.

Prefer this over plain `fanout` when a fix being *plausible* is not enough —
you want it *refuted and survived*. It combats self-preferential bias: the
implementer never certifies its own work; different-vendor refuters do.

## When NOT to use it

- A single unit of work → just `fanout` + `cross-review`, no DAG.
- Units share files or must land in order → they are not parallel-safe; a
  static DAG cannot serialize writes to the same file (WS2 conflict management
  is deferred). Sequence them across turns instead.

## Shape

For each unit, a `implement` node with two (or more) `review` refuter nodes
depending on it. Refuters are DIFFERENT vendors from the implementer and from
each other where possible — diversity catches failure modes redundancy can't.

```
unit A:  implement_a ─┬─ refute_a_1 (different vendor)
                      └─ refute_a_2 (third vendor)
unit B:  implement_b ─┬─ refute_b_1
                      └─ refute_b_2
...                    (all units run concurrently)
```

## Procedure

1. Enumerate the units (findings, files, callsites). One `implement` node per
   unit, each with a self-contained `contract` and its own worktree (set
   `worktree_path`, and create it first with
   `git worktree add .worktrees/<unit> -b polly/<unit>`).
2. For each `implement` node add 2+ `review` nodes that `deps` on it. Word the
   review contract adversarially: *"You are an independent refuter. Try to
   break this change — find a failing input, a broken invariant, a missed
   edge case, an untested path. Default to request_changes if uncertain."*
   Give each refuter ONLY the diff + the original contract, never the
   implementer's worktree or transcript.
3. **Ban slow commands inside parallel `implement` nodes** so many units can
   run at once without thrashing the machine or stepping on a shared branch:
   put in every implement contract *"Do NOT run git, cargo, npm build, or
   other slow/global commands — another agent runs concurrently. Edit files
   only; the workflow runs gates centrally."* Run tests/lint/build once, later,
   in a synthesis/gate node — not inside each parallel unit.
4. Set each node's `output_schema` so the refuter must return a verdict, e.g.
   `{verdict: "approve"|"request_changes", blocking: string[], summary}`. The
   contract requires ending with `<workflow_result>{...}</workflow_result>`.
5. Submit with `sys_workflow_submit`, present the graph + budget, then
   `sys_workflow_start` after approval. Set `budget.max_concurrency` to how
   many units the machine can run at once.
6. On the terminal wake, read `sys_workflow_get`. A unit "survives" only when
   its implement node succeeded AND a majority of its refuters returned
   `approve`. For units whose refuters found real blocking issues, send the
   concrete fixes back to the SAME implementer conversation (reuse its
   `agent` + `title`) via `sys_session_send` and re-review — the DAG's own
   retries reuse the child session, so a schema-valid-but-wrong result needs
   this manual fix loop, not a blind retry.

## Notes

- Two refuters is the default; use three for high-stakes changes. More than
  that rarely pays — most units do not need a panel of five.
- Refuters that all *agree it is fine* is the signal to apply; refuters that
  disagree with each other is a signal the unit is under-specified — tighten
  the contract, not the count.
- Composes with [[tiered-models]] (plan the units with a strong model, let a
  cheaper model implement) and [[cross-review]] (its fix-loop is the same
  implementer-reuse pattern used here in step 6).
- This is a workflow recipe; the mechanics of nodes, deps, budgets, and the
  submit → present → start → wake lifecycle live in `WORKFLOWS_V0.md`.
