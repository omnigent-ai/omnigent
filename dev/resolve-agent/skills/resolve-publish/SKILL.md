---
name: resolve-publish
description: Commit the validated fix and follow local-only, workflow-owned, or direct PR publication.
---

## Step 3 — Commit, push, and open the pull request (author path only)

This step applies **only when you authored a fix in Step 2B** — it's about
*opening* a PR. (The review path 2A adopts the existing PR instead of opening one,
then goes straight to Step 4 to land it.) Once the set is genuinely green:

**Check again before publishing.** Once the fix and PR body are ready, repeat
Step 1's search immediately before creating a new PR, or before the final
handoff to a CI publisher. Inspect only new or changed candidates, using Step 2A
if one may cover the bug; preserve your work while evaluating it. Recheck the
state of earlier candidates too: if one merged, use the shared repro audit on
updated main before deciding whether your fix is still needed. Record the check
and decision in `fix_summary`. Skip this refresh for `skip_push` and updates to
an existing PR.

### Choose the publication mode before proceeding

- **Local-only (`skip_push: true`)** — commit the fix and stop at Step 3.2. No PR
  will be published automatically, so do not prepare a PR body or run Step 4.
  This takes precedence even when CI appended a generic publisher contract.
- **Workflow-owned publication (`skip_push: false` plus an explicit CI publisher
  contract)** — do not push or make any `gh` write. Prepare and validate
  `.omnigent/pr-body.md` using the body-writing instructions in Step 3.4, but do
  not run its `gh pr create` command. Complete the deferred live-validation
  preparation described in Step 4.4, then write the final handoff and stop. The
  publisher performs the GitHub writes; do not run the PR-facing
  CI/preview/review loop in the rest of Step 4.
- **Direct publication (no publisher contract)** — perform all of Step 3, then
  drive the published PR through Step 4.

In workflow-owned mode, `.omnigent/` is intentionally gitignored, so body
transport does not rely on the file being committed. The workflow captures
`pr-body.md` separately in the resolve artifact bundle alongside the committed
checkpoint, then restores it into the publication worktree before running the PR
finalizer. The finalizer validates and uses that restored file as the PR
description; without it, the publisher can only construct a less readable
fallback from machine-oriented handoff fields.

### Get the GitHub write token (needed for every push / `gh` write)

Any write to GitHub — `git push`, `gh pr create`, `gh pr edit --add-reviewer`,
`gh pr comment`, `gh pr close` — needs the resolve-agent App installation token
(`omni-resolve-agent[bot]`, `contents`+`pull_requests` write on
`omnigent-ai/omnigent`). **Your shell does not inherit it in a usable env var**:
you run inside the session's runner process (a different process, often a
different machine when hosted on `--server`), so `$GH_TOKEN` in your shell is
empty and a bare `git push` fails with a 403 / permission error. This is **not**
a missing/expired/read-only token — the write credential IS on this machine, in
the git config of your checkout. Recover it before any GitHub write.

