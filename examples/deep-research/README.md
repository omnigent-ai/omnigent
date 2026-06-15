# deep-research

A single-agent example that does **cited, cross-checked web research** using an
MCP search server.

Unlike the other examples (which orchestrate coding sub-agents), this one shows
the simplest high-value pattern: **one agent + one MCP tool server**. It is also
the repo's first example that wires an MCP server via `tools/mcp/*.yaml`.

## What it does

Given a question, the agent:
1. decomposes it into focused sub-queries,
2. searches the live web (`search_web_pages`) and reads full pages
   (`fetch_page_content`) via the Keenable MCP server,
3. cross-checks each claim across independent sources, and
4. returns a synthesized answer with inline citations + a sources list.

## Layout

```
deep-research/
├── config.yaml                     # the agent (claude-sdk brain, no model pinned)
├── tools/mcp/keenable.yaml         # MCP server — auto-discovered, exposes the search/fetch tools
└── skills/deep-research/SKILL.md   # the research procedure the agent follows
```

## Run it

This example's brain uses the `claude-sdk` harness, so it needs a Claude
provider configured (`omnigent setup`) — an Anthropic API key, a Claude
subscription, an OpenAI-compatible gateway, or a Databricks workspace.

The Keenable MCP endpoint (`https://api.keenable.ai/mcp`) has a **keyless
public mode** (rate-limited), so the search side runs with zero signup:

```bash
omnigent run examples/deep-research/   # opens the UI; then ask your question
```

To lift the public rate limits, add an API key: uncomment the `X-API-Key`
line in `tools/mcp/keenable.yaml`'s `headers` block and set `KEENABLE_API_KEY`.

## Why MCP (not a custom backend)

Keenable is reached through omnigent's standard MCP path — the framework's
sanctioned extension point — so there are **no core changes**: the agent's tools
are defined entirely by its config, exactly as omnigent intends.
