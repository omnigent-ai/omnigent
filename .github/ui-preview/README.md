# UI Preview

Deploy a live, per-PR preview of the Omnigent web UI as a
[Databricks App](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/)
when a PR changes the frontend (`web/`).

## How it works

1. A maintainer adds the `ui-preview` label to a PR. The label — not the PR
   author — is the gate: the workflow deploys any labelled non-draft PR, forks
   included, and applying the label needs Triage+ on the repo, so an outside
   contributor can't self-label their own PR.
2. The [UI Preview workflow](../workflows/ui-preview.yml) builds the SPA + the
   Omnigent wheels and deploys them to an ephemeral Databricks App
   (`omnigent-ui-preview-pr-<N>`).
3. A comment with the preview URL is posted on the PR and updated on each push.
4. The app is deleted automatically when the PR is closed.

## Provisioning retries

Initial compute provisioning gets up to three attempts, each polling for about
10 minutes. The logs include the compute state and Databricks' status message.
If compute stays `STARTING` or enters `ERROR` / `STOPPED`, the workflow can
delete the undeployed app and recreate it with the **same name**. It confirms
deletion first, then backs off for 15 seconds before the second attempt and
30 seconds before the third. An undeployed app left by an earlier failed run
can also be recovered this way.

Recreation is restricted to the same app ID, deployment principal, and preview
description, with no deployments, source path, resources, or pending updates.
Existing deployed previews are reused, never deleted by provisioning retries.
API/authentication errors, identity changes, and unconfirmed deletions fail the
step instead of triggering recreation. The final failed app is left for
inspection; closing the PR still cleans it up.

To verify the retry behavior without touching Databricks, run
`uv run --no-sync python -m pytest tests/test_ui_preview_workflow.py`.
These tests execute the workflow's shell with a fake CLI and no real sleeps.
After a live deployment, check the `Create or update app` log for the attempt
number and confirm the preview URL posted on the PR opens successfully.

## Fork PR safety

`ui-preview.yml` runs its build/deploy on `pull_request_target`, so the label is
a trust boundary a maintainer applies. Two guards keep a labelled fork PR from
deploying *unreviewed* code when a later commit is pushed (a TOCTOU / "pwn
request"):

- **Build pins the head SHA.** The build checks out the PR's immutable head SHA,
  not the live `refs/pull/<n>/merge` ref (which re-points to the newest push), so
  a run only ever builds one specific, inspectable commit.
- **Fork deploys need per-run approval.** The secret-bearing `deploy` job runs in
  the `ui-preview-fork` Environment for fork PRs, so each new commit waits for a
  human reviewer before it can deploy. Same-repo PRs (maintainers + the
  resolve-agent bot, which outside contributors can't push to) are trusted and
  skip the gate.

## What it is

Unlike Omnigent's production Databricks deploy (`deploy/databricks/`, backed by
Lakebase Postgres + UC Volumes), the preview is intentionally ephemeral and
self-contained: a **SQLite** database + local-disk artifact store, thrown away
on teardown.

There is **no LLM or runner baked into the preview** -- Omnigent runs agent
turns on a runner the user connects from their own machine or sandbox
(`omnigent host --server <preview-url>`), where the model credentials live. So
the preview is for reviewing the UI's look-and-feel and navigation; to drive a
real session, connect your own host to the preview URL.

## Access

Preview apps are only accessible to maintainers with Databricks workspace
access (the Apps proxy injects `X-Forwarded-Email`, so the app runs in header
auth mode).

## Setup (one-time, by a maintainer)

Add these repo secrets:

- `DATABRICKS_HOST`
- `DATABRICKS_CLIENT_ID`
- `DATABRICKS_CLIENT_SECRET`

Create a `ui-preview` label. If the workspace IP-allowlists, register a
static-IP runner and point the `deploy`/`cleanup` jobs at it.

Create a protected Environment named `ui-preview-fork` (Settings ->
Environments) with **Required reviewers** set to the maintainer team. This is
what enforces per-commit re-approval for fork PR deploys; without it the
`environment:` reference on the `deploy` job is a no-op and fork deploys run
unattended. (Required reviewers must be users or teams — a GitHub App / bot
identity can't be a reviewer, which is intentional here: fork code must be
approved by a human, not auto-approved by the bot.)