**The reliable source is the checkout's persisted `http.extraheader`.**
`actions/checkout` bakes the App installation token into your repo's git config
as an `AUTHORIZATION: basic <base64>` header (git worktrees share it via the
common config, so it's readable from your fix worktree too). Decode it and export
it as `GH_TOKEN`:

```bash
# Run from anywhere inside your checkout / fix worktree. The extraheader value is
# base64("x-access-token:<token>"), so strip the prefix, base64 -d, take the part
# after the colon.
export GH_TOKEN="$(git config --get http.https://github.com/.extraheader \
  | sed 's/^AUTHORIZATION: basic //' | base64 -d | cut -d: -f2-)"
[ -n "$GH_TOKEN" ] || echo "no extraheader token found in git config"
gh auth setup-git   # route git pushes through gh's credential helper with this token
```

- Do this **once** at the start of Step 3 (and again in Step 4 if a later
  `gh`/`git push` call reports it lost auth). Then push, open the PR, request the
  reviewer, and comment normally — all of them use this token. Confirm it works
  and is write-scoped with `gh auth status` / a cheap `gh api /repos/omnigent-ai/omnigent`
  before relying on it.
- **Do not go hunting elsewhere first.** The token is **not** reachable via
  `/proc/*/environ` (that is denied in the session sandbox), and the ambient
  `github-actions[bot]` credential is read-only on `omnigent-ai/omnigent` (it's
  scoped to `omnigent-internal`) — both are dead ends that waste the turn. The
  extraheader above is the one that works.
- If the extraheader is genuinely absent (rare — e.g. a `skip_push` run, or the
  checkout didn't persist it), report that exact fact in `maintainer_review` with
  the command output. **Never** substitute a guess like "token expired" or "PAT
  is read-only" — those are false and drop the hand-off silently. Only a real,
  quoted failure goes in `maintainer_review`.
- CI may also configure `omnigent.forkPushTokenFile`. That is a separate
  maintainer credential for one purpose only: pushing a fix to an existing fork
  PR whose author enabled maintainer edits. Never export it as `GH_TOKEN` and
  never pass it to `gh`; PR creation, comments, reviews, labels, and every other
  visible action must continue using the App token so GitHub attributes them to
  `omni-resolve-agent[bot]`.

Once the set is genuinely green:

1. **Commit** the fix and the tests on the working branch (the fix builds on the
   repro branch, so the reproduction test and the fix land in one reviewable
   diff). Follow the repo's commit conventions.
   **Never commit workspace artifacts.** The commit must contain only the fix and
   its reproduction test — nothing else. In particular, **never** stage or commit
   the `recordings/` clips or any `.omnigent/` handoff files (e.g.
   `.omnigent/repro-handoff.json`): recordings are workspace artifacts that ride
   in the PR's Demo section / CI artifact bundle, not in the diff (see
   [`dev/recording-lanes.md`](../../../recording-lanes.md)). Do **not** use a blanket
   `git add -A` / `git add .` that sweeps them in — stage the fix and test paths
   explicitly, and run `git status` / `git diff --cached --stat` before committing
   to confirm the staged set is only the fix + test. If a recording or handoff
   file already landed in an earlier commit on this branch, remove it (e.g.
   `git rm --cached`) so it never reaches the PR.
   Refresh the shared impact assessment against the committed deliverable. If a
   hook changed tested files, rerun their affected checks before the handoff.
2. **If the input has `skip_push: true`, stop here** — the fix is committed
   locally; do **not** push and do **not** open a PR. Report the branch name in
   your output (`pushed_branch`) so a human can inspect, push, and PR it. The
   focused local validation in 2B.5 still runs before the handoff is written.
   This local-only input also suppresses workflow-owned publication; never treat
   the presence of the generic CI publisher overlay as permission to continue.
3. Otherwise **push** the branch. **First make sure `git push` / `gh` have the
   write token — see "Get the GitHub write token" below.** Your shell does **not**
   inherit `GH_TOKEN` (you run in the session's runner, not the CI wrapper's
   process), so `echo $GH_TOKEN` is normally empty and a bare `git push` / `gh pr
   create` fails with a permission error. Recover the token first; do **not**
   conclude the token is "expired" or "read-only" from an empty env var — it is
   present on the machine, just not exported to your shell.
4. **Open a ready-for-review PR** with `gh pr create` (not a draft — the repo's
   automated review runs on ready PRs). Create `.omnigent/` if needed. If the
   target repository provides `.github/pull_request_template.md`, copy it to
   `.omnigent/pr-body.md` and edit that file. Otherwise create
   `.omnigent/pr-body.md` with concise **Related issue**, **Summary**, and **Test
   Plan** sections. Pass the finished file to `gh pr create --body-file
   .omnigent/pr-body.md`. The
   workflow-owned publisher also restores this file from the resolve artifact
   bundle if it has to finish publication after your session ends, so write it
   before the GitHub call or final handoff. Link the bug in the template's
   **Related issue** section.

   Write the description for a reviewer, not for the handoff parser:

   - Keep the template's required headings and every checkbox row. Follow its
     instructions for optional sections such as Changelog. Do not replace the
     standard structure with custom `Root Cause`, `Validation`, or `Issues`
     sections.
   - In **Summary**, lead with the user-visible problem and result, then explain
     the cause and implementation in 1–3 short bullets or paragraphs. Use
     complete sentences. For a non-trivial change, include the template's ELI5
     explanation and a small diagram.
   - In **Test Plan**, group the proof into short, scannable bullets. Name the
     command or test, what failed before the fix, and what passes now. Do not
     paste `facets`, `test_transition`, other handoff fields, or a long comma-
     separated inventory of test names into the body.
   - Keep workflow/session URLs and machine-oriented publication details out of
     the narrative. The internal workflow links those separately. Never paste
     the JSON handoff into the PR description.
   - Read the finished Markdown once as rendered prose. Split run-on sentences,
     expand unexplained internal shorthand, and remove repeated evidence before
     opening the PR.

   If the target repository provides the template validator, validate the body
   locally before publishing it:

   ```bash
   PR_BODY="$(cat .omnigent/pr-body.md)" \
     python .github/scripts/pr-template/validate.py
   ```

   Fix every validation error before `gh pr create`. In **Related issue**, use a
   GitHub closing keyword **only against a GitHub issue number** —
   `Resolve #<closing_issue_number>` (equivalently `Closes #<n>`), using the
   `closing_issue_number` you determined in Step 1 (the `bug_url` issue, or the
   mirrored GitHub issue for a Linear ticket). **Never** point a closing keyword
   at a raw Linear URL — GitHub can't close it, and it clutters the body. When
   there is no `closing_issue_number` (Linear-only bug with no mirror), don't use
   a closing keyword at all: reference the ticket in prose
   (e.g. "Resolves OMNI-1234 (Linear)"). Then summarize the root cause and the
   fix, and in the **Test Plan** give the concrete fail→pass proof (test paths,
   the pre-fix fail reason, the post-fix pass). Check
   "Bug fix" and the test-coverage boxes that apply. Generate the body from the
   actual diff and this reproduction — do not skip template sections. Put the
   before/after recordings in the **Demo** section: upload the files when your
   environment can attach media to the PR; otherwise link where they live (the
   CI run's artifact bundle, or the repro session) so reviewers can watch the
   failure and the fix. For internal/API-only results with no visible user
   interaction, put the written before/after evidence in **Demo**. If recording
   was blocked, explain why and include the available evidence. When the bug
   is a Linear ticket and a Linear key is available, also attach both recordings
   to the ticket (GraphQL `fileUpload` + `attachmentCreate`).
5. **Emit an interim handoff now — the moment the PR is open.** As soon as
   `gh pr create` succeeds, print the full handoff json block (the Output schema)
   with `pr_url` set and `outcome` at its current best assessment, *before* you
   start Step 4. This is what lets the workflow post the PR link to the Linear
   ticket promptly, rather than waiting the ~hour Step 4 can take. Leave the
   not-yet-known Step-4 fields empty (`ci_status`, `polly_review`,
   `maintainer_review`) — you refill them in the final handoff. Emit it as a
   normal intermediate message (json block last in *that* message), then carry on.
   **Before this handoff, do the two outward actions a mid-turn drop would
   otherwise strand:**
   - **Label your PR `ui-preview`** (author path) — `gh pr edit <pr> --add-label
     ui-preview`. Your own PR is same-repo, already-pipelined code, so it needs no
     CI-green gate (see 4.1); label it now so the preview builds while you drive
     Step 4. (Review-path fork PRs still wait for green — 4.1.)
   - **If you opened this PR to supersede another** (fork take-over, or the
     "approach is wrong" escape hatch), you already commented on that PR but left
     it open. Ensure the replacement body contains `Supersedes #<old>` on its own
     line, and set `reviewed_pr_url` so workflow reconciliation can preserve and
     inform the original PR until the replacement merges.
6. You do **not** merge. Opening the PR is not the finish line — go to Step 4 and
   drive it to a green, reviewed, ready-for-a-human state.
