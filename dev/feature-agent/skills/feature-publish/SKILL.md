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
- **Workflow-owned (CI says the workflow publishes after your handoff):**
  do not push or make any PR write, including through `pr.py`.
  Write and validate `.omnigent/pr-body.md`
  (below), write the final handoff, and stop. The workflow publishes the PR and
  runs the PR loop.
- **Agent-owned (CI explicitly says this session owns publication):** follow
  that contract to push, create the PR with the checked helper, then upload
  demos and drive the PR. This overrides the workflow-owned handoff procedure.
- **Direct (no publisher contract):** push the branch, open the PR, then drive it
  (below).

If a publisher contract is present but does not explicitly authorize this
session to publish, use workflow-owned mode.

The gate file selects the transport; it does not grant publication authority.
When PR writes are authorized and `.omnigent/pr-gate.json` exists, make every
PR write (create, edit, comment, review, ready) with the `pr.py` helper CI supplies,
at the path given in the publisher contract. Keep Git pushes on the supplied credential. Never decode,
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
- **Demo:** a `### CUJ-n` heading for each covered CUJ, with its uploaded
  recordings (see "Upload the demo" below). For a CUJ with a blocked recording,
  give the blocker and the manual test steps instead. When a Linear key is
  available, also attach the recordings to the ticket (GraphQL `fileUpload` +
  `attachmentCreate`).
- Check **Feature**, plus **UI / frontend change** when the UI changes.

Aim for fewer than 800 visible words. If the checkout has
`.github/scripts/pr-template/validate.py`, run it with
`PR_BODY="$(cat .omnigent/pr-body.md)"` and fix every error. In workflow-owned
mode, hand off and stop here. Otherwise publish with
`gh pr create --body-file .omnigent/pr-body.md`, or the helper in checked mode.

## Upload the demo

This step requires an existing PR and authorization to write to it: an
agent-owned/direct publication run, or an authorized PR-loop/remediation run.
Workflow-owned author runs leave uploading and description edits to the publisher.

Wait for recording and rendering to finish before uploading. Upload the clips
in a PR comment, then copy their returned URLs into the description:

1. Write a comment body with a `### CUJ-n` heading per covered CUJ, each
   followed by `![CUJ-n](<path to the rendered clip>)` on its own unindented
   line outside code blocks, inline code, and HTML comments. Cite each path
   exactly as passed to `--attach`, without `..` components.
2. **Checked mode (`.omnigent/pr-gate.json` exists):** use the supplied helper:
   `python <helper-dir>/pr.py comment --number <pr> --body-file <file> --attach <clip>`.
   Repeat `--attach` for additional clips. The host uploads with its dedicated
   media credential and posts as the bot. Use the returned `assets` mapping
   (path to URL) to match clips to CUJs. Do not fall back to `gh` or seek tokens.
   **Direct mode:** check `gh pr comment --help` for attachment support, then
   use `gh pr comment <pr> --body-file <file> --attach <clip> …` with the supplied
   credential. If uploading is unavailable, report the recording blocker and
   manual test steps; do not change credentials.
   In direct mode, read the uploaded asset URLs from the posted comment using
   `gh pr view <pr> --comments` or the comment URL printed by `gh`.
3. Put those URLs under the matching `### CUJ-n` headings in the Demo section,
   each on its own line so videos play inline. Update the description with
   the helper in checked mode or `gh pr edit --body-file` in direct mode.

Only these uploaded URLs count as recordings: they must be
`https://github.com/user-attachments/assets/…` URLs. Local `recordings/` paths
and CI artifact links do not.

For maintainers, the checked helper's CLI and upload contract live in the private
`omnigent-internal` repository:
`.github/scripts/resolve_pr_client.py` and `.github/scripts/resolve_pr_gate.py`;
regressions are covered by `agents/repro-agent/tests/unit/test_pr_attachments.py`.

## Drive the PR (agent-owned or direct publication)

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
