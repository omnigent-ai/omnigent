---
name: triage
description: Build a static-DAG workflow that processes a backlog (bug reports, support tickets, incidents) — classify each item, dedupe against what's already tracked, and act (fix, file, or escalate). Uses a quarantine boundary so agents reading untrusted content can't take privileged actions.
---

# triage — classify, dedupe, act at scale

Use for a backlog no human can fully process: bug reports, a support queue,
incident threads, failing checks. The workflow classifies each item, dedupes
it against what's already tracked, and takes the right action — attempt a fix,
file a ticket, or escalate to a human.

## Shape

```
classify_1 ─┐
classify_2 ─┼─→ dedupe (barrier: needs the full set) ─→ act_1
 ...        │                                          act_2  (per surviving item)
classify_N ─┘                                          ...
```

1. **Classify** — one node per backlog item (or per batch), `role: generic`.
   Contract: categorize the item (type, severity, component) and extract a
   stable dedupe key. `output_schema`: `{category, severity, key, summary}`.
2. **Dedupe** — a barrier node depending on all classifiers: it needs the full
   set at once to collapse duplicates and to drop items already tracked
   (query the tracker deterministically). Returns the list of items that
   actually need action.
3. **Act** — per surviving item, a node that does the right thing for its
   category: a fixable bug → an `implement` node in a worktree (then
   [[cross-review]]); a real-but-unowned issue → file a ticket; ambiguous or
   high-risk → escalate to the human via the plan gate.

## Quarantine — the security boundary

Backlog items often contain **untrusted, attacker-influenceable text** (a bug
report body, a support message, a public incident thread). Prompt-injection
risk is real, so split privilege:

- **Reader/classifier nodes** consume the untrusted content but are granted NO
  high-privilege actions — no shell that mutates state, no ticket writes, no
  merges. They only read and emit structured classifications.
- **Actor nodes** take privileged actions but operate on the *structured,
  sanitized* output of the dedupe step — never on the raw untrusted text.

State this explicitly in each node's contract. The boundary means a malicious
ticket body can at worst produce a bad classification, never trigger a
privileged action directly.

## Notes

- Pair with `/loop` to run triage continuously on a standing queue, and with
  `/goal` for a hard completion requirement ("don't stop until the queue is
  empty").
- polly still never merges — a fix produces a PR for a human (see [[fanout]]).
- The dedupe barrier is the one place a join is genuinely required here (it
  needs every classification to collapse duplicates); classify and act
  otherwise fan out freely.
- Composes with [[tiered-models]] (cheap classifiers, capable actors) and
  [[adversarial-implement]] (refute attempted fixes). Mechanics live in
  `WORKFLOWS_V0.md`.
