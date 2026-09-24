### 4.4 — Write a live-validation prompt a human can paste to an agent

Now that the fix is green and reviewed, give the human an **agent-ready prompt**
that reproduces the original journey and confirms the fix — the fastest way for
them to trust it without reading the diff. Build it from the recovered `journey`,
`facets`, and `bug_url`: a self-contained natural-language instruction they can
paste to an Omnigent agent (driving the UI preview from 4.1, or their own local
app) that (a) walks the exact steps that used to fail and (b) states the corrected
behavior to look for. Keep it copy-pasteable and specific — concrete inputs,
routes, or clicks; the expected *correct* result for each live facet; and for a
compound bug, every reproduced facet.

Put it where it belongs for the path you're on, and carry the same text in the
`validation_prompt` handoff field either way:
- **Direct author path (your PR already exists):** treat
  `.omnigent/pr-body.md` as the source of truth for the complete description
  through the final handoff, not merely as input to initial PR creation. Add the
  **"Validate the fix live"** section to that saved file, preserve the existing
  template sections, run the template validator again, then sync that exact file
  with `gh pr edit <pr> --body-file .omnigent/pr-body.md`. Never make a live-body
  edit without making the same edit in the saved file first.
- **Workflow-owned author path (no PR exists yet):** before the final handoff,
  generate the bare `validation_prompt` from the recovered journey and add a
  **"Validate the fix live"** section to `.omnigent/pr-body.md`. Since there is
  no preview URL or PR number yet, use the server-surface command without
  `--server`, or plain instructions to check out the eventual PR for a
  runner/`both` surface. Validate the saved body, make no `gh` call, and leave it
  in the worktree for the resolve artifact bundle. This prompt/body preparation
  is the only part of Step 4 performed in deferred mode.
- **Review path (someone else's PR):** don't rewrite their PR body — post the
  **"Validate the fix live"** block as a PR comment (`gh pr comment <pr>`) so the
  reviewer and author get the command without you editing their description.

**Lead with the one command that runs it — and pick it by `validation_surface`
(4.1).** The command shape differs by which side your fix runs on:

- **`server` surface** — the preview build carries the fix. When 4.1 produced a
  preview `<url>`, lead with the `--server <url>` line so a reviewer copies one
  line. When there was no preview URL, give the same command without `--server`
  (runs against the reviewer's own local app). Shape:

  > **Validate the fix live**
  >
  > Run this against the deployed UI preview (attaches your own host, which carries
  > your model credentials):
  > ```
  > omnigent claude -p 'Reproduce and validate a bug fix. Steps: <the journey —
  > concrete inputs/clicks/routes>. Before this fix, <the buggy behavior>. Confirm
  > the fix by checking that <the corrected behavior / value for each live facet>.
  > Report whether each step now behaves correctly.' --server <url>
  > ```
  > No preview URL? Drop `--server <url>` to run against your own local app. Or paste
  > just the prompt to an agent already connected to an Omnigent app.

- **`runner` or `both` surface** — the preview's server carries the fix but the
  reviewer's local runner would not, so **do not** lead with `--server <preview>`
  (it validates only half the change). Lead with a **local PR build** instead, so
  both halves are your code:

  > **Validate the fix live** (this fix runs in the runner/host process, so check
  > out the PR — attaching a local runner to the preview would run an *unfixed*
  > runner):
  > ```
  > gh pr checkout <pr>
  > omnigent claude -p 'Reproduce and validate a bug fix. Steps: <the journey>.
  > Before this fix, <the buggy behavior>. Confirm the fix by checking that <the
  > corrected behavior>. Report whether each step now behaves correctly.' --server ''
  > ```
  > `--server ''` runs a local server from this same checkout, so the server *and*
  > runner are the PR build. (The preview `<url>` is still linked for the UI, but
  > it can't exercise a runner-side fix on its own.)

Use the real `<pr>`/`<url>` from 4.1 and the concrete journey — no placeholders in
what you post. Keep the `validation_prompt` handoff field as the bare prompt text
(the part inside `-p '…'`), so the workflow can reuse it; the assembled command
lives in the PR body and the maintainer comment.
