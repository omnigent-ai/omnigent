# Issue prioritization pipeline

This bundle owns the issue-prioritization v2 implementation. The scoring core is
pure and reusable; Databricks and GitHub adapters are layered on top.

## Local dry-run

Prepare normalized issue JSON, then run:

```bash
uv run --project deploy/issue_prioritization issue-priority \
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

## Databricks dry-run

The bundle defines a paused six-hour job. Manual runs default to `mode=dry_run`:

```bash
databricks bundle validate --strict --target dev --profile <profile>
databricks bundle deploy --target dev --profile <profile>
databricks bundle run issue_prioritization --target dev --profile <profile>
```

The job reads open community issues from `github_issues_bronze`, persists LLM
classifications in `issue_classifications`, appends the ranking to `issue_scores`,
and writes ranking plus proposed label mutations to the managed
`issue_priority_artifacts` volume. Dry-run never changes GitHub issues.
`issue_scores_latest` always exposes the newest complete run for dashboard queries.

Force a classifier refresh after prompt changes or for a backfill:

```bash
databricks bundle run issue_prioritization --target dev --profile <profile> \
  --params regrade=true
```

For the one-time backfill, preview regrading only priorities whose latest label
event came from a known legacy bot. This needs read credentials but keeps the
GitHub write gate off:

```bash
databricks bundle deploy --target dev --profile <profile> \
  --var="github_secret_scope=<scope>" \
  --var="model_endpoint=<endpoint>"
databricks bundle run issue_prioritization --target dev --profile <profile> \
  --params regrade=true,adopt_legacy_bot_priorities=true
```

`run.json` records whether regrade/adoption was enabled and how many historical
priorities were adopted. Human-authored priority events remain blocked in
`mutations.json`.

## Dashboard draft

Prepare an idempotent local dashboard draft after a complete scoring run:

```bash
databricks api get /api/2.0/lakeview/dashboards/<dashboard-id> \
  --profile <profile> > /tmp/issue-dashboard.json
uv run --project deploy/issue_prioritization issue-priority-dashboard-draft \
  --input /tmp/issue-dashboard.json \
  --output /tmp/issue-dashboard-draft.json
```

The draft adds a complete ranking table backed by `issue_scores_latest`. The
command only writes the local output file; it never updates or publishes a
dashboard.

## GitHub apply gate

The schedule is paused. GitHub writes additionally require `mode=apply`, the
deploy variable `allow_github_writes=true`, and a configured secret scope. The
job re-reads every issue's live labels before writing and preserves maintainer
priority and severity overrides. Removing a bot-owned label is also a durable
override; human-added component labels are never removed.

```bash
databricks bundle deploy --target dev --profile <profile> \
  --var="allow_github_writes=true" \
  --var="github_secret_scope=<scope>"
databricks bundle run issue_prioritization --target dev --profile <profile> \
  --params mode=apply,adopt_legacy_bot_priorities=true
```

Keep the write variable false until a dry-run's `ranking.*` and
`mutations.json` artifacts have been reviewed. Apply mode also creates any
missing labels declared in `.github/issue-prioritization-labels.json`.

At rollout, set the repository variable `ISSUE_PRIORITIZATION_V2_ENABLED=true`
at the same time as enabling this job. That stops the legacy issue-triage action
from writing priority or component labels, so Databricks is the only owner.

## Tests

```bash
uv run --project deploy/issue_prioritization pytest deploy/issue_prioritization/tests
```
