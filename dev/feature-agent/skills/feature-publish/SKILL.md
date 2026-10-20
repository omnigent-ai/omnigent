---
name: feature-publish
description: Commit the feature, write the PR body, publish in the selected mode, and drive the PR.
---

# Publish and drive the PR

## Commit

Commit the implementation and its tests on the working branch with the
repository's conventions, signed off (`git commit -s`). Stage paths explicitly
and check `git diff --cached --stat`: **never** commit `recordings/` or
`.omnigent/` files. Read the staged diff for redundant comments. If a commit
hook changed tested files, rerun their checks. If the checkout has
`.github/scripts/pr-template/hygiene.py`, run it with `--base origin/main`.

## Choose the publication mode

- **Local-only (`skip_push: true`):** stop after committing. Report the branch in
  `pushed_branch` and leave `pr_url` empty; do not prepare a PR or drive one.
  The outcome is `partially_implemented`, with publishing and the PR loop in
  `remaining_work`.
- **Workflow-owned (a CI publisher contract is appended to your instructions):**
  do not push or make any `gh` write. Write and validate `.omnigent/pr-body.md`
  (below), write the final handoff, and stop. The workflow publishes the PR and
  runs the PR loop.
- **Direct (no publisher contract):** push the branch, open the PR, then drive it
  (below).

When `.omnigent/pr-gate.json` exists, make every PR write (create, edit, comment,
review, ready) with the `pr.py` helper CI supplies, at the path given in the
publisher contract. Keep Git pushes on the supplied credential. Never decode,
search for, or export another token, and never change credential configuration.

The PR is always **ready for review, never a draft**, including when a
recording is blocked.

## PR body

Copy `.github/pull_request_template.md` to `.omnigent/pr-body.md` and fill it
in, keeping every heading and checkbox row:

- First lines: `Implements <feature_url>` and `<!-- omnigent-feature-managed -->`.
  Do not use a closing keyword against a Linear URL.
- **Summary:** a 1–2 sentence plain-language description of what users can now
  do, then subsections *Design*, *Alternatives considered*, *CUJs covered*, and
  *CUJs not covered (and why)*.
- **Test Plan:** each covered CUJ with the tests that exercise it, plus the
  impact-assessment checks, as short bullets.
- **Demo:** the recordings for each covered CUJ, labelled by CUJ ID. Upload them
  as GitHub attachments when your environment can, otherwise link where they
  live (the CI artifact bundle). For a CUJ with a blocked recording, give the
  blocker and the manual test steps instead. When a Linear key is available,
  also attach the recordings to the ticket (GraphQL `fileUpload` +
  `attachmentCreate`).
- Check **Feature**, plus **UI / frontend change** when the UI changes.

Aim for fewer than 800 visible words. If the checkout has
`.github/scripts/pr-template/validate.py`, run it with
`PR_BODY="$(cat .omnigent/pr-body.md)"` and fix every error. Then publish with
`gh pr create --body-file .omnigent/pr-body.md`, or the helper in checked mode.

## Drive the PR (direct publication)

Stay on the PR until CI is green and both reviews are settled:

1. Load the shared `otto-drive-pr` skill. Follow `ci.md` to keep the branch
   mergeable and CI green, and `preview.md` to label `ui-preview`. Skip its
   live-validation prompt and `validation_surface` steps; the Demo section and
   the CUJ steps serve that purpose.
2. Load the shared `otto-pr-review-loop` skill and run Polly and OCR until every
   finding is addressed or answered with a reason. Request both again after
   every push. Run its live gate before claiming `implemented`.
3. Refresh `otto-impact-assessment` after every change in this loop.
4. Request review from a maintainer: the ticket's assignee or the owners of the
   main changed files. Never approve or merge.

If a concrete blocker or deadline prevents finishing, leave the PR open and
report `partially_implemented` with the remaining work.
