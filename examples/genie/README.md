# Sales Genie

An Omnigent agent that registers a remote **Databricks AI/BI Genie space** as
its harness. Each turn is posted to the space's Genie **Agent-mode Responses
API** (`POST /api/2.0/genie/agents/{space_id}/responses`) and streamed back over
SSE. The stream carries each step of the turn as Genie finishes it — whole
items, not token-by-token deltas — so the work is visible while it happens
rather than only at the end:

- Genie's planning arrives as **reasoning** while the turn is still running.
- Each **SQL query it runs** surfaces *during* the turn as a tool card, with the
  query's output attached when it finishes. Genie runs that SQL itself and
  Omnigent only observes it — it is not a tool call Omnigent serves.
- The **report text** lands when Genie finishes writing it, with result rows
  re-rendered as Markdown tables (capped at 50 rows) from Genie's
  machine-readable `columns` / `preview_rows` metadata — Databricks documents
  Genie's own Markdown as unstable.

Genie executes its SQL itself, so those calls are observations: this harness
dispatches no Omnigent tools. Because Omnigent never serves them, they are not
subject to `TOOL_CALL` policy gating and emit no usage toward cost budgets. The
Genie space also carries its own instructions, so the `prompt` in
[`config.yaml`](config.yaml) documents the agent rather than being sent to Genie.

For the same reason, **tools attached to a `databricks-genie` agent are inert**.
A `tools:` block — or an MCP server wired to the agent — parses and connects
without complaint, but the harness reports no tool-calling support and forwards
only the latest user message to Genie, so the tools are silently discarded. If
you need tool composition, point a tool-calling harness (`claude-sdk`, `codex`,
…) at the Genie MCP server instead of using this harness.

## Prerequisites

1. **Install the Databricks extra** (provides `databricks-sdk`):

   ```bash
   uv tool install --force "omnigent[databricks]"
   ```

2. **Authenticate with the Databricks CLI** — this writes OAuth/PAT credentials
   into `~/.databrickscfg`, which the harness reads. Every request to Genie
   carries a freshly minted bearer token, so a long turn survives OAuth
   access-token expiry:

   ```bash
   databricks auth login --host https://<your-workspace>.databricks.com
   ```

3. **A Genie space** you can query. Find its id in the Genie room URL:
   `https://<workspace>/genie/rooms/<SPACE_ID>`.

4. **Genie Agent mode enabled for the workspace.** These APIs are **Beta** and
   gated behind a preview toggle a workspace admin has to turn on. Without it,
   every turn fails with a 404 `FEATURE_DISABLED` error saying so.

## Configure

Edit [`config.yaml`](config.yaml):

- `executor.model` — your Genie **space id** (the space is the conversational
  unit, so it is carried in `model`).
- `executor.auth.profile` — the Databricks profile name from `~/.databrickscfg`.
  Drop the `auth:` block to use the default resolution
  (`DATABRICKS_CONFIG_PROFILE` env var / `[DEFAULT]` section).
- `executor.config.enable_viz` — set to `true` to ask Genie to attach
  visualizations to its answer. Defaults to `false`, and when off the field is
  omitted from the request entirely. The chart itself renders in the Genie
  room — follow the citation link in the answer; Omnigent's own output stays
  text and tables.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `HARNESS_DATABRICKS_GENIE_TIMEOUT` | `900` | Stream idle timeout in seconds — bounds each silent gap in the streamed response (one long warehouse query is a single gap), not the turn's total length. A malformed value falls back to the default. |
| `HARNESS_DATABRICKS_GENIE_ENABLE_VIZ` | unset (off) | `true` / `1` turns the visualization opt-in on. `executor.config.enable_viz` sets this for you; set it in the shell for specs that have no `config:` block. |
| `HARNESS_DATABRICKS_GENIE_MODEL` | — | The space id. Omnigent sets it from `executor.model`, which wins over any value already in your shell. |
| `HARNESS_DATABRICKS_GENIE_PROFILE` | — | The `~/.databrickscfg` profile. Omnigent sets it from `executor.auth.profile`, which wins over any value already in your shell. |

## Run

```bash
omnigent run examples/genie -p "What were total sales by region last quarter?"
```

Or interactively:

```bash
omnigent run examples/genie
```

Follow-up questions in the same session continue the same Genie conversation, so
you can refine ("now break that down by month") without repeating context. That
context lives server-side in Databricks, keyed by a `conversation_id` the
running harness process holds in memory — it is never persisted, and the harness
declares resume `NONE`. Restarting Omnigent (or resuming a saved session) starts
a fresh, contextless Genie conversation: only the latest user message is
forwarded, so earlier turns are not replayed.

If a turn fails with `RESOURCE_CONFLICT` (HTTP 409), the conversation still has
a turn in progress — wait for it to finish before sending the next message.
