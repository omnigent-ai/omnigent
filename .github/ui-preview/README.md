# UI Preview

Deploy a live, per-PR preview of the Omnigent web UI as a
[Databricks App](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/)
when a PR changes the frontend (`web/`).

This is a close port of [MLflow's UI Preview](https://github.com/mlflow/mlflow/blob/master/.github/workflows/ui-preview.yml).

## How it works

1. A maintainer adds the `ui-preview` label to a PR (the workflow is gated to
   `OWNER`/`MEMBER`/`COLLABORATOR` authors).
2. The [UI Preview workflow](../workflows/ui-preview.yml) builds the SPA + the
   Omnigent wheels and deploys them to an ephemeral Databricks App
   (`omnigent-ui-preview-pr-<N>`).
3. A comment with the preview URL is posted on the PR and updated on each push.
4. The app is deleted automatically when the PR is closed.

## What it is

Unlike Omnigent's production Databricks deploy (`deploy/databricks/`, backed by
Lakebase Postgres + UC Volumes), the preview is intentionally ephemeral and
self-contained: a **SQLite** database + local-disk artifact store, thrown away
on teardown.

There is **no LLM or runner baked into the preview** -- Omnigent runs agent
turns on a runner the user connects from their own machine or sandbox
(`omnigent run … --server <preview-url>`), where the model credentials live. So
the preview is for reviewing the UI's look-and-feel and navigation; to drive a
real session, connect your own host to the preview URL.

## Access

Preview apps are only accessible to maintainers with Databricks workspace
access (the Apps proxy injects `X-Forwarded-Email`, so the app runs in header
auth mode).

## Setup (one-time, by a maintainer)

Add these repo secrets (same names MLflow uses):

- `DATABRICKS_HOST`
- `DATABRICKS_CLIENT_ID`
- `DATABRICKS_CLIENT_SECRET`

Create a `ui-preview` label. If the workspace IP-allowlists, register a
static-IP runner and point the `deploy`/`cleanup` jobs at it.
