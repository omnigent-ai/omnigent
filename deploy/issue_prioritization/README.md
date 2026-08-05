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
but disabled by default.

## Databricks dry-run

The bundle defines a paused six-hour job. Manual runs default to `mode=dry_run`:

```bash
databricks bundle validate --strict --target dev --profile <profile>
databricks bundle deploy --target dev --profile <profile>
databricks bundle run issue_prioritization --target dev --profile <profile>
```

The job reads open community issues from `github_issues_bronze`, persists LLM
classifications in `issue_classifications`, appends the ranking to `issue_scores`,
and writes the same review artifacts to the managed `issue_priority_artifacts`
volume. It has no GitHub mutation adapter in this layer.

## Tests

```bash
uv run --project deploy/issue_prioritization pytest deploy/issue_prioritization/tests
```
