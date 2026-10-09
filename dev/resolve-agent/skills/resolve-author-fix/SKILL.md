---
name: resolve-author-fix
description: Find the root cause, implement a focused fix, and prove behavior with targeted tests and recordings.
---

## Step 2B — Author the fix

No candidate PR exists, so you fix it yourself. Steps 2B.1–2B.5 below are the full
author flow; then open a PR in Step 3.

### 2B.1 — Confirm the shared repro audit

Complete the shared repro audit before changing product code. Reuse its recorded
behavioral baseline rather than trusting the recovered verdict or rerunning an
unchanged audit. If the test, base, or relevant environment changes, repeat the
audit. Ticket-only mode instead establishes its targeted fail→pass proof in 2B.4.
The failure-quality checks below elaborate the shared requirement; they do not
replace patch inspection or excuse the review path from the same audit.

It **must fail because the buggy behavior is observed** — a wrong value, an error
toast, a traceback, a bad HTTP response, a missing/incorrect UI affordance.

It **must not** fail merely because it references something that does not exist
yet — an `AttributeError`/`ImportError` on a symbol the fix would add, an
element-not-found for UI the fix would introduce, a 404 on a route the fix would
register. That is an **existence-check**, not a reproduction: it would go green
the moment the symbol exists, regardless of whether the behavior is correct. If
the test fails that way:

- **Rewrite it into a behavioral assertion** that exercises the real journey and
  asserts the correct *behavior/value*, and confirm the rewrite fails for the
  right reason before proceeding.
- **Flag it loudly** in your handoff (`test_audit`) so a reviewer knows the
  original repro test was an existence-check and you corrected it.

**If the test PASSES on the unfixed tree, it may be stale or unreliable; do not
assume `main` has fixed the bug.** A recovered verdict is a statement
about main AT REPRO TIME, not now. Verify the way repro-agent would: re-drive
enough of the journey to confirm the behavior is genuinely correct on the
current tree, and hunt for the fixing commit (`git log` on the code the
evidence points at). When it is really fixed, do not manufacture work: stop
with outcome `nothing_to_fix`, name the fixing commit in `root_cause`, and
recommend closing the ticket in your prose summary. If the test passes but the
journey still misbehaves, the test was too loose — treat it like the
existence-check case above: rewrite it until it fails on the real, still-live
behavior, and flag the rewrite in `test_audit`.

For a **compound** bug, do this for **every facet whose verdict is `reproduced`**.
Facets already `already_fixed` need no transition (note them skipped). Record, per
live facet, the **exact fail reason** — the "from" half of your fail→pass proof.

### 2B.2 — Root-cause

Apply `resolve-investigate`: match the reported path and configuration, check
competing explanations, and find the historical rationale. State the supported
cause before editing. If policy must change, distinguish that proposal from
repairing an implementation defect and retain unresolved choices for PR review.

### 2B.3 — Implement the fix

Fix the root cause, not the symptom. Change the code the bug lives in, matching
surrounding conventions, as small as the root cause allows. Do not touch the test
to make it pass; the *code* must change to satisfy it.

### 2B.4 — Select permanent regression coverage

Choose which tests belong in the final PR. Search existing tests by behavior
and fixture, and read the nearest scenarios. If one already drives the relevant
setup and state transition, fold the missing assertions into it; parameterize
configuration variants when useful. Apply this to recovered Repro tests too:
archive the original, then consolidate its regression assertions into the
existing scenario. Keep a separate test when a distinct ordering or boundary
would make that extension misleading. Keep investigation history in evidence
and test comments short. Reuse unchanged coverage when sufficient; there is no
requirement for a new test file or both a new e2e and a smaller test.

When existing coverage is insufficient, start with an edit to the nearest
compatible scenario: preserve its existing assertions and add the regression
input, seed records, or missing assertion. Leave sufficient coverage unchanged.
Needing richer data does not make its journey incompatible. Keep a separate
scenario only for a concrete ordering, lifecycle, or isolation conflict; name
that conflict and the nearest existing test in `test_audit`. Before final
verification, compare the setups and consolidate any compatible duplication.
Moving a reproduction into an existing file is not consolidation.

Each reported facet needs reliable coverage, not coverage at every layer.
Keep the input and edge-case matrix at the lowest reliable layer. At a higher
layer, use a representative regression input for each distinct boundary the
lower tests cannot expose; do not replay the whole matrix there. A new helper
does not automatically need direct unit tests when its real callers already
exercise its contract. Retain a helper test only for behavior those caller
checks miss. For every added layer, briefly identify the regression that would
escape the other selected checks if that layer were omitted.

Retain an e2e when it protects a distinct production boundary that lower-level
coverage would miss, and explain that boundary briefly in `test_audit`. Do not
mock away the failure, skip configurations, or supply already-correct objects
in place of testing serialization, startup, process, or browser wiring.
For documentation/instruction-only changes, existing contract/bundle checks
may suffice; do not add a standalone module of sentence assertions or fabricate
a behavioral failure.

Preserve investigation-only reproduction source and logs before omitting a
test introduced for this task from the final diff. Do not delete existing
repository coverage just to reduce LOC. Stage evidence in
`.omnigent/repro-evidence/` with original paths, commands, exact tested revision,
and results, or cite an intact CI repro baseline/bundle. This worktree-local
directory alone does not survive worktree deletion. Record the retrieval
location and retention status in `test_audit`:

