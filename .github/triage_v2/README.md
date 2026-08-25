# Issue prioritization pipeline

This bundle owns the issue-prioritization v2 implementation. The scoring core is
pure and reusable; Databricks and GitHub adapters are layered on top.

## Local dry-run

Prepare normalized issue JSON, then run:

```bash
uv run --project .github/triage_v2 issue-priority \
  --input issues.json \
  --areas .github/areas.json \
  --output-dir /tmp/issue-priority-preview
```

The output directory contains `ranking.json`, `ranking.csv`, `ranking.md`,
`summary.json`, and the exact `config.json` used. This command has no network or
GitHub write path.

All weights and enabled modules live in
`src/issue_prioritization/default_scoring.json`. Readiness and age are present
but disabled by default. Duplicate reach is also disabled until the upstream
triage pipeline exposes confirmed duplicate links as structured data. Community
demand counts GitHub `+1` reactions only, not all reaction types.

## New-issue grading

When `ISSUE_PRIORITIZATION_V2_ENABLED=true`, the existing Issue Triage workflow
runs v2 after intake for each new non-bot issue, including maintainer-authored
issues. It calls the configured model serving endpoint, applies component and
priority labels, posts one bot-owned triage comment with its assessment of impact,
and uploads a 30-day decision artifact.
Legacy `severity:S*` labels are removed instead of replaced with another label.
The periodic Databricks job remains responsible for
the complete ranking and dashboard; the issue-open path does not wait for it.

Configure these repository settings before enabling the switch:

| Setting | Kind | Purpose |
| --- | --- | --- |
| `DATABRICKS_HOST` | Secret | Workspace URL containing the serving endpoint. |
| `DATABRICKS_CLIENT_ID` | Secret | OAuth service-principal client ID. |
| `DATABRICKS_CLIENT_SECRET` | Secret | OAuth service-principal secret. |
| `ISSUE_PRIORITIZATION_V2_MODEL_ENDPOINT` | Variable | Endpoint name, such as `databricks-gpt-5-6-luna`. |
| `ISSUE_PRIORITIZATION_V2_ENABLED` | Variable | Set to `true` only after the other settings are ready. |

The service principal needs `CAN QUERY` on the endpoint. GitHub supplies the
issue-write token automatically; no GitHub PAT is stored in Actions. Enable v2
last:

```bash
gh secret set DATABRICKS_HOST --repo omnigent-ai/omnigent
gh secret set DATABRICKS_CLIENT_ID --repo omnigent-ai/omnigent
gh secret set DATABRICKS_CLIENT_SECRET --repo omnigent-ai/omnigent
gh variable set ISSUE_PRIORITIZATION_V2_MODEL_ENDPOINT \
  --repo omnigent-ai/omnigent --body databricks-gpt-5-6-luna
gh variable set ISSUE_PRIORITIZATION_V2_ENABLED \
  --repo omnigent-ai/omnigent --body true
```

For a no-write check, export the same Databricks credentials plus
`GITHUB_TOKEN`, then run:

```bash
uv run --frozen --project .github/triage_v2 issue-priority-event \
  --issue-number 2125 \
  --github-repo omnigent-ai/omnigent \
  --model-endpoint databricks-gpt-5-6-luna \
  --areas .github/areas.json \
  --label-manifest .github/issue-prioritization-labels.json \
  --output-dir /tmp/issue-priority-v2 \
  --run-id local-2125 \
  --mode dry_run
```

The output includes the classification, score breakdown, proposed mutations,
proposed bot comment, prompt input hash, and model endpoint, so a later
Databricks importer can consume it without changing the event path.

## Databricks dry-run

The bundle defines a paused trigger on updates to `github_issues_bronze`. It
waits five minutes after an update and runs at most once per hour. Manual runs
default to `mode=dry_run`:

```bash
databricks bundle validate --strict --target dev --profile <profile>
databricks bundle deploy --target dev --profile <profile>
databricks bundle run issue_prioritization --target dev --profile <profile>
```

The job reads all open issues from `github_issues_bronze`, persists LLM
classifications in `issue_classifications`, appends the ranking to `issue_scores`,
and writes ranking plus proposed label mutations to the managed
`issue_priority_artifacts` volume. Dry-run never changes GitHub issues.
`issue_scores_latest` always exposes the newest complete run for dashboard queries.

The classifier rubric lives in
`src/issue_prioritization/classification_prompt.txt`. After editing it, force a
classifier refresh with a regrade run:

```bash
databricks bundle run issue_prioritization --target dev --profile <profile> \
  --params regrade=true
```

Impact replaces severity as the model's base judgment. Existing cached S0-S3
classifications are mapped to critical/high/medium/low Impact values, so this
migration does not require a full LLM regrade. Legacy S-code and classification
schema compatibility remains for the 0.2.x wheel and is expected to be removed
in 0.3.0 after the label backfill and table migration are complete.

