# Sales Genie

An Omnigent agent that registers a remote **Databricks AI/BI Genie space** as
its harness. Each turn is forwarded to the Genie space, which answers
natural-language questions over your curated data and returns a text summary,
the SQL it generated, and the result rows.

## Prerequisites

1. **Install the Databricks extra** (provides `databricks-sdk`):

   ```bash
   uv tool install --force "omnigent[databricks]"
   ```

2. **Authenticate with the Databricks CLI** — this writes OAuth/PAT credentials
   into `~/.databrickscfg`, which the harness reads (refreshing OAuth tokens
   transparently):

   ```bash
   databricks auth login --host https://<your-workspace>.databricks.com
   ```

3. **A Genie space** you can query. Find its id in the Genie room URL:
   `https://<workspace>/genie/rooms/<SPACE_ID>`.

## Configure

Edit [`config.yaml`](config.yaml):

- `executor.model` — your Genie **space id** (the space is the conversational
  unit, so it is carried in `model`).
- `executor.auth.profile` — the Databricks profile name from `~/.databrickscfg`.
  Drop the `auth:` block to use the default resolution
  (`DATABRICKS_CONFIG_PROFILE` env var / `[DEFAULT]` section).

## Run

```bash
omnigent run examples/genie -p "What were total sales by region last quarter?"
```

Or interactively:

```bash
omnigent run examples/genie
```

Follow-up questions in the same session continue the same Genie conversation,
so you can refine ("now break that down by month") without repeating context.
