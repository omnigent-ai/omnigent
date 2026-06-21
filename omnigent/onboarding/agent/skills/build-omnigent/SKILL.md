---
name: build-omnigent
description: Patterns and templates for generating valid Omnigent agent directories. Load when ready to create files.
---

# Agent Generation

Use these patterns to generate a valid agent directory. Always generate
the minimal set of files needed — don't over-engineer.

Every template below has been validated with the same parser/validator
that `omnigent server` uses. If your environment exposes the
`validate_agent` tool (the dedicated agent-authoring environment does),
run it after generating files to confirm the spec loads. Load the
**`omnigent-knowledge`** skill if you need the deeper field reference
(executor types, os_env, guardrails, sandboxing).

## Step 1: Choose a directory name

Use the agent name in kebab-case: `my-research-agent/`

## Step 2: Generate config.yaml

Always include:
- `spec_version: 1`
- `name` (lowercase, hyphens OK)
- `description` (one sentence)
- `instructions` — path to a file (default `AGENTS.md`) or inline text.
  (`prompt:` is an accepted alias; `instructions:` wins if both are set.)
- `executor` — how the agent runs. See Step 2a.

Include if needed:
- `tools.builtins` — built-in tools. The current set is `download_file`,
  `export_agent`, `list_files`, `search_conversations`, `upload_file`,
  `web_fetch`, `web_search`. If the `list_builtin_tools` tool is available,
  call it for the authoritative live set rather than trusting this list.
- `tools.agents` — sub-agents (must match `agents/` subdirectories).
- `os_env` — filesystem/shell access for harness agents (see the
  shell-capable template).
- `interaction.modalities` — if the agent handles images or files.
- `guardrails` — runtime policy gates (see `omnigent-knowledge`).

## Step 2a: Choose an executor

`executor.type` must be one of **`claude_sdk`**, **`agents_sdk`**, or
**`omnigent`**. There is **no `llm` executor** — do not use it.

| Need | executor |
|------|----------|
| A fresh, simple LLM agent (default) | `claude_sdk` (Anthropic) or `agents_sdk` (OpenAI), in-process |
| Existing Claude SDK / OpenAI Agents SDK code | `claude_sdk` / `agents_sdk` |
| A CLI/coding harness, shell + file tools, sub-agents | `omnigent` + a `config.harness` |

When `executor.type: omnigent`, **`config.harness` is required** and must
be one of: `claude-native` (Claude Code, full coding tools), `claude-sdk`,
`codex-native`, `codex`, `openai-agents`, `open-responses`, `pi`.
(`claude` is an alias for `claude-native`.)

Model selection is optional — if omitted, the executor resolves the
provider's default model from the configured credentials (e.g. an
Anthropic key, a Claude subscription, or a Databricks profile). Pin one
only when asked; see `omnigent-knowledge` for `executor.model` / `auth`.

## Step 3: Generate AGENTS.md

Write a focused system prompt:
- Identity: "You are a [role] that [does what]."
- Capabilities: what tools/skills are available
- Constraints: what NOT to do
- Style: how to communicate

Keep it under 500 words for a starter agent. The user can expand later.

## Step 4: Generate skills (optional)

Only generate skills if the agent has distinct modes of operation.
Each skill needs:

```
skills/<skill-name>/SKILL.md
```

With YAML frontmatter:
```markdown
---
name: skill-name
description: One-line description of what this skill does.
---

Detailed instructions for when this skill is loaded...
```

## Templates

### Minimal agent (simplest — in-process SDK)

**config.yaml:**
```yaml
spec_version: 1
name: {agent_name}
description: {description}
executor:
  type: claude_sdk      # or agents_sdk for OpenAI
instructions: AGENTS.md
```

**AGENTS.md:**
```markdown
You are {agent_name}, {description}.

Answer questions clearly and concisely. If you don't know something,
say so rather than guessing.
```

### Agent with web search

**config.yaml:**
```yaml
spec_version: 1
name: {agent_name}
description: {description}
executor:
  type: claude_sdk
tools:
  builtins:
    - web_search        # one of the builtins listed in Step 2
interaction:
  modalities:
    input: [text]
    output: [text]
instructions: AGENTS.md
```

