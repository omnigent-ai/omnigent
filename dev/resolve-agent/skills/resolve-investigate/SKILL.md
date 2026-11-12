---
name: resolve-investigate
description: Verify the reported path, cause, and historical intent before choosing a fix.
---

# Establish the cause and intended behavior

Use this in every mode before choosing assertions or changing code. Keep the
investigation proportional to the reported problem; reuse evidence that still
matches the checkout. Record concise findings in the existing `root_cause`,
`fix_summary`, and `test_audit` fields, not a separate report.

Before editing product code, complete the applicable checks below and record
their commands and observations. A plan to check later is not evidence. When a
check cannot run, name the concrete blocker and keep the cause unresolved.

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
   Exercise the reported configuration through the implementation being
   diagnosed. Controlled external services, clocks, and credentials are useful
   substitutes; replacing the suspected component with canned results assumes
   the cause instead of testing it. For example, a fake token factory proves how
   its caller handles those tokens, not how the configured credential provider
   behaves. Record which parts actually ran and which were substituted. A
   fail-before/pass-after test of that substitute does not confirm the incident.
   For credential-lifetime or recovery hypotheses, first establish a working
   control through the reported credential-selection code and SDK. When external
   auth services are unavailable or sanitized, substitute only boundaries this
   provider uses, with bounded local services and fake credentials. Static
   credentials need no invented issuance or refresh service. Keep application
   token resolution and the installed SDK intact. Record which provider actually
   returns the credential, including any fallback, using its runtime type or call
   trace. Starting SDK configuration or discovery does not prove SDK-provider
   success. Bound discovery and subprocess calls with timeouts. Get credential
   resolution working on its own before adding
   the host/reconnect loop. If initialization hangs, inspect the blocking stack.
   If it identifies external configuration metadata/discovery calls, provide
   controlled responses and record whether the control reached them. Preserve
   other concrete blockers. Load an isolated reported profile and use its credential
   at the caller's transport boundary. Keep the same application credential
   object while exercising the suspected expiry or rejection; record whether it
   refreshes or recovers before proposing a cache reset. Mocking that object or
   its factory cannot satisfy this check. Retain the controlled boundaries and
   remaining incident uncertainty.
   If you change this path, rerun the same configured control and discriminator
   on the candidate. A new mock-only regression test cannot verify recovery
   through the implementation you diagnosed; limit the fix claim accordingly.
3. **Find the design intent.** Read nearby tests and documentation, targeted
   `git log -S`/`git blame`, and relevant commit/PR discussion. Cite the source
   and revision; distinguish documented rationale from inference and note
   unavailable history. A fallback, fail-open path, default, or restriction may
   be deliberate. Do not reverse it just to satisfy a repro assertion. Check
   whether later decisions supersede the original rationale.
   Follow the reported path to any shared registry, default, or policy definition
   that governs the behavior, then read its rationale, relevant callers, and
   tests. Do not infer the full contract from one local fallback.
   Even when the local fallback is explicit, follow the phase or operation type
   to its declaration and inspect other consumers for a shared table or default
   controlling error behavior. Cite that declaration, or a bounded search if absent.
   Follow imported policy symbols to their defining files. Search for the
   declaration and other consumers; a filename in search output or a symbol in
   a caller is not a definition read. Record the controlling symbol/value,
   definition path, historical rationale, and pinning test in `test_audit`.
   If there is no shared definition, record the search that established that.
4. **Choose and explain.** Preserve intended behavior when correcting the
   defect. For a necessary policy change, explain the old behavior, the proposed
   behavior, affected users/configurations, alternatives, and supporting
   evidence. Existing tests are evidence of intent, not unquestionable policy.
   Repair unsupported repro expectations with the original evidence preserved;
   label tests of a proposed policy as such, not proof the old policy was a bug.
   Before adding a retry, trace the retried operation's state writes and external
   effects. If there are none, record the inspected read-only path. Otherwise,
   test failure after an effect commits but before success reaches the caller;
   retry counts alone do not show safety. Preserve existing behavior
   when the proposed retry lacks that evidence, and record the unsupported
   proposal as remaining work.

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

Apply the same distinction to an unverified cause. If a proposed fix is supported
only by a substitute configuration or canned failure, describe it as a hypothesis,
use `partially_fixed`, and name the missing discriminator in `remaining_work`.
When a reliable reproduction cannot be established, use the existing
`needs_more_info` outcome. Passing tests that assume the cause do not justify
`fixed`.

Concrete blockers still apply: missing required inputs or credentials, unsafe
evidence, an unrecoverable verification environment, conflicting authoritative
requests, or work outside authorization. Name the blocker and complete independent
authorized work. Never weaken security controls, guess missing facts, silently
expand scope, or claim an unrun check passed to get a proposal through review.