For the one-time migration backfill, first preview comment creation, legacy
severity-label removal, and priority changes whose latest label event came from
a known legacy bot. This needs read credentials but keeps the GitHub write gate
off:

```bash
databricks bundle deploy --target dev --profile <profile> \
  --var="github_secret_scope=<scope>" \
  --var="model_endpoint=<endpoint>"
databricks bundle run issue_prioritization --target dev --profile <profile> \
  --params mode=dry_run,regrade=false,adopt_legacy_bot_priorities=true
```

`run.json` records whether regrade/adoption was enabled and how many historical
priorities were adopted. Human-authored priority events remain blocked in
`mutations.json`. Each mutation also contains the comment body that apply mode
will create or update.

## Dashboard draft

Prepare an idempotent local dashboard draft after a complete scoring run:

```bash
databricks api get /api/2.0/lakeview/dashboards/<dashboard-id> \
  --profile <profile> > /tmp/issue-dashboard.json
uv run --project .github/triage_v2 issue-priority-dashboard-draft \
  --input /tmp/issue-dashboard.json \
  --output /tmp/issue-dashboard-draft.json
```

The draft adds a complete ranking table backed by `issue_scores_latest`. The
command only writes the local output file; it never updates or publishes a
dashboard.

## GitHub apply gate

The table-update trigger is paused. GitHub writes additionally require
`mode=apply`, the deploy variable `allow_github_writes=true`, and a configured
secret scope. The job re-reads every issue's live labels before writing and
preserves maintainer priority overrides. Removing a bot-owned priority is also a
durable override; human-added component labels are never removed. Retired
`severity:S*` labels are always removed because they no longer participate in
scoring.

For scheduled runs, prefer a GitHub App installation token over a personal PAT.
Install the App on `omnigent-ai/omnigent` with metadata read and issues read/write,
then store its client ID and PEM private key. The job discovers the installation
ID from the repository and mints a fresh token for every run:

```bash
printf '%s' "$GITHUB_APP_CLIENT_ID" | databricks secrets put-secret \
  <scope> github-app-client-id --profile <profile>
databricks secrets put-secret \
  <scope> github-app-private-key --profile <profile> < app-private-key.pem
```

The existing `github-token` secret remains a temporary fallback. Secret values
are stripped before use, so a trailing newline from stdin does not become part
of the HTTP authorization header.

Deploy with App authentication while the trigger remains paused, then run a
read-only ownership check. Confirm the run log does not contain the PAT fallback
warning:

```bash
databricks bundle deploy --target dev --profile <profile> \
  --var="model_endpoint=<endpoint>" \
  --var="github_secret_scope=<scope>" \
  --var="github_auth_mode=app" \
  --var="allow_github_writes=true"
databricks bundle run issue_prioritization --target dev --profile <profile> \
  --params mode=dry_run,regrade=false,adopt_legacy_bot_priorities=true
```

After reviewing that run, enable apply-mode table-update runs. Keep legacy
adoption enabled until new-issue artifacts are imported into `issue_bot_state`:

```bash
databricks bundle deploy --target dev --profile <profile> \
  --var="model_endpoint=<endpoint>" \
  --var="github_secret_scope=<scope>" \
  --var="github_auth_mode=app" \
  --var="allow_github_writes=true" \
  --var="scheduled_mode=apply" \
  --var="scheduled_adopt_legacy_bot_priorities=true" \
  --var="schedule_pause_status=UNPAUSED"
```

Defaults remain `token`, `dry_run`, and `PAUSED`, so an ordinary development
deployment cannot silently enable scheduled writes.

```bash
databricks bundle deploy --target dev --profile <profile> \
  --var="allow_github_writes=true" \
  --var="github_secret_scope=<scope>" \
  --var="github_auth_mode=app"
databricks bundle run issue_prioritization --target dev --profile <profile> \
  --params mode=apply,adopt_legacy_bot_priorities=true
```

That apply run is also the comment backfill. The bot finds comments by the
`omnigent-issue-prioritization-v2` marker and updates the existing comment rather
than posting another one. The base score is embedded in HTML metadata for audit
and is not rendered by GitHub; it is hidden, not secret. Visible text contains
the bot assessment, effective priority, the automated recommendation when a
human override is retained, and a concise rationale.

Keep the write variable false until a dry-run's `ranking.*` and
`mutations.json` artifacts have been reviewed. Apply mode also creates any
missing labels declared in `.github/issue-prioritization-labels.json`.

The same repository switch stops legacy intake from writing priority or
component labels. New-issue v2 becomes their owner, and Databricks runs remain
available for ranking and backfills. Event ownership is recorded in
`event.json`, but periodic apply runs preserve those labels until an artifact
importer shares that ownership with `issue_bot_state`.

## Tests

```bash
uv run --project .github/triage_v2 pytest .github/triage_v2/tests
```