### Harness agent with shell + filesystem access

Use the `omnigent` executor with a coding harness when the agent needs to
run commands and read/write files. `os_env` grants OS access; the harness
exposes `sys_os_read` / `sys_os_write` / `sys_os_edit` / `sys_os_shell`.

**config.yaml:**
```yaml
spec_version: 1
name: {agent_name}
description: {description}
executor:
  type: omnigent
  config:
    harness: claude-native
    # Headless runs can't answer approval prompts — bypass them. Pair
    # with a read-only prompt and/or a blast_radius guardrail for safety.
    permission_mode: bypassPermissions   # codex-native uses `yolo: true`
os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none          # or linux_bwrap / darwin_seatbelt to sandbox
instructions: AGENTS.md
```

### Agent with MCP server integration

**Directory structure:**
```
{agent_name}/
  config.yaml
  AGENTS.md
  tools/
    mcp/
      github.yaml
```

**config.yaml:**
```yaml
spec_version: 1
name: {agent_name}
description: {description}
executor:
  type: claude_sdk
instructions: AGENTS.md
```

**tools/mcp/github.yaml:**
```yaml
transport: http
url: https://your-mcp-server.example.com/sse
headers:
  Authorization: Bearer ${{{mcp_token_var}}}
```

### Multi-agent system with sub-agents

Sub-agents need the `omnigent` executor (it provides the spawn tools).

**Directory structure:**
```
{agent_name}/
  config.yaml
  AGENTS.md
  agents/
    {sub_agent_1}/
      config.yaml
    {sub_agent_2}/
      config.yaml
```

**Parent config.yaml:**
```yaml
spec_version: 1
name: {agent_name}
description: {description}
executor:
  type: omnigent
  config:
    harness: claude-sdk
tools:
  agents:
    - {sub_agent_1}
    - {sub_agent_2}
instructions: AGENTS.md
```

**Sub-agent config (agents/{sub_agent_1}/config.yaml):**
```yaml
spec_version: 1
name: {sub_agent_1}
description: {sub_agent_1_description}
executor:
  type: omnigent
  config:
    harness: claude-sdk
instructions: |
  You are {sub_agent_1}. {sub_agent_1_instructions}
```

**Parent AGENTS.md should reference sub-agents:**
```markdown
You have sub-agents you can delegate to:
- **{sub_agent_1}** — {sub_agent_1_description}
- **{sub_agent_2}** — {sub_agent_2_description}

Call `sys_session_send(type="<name>", input="<task>")` to dispatch a
declared sub-agent. Emit multiple `sys_session_send` tool calls in the
same response to run them in parallel; results arrive via the inbox.
```

## Environment variable naming conventions

When pinning credentials with `${ENV_VAR}`, map providers to their
standard env var names:
- `openai` → `OPENAI_API_KEY`
- `anthropic` → `ANTHROPIC_API_KEY`
- `gemini` → `GEMINI_API_KEY` or `GOOGLE_API_KEY`
- `groq` → `GROQ_API_KEY`
- `deepseek` → `DEEPSEEK_API_KEY`
- `xai` → `XAI_API_KEY`
- `mistral` → `MISTRAL_API_KEY`
- `databricks` → `DATABRICKS_TOKEN` (or an `auth.profile`)

## Validation checklist

Before presenting the generated files to the user, verify (and if
`validate_agent` is available, run it to confirm):
- [ ] `spec_version: 1` is present
- [ ] `name` is set and uses lowercase + hyphens
- [ ] `executor.type` is one of `claude_sdk`, `agents_sdk`, `omnigent`
- [ ] If `executor.type: omnigent`, `executor.config.harness` is set to a
      valid harness
- [ ] `instructions` (or `prompt`) points to a file that exists or is
      inline text
- [ ] When declaring `tools.agents`, the parent uses `executor.type:
      omnigent`, and each entry has a matching `agents/` subdirectory
      (sub-agents may use any executor)
- [ ] `tools.builtins` names are from the known set (Step 2) — or, if
      `list_builtin_tools` is available, were confirmed against it
- [ ] Skill names match their directory names and use `[a-z0-9-]+` pattern