- Under the compatible internal Resolve workflow, evidence is captured as
  `repro-evidence/` inside the GitHub Actions artifact `resolve-bundle-<run-id>`.
  This includes ticket-only and `skip_push` runs; it needs no upstream Repro
  bundle. Record the workflow run URL, artifact name, and path within it. The
  workflow uploads after the session, so mark that upload pending until it is
  confirmed; a staged directory is not proof of a successful upload. Retrieval
  is subject to the artifact's retention period.
- For local or direct runs without that collector, copy the evidence to an
  authorized persistent location outside the disposable worktree, verify the
  copied files, and record its absolute path or retrievable artifact URL. In
  `skip_push` mode keep this local; do not publish evidence as a workaround.
  If no such destination is available, preserve the worktree and report the
  unresolved retention requirement instead of claiming a durable archive.

Keep this archive separate from the permanent tests. On retries, preserve the
selection and retained evidence instead of reinstating the omitted source.

- Tests of the reported bug must **fail on the unfixed code and pass with your
  fix** — same fail→pass discipline. Checks of previously correct behavior may
  pass on both revisions, as the shared impact assessment explains.
- Cover the **specific behavior the bug got wrong**, plus the obvious adjacent
  edge cases the root cause implies — not just "the function runs."
- Put them where the repo keeps tests for that layer, following existing files'
  fixtures and structure. Do not invent a new harness.
- **Name by the problem, never the ticket.** Test files, test functions, fixtures,
  and any other identifier must describe the *behavior* — never embed an issue or
  ticket number (no `test_omni_2812_*.py`, no `OMNI-2812`/`#4458` in symbol names
  or comments). Prefer the observable defect: e.g.
  `test_mid_stream_error_surfaces_as_abort.py`, not `test_omni_2812_*`. This
  applies to a reproduction test introduced by this task too — if its
  `test_path` has a ticket-numbered name or ticket references in code,
  **rename it and strip the references** as part of the fix. A reader six months
  from now shouldn't need to chase a ticket to know what the test guards.
  The bug link belongs in the **PR body** (Step 3.4), not in code. Reusing a
  pre-existing test does not require an unrelated rename or comment cleanup.

### 2B.5 — Prove the whole set goes fail→pass

Run the selected permanent checks on the exact committed candidate, including
unchanged tests; they do not need a new commit to qualify. Record fail→pass
proof with the same assertions for the live behavioral bug, plus the required
preservation checks. Keep the audited reproduction evidence even when its
source is artifact-only; CI may also rerun the archived original independently.
Complete the shared impact assessment for the final diff:

- Each live facet has a **fail reason on the unfixed tree** and a **pass on the
  fixed tree** — that pair is the proof.
- **Sanity-check the diff:** the green came from a genuine behavior fix, not from
  loosening an assertion, `skip`/`xfail`, or narrowing the test to dodge the bug.
- Run the directly affected test modules and the focused checks selected by the
  shared impact assessment for other affected consumers and boundaries. Do not run
  the full repository suite, an entire broad test directory, every backend matrix,
  or unrelated lint/typecheck/build jobs locally; GitHub CI owns that exhaustive
  coverage after publication. A concrete dependency edge is enough to include
  another focused check; do not wait for a regression before testing that consumer.

**Prove new tests are hermetic — re-run them in a hostile environment.** A test
that passes only because the machine happens to be clean is flaky, not green, and
an LLM review is the wrong tool to catch it — running it is. For any test you
**added or edited** that asserts an environment-derived value is *absent, None, or
at its default* (e.g. a config/host/token/endpoint reported as unset), re-run it
**once with the relevant ambient variables exported** and confirm it still passes.
Set whichever variables the code-under-test reads — and their sibling names — to
non-empty values on the test command, e.g. `VAR=x SIBLING=x <your test command>`.
If the test flips under them, its fixture doesn't isolate the environment — **fix
the fixture to clear *every* relevant var** (not just the one you first thought
of), then re-run both clean and hostile. This is a required check whenever the
diff touches env-derived defaults; note it in the handoff (`hermetic_check`).

If any live facet can't be made to pass with a real fix, say so honestly rather
than shipping a hollow green.

**Record the result after the fix.** Use the recovered reproduction test and
journey to prepare the recording, even if the earlier run left no video.
See [`dev/recording-lanes.md`](../../../recording-lanes.md) for setup and recording
steps, including `OMNIGENT_E2E_RECORD_DIR` (`--video on` does not work here).

- Record the user action and the corrected product behavior. Tests may drive
  and verify the interaction, but the clip must show the product, not pytest,
  assertions, debug logs, or test source.
- For CLI or terminal output, record the real command and its output, even if
  only an error message changes. For example, run `omnigent host` with an
  expired login and capture the corrected error message.
- Record your fix on the author path, or the reviewed PR head on the review
  path. Save the clip as `recordings/<slug>/after-<facet>.<ext>` with
  `kind: "after"`, and include it in the PR Demo section and handoff.
- Keep any recovered before-clip unchanged. A missing before-clip is not a
  reason to skip the after-clip; note the missing before-clip in your evidence.
- For internal/API-only results with no visible user interaction, written
  evidence is enough. Set `recordings: []` and describe the before/after result
  in your evidence and the PR Demo section.
- If recording is blocked by missing tools or an environment that cannot run
  the journey, set `recordings: []` and name the specific blocker in
  `recording_unavailable_reason`. Do not block the fix or PR because footage is
  missing or rejected; explain the gap and continue. Only report clips you
  actually produced.

Build the SPA before starting the recorder. If you are inside a server-spawned
runner (`OMNIGENT_RUNNER_ID` is set), strip the inherited runner/host variables
as described in `dev/recording-lanes.md`. If the recorder reports `online: false`,
retry with those variables removed before reporting an environment blocker.
