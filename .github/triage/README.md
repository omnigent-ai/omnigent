# Issue-triage GitHub Action

[`issue-triage.yml`](../workflows/issue-triage.yml) runs when a person opens an
issue and when `needs-info` is removed. Issues opened by bots are intentionally
skipped. GitHub supplies the workflow's issue-write token; no GitHub PAT or App
key is required.

## Repository settings

Configure these under **Settings → Secrets and variables → Actions**:

| Setting | Kind | Purpose |
| --- | --- | --- |
| `LLM_API_KEY` | Secret | Token accepted by the Databricks model gateway. |
| `GATEWAY_BASE_URL` | Secret | Gateway root, such as `https://<workspace>/serving-endpoints`. |
| `OMNIGENT_CI_FAST_ANTHROPIC_MODEL` | Variable | Anthropic endpoint used by the triage agent. |

Set or rotate them with `ghx` (secret commands prompt for the value):

```bash
ghx secret set LLM_API_KEY --repo omnigent-ai/omnigent
ghx secret set GATEWAY_BASE_URL --repo omnigent-ai/omnigent
ghx variable set OMNIGENT_CI_FAST_ANTHROPIC_MODEL \
  --repo omnigent-ai/omnigent --body databricks-claude-sonnet-4-6
```

If `LLM_API_KEY` is absent, the workflow exits without changing the issue.

## Safe manual test

Manual dispatch is read-only by default:

```bash
ghx workflow run issue-triage.yml --repo omnigent-ai/omnigent \
  -f issue_number=2125 -f apply_labels=false -f post_comment=false
ghx run list --repo omnigent-ai/omnigent --workflow issue-triage.yml --limit 1
```

The Action's intake prompt is [`config.yaml`](config.yaml). The Databricks v2
scoring prompt is separate. Keep `ISSUE_PRIORITIZATION_V2_ENABLED` unset or
`false` until the Databricks apply job is enabled; setting it to `true` stops
this Action from applying priority and component labels.
