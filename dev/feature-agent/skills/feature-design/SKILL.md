---
name: feature-design
description: Choose the design, record the alternatives and CUJ coverage, or stop when a choice needs a human.
---

# Design

Read the code each requirement touches before choosing. Then record, for the PR
and the handoff's `design` and `cujs` fields:

- **Chosen design:** where it plugs in (layers, APIs, configuration, flags) and
  how each requirement is met. Follow the repository's existing patterns and
  `CLAUDE.md`.
- **Alternatives considered:** each realistic alternative and why you rejected
  it. One or two well-argued alternatives beat a long list.
- **Risks:** compatibility, migration, performance, and security concerns, and
  how the design handles them.
- **CUJ coverage:** mark each CUJ covered or not covered. Every not-covered CUJ
  gets a reason (out of scope, needs a separate feature, blocked by a missing
  capability). Cover at least one CUJ.

## Decide, or stop for a human

Make product and technical choices yourself when the ticket, code, docs, or
existing conventions support one. Write the choice and the alternatives down;
reviewers judge them on the PR.

Stop with `needs_more_info` **before implementation** only when a choice really
needs a human decision, for example:

- a product-design question the ticket leaves open and the product gives no
  basis to settle;
- a technical choice that is hard to reverse, such as a public API, data model,
  or stored-format change, where the options trade off differently and nothing
  in the ticket or code favors one;
- a ticket that contradicts existing behavior other users depend on.

In that case `ticket_comment` lays out the options, their tradeoffs, and your
recommendation, so a human can answer on the ticket and rerun the agent. Small
or easily reversed choices never justify stopping.

Then load `feature-implement`.
