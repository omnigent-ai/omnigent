# parallel-search

A single-agent example that produces cited, cross-checked web research using
the existing Omnigent remote-MCP path and the Parallel Search MCP server.

## What it does

Given a question, the agent:

1. decomposes it into focused searches,
2. uses `web_search` to find candidate sources,
3. uses `web_fetch` to read the pages it cites,
4. cross-checks important claims, and
5. returns a structured answer with inline citations and a Sources list.

## Layout

```
parallel_search/
├── config.yaml                         # the agent
├── tools/mcp/parallel.yaml             # auto-discovered MCP server
└── skills/parallel-search/SKILL.md     # the research procedure
```

## Run it

The example's brain uses the `claude-sdk` harness, so configure an LLM provider
with `omnigent setup` first. The Parallel Search MCP endpoint itself is free
and needs no account, API key, or OAuth configuration:

```bash
omnigent run examples/parallel_search/
```

The server declaration is auto-discovered from `tools/mcp/parallel.yaml`; no
`tools:` block or local shell environment is required. It exposes
`web_search` for live search and `web_fetch` for reading a specific URL.

This is an opt-in provider example. The existing
[`examples/deep-research/`](../deep-research/) bundle and its Keenable server
remain unchanged.
