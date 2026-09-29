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

Set the two variables from the token panel first:

```bash
export PIPESHUB_MCP_URL=https://my-org.pipeshub.com/mcp
export PIPESHUB_MCP_TOKEN=phpat_...
```

**From a repo checkout:**

```bash
omnigent run examples/pipeshub/   # opens the UI; then ask your question
```

**From a `uv tool install` or `pip install`:** the installed package ships
this example, but there is no `examples/` directory next to it, so copy the
example into the current directory once and run the copy:

```bash
# uv tool install (what the install script uses):
"$(uv tool dir)/omnigent/bin/python" -c 'import importlib.resources as r, shutil; shutil.copytree(r.files("omnigent.resources.examples") / "pipeshub", "pipeshub")'
# pip install: same command with that environment's python
python -c 'import importlib.resources as r, shutil; shutil.copytree(r.files("omnigent.resources.examples") / "pipeshub", "pipeshub")'

omnigent run pipeshub/
```

The copy is yours to edit (the prompt in `AGENTS.md`, the tool allow-list in
`tools/mcp/pipeshub.yaml`); rerunning the copy command stops with an error
instead of overwriting it.

### Where the endpoint and token come from

`tools/mcp/pipeshub.yaml` holds only the templates `${PIPESHUB_MCP_URL}` and
`Bearer ${PIPESHUB_MCP_TOKEN}`. Omnigent fills them in from your environment
when it loads the agent (`omnigent run` does this in your shell, before the
agent is uploaded to the server), and never writes the values back to disk.
So this directory, or your copy of it, is safe to commit as-is. If either
variable is unset, Omnigent stops with `Unresolved environment variable
'${PIPESHUB_MCP_URL}'` (or the same for the token) instead of sending the
literal text to PipesHub.

## If PipesHub can't be reached

If the PipesHub MCP server can't be reached when the session starts (a
revoked or expired token, a wrong URL, or an outage), the session still opens,
but without PipesHub's tools, and the chat shows no notice about it. The
agent's instructions tell it to say it has no PipesHub access rather than
answer from its own training data. The reason is in the runner log:
`omnigent debug logs` shows a `runner mcp connect failed ... server=pipeshub`
line with the error. A revoked token is the most common cause, so check
workspace → Developer settings → Personal Access Tokens before assuming the
deployment itself is down.

## Why MCP (not a custom connector)

PipesHub is reached through omnigent's standard MCP path — the same
extension point any MCP server uses — so there are no core changes involved.
The same pattern (one agent + one `tools/mcp/*.yaml` file) works for any other
MCP server; see `examples/deep-research/` for another one.

## Attach PipesHub without cloning this example

You don't need this packaged example to use PipesHub — the fastest path for
most people is the web UI's own MCP attach flow: open a session's info panel,
click **+** next to *Tools* (Manage MCP servers), add a server with your
PipesHub URL and an `Authorization: Bearer <token>` header, and restart the
session. This example exists for users who want a
ready-made agent bundle (a tuned prompt + instructions) rather than attaching
PipesHub to an agent they already have.
