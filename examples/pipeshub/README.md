# pipeshub

A single-agent example that answers questions using your organization's
knowledge — Slack, Drive, Confluence, and whatever else is connected — through
the [PipesHub](https://pipeshub.com) MCP server.

## What it does

Given a question, the agent searches your PipesHub-connected sources, reads
the relevant results, and returns a cited answer grounded in what it actually
found — instead of falling back on its own training data when a PipesHub
lookup comes up empty or fails.

## Layout

```
pipeshub/
├── config.yaml                 # the agent (claude-sdk brain, no model pinned)
├── AGENTS.md                   # instructions — search-first, cite sources, say when it fails
└── tools/mcp/pipeshub.yaml     # MCP server — auto-discovered, exposes PipesHub's tools
```

## Prerequisites

- A PipesHub deployment you have access to.
- A **personal access token**: in PipesHub, go to workspace → Developer
  settings → Personal Access Tokens → New token. Pick an expiry (30 / 90 /
  365 days, or never) and the default scope set — it's already tuned for MCP
  access. The token panel gives you a ready-to-paste block with both
  environment variables below.
- This example's brain uses the `claude-sdk` harness, so it needs a Claude
  provider configured (`omnigent setup`) — an Anthropic API key, a Claude
  subscription, an OpenAI-compatible gateway, or a Databricks workspace.

## Run it

```bash
export PIPESHUB_MCP_URL=https://my-org.pipeshub.com/mcp
export PIPESHUB_MCP_TOKEN=phpat_...

omnigent run examples/pipeshub/   # opens the UI; then ask your question
```

`tools/mcp/pipeshub.yaml` expands both `${PIPESHUB_MCP_URL}` and the token
inside its `Authorization` header at parse time, so this directory is safe to
commit as-is — no endpoint or secret is hardcoded into it.

## If a PipesHub tool call fails

A revoked or expired token, or a PipesHub outage, shows up as a startup band
in the web UI (`MCP startup incomplete ...`) rather than a silent fallback to
the model's own training data. If you see it, check that your token hasn't
been revoked (workspace → Developer settings → Personal Access Tokens) before
assuming the deployment itself is down.

## Why MCP (not a custom connector)

PipesHub is reached through omnigent's standard MCP path — the same
extension point any MCP server uses — so there are no core changes involved.
The same pattern (one agent + one `tools/mcp/*.yaml` file) works for any other
MCP server; see `examples/deep-research/` for another one.

## Attach PipesHub without cloning this example

You don't need this packaged example to use PipesHub — the fastest path for
most people is the web UI's own MCP attach flow: open a session's info panel,
add an MCP server, paste your PipesHub URL and `Authorization: Bearer <token>`
header, and restart the session. This example exists for users who want a
ready-made agent bundle (a tuned prompt + instructions) rather than attaching
PipesHub to an agent they already have.
