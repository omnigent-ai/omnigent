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

## Hourly preview cleanup

The **UI Preview Cleanup** workflow runs hourly at minute 43 and automatically
deletes PR-preview apps when **the PR is merged OR the app is more than 24 hours
old**, including apps for open PRs. Age is measured from the app's `create_time`,
not the PR's age or the app's latest deployment. Merged PRs have no grace period.
This is a lifecycle policy, not an inference that nobody is currently using the
preview: local sessions/artifacts are ephemeral and are lost on deletion.

Apps must match `omnigent-ui-preview-pr-<N>` and the exact PR URL in their
description, have the expected per-app source directory, and have no attached
resources. Unknown states, API errors, pending updates, in-progress deployments,
and unfinished preview workflows retain the app until a later hourly run.
Shared apps such as `omnigent-repro` and `omnigent-ui-preview-dev` are excluded.
The existing immediate close-event cleanup is unchanged.

The workflow uses the same Databricks secrets as UI Preview and starts scheduling
after merge to the default branch. Each run uploads `preview-cleanup-report` for
14 days. Manual dispatch also deletes by default; check **dry_run** to preview
the selection, and optionally enter a PR number to limit the run to one app.
No per-app approval or reviewer-activity confirmation is required.

To preview the selection locally (requires authenticated `gh` and Databricks CLIs):

```bash
python .github/scripts/ui-preview/reconcile.py --profile YOUR_PROFILE
```

If `DATABRICKS_HOST` points elsewhere, unset it for this command so it cannot
override the selected profile. The app creator identifies the deploying identity,
not necessarily Otto; the report includes the linked PR's author. These apps have
no dedicated Otto tag. For human-authored PRs, inspect who applied `ui-preview`
in the PR timeline to attribute an Otto-triggered deployment.

To apply the same policy locally to all PR previews:

```bash
python .github/scripts/ui-preview/reconcile.py --profile YOUR_PROFILE --apply
```

Use `--app omnigent-ui-preview-pr-123` to limit either mode to one app. The script
rechecks app identity, PR state, and deployment activity immediately before
deletion. Targeted workflow runs share the deployment's per-PR concurrency group;
there is no atomic cross-service check-and-delete for the full sweep.

The reconciler preserves workspace source directories and does not delete
Lakebase/UC resources or modify PR comments. Stopping alone does not free an app
slot. After deletion, check that `databricks apps get` reports the exact app
missing, and rerun the previously blocked deployment. For an open PR whose
preview expired, pushing a new commit while `ui-preview` remains applied
recreates the app with a fresh 24-hour TTL.

Test the safety checks without external access:

```bash
python -m unittest discover -s .github/scripts/ui-preview -p 'test_*.py' -v
```

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
