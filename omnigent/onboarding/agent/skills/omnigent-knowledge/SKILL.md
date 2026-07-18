---
name: omnigent-knowledge
description: Deep reference on Omnigent config format, executor types, skill/tool structure, and conventions. Load when you need to look up how the platform works.
---

# Omnigent Knowledge Base

## What is Omnigent?

Agent plane is a server that hosts, manages, and executes agents via an
OpenResponses-compatible API. Users create **agent directories** (also
called agent images) that contain configuration, instructions, skills,
and tools. The server loads these directories and serves them via HTTP.

## Agent Directory Layout

```
my-agent/
├── config.yaml          # REQUIRED — agent spec
├── AGENTS.md            # Recommended — instructions/personality
├── skills/              # Optional — load-on-demand skills
│   └── <skill-name>/
│       └── SKILL.md
├── tools/               # Optional — packaged tools
│   ├── python/          # Local Python tools (auto-discovered *.py)
│   ├── typescript/      # Local TypeScript tools (auto-discovered *.ts)
│   └── mcp/             # MCP server declarations (*.yaml)
└── agents/              # Optional — sub-agent directories (recursive)
    └── <agent-name>/
        ├── config.yaml
        └── ...
```

## config.yaml Reference

The only required file. All fields except `spec_version` are optional.

```yaml
spec_version: 1               # REQUIRED, must be 1

name: my-agent                # Display name
description: Does X and Y.    # One-line summary

# Instructions — path to a file or inline text.
# Default: looks for AGENTS.md in the agent directory.
instructions: AGENTS.md

executor:
  # REQUIRED area. type must be one of: claude_sdk | agents_sdk | omnigent.
  # There is NO `llm` executor type.
  type: claude_sdk     # Anthropic Claude SDK, in-process (simplest)
  # type: agents_sdk   — OpenAI Agents SDK, in-process
  # type: omnigent     — subprocess harness; requires config.harness below

  # Only for type: omnigent — pick the harness that runs the loop.
  # One of: claude-native | claude-sdk | codex-native | codex |
  #         openai-agents | open-responses | pi
  # config:
  #   harness: claude-native
  #   permission_mode: bypassPermissions   # claude-native headless
  #   yolo: true                           # codex-native headless

  # Model is OPTIONAL — omit to use the configured provider's default.
  # Pin one directly on the executor when needed:
  # model: anthropic/claude-sonnet-4-20250514   # LiteLLM provider/model
  # model: databricks-claude-opus-4-7           # or a serving-endpoint name
  # connection:                                 # provider credentials
  #   api_key: ${ANTHROPIC_API_KEY}
  # auth:                                        # or Databricks profile auth
  #   type: databricks
  #   profile: oss

  timeout: 3600        # Task deadline in seconds (default: 3600)
  max_iterations: 1000 # Max LLM calls per task (default: 1000)

# os_env — grant filesystem/shell access (harness agents). Exposes
# sys_os_read / sys_os_write / sys_os_edit / sys_os_shell.
os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none         # or linux_bwrap / darwin_seatbelt to sandbox

# guardrails — runtime policy gates (optional).
guardrails:
  ask_timeout: 86400   # seconds to wait on an approval prompt
  policies:
    blast_radius:
      type: function
      function:
        path: omnigent.inner.nessie.policies.blast_radius

interaction:
  conversational: true   # Maintain turn history (default: true)
  modalities:
    input: [text, image, file]   # default: [text]
    output: [text]               # default: [text]

tools:
  # Sub-agents this agent can spawn (must match agents/ subdirectories)
  agents:
    - researcher
    - summarizer

  # Built-in tools — string name or dict with config
  builtins:
    - web_search                 # auto-detects backend based on model provider
    - terminal_run               # persistent bash shell scoped to the conversation
    - upload_file
    - search_conversations

  timeout: 60          # Default tool timeout in seconds

params:                # Arbitrary key-value (readable by skills/tools)
  max_results: 10
```

## Executor Types

| Type | When to use | How it works |
|------|------------|--------------|
| `claude_sdk` | New simple agents; existing Claude SDK code | In-process Anthropic Claude SDK; it manages its own loop |
| `agents_sdk` | New simple agents; existing OpenAI Agents SDK code | In-process OpenAI Agents SDK runner |
| `omnigent` | Coding/CLI harnesses, shell + file tools, sub-agents | Spawns a subprocess harness selected by `config.harness` |

