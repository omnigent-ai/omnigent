---
name: tiered-models
description: Build a static-DAG workflow that spends intelligence where it pays — an expensive model plans, then cheaper models implement in parallel, then an independent model reviews. Matches model tier to node role via per-node agent + model.
---

# tiered-models — expensive plan, cheap implement, independent review

Not every node needs the strongest model. Planning and judging reward
intelligence; mechanical implementation of a well-specified plan often does
not. This recipe matches the model tier to the node's role using each node's
`agent` and `model` fields, so a run costs far less than putting the top model
on every node while keeping quality where it matters.

Typical tiering:

- **Plan** — strongest model (e.g. `claude_code` on an Opus-class model). Turns
  a fuzzy goal into a precise, decomposed spec with per-unit acceptance
  contracts.
- **Implement** — cheaper/faster models on the multi-model workers (`pi`,
  `opencode`), one node per unit, running in parallel. A tight spec makes a
  cheaper model reliable.
- **Review** — an independent, capable model (different vendor from the
  implementer) that verifies against the plan's contract.

## Shape

```
plan (claude_code, strong model)
      │  (produces per-unit contracts)
      ├─→ impl_1 (pi, cheap model)     ─→ review_1 (different vendor)
      ├─→ impl_2 (opencode, cheap model) ─→ review_2
      └─→ impl_3 (pi, cheap model)     ─→ review_3
```

## Procedure

1. **Resolve real model ids first.** Do NOT hard-code model names — call
   `sys_list_models` to see what each worker can actually run on THIS machine
   (available models differ per deployment/gateway). Pick a strong model for
   the plan node and a cheaper one for the implement nodes from that list. An
   invalid `agent`/`model` pair fails loud at dispatch, so verify before
   submitting.
2. **Plan node** — one node, strong model, `role: investigate` (or `generic`).
   Contract: decompose the goal into independent units, each with a crisp
   acceptance contract and file scope. Its `output_schema` should emit the
   unit list so you can read it back.
3. Because the unit list comes from the plan, the implement fan-out is sized
   AFTER the plan node returns: submit the plan as its own small workflow (or
   first phase), read its result, then submit the implement+review phase with
   one implement node per unit — each pinned to a cheaper `model`, each with
   its own `worktree_path`, depending conceptually on the plan.
4. **Implement nodes** — cheaper model, `role: implement`, parallel, each
   scoped to its worktree. Ban slow git/build commands inside them (see
   [[adversarial-implement]]) so they parallelize cleanly.
5. **Review nodes** — one per implement node, `deps` on it, DIFFERENT vendor,
   a capable model, `role: review`. Verify against the plan's contract only.

## Notes

- The win is spending tokens where reasoning happens (plan, review) and saving
  them on mechanical execution — ask for a token budget if cost matters.
- If a cheap implementer keeps failing review, that's usually an under-specified
  plan, not too-weak a model — tighten the plan contract before upgrading the
  implement model.
- Model routing can also be dynamic: a classifier node can research the task
  and recommend a tier. When intelligent routing is configured, `sys_advise_models`
  gives per-worker recommendations; when it returns `router_on: false`, pick
  tiers yourself from `sys_list_models`.
- Composes with [[adversarial-implement]] (swap single review for 2+ refuters)
  and [[tiered-models]]'s plan node is the natural front-end to
  [[fanout-synthesize]]. Mechanics live in `WORKFLOWS_V0.md`.
