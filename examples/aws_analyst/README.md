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
