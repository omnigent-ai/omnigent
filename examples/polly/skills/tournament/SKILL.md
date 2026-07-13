---
name: tournament
description: Build a static-DAG workflow that picks a winner among options by pairwise comparison instead of absolute scoring — for taste/selection tasks (naming, design, ranking) where comparative judgment is more reliable. Fixed bracket in one DAG; larger fields span turns.
---

# tournament — compete, don't divide

Some tasks are decided by competition, not decomposition: choosing a name,
picking a design direction, ranking a set by a qualitative measure. Comparative
judgment ("A vs B, which is better?") is more reliable than absolute scoring
("rate A 1–10"), so a tournament of pairwise-compare agents beats one agent
scoring everything — and it keeps each comparison in its own clean context
instead of degrading as one prompt tries to rank 100 items.

Use for: naming, design/taste exploration, ranking support tickets by severity,
sorting a list by a qualitative rubric, selecting the best of several attempts.

## Shape (fixed bracket, one DAG)

For a known, small field (e.g. 4 or 8 contenders), the whole bracket is a
static DAG:

```
        compare(A,B) ─┐
                      ├─ compare(winnerAB, winnerCD) ─→ champion
        compare(C,D) ─┘
```

1. Optionally, **generate** nodes first produce the contenders (see the
   generate-and-filter variant of [[fanout-synthesize]]).
2. **Round-1 compare** nodes: one `review` node per pair. Contract: *"Compare
   these two against this rubric; return the winner and why."* `output_schema`:
   `{winner, reason}`. State the rubric explicitly and identically in every
   comparison node so judgments are consistent.
3. **Later-round** compare nodes `deps` on the prior round's nodes. Because the
   bracket structure is fixed for a known field size, the entire tree is one
   submission.
4. The final node emits the champion (and, for ranking, the full order).

## Larger or unknown fields span turns

The DAG is acyclic and fixed at submit, so a bracket whose later pairings
depend on earlier *winners* that aren't known until runtime cannot be a single
DAG beyond the fixed structure above. For a large field:

- **Bucket-rank then merge:** fan out — each node ranks a small bucket in
  parallel (one DAG) — then a synthesis node merges bucket winners. One
  submission, no runtime branching.
- **Round-by-round across turns:** submit round 1, read the winners on the
  wake, then submit round 2 seeded with them, and so on. The deterministic loop
  (which winners advance) lives in your turn logic; only the current round's
  comparisons are in the DAG.

Say which you're doing when you present the plan — don't imply a 64-way live
bracket runs as one DAG.

## Notes

- Pairwise > absolute: prefer "A vs B" nodes over "score each" nodes.
- Keep the rubric identical across all comparisons; drift in the rubric is the
  main source of inconsistent brackets.
- Ask the human to confirm the rubric at the plan gate for taste tasks — the
  rubric IS the task.
- Composes with [[fanout-synthesize]] (bucket-rank) and [[tiered-models]]
  (cheap generators, capable judges). Mechanics live in `WORKFLOWS_V0.md`.