**There is no `llm` executor type** — the only valid values are
`claude_sdk`, `agents_sdk`, and `omnigent`. For **most new simple agents**,
use `claude_sdk` (or `agents_sdk`) — in-process, no extra config. Use
`omnigent` when the agent needs a specific harness, shell/file access, or
sub-agents; it **requires** a `config.harness`:

| `config.harness` | What it is |
|------------------|------------|
| `claude-native` (alias `claude`) | Claude Code — full coding tools, native permissions |
| `claude-sdk` | Claude Agent SDK loop |
| `codex-native` / `codex` | Codex CLI / harness |
| `openai-agents` | OpenAI Agents harness (any gateway model) |
| `open-responses` | OpenResponses-compatible harness |
| `pi` | Headless multi-model worker (bridged `sys_os_*` tools) |

## AGENTS.md Format

Free-form markdown. This becomes the agent-authored portion of the system
prompt; Omnigent may append framework-owned lifecycle or metadata instructions
at runtime. Best practices:

- Start with a clear identity statement ("You are a ...")
- List capabilities and constraints
- Reference skills by name ("You have a skill called deep-research")
- Reference sub-agents if any ("You can spawn the fact_checker agent")
- Keep it focused — the model reads this on every turn

## Skills Format

Each skill lives in `skills/<skill-name>/SKILL.md`:

```markdown
---
name: deep-research
description: Investigate a topic in depth using web search and source synthesis.
---

When researching a topic:

1. Search broadly first using web search...
2. Cross-reference multiple sources...
```

Rules:
- YAML frontmatter with `name` and `description` (both required)
- `name` must match the directory name, be lowercase, use `[a-z0-9-]+`
- Body is markdown instructions loaded on demand by the agent
- Referenced in AGENTS.md or config.yaml

## Tools

### Built-in tools

Call `list_builtin_tools` to get the current set of available
built-in tools and their descriptions. Do not rely on a hardcoded
list — new tools may be added at any time.

**Tool recommendation guide:**

- "I want a research agent" → `web_search` + `web_fetch`
- "I want a coding agent" → `terminal_run` + `upload_file`
- "I want a data analysis agent" → `terminal_run` + `upload_file` + `download_file`
- "I want a conversational assistant" → no tools needed (or `web_search` for current info)
- "I want an agent that can access external APIs" → consider MCP servers (see below)

### MCP servers (external tool integrations)

MCP (Model Context Protocol) lets agents connect to external services —
databases, APIs, Slack, GitHub, etc. Each MCP server is declared as a
YAML file in `tools/mcp/`:

```
my-agent/
  tools/
    mcp/
      github.yaml
      slack.yaml
```

**MCP server config format** (`tools/mcp/github.yaml`):

```yaml
transport: http
url: https://mcp-server.example.com/sse
headers:
  Authorization: Bearer ${GITHUB_TOKEN}
```

- `transport`: must be `http`
- `url`: the MCP server's SSE endpoint URL
- `headers`: optional auth headers (use `${ENV_VAR}` for secrets)

**When to recommend MCP:**

- User wants to connect to an external service (database, API, SaaS tool)
- User mentions Slack, GitHub, Jira, Postgres, etc.
- The integration isn't covered by built-in tools

**Finding MCP servers:** Use `web_search` (if available) or `web_fetch`
to search for available MCP servers. Good starting points:
- https://modelcontextprotocol.io — official MCP directory
- https://github.com/modelcontextprotocol — official GitHub org
- Search for "<service-name> MCP server" (e.g. "Slack MCP server",
  "Postgres MCP server")

If the user mentions a specific service they want to connect to,
use `web_search` or `web_fetch` to find if an MCP server exists
for it and how to configure it.

**What to tell the user:** MCP servers are external processes that
expose tools via HTTP. The user needs to run the MCP server separately
(or use a hosted one) and provide the URL in the config.

### Local tools (custom Python/TypeScript)

Python files in `tools/python/` are auto-discovered. Each
`@tool`-decorated module-level function in those files becomes a
separate tool — one file may export many tools. The decorator
derives the JSON schema from the function's type hints and
Google-style docstring.

