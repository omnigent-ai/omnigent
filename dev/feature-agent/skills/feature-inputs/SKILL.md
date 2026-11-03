---
name: feature-inputs
description: Read the input and ticket, choose the mode, run preflight, and check for existing delivery.
---

# Start a feature run

## Input contract

You are invoked with exactly one work source:

- `feature_url`: a Linear feature ticket, e.g.
  `https://linear.app/omnigent/issue/OMNI-1234`. Build this feature.
- `review_pr`: a canonical GitHub PR URL for a PR this agent opened, where a
  trusted human requested changes. Load the shared `otto-review-remediation`
  skill; it takes precedence over the build phases. Preserve the scanner's
  `review_fingerprint` and `review_requests` ids in the final handoff.

Optional fields:

- `skip_push` (boolean): commit locally and stop before any push or PR. It takes
  precedence over a CI publisher contract.
- `public` (boolean): share this session public-read as the first preflight
  action.

## Preflight (first turn)

1. If `public: true`, call `sys_session_share` with no `session_id`,
   `user_id: "__public__"`, `level: "read"`. If it returns `access_denied`, note
   that and continue.
2. Confirm the workspace: `git remote get-url origin` is
   `omnigent-ai/omnigent`, the worktree is on a fresh branch off current
   `main` (in `review_pr` mode, the PR's current head branch instead), and
   `git`, `gh` (`gh auth status`), and the test runner work.
3. Read the ticket with the Linear GraphQL API, using
   `${LINEAR_API_KEY:-$DATABRICKS_LINEAR_API_KEY}` (local runs set the first,
   `--server` runs the second): title, description, comments, labels, parent,
   children, and attachments. Read the parent too when there is one; a child
   ticket's scope is the child itself.
4. **Check for existing delivery.** Search open and recently merged PRs
   (`gh pr list --search`, the ticket key, and the ticket's GitHub attachments)
   and `main` for the feature. If it is already delivered, stop with
   `nothing_to_build`, naming the PR or commit. If an open PR is in progress,
   stop with `needs_more_info` naming it rather than opening a competing PR.

If you can't read the ticket or reach your tooling, stop with `needs_more_info`
and say what is missing. Don't narrate a clean preflight.

Then load `feature-requirements`.
