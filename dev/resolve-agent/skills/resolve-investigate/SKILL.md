---
name: resolve-investigate
description: Verify the reported path, cause, and historical intent before choosing a fix.
---

# Establish the cause and intended behavior

Use this in every mode before choosing assertions or changing code. Keep the
investigation proportional to the reported problem; reuse evidence that still
matches the checkout. Record concise findings in the existing `root_cause`,
`fix_summary`, and `test_audit` fields, not a separate report.

1. **Reconstruct the report.** Read the report and relevant discussion, including
   linked evidence. Separate reported facts, observations, and hypotheses. Pin
   the entry point, build, harness, authentication mode/profile, configuration,
   and starting state. Mark unknowns; do not silently swap an OAuth user login
   for a service principal or PAT because they produce similar errors.
2. **Distinguish symptom from cause.** Trace the actual path from trigger to
   failure. Treat the reporter's diagnosis, Repro leads, and proposed fixes as
   hypotheses. Check the strongest competing explanation suggested by code,
   logs, or history with an observation or focused test that distinguishes it.
   Explain why the selected cause accounts for the reported configuration and
   symptom. If no credible alternative emerges, say so briefly; do not invent
   one or implement a plausible generic fix without evidence.
3. **Find the design intent.** Read nearby tests and documentation, targeted
   `git log -S`/`git blame`, and relevant commit/PR discussion. Cite the source
   and revision; distinguish documented rationale from inference and note
   unavailable history. A fallback, fail-open path, default, or restriction may
   be deliberate. Do not reverse it just to satisfy a repro assertion. Check
   whether later decisions supersede the original rationale.
4. **Choose and explain.** Preserve intended behavior when correcting the
   defect. For a necessary policy change, explain the old behavior, the proposed
   behavior, affected users/configurations, alternatives, and supporting
   evidence. Existing tests are evidence of intent, not unquestionable policy.
   Repair unsupported repro expectations with the original evidence preserved;
   label tests of a proposed policy as such, not proof the old policy was a bug.

## PR review is the human decision boundary

Continue with the best-supported proposal within the authorized task. Ambiguous
design intent alone is not `needs_more_info` and does not require an interactive
question. Implement and validate the proposal; state assumptions and unresolved
tradeoffs in the PR and `fix_summary` so a maintainer can decide on concrete work.

If a material choice remains unresolved, use `partially_fixed` and list that
choice in `remaining_work`; do not claim `fixed` or approve the PR. In direct
author mode, open a draft with `--draft` and follow `resolve-publish`'s draft
exception. For an existing PR, leave the supported recommendation on that PR,
continue independent work, and respect branch ownership and trusted human
reviews; do not create a replacement just to resolve a policy disagreement.
For workflow-owned publication, prepare the proposal body and incomplete handoff
under the supplied publisher contract; never bypass it or invent a draft flag.
For `skip_push`, leave the proposal in the local commit and handoff.

Concrete blockers still apply: missing required inputs or credentials, unsafe
evidence, an unrecoverable verification environment, conflicting authoritative
requests, or work outside authorization. Name the blocker and complete independent
authorized work. Never weaken security controls, guess missing facts, silently
expand scope, or claim an unrun check passed to get a proposal through review.
