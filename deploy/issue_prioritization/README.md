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

## Tests

```bash
uv run --project deploy/issue_prioritization pytest deploy/issue_prioritization/tests
```
