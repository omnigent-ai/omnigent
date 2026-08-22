# AWS Analyst

An example Omnigent agent that answers questions over **governed AWS data** through
the official [AWS Labs MCP servers](https://github.com/awslabs/mcp) — no custom
connector code required. It shows how any AWS Labs MCP server plugs into Omnigent as
a `type: mcp` tool.

Wired connectors (both **read-only** by default):

| Connector | AWS Labs server | Tools surfaced |
|---|---|---|
| `redshift` | `awslabs.redshift-mcp-server` | `list_clusters`, `list_databases`, `list_schemas`, `list_tables`, `list_columns`, `execute_query` |
| `s3-tables` | `awslabs.s3-tables-mcp-server` | metadata discovery + read-only SQL |

## Prerequisites

- [`uv`/`uvx`](https://docs.astral.sh/uv/) on `PATH` — the AWS Labs servers are
  published to PyPI as `awslabs.*` and launched via `uvx ...@latest`.
- AWS credentials the servers can resolve: an `AWS_PROFILE` + `AWS_REGION`, or an
  IAM role on the host.

## Run

```bash
AWS_PROFILE=my-profile AWS_REGION=us-east-1 omnigent run examples/aws_analyst
```

## Notes

- The S3 Tables server defaults to read-only; this recipe intentionally does **not**
  pass `--allow-write`.
- The `tools:` allow-list on the Redshift connector limits what the model can call —
  a good default for a governed analytics agent.
- Pairs naturally with a Databricks Genie connector for a Databricks-on-AWS
  "better together" analyst that reasons across both platforms.

## Run the brain on Amazon Bedrock (governed by Databricks AI Gateway)

The connectors above govern the *data*. You can also run the agent's *reasoning* on
**Amazon Bedrock**, governed by **Databricks AI Gateway** — a fully AWS-native setup
that's governed end to end.

Amazon Bedrock is a supported provider for Databricks
[external model endpoints](https://docs.databricks.com/aws/en/generative-ai/external-models/)
(`amazon-bedrock`). Serve a Bedrock-hosted Anthropic Claude model as a Databricks
serving endpoint, enable [AI Gateway](https://docs.databricks.com/aws/en/ai-gateway/) on it, then point this agent at that endpoint via
the `claude-sdk` harness:

```yaml
executor:
  type: omnigent
  config:
    harness: claude-sdk
  # Name of YOUR Databricks external-model serving endpoint (backed by Bedrock).
  model: databricks-claude-bedrock
  auth:
    type: databricks
    profile: my-databricks-profile
```

The `claude-sdk` harness runs any Claude model; here the endpoint is served by
Amazon Bedrock and fronted by Databricks AI Gateway, so every LLM call picks up rate
limiting, usage/cost tracking, payload logging, and guardrails — while the Redshift
and S3 Tables connectors keep the *data* access read-only. Reasoning on Bedrock, data
governed in AWS, all model calls governed through Databricks.

> Not validated end-to-end in this repo — this documents a supported configuration
> path (Bedrock external model + AI Gateway on Databricks Model Serving), verified
> against the Databricks docs linked above. Substitute your own endpoint name and
> profile.