```python
# tools/python/my_tools.py
from omnigent.tools import tool


@tool
def my_tool(text: str, count: int = 1) -> str:
    """
    Repeat the text count times.

    Args:
        text: The text to repeat.
        count: Number of repetitions (default 1).
    """
    return text * count
```

Authoring rules:

- Decorate a **module-level** function — not a class method,
  lambda, or nested function (the decorator rejects those at
  decoration time with a clear error).
- Type hints on parameters drive the LLM-facing JSON schema. Use
  concrete types — `Any` and `object` produce permissive schemas
  with no validation.
- The function name becomes the LLM-facing tool name. Names must
  not collide with built-in tools or with other custom tools in
  the same agent (collisions fail loud at agent load).
- Both `def` and `async def` are supported. Sync `def` bodies are
  wrapped in `asyncio.to_thread` automatically so they don't
  block the event loop.
- Pydantic `BaseModel` arguments are first-class — they get
  expanded into the schema correctly with full validation.

**When to recommend local tools:** When the user needs custom logic that
isn't covered by builtins or MCP servers.

## Example: Minimal Agent

```yaml
spec_version: 1
name: my-assistant
description: A helpful assistant.
executor:
  type: claude_sdk
instructions: |
  You are a helpful assistant. Answer questions clearly and concisely.
```

This is the simplest valid agent — a name, an executor, and instructions.
No model is pinned, so it uses the configured provider's default. No
skills, no tools, no sub-agents.

## Example: Research Agent with Tools and Skills

```yaml
spec_version: 1
name: researcher
description: A research agent that searches the web and synthesizes findings.
executor:
  type: agents_sdk
tools:
  builtins:
    - web_search
    - upload_file
interaction:
  modalities:
    input: [text, file]
    output: [text]
instructions: AGENTS.md
```

## Sub-Agents (multi-agent systems)

An agent can spawn child agents to delegate tasks. Sub-agents are
full agents with their own config.yaml, living in the `agents/`
directory:

```
my-agent/
  config.yaml
  AGENTS.md
  agents/
    researcher/
      config.yaml        # sub-agent spec
    fact-checker/
      config.yaml        # another sub-agent
```

### Declaring sub-agents

The parent's config.yaml lists sub-agent names under `tools.agents`:

```yaml
tools:
  agents:
    - researcher
    - fact-checker
  builtins:
    - web_search
```

Each name must match a directory under `agents/`. The **parent** must use
`executor.type: omnigent` — that's what provides the spawn tools. Each
sub-agent is a full agent and may use any executor (`claude_sdk`,
`agents_sdk`, or `omnigent`).

### Sub-agent config

Each sub-agent has its own complete config.yaml:

```yaml
# agents/researcher/config.yaml
spec_version: 1
name: researcher
description: Sub-agent that searches the web for information.
executor:
  type: claude_sdk
tools:
  builtins:
    - web_search
    - web_fetch
instructions: |
  You are a researcher. When given a topic, search the web
  and return a summary with sources.
```

### How spawning works

The parent agent gets `sys_session_send` (singular), `check_task`,
and `sys_cancel_task` tools automatically when sub-agents are
declared. The parent's AGENTS.md should reference them:

```markdown
You have two sub-agents you can delegate to:
- **researcher** — searches the web for information
- **fact-checker** — verifies claims with evidence

Call `sys_session_send(type="<name>", input="<task>")` to
dispatch one. Emit multiple `sys_session_send` tool calls in the
same response to run sub-agents in parallel. Each result auto-
delivers as a system message when ready — `check_task` polls,
`sys_cancel_task` aborts.
```

### When to recommend sub-agents

- User wants specialized roles (researcher + summarizer + reviewer)
- User wants parallel execution (search multiple sources at once)
- User wants separation of concerns (each sub-agent has focused instructions)

**For simple agents, sub-agents are overkill.** Only suggest them when
the user describes a workflow with distinct steps or roles.

## Running an Agent

Once the agent directory is created:

```bash
# Start the server with the agent pre-registered
ap server --agent ./my-agent/

# Or deploy to a running server
ap deploy ./my-agent/ --server http://localhost:6767
```
