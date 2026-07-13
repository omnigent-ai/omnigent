---
name: fanout-synthesize
description: Build a static-DAG workflow that splits a task into many small independent steps, runs one agent per step in parallel, and merges their structured outputs in a single synthesis node. Includes the generate-and-filter variant.
---

# fanout-synthesize — decompose, parallelize, merge

The workhorse pattern: when a task is many small independent steps, or when
each step benefits from its own clean context so they don't interfere, run one
agent per step and merge the results. The synthesis node is a **barrier** — it
waits for every fan-out node, then combines their structured outputs into one
result.

Use this when the steps are read-mostly or produce data (summaries, ratings,
extracted facts, per-item analyses). For steps that WRITE code in parallel,
use [[adversarial-implement]] instead (it adds worktrees + refuters).

## Shape

```
step_1 ─┐
step_2 ─┤
step_3 ─┼─→ synthesize (depends on all steps)
 ...    │
step_N ─┘
```

1. Decompose into N independent steps. One `generic` or `investigate` node per
   step, each with a self-contained contract and an `output_schema` that makes
   the result structured (so synthesis merges data, not prose).
2. One synthesis node that `deps` on all step nodes. Its contract: *"Here are
   the N structured results. Merge them into <the deliverable>; note conflicts
   and gaps."* Give it its own `output_schema`.
3. Size `budget.max_concurrency` to how many steps should run at once.

## Variant: generate-and-filter

For "brainstorm a bunch of options and return only the best":

```
generate_1 ─┐
generate_2 ─┼─→ dedupe_and_rank ─→ verify_top_k (optional)
generate_3 ─┘
```

1. Several `generic` **generate** nodes, each told to approach the problem from
   a different angle so the pool is diverse, not N copies of one idea.
2. A **filter** node depending on all generators: dedupe near-duplicates, rank
   by an explicit rubric stated in its contract, and return the top K.
3. Optionally, `review` nodes that verify each of the top K survives scrutiny
   before returning — generate-and-filter plus [[adversarial-implement]]'s
   refuter discipline.

## Notes

- Structured `output_schema` on the fan-out nodes is what makes the barrier
  useful — the synthesizer merges typed fields, not free text.
- A barrier is only worth it when synthesis genuinely needs ALL step results
  together (dedupe across the full set, an aggregate ranking, "0 results →
  skip"). If a step's output can be used the moment it lands with no
  cross-step dependency, you don't need the join — just fan out.
- Keep step contracts narrow; a step that's too broad defeats the clean-context
  benefit. Split it into more steps instead.
- Node/dep/budget mechanics and the submit → present → start → wake lifecycle
  live in `WORKFLOWS_V0.md`.
