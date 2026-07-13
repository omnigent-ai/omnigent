---
name: deep-research
description: Build a static-DAG workflow that researches a question by fanning out independent source-gathering agents, adversarially verifying each claim, and synthesizing one cited answer. Also covers the reverse — verifying every claim in an existing report.
---

# deep-research — gather, verify, synthesize

Use for a question that needs breadth AND rigor: gather evidence from several
independent angles, check each claim rather than trusting it, and produce one
answer whose every assertion is sourced. The DAG gives each gatherer and each
verifier its own clean context so findings don't cross-contaminate, and the
synthesis node is a true barrier — it waits for all verified evidence before
writing the answer.

Research here is not only web search. The same shape fits: exploring a
codebase in depth, compiling a status report from Slack/connector context, or
comparing several documents.

## Shape (research a question)

```
gather_docs   (pi/opencode)  ─┐
gather_code   (claude_code)  ─┼─→ verify_1 (refuter) ─┐
gather_data   (cursor)       ─┘   verify_2 (refuter) ─┴─→ synthesize (claude_code)
```

1. **Gather** — 2–4 `investigate` nodes, each a DIFFERENT lens/source (each
   blind to the others) so no single search angle is the only one tried.
   Contract: return findings with hard evidence — file:line, URL, quote,
   command output — never unsourced assertions.
2. **Verify** — one or more `review` nodes that `deps` on the gatherers,
   prompted adversarially: *"Here are the claims and their cited sources. For
   each, check the source actually supports the claim and is high-quality.
   Flag anything unsupported, stale, or misread."*
3. **Synthesize** — one node depending on the verifiers that writes the final
   answer using ONLY verified claims, each with its citation. Its
   `output_schema` should require `{answer, citations: [...]}`.

## Shape (verify an existing report / draft)

For "check every technical claim in this before I ship it":

1. One `investigate` node that extracts every checkable factual claim from the
   document into a list (its `output_schema`: `{claims: string[]}`).
2. Present that list, then amend/submit a second workflow (or a second phase)
   with one `review` node per claim, each verifying that single claim in
   detail against the codebase / sources. One claim per node keeps each
   context focused; a claim's node can itself spawn a source-quality check.
3. A synthesis node collects the per-claim verdicts into a pass/fail report so
   nothing ships wrong.

Because the claim count is unknown until step 1 finishes, this spans two
workflow submissions (extract, then verify) — the DAG is fixed at submit, so
you size the per-claim fan-out from the extracted list, not up front.

## Notes

- Keep gatherers narrow and independent; broad questions split into more
  parallel gather nodes, not bigger contracts.
- Prefer `pi` for a non-Claude/GPT lens when you want genuine model diversity
  in the evidence.
- Pair with `/loop` for a standing research digest (e.g. a recurring status
  report from a connector).
- Composes with [[fanout-synthesize]] (the generic gather→merge core) and
  [[adversarial-implement]] (same refuter discipline, applied to claims
  instead of code). Node/dep/budget mechanics live in `WORKFLOWS_V0.md`.
