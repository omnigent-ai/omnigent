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

Find *why* the test fails. Read the code the journey and `evidence` point at. Use
repro-agent's root-cause leads as hypotheses, but confirm them against the code.
State the root cause concretely before you change anything.

### 2B.3 — Implement the fix

Fix the root cause, not the symptom. Change the code the bug lives in, matching
surrounding conventions, as small as the root cause allows. Do not touch the test
to make it pass; the *code* must change to satisfy it.

### 2B.4 — Add targeted tests at the layer you changed

The reproduction test is a full end-to-end journey — slow, one layer above your
fix. Add **targeted, fast tests at the layer you changed** (a unit/integration
test on the function/module/component you edited):

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
  applies to the repro e2e test too — if the file you recovered at `test_path` has
  a ticket-numbered name or ticket references in code, **rename it and strip the
  references** as part of the fix (fold the rename into your diff). A reader six
  months from now shouldn't need to chase a ticket to know what the test guards.
  The bug link belongs in the **PR body** (Step 3.4), not in code.

### 2B.5 — Prove the whole set goes fail→pass

Re-run **every** test in the deliverable — the (possibly rewritten) repro e2e test
plus your new targeted tests — on the fixed tree. They must all pass. Then confirm
the transition is real and complete the shared impact assessment for the final
diff, including its checks of previously correct behavior:

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
See [`dev/recording-lanes.md`](../recording-lanes.md) for setup and recording
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
