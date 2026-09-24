---
name: resolve-review-pr
description: Review an existing fix PR using the audited repro and full diff, preserving branch and approval rules.
---

## Step 2A — Review the existing fix PR

You are reviewing someone else's candidate fix, not writing your own. The
reproduction test is evidence only after independent validation. Complete the
shared repro audit before this path; the recovered verdict is not an endorsement
of the test. A passing repro alone does not prove the PR fixes the bug.

1. **Check out the PR head** into your worktree (`gh pr checkout <number>`), then
   ensure the repro test at `test_path` is present on top of it (it is your
   artifact, not theirs — re-apply it if the checkout doesn't carry it). If a test
   you keep — the repro test, or one the PR adds — names a ticket/issue in its
   filename or code, rename it and strip the reference per the "name by the
   problem, never the ticket" rule in 2B.4.
2. **Run the same audited repro test against the PR.** Compare it with the
   behavioral failure on the recorded unfixed base:
   - **Passes** → evidence that the tested behavior is corrected, subject to the
     journey and diff review below. For a compound bug, run every `reproduced`
     facet; all live facets must pass for the PR to fully resolve it.
   - **Fails behaviorally** → the PR does **not** fix that reproduced behavior;
     capture the exact failure. Setup/import failures or invalid test assumptions
     are verification blockers, not proof that the PR is wrong. Resolve or
     disclose them without approving the PR or inventing a product change.
3. **Record the journey against the PR head — always.** You drive the recorder
   off the reproduction test (the e2e_ui test for `web`/`terminal` facets, a VHS
   tape for `cli` facets) run against the PR head, and add an `after`-kind entry
   to your handoff `recordings`. This is **not** gated on the repro handoff
   carrying footage — you have the test and the journey, which is all the recorder
   needs, so produce the after-clip whether or not any before-clip was recovered.
   Use the same lanes as 2B.5 — see [`dev/recording-lanes.md`](../recording-lanes.md)
   (build the SPA first, record via `OMNIGENT_E2E_RECORD_DIR`, per-surface `web` /
   `mobile` / `terminal` / `cli` / `desktop` mechanics) — saving to
   `recordings/<slug>/after-<facet>.<ext>` with a `caption` for what the clip
   shows. The test result determines the verdict separately; the footage must
   show the product journey and its visible outcome, never the test runner. When
   the handoff *does* carry a before clip, carry it
   through **and** produce the after; when it carries none, still produce the
   after and note the missing before. Only omit the after clip when it is
   genuinely unobtainable (recorder tooling missing, or the fixture can't come
   online after the SPA build **and** the leaked runner env is stripped) — say so
   explicitly in your review comment and in `evidence`, naming the blocker. An
   `online: false` seen while `OMNIGENT_RUNNER_ID` is still set is your own
   un-stripped env, not a blocker: re-run with the `env -u` prefix from
   `dev/recording-lanes.md` first. A missing upstream before-clip is never that
   blocker. Never drop it silently.
4. **Review the diff** for quality, not just green. Decide whether this is the
   **best practical approach** for the repository, not merely an approach that
   makes the reproduction pass. Identify the plausible alternatives suggested by
   the surrounding architecture and compare them briefly: does this PR fix the
   root cause at the correct layer, follow the established abstraction, minimize
   special cases and long-term maintenance cost, and preserve security,
   compatibility, and performance? Does it miss facets or obvious adjacent edge
   cases, or introduce a regression in the surrounding code? Complete the shared
   impact assessment and run its checks for the whole PR. Record why the selected
   approach is preferable in the review.
   "Best" means the strongest maintainable fit for this codebase and bug, not a
   license to replace a sound, idiomatic contribution with a theoretically purer
   rewrite or a personal style preference.

   **Check the full PR for scope**, including changes made before you arrived.
   Establish one concrete reported failure or requested outcome and its
   acceptance criteria from `bug_url` and the PR's linked issue. Different
   layers or root causes can contribute to that outcome. If the issue bundles
   independent problems, ask the author to split them or track them separately;
   stop with `needs_more_info` if the intended scope is unclear.

   For each change, ask whether removing it would leave the intended fix
   incomplete, incorrect, unsafe, or inadequately tested or documented.
   Necessary refactors and repairs for regressions introduced by this PR belong
   with the fix. Independent features, bug fixes, cleanup, and upgrades do not,
   even in the same file or when tests pass. Identify the unrelated files/hunks
   and remove clearly separable changes when branch edits are permitted;
   otherwise ask the author to split or remove them. Do not guess when changes
   are entangled. Carry only in-scope work into any fork takeover.

   Address Polly's scope findings through the ordinary review process in Step
   4.3 before approving this existing PR. Keep your own edits within the same
   scope. Request clarification when its relationship to the reported bug is
   uncertain; do not approve until clarified. Record unresolved scope concerns
   in the review and `fix_summary`.
5. **Report on the existing PR.** Post your fail→pass (or fail→still-fails) result
   and any diff concerns now as a `gh pr comment` / `gh pr review --comment`, and
   record its `pr_url` in your output. The `outcome` reflects what you found
   (`fixed` when the PR resolves every live facet, the shared impact assessment
   has no unresolved required checks, the diff is sound, and the changes stay
   within the reported problem;
   `partially_fixed` / `not_fixed` otherwise, with specifics). **Default to
   commenting, not competing** — if the PR is close and its approach is sound,
   review it and let the author iterate; don't open a rival PR over fixable nits.

   The **review verdict (approve / request-changes)** comes at the *end*, after
   Step 4 settles (4.5) — because whether you end up pushing to the PR is decided
   there. When you get to it, submit the final review this way:

   **Match the review verdict to what you found — and approve when you're a clean,
   independent reviewer.** You are a `[bot]`, so your review never satisfies the
   merge gate (a human maintainer's approval is always required); it's an
   *indicator* for that maintainer. Choose:
   - **`fixed` and you never pushed to or authored this code** (pure reviewer: the
     repro test passes against the PR as-is, CI green, Polly clean, **the branch is
     mergeable** — not `CONFLICTING`/`DIRTY` — the current diff stays within the
     reported problem, and no fix from you was needed) →
     submit an **approving** review: `gh pr review <pr> --approve
     --body '…'`. A genuine independent verification — the "someone checked it, take
     your pass" signal a maintainer wants. Note in the body that it's an automated
     reviewer's approval and a maintainer's approval is still required to merge.
     **"Polly clean" here means a real Polly review actually ran and came back
     clean** — a fresh `<!-- polly-review-bot -->` comment for the current head,
     every finding fixed or justified (4.3), **not** a green check. If you could not
     get a real Polly review to run (the dispatch failed, no comment ever landed,
     or you only ever saw the phantom green check on a fork PR), you have **not**
     verified this precondition: do **not** approve. Leave a `--comment` review
     that states the fail→pass evidence *and* that a Polly review could not be
     obtained, and let a maintainer take over the review from there.
   - **`not_fixed` / `partially_fixed`** → `gh pr review <pr> --request-changes
     --body '…'` naming what still fails or which unrelated changes must be
     removed or split out, even if the reproduction passes.
   - **You pushed fixes to this PR** (in-repo branch) **or took it over** (fork) →
     do **not** approve: that's self-approval of your own commits (branch
     protection rejects it anyway). Leave a `--comment` review and let a human
     approve.
6. **Then drive it to landable — go to Step 4.** Once you've kept the PR as the
   fix (the sound-PR default), it gets the **same landing treatment as a PR you
   authored**: `ui-preview`, green CI, a clean Polly review, a copy-paste
   live-validation command, and a maintainer tagged (Step 4, all sub-steps). The
   one difference is whose branch a fix lands on — Step 4's "push or take over"
   rule handles it: push fixes directly when the PR branch is in-repo; when it's a
   **fork PR** you can't push to *and it needs a fix*, take over by opening your
   own PR that carries their commits + your fix (crediting them). If the fork PR
   needs no fix, keep it as-is. Either way you **do** iterate CI and Polly, rather
   than triggering one review and stopping — and on a fork PR you must actually
   dispatch Polly and wait for its comment, because the automatic check reports a
   green `pass` there without ever running (see 4.3). Record
   `mode: "reviewed_existing_pr"`
   and its `pr_url` when you keep it; if a fork takeover made you open your own,
   record `mode: "authored_fix"` with the fork PR in `reviewed_pr_url`.

**When the existing PR's *approach* is wrong, open your own fix instead.** The
default above is for a sound PR. But if reviewing shows the PR is not a viable
base — its approach is fundamentally incorrect (masks the symptom, wrong layer,
doesn't address the root cause), needlessly complex, or so low-quality that
correcting it in review would be more work than a clean fix — don't force a
comment-only outcome. Say precisely why the existing approach won't do (in a
review comment on that PR, so the author knows), identify the preferable approach
and its concrete advantages, then **switch to the author path
(Step 2B) and open your own PR** that resolves the bug correctly. In your PR,
reference the existing one and summarize why a fresh approach was warranted.
Record `mode: "authored_fix"`, put `Supersedes #<old>` on its own line in the new
PR body, and keep the reviewed PR open while the replacement is under review.
The trusted post-merge workflow closes the old PR only after the replacement
actually merges. Comment on the old PR immediately with the replacement link so
the contributor understands the handoff, but **do not close it yourself**. Use
this escape hatch deliberately, not for style preferences — a working,
root-cause-sound PR should be reviewed and improved in place, not replaced.

When you keep the PR, you drive it to landable per Step 4 — pushing fixes directly
when its branch is in-repo, or (for a fork PR you can't push to that needs a fix)
taking over into your own PR that carries their commits plus your fix. So there
are **two** reasons you end up authoring your own PR from the review path: the
existing approach is *wrong* (this escape hatch), or the approach is *fine* but
it's an unpushable fork PR that needs changes (Step 4's take-over). Never rewrite a
sound approach wholesale — a fork takeover replays the contributor's commits and
adds to them, it doesn't discard their work.
