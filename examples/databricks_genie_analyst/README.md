# Databricks Genie Analyst

An example Omnigent agent that answers natural-language questions over
**Unity Catalog-governed data** through a [Databricks Genie](https://docs.databricks.com/aws/en/genie/)
space — reached over the **Databricks-managed MCP server**, no custom connector
code required.

This is the Databricks-on-AWS companion to the [`aws_analyst`](../aws_analyst/)
example. Where `aws_analyst` reaches Amazon Redshift + S3 Tables through the AWS
Labs MCP servers, this agent reaches Databricks Genie through its managed MCP
server — the two together make a "better together" analyst that reasons across
both platforms.

Wired connector (**read-only** — Genie answers questions and returns SQL, it
does not mutate data):

| Connector | Server | Transport | Auth |
|---|---|---|---|
| `genie` | Databricks-managed MCP (`/api/2.0/mcp/genie/<space-id>`) | `http` | OAuth via `databricks_profile` |

## Prerequisites

- A Databricks workspace on AWS with a **Genie space** you can query.
- The [Databricks CLI](https://docs.databricks.com/aws/en/dev-tools/cli/) with a
  configured profile that can reach that space:
  ```bash
  databricks auth login --profile my-profile
  ```
  omnigent resolves an OAuth token from that profile at connection time and
  injects it as the `Authorization` header — **no token is written into the
  config**.

## Run

```bash
DATABRICKS_HOSTNAME=my-workspace.cloud.databricks.com \
DATABRICKS_GENIE_SPACE_ID=01ef1234-5678-90ab-cdef-1234567890ab \
DATABRICKS_PROFILE=my-profile \
  omnigent run examples/databricks_genie_analyst
```

- `DATABRICKS_HOSTNAME` — your workspace hostname (no scheme).
- `DATABRICKS_GENIE_SPACE_ID` — the id of the Genie space (from its URL).
- `DATABRICKS_PROFILE` — the `~/.databrickscfg` profile to authenticate with.

## Notes

- The connector uses omnigent's built-in Databricks auth (`databricks_profile`):
  it mints a short-lived OAuth bearer token per connection instead of hardcoding
  one. If you'd rather pass a token explicitly, drop `databricks_profile` and set
  `headers: { Authorization: "Bearer ${DATABRICKS_TOKEN}" }` instead.
- The managed MCP URL follows the documented pattern
  `https://<workspace-hostname>/api/2.0/mcp/genie/<space-id>`; on-behalf-of-user
  auth uses the `genie` OAuth scope.
- Genie is inherently read-only from the MCP surface — it turns questions into
  governed SQL over Unity Catalog and returns the SQL alongside the answer, so
  every result is auditable.
- Pairs naturally with [`aws_analyst`](../aws_analyst/) for a Databricks-on-AWS
  "better together" analyst that reasons across Databricks **and** native AWS
  data sources.
