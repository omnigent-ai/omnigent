---
name: grill-with-docs
description: A relentless one-question-at-a-time interview that sharpens a plan or design and writes the decisions down as a CONTEXT.md glossary and docs/adr/ ADRs. Use when the user wants their thinking stress-tested rather than two answers side by side, or says grill me / grill this / stress-test this plan.
---

# grill-with-docs — interview the human, write the decisions down

Load this only when the user asks for it — upstream marks it user-invoked
only, and it should never fire on its own mid-conversation.

Ported from Matt Pocock's `grill-with-docs` skill — MIT, © 2026 Matt Pocock —
[`mattpocock/skills`](https://github.com/mattpocock/skills), path
`skills/engineering/grill-with-docs/`, pinned at commit
[`658d53e`](https://github.com/mattpocock/skills/blob/658d53e6ded8cc0eaa26a96e0580bee9381ca0e3/skills/engineering/grill-with-docs/SKILL.md).
The upstream MIT permission notice is retained verbatim in
[`LICENSE`](./LICENSE) beside this file.

Upstream this skill is a one-line composition — *"Run a `/grilling` session,
using the `/domain-modeling` skill."* — over two sibling skills. Neither
sibling ships in this bundle, so both are inlined verbatim below and this
skill stands alone. The only edits are the two format links, repointed at
`references/`, and the final adaptation section, which is ours.

## 1. The interview — upstream `grilling`, verbatim

Interview me relentlessly about every aspect of this until we reach a shared
understanding. Walk down each branch of the decision tree, resolving
dependencies between decisions one-by-one. For each question, provide your
recommended answer.

Ask the questions one at a time, waiting for feedback on each question before
continuing. Asking multiple questions at once is bewildering.

If a *fact* can be found by exploring the environment (filesystem, tools,
etc.), look it up rather than asking me. The *decisions*, though, are mine —
put each one to me and wait for my answer.

Do not act on it until I confirm we have reached a shared understanding.

## 2. The docs — upstream `domain-modeling`, verbatim

Actively build and sharpen the project's domain model as you design. This is
the *active* discipline — challenging terms, inventing edge-case scenarios,
and writing the glossary and decisions down the moment they crystallise.
(Merely *reading* `CONTEXT.md` for vocabulary is not this skill — that's a
one-line habit any skill can do. This skill is for when you're changing the
model, not just consuming it.)

### File structure

Most repos have a single context:

```
/
├── CONTEXT.md
├── docs/
│   └── adr/
│       ├── 0001-event-sourced-orders.md
│       └── 0002-postgres-for-write-model.md
└── src/
```

If a `CONTEXT-MAP.md` exists at the root, the repo has multiple contexts. The
map points to where each one lives:

```
/
├── CONTEXT-MAP.md
├── docs/
│   └── adr/                          ← system-wide decisions
├── src/
│   ├── ordering/
│   │   ├── CONTEXT.md
│   │   └── docs/adr/                 ← context-specific decisions
│   └── billing/
│       ├── CONTEXT.md
│       └── docs/adr/
```

Create files lazily — only when you have something to write. If no
`CONTEXT.md` exists, create one when the first term is resolved. If no
`docs/adr/` exists, create it when the first ADR is needed.

### During the session

#### Challenge against the glossary

When the user uses a term that conflicts with the existing language in
`CONTEXT.md`, call it out immediately. "Your glossary defines 'cancellation'
as X, but you seem to mean Y — which is it?"

#### Sharpen fuzzy language

When the user uses vague or overloaded terms, propose a precise canonical
term. "You're saying 'account' — do you mean the Customer or the User? Those
are different things."

#### Discuss concrete scenarios

When domain relationships are being discussed, stress-test them with specific
scenarios. Invent scenarios that probe edge cases and force the user to be
precise about the boundaries between concepts.

#### Cross-reference with code

When the user states how something works, check whether the code agrees. If
you find a contradiction, surface it: "Your code cancels entire Orders, but
you just said partial cancellation is possible — which is right?"

#### Update CONTEXT.md inline

When a term is resolved, update `CONTEXT.md` right there. Don't batch these
up — capture them as they happen. Use the format in
[references/CONTEXT-FORMAT.md](./references/CONTEXT-FORMAT.md).

`CONTEXT.md` should be totally devoid of implementation details. Do not treat
`CONTEXT.md` as a spec, a scratch pad, or a repository for implementation
decisions. It is a glossary and nothing else.

#### Offer ADRs sparingly

Only offer to create an ADR when all three are true:

1. **Hard to reverse** — the cost of changing your mind later is meaningful
2. **Surprising without context** — a future reader will wonder "why did they
   do it this way?"
3. **The result of a real trade-off** — there were genuine alternatives and
   you picked one for specific reasons

If any of the three is missing, skip the ADR. Use the format in
[references/ADR-FORMAT.md](./references/ADR-FORMAT.md).

## 3. In debby — how this composes with the two heads

**Suspend the reflex fan-out.** Your default is to send every substantive
question to both `claude` and `gpt` and show both answers. Under this skill
that default is off. The interview is yours: you ask, one question at a time,
and you wait. Fanning each interview question out to both heads buries the
user in two essays when what they owe you is one decision.

**Consult the heads deliberately, at three moments:**
- *Mapping the tree* — before the first question, you may dispatch both heads
  once to enumerate the decisions and dependencies worth walking, so you grill
  from two maps rather than one.
- *When the user is genuinely torn* — if they ask for both views on a live
  decision, dispatch it to both, then show the 🟠 / 🔵 split as the framing for
  that single question.
- *When a fact needs real digging* — the partners have their own filesystem
  access; ask one rather than stalling the interview.

Dispatch both in the same turn with distinct per-partner titles
(`grill-<topic>-claude` / `grill-<topic>-gpt`), END your turn, and collect with
a single `sys_read_inbox`. Never poll, never `sys_timer_set`.

**You still owe a recommended answer on every question.** Give one in your own
voice. If the two heads disagreed, say where in one line — then still
recommend. A moderator who refuses to pick is not grilling.

**Writing the docs is yours.** You have `os_env`: write `CONTEXT.md` and
`docs/adr/NNNN-slug.md` yourself with `sys_os_write` / `sys_os_edit` as terms
and decisions crystallise. Never delegate a doc write to a partner, and never
batch them to the end.

**Stop at the gate.** Do not act on the plan — no implementation, no fan-out
for "let's just try it" — until the user confirms you have reached shared
understanding.
