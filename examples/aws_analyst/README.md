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

## Optional: run the reasoning on Amazon Bedrock through Databricks Unity Gateway

By default this agent uses your default Omnigent model provider. A `kind: bedrock`
provider only works with the native `omnigent claude` terminal, not the `claude-sdk`
harness this example runs on. To run the agent's reasoning on **Amazon Bedrock**,
route it through [Databricks Unity Gateway](https://docs.databricks.com/aws/en/ai-gateway/).
The Redshift and S3 Tables connectors are unaffected either way.

1. Register Bedrock as a Unity Catalog
   [model provider service](https://docs.databricks.com/aws/en/ai-gateway/create-model-provider-services)
   (provider type **Amazon Bedrock**) with an Anthropic Claude model as its target,
   since `claude-sdk` calls the gateway with the Anthropic Messages API.
   Authenticate it with a Unity Catalog service credential (an IAM role that can
   invoke that model) so no AWS keys are stored and callers never see the credential.
2. Create a [model service](https://docs.databricks.com/aws/en/ai-gateway/model-services/)
   that routes to that provider, for example `main.default.aws_analyst_claude`. Grant
   `EXECUTE` on it (plus `USE CATALOG` and `USE SCHEMA`) to whoever runs the agent.
   Model services run with their owner's privileges, so the owner also needs
   `EXECUTE` on the provider service.
3. Log in the Databricks CLI profile the agent will use
   (`databricks auth login --profile my-databricks-profile`), then replace the
   `executor:` block in `config.yaml` with:

```yaml
executor:
  type: omnigent
  # Three-part name of YOUR Unity Gateway model service (backed by Bedrock).
  model: main.default.aws_analyst_claude
  auth:
    type: databricks
    profile: my-databricks-profile
  config:
    harness: claude-sdk
```

As in the [agent spec](../../docs/AGENT_YAML_SPEC.md#executor), `model` and `auth`
sit directly under `executor:`. Nested inside `config:`, `claude-sdk` ignores them
and the run silently falls back to your default model setup. Also unset
`CLAUDE_CODE_USE_BEDROCK` if your shell exports it: Claude Code then calls Bedrock
itself and bypasses the gateway.

`claude-sdk` now sends the agent's turns through your workspace's Unity Gateway (its
Anthropic endpoint, `/ai-gateway/anthropic` by default) to the Bedrock-hosted model,
and the IAM role stays in your AWS account. Unity Catalog grants decide who can call
the model service, and Unity Gateway applies the rate limits and guardrails you set
on it and records usage and estimated spend, which a Databricks budget counts when
you enable **Include external model usage**. Side calls, such as session titles and
any call on Claude Code's `opus`/`sonnet`/`haiku` aliases, still go through Unity
Gateway but to the workspace's `system.ai` Claude models rather than to Bedrock. To
send session titles to your model service too, set
`OMNIGENT_BACKGROUND_TITLE_CLAUDE_MODEL=main.default.aws_analyst_claude`.
The connectors keep the *data* access read-only.

Substitute your own model service name and profile.
