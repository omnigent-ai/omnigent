# Agent Image Spec

An **agent image** is a directory that fully describes an agent — its identity,
instructions, LLM config, tools, skills, and optionally sub-agents. It is a
self-contained, portable artifact. The server stores it as a tarball; the spec
layer extracts and parses it into a typed `AgentSpec`.

This document defines the format. The `spec/` module (`types.py`, `parser.py`,
`validator.py`, `tar_utils.py`) is the authoritative implementation.

---

## Directory Layout

```
my-agent/
├── config.yaml          required — LLM config, interaction contract, tools
├── AGENTS.md            optional — agent identity and behavior instructions
├── skills/              optional — agent skills
│   └── <skill-name>/
│       └── SKILL.md
├── tools/               optional — packaged tools
│   ├── python/          local Python tools (auto-discovered)
│   │   └── *.py
│   ├── typescript/      local TypeScript tools (auto-discovered)
│   │   └── *.ts
│   └── mcp/             MCP server declarations
│       └── *.yaml
└── agents/              optional — sub-agent images (recursive)
    └── <agent-name>/
        ├── config.yaml
        └── ...
```

Any files or directories not listed above are ignored by the parser.

---

## config.yaml

The only required file. All top-level keys except `spec_version` are optional.

```yaml
spec_version: 1               # required; must be 1

name: my-agent                # display name
description: Does X and Y.   # optional free-form description
instructions: AGENTS.md       # inline text or path to file (default: AGENTS.md)

llm:
  model: openai/gpt-5.4       # required if llm block present; LiteLLM format
                              # examples: openai/gpt-5.4, openai/o4-mini,
                              #   anthropic/claude-opus-4-6,
                              #   google/gemini-2.5-pro
  max_completion_tokens: 4096 # optional; caps total output including reasoning tokens
  reasoning_effort: medium    # optional; low | medium | high

interaction:
  conversational: true        # maintain history across turns (default: true)
  modalities:
    input: [text, image, file]  # default: [text]
    output: [text]              # default: [text]

tools:
  agents:                     # sub-agents this agent is allowed to call
    - researcher              # must match a directory name under agents/

params:                       # arbitrary key-value; readable by skills and tools
  max_results: 10             # not interpreted by the runtime
  prefer_recent: true
```

### `interaction` axes

| Field | What it means | Who acts on it |
|---|---|---|
| `conversational` | runtime maintains turn history; frontend shows chat thread | runtime + frontend |
| `modalities` | input/output content types the agent supports | frontend |

All agents are interruptible and support streaming — both are always provided
by the runtime regardless of agent config.

### `interaction.modalities`

Declares which content types the agent accepts and produces. Omitting the block
entirely is equivalent to `input: [text], output: [text]`. Omitting one side
defaults that side to `[text]`.

**Supported input modalities:**

| Value | Meaning |
|---|---|
| `text` | plain text (always the baseline) |
| `image` | images (jpg, png, etc.) processed via vision |
| `audio` | audio input |
| `video` | video input |
| `file` | document/data files (PDF, docx, csv, code) processed via document understanding |

**Supported output modalities:**

| Value | Meaning |
|---|---|
| `text` | text response (always the baseline) |
| `image` | generated images |
| `audio` | generated speech / audio |

`file` is not a supported output modality in v1 (see Not Yet).

The frontend uses modalities to decide which UI affordances to show — file
upload button, image picker, audio recorder, etc. The runtime uses them to
validate that the underlying model actually supports the requested modalities.

### `tools.agents`

Declares which sub-agents this agent is allowed to call. Any name listed here
must have a corresponding directory under `agents/`. Listing an agent in
`tools.agents` is sufficient to call it — no additional builtin declaration is
needed.

### `tools.builtins`

Enables built-in tools provided by omnigent. Each entry is either a plain
string (tool name, no config needed) or a dict with `name` and tool-specific
config fields (API keys, engine IDs, etc.):

```yaml
tools:
  builtins:
    - web_search                           # string — auto-detects backend
    - name: web_search                     # dict — explicit Google config
      api_key: ${GOOGLE_SEARCH_API_KEY}
      engine_id: ${GOOGLE_SEARCH_ENGINE_ID}
    - name: web_search                     # dict — explicit Perplexity
      search_provider: perplexity
      api_key: ${PERPLEXITY_API_KEY}
    - name: web_search                     # dict — explicit Nimble
      search_provider: nimble
      api_key: ${NIMBLE_API_KEY}
      # optional: max_results (1-100, default 5); search_depth (lite | deep)
```

Keys can be hardcoded or use `${ENV_VAR}` references (resolved at deploy time
by the client, not at runtime by the server — the spec is self-contained).

**`web_search` backend selection:**

- **OpenAI models:** `web_search` works automatically with no config —
  it uses OpenAI's native `web_search_preview` (server-side). Just add
  `- web_search` to builtins.
- **Other models:** `search_provider` must be set to `"google"`,
  `"perplexity"`, or `"nimble"` with credentials. All config comes from the
  spec (no environment variable fallbacks).
- **Nimble** (`search_provider: nimble`): returns a ranked list of titles,
  URLs, and snippets from Nimble's AI search API. Requires `api_key`; optional
  `max_results` (1-100, default 5) and `search_depth` (`lite` default, or
  `deep`). Works with any non-OpenAI model.

**`web_fetch` — zero-config web research:** Spawns an internal sub-agent with
`terminal_run` to search the web and fetch pages using plain HTTP. No API keys
needed — works with any model provider. The sub-agent inherits the parent's
LLM model and credentials. Only works with the default `llm` executor.

```yaml
tools:
  builtins:
    - web_fetch                            # no config needed
```

**`hindsight_retain` / `hindsight_recall` / `hindsight_reflect` — long-term
memory:** Persist and recall memory across runs via
[Hindsight](https://github.com/vectorize-io/hindsight). Requires the optional
`hindsight` extra (`pip install 'omnigent[hindsight]'`). The memory bank
defaults to the agent's id, so a single declaration isolates memory per agent.

```yaml
tools:
  builtins:
    - name: hindsight_recall               # search long-term memory
      api_key: ${HINDSIGHT_API_KEY}
    - name: hindsight_retain               # store to long-term memory
      api_key: ${HINDSIGHT_API_KEY}
    - name: hindsight_reflect              # synthesize an answer from memory
      api_key: ${HINDSIGHT_API_KEY}
      # optional: api_url (defaults to Hindsight Cloud), bank_id (defaults to
      # the agent id), budget (low | mid | high), max_tokens, tags, recall_tags
```

---

## Instructions

Free-form text injected into the system prompt. Defines personality,
constraints, and behavioral guidelines.

The `instructions` key in `config.yaml` controls where instructions come from:

| `instructions` value | Behavior |
|---|---|
| *(omitted)* | Read `AGENTS.md` from the agent root if present |
| `path/to/file.md` | Read the file at that path relative to the agent root |
| `"You are a helpful assistant."` | Use the string as inline instructions |

Resolution: if the value matches an existing file relative to the agent root,
the file contents are used. Otherwise the value is treated as inline text.

```markdown
You are a research assistant. Always cite sources. Ask one clarifying
question before diving in. When unsure, say so.
```

Not machine-parsed — the entire contents (file or inline) are passed to the
model as instructions. Optional; if absent, the model receives no agent-level
system prompt (per-request `instructions` from the API still apply).

---

## Skills — `skills/<name>/SKILL.md`

A skill is a named chunk of instructions the agent can load on demand. Each
skill lives in its own subdirectory under `skills/`.

```markdown
---
name: deep-search
description: Search the web and arxiv for sources on a topic.
---

When asked to research a topic:
1. Use search.web for general context.
2. Use arxiv.search for academic papers.
3. Collect at least 3 sources before synthesizing.
```

**Frontmatter fields:**

| Field | Required | Constraints |
|---|---|---|
| `name` | yes | max 64 chars; lowercase letters, digits, hyphens; must match directory name |
| `description` | yes | max 1024 chars; one-line description of when to use this skill |

Everything after the frontmatter is markdown content passed to the model.

---

## MCP Tools — `tools/mcp/<name>.yaml`

Declares an MCP server the agent can use.

Only the HTTP (SSE) transport is supported.

```yaml
name: my-service
description: Internal service tools.
url: http://localhost:9000/mcp
headers:                      # optional headers
  Authorization: Bearer ${API_KEY}
```

**Required fields:** `name`, `transport`, `url`

**Optional fields:**

| Field | Type | Default | Notes |
|---|---|---|---|
| `description` | string | *(none)* | Human-readable summary |
| `headers` | map | `{}` | HTTP headers; supports `${ENV_VAR}` expansion |
| `timeout` | int (seconds) | *(see below)* | Per-tool timeout override; `None` inherits `tools.timeout` |
| `retry` | object | *(see below)* | Per-tool retry override; `None` inherits `tools.retry` |

**Timeout defaults:** When `timeout` is omitted (or `None`), the MCP SDK
defaults apply: **5 seconds** for the initial HTTP connection handshake and
**300 seconds (5 minutes)** for each SSE event read. Setting an explicit
`timeout` overrides both values to the same number of seconds.

**Security note — `${VAR}` is NOT expanded for uploaded bundles:**
``${VAR}`` references in `headers`, `env`, and connection blocks are
resolved against the spec author's *own* environment at the client /
registration boundary (`omnigent.cli._resolve_bundle_env_vars`), never
at runtime by the server or runner for a tenant-uploaded
(session-scoped) bundle. Expanding an uploaded spec's ``${VAR}`` against
the server process env would let any tenant exfiltrate server-side
secrets by referencing them in a header pointed at an attacker URL
(W7-3). Only operator-authored template agents
(`--agent`, built-ins; `Agent.session_id is None`) expand server-side.

**Security note — SSRF risk:** The omnigent server makes outbound HTTP
requests to the configured `url`. There is currently no application-level
URL validation (e.g. blocking private IPs or cloud metadata endpoints).
In multi-tenant or untrusted-bundle environments, use network-level
controls (egress proxy, network policies) to restrict which destinations
the server process can reach.

---

## Local Tools — `tools/python/*.py` / `tools/typescript/*.ts`

Python and TypeScript files under `tools/python/` and `tools/typescript/` are
auto-discovered. The tool name is derived from the filename
(`arxiv_search.py` → `arxiv.search`).

The runtime loads these files and exposes their public functions as tools.
Schema is inferred from type hints and docstrings. Refer to the runtime
documentation for the exact loading convention.

---

## Sub-agents — `agents/<name>/`

Each subdirectory under `agents/` is itself a full agent image (recursive).
The parent declares which sub-agents it is allowed to call via `tools.agents`.

```
parent/
├── config.yaml          tools.agents: [researcher, critic]
└── agents/
    ├── researcher/
    │   ├── config.yaml
    │   └── skills/
    └── critic/
        ├── config.yaml
        └── skills/
```

**Resolution rules:**

1. The called name must appear in `tools.agents` — names not listed are
   rejected at call time.
2. The runtime resolves `agents/<name>/` relative to the calling agent's root.
   There is no global registry and no parent-walking.
3. Sub-agents are isolated by default — they see only tools declared in their
   own `config.yaml`. Tool inheritance is not supported in v1.
4. Each sub-agent call produces its own trace span under the parent span.

---

## Validation Rules

The validator (`validator.py`) enforces:

- `spec_version` must be `1`
- `llm.model` must be present if the `llm` block is present
- Skill `name` in frontmatter must match the directory name
- Skill `name`: max 64 chars, pattern `[a-z0-9-]+`
- Skill `description`: max 1024 chars
- MCP configs must have `transport: http` and a non-empty `url` (presence checked, not format)
- No duplicate skill names across `skills/`
- No duplicate tool names across `tools/mcp/`, `tools/python/`, and
  `tools/typescript/`
- Sub-agent names in `tools.agents` must have a corresponding directory under
  `agents/`

---

## Key Design Decisions

- **Pure filesystem layer.** The `spec/` module takes a `Path` and returns an
  `AgentSpec`. No network, no database, no storage awareness. The server
  (bundle upload/extraction) is separate from parsing.

- **Listing an agent is enough to call it.** No explicit `agent.call` builtin
  needed. If a name appears in `tools.agents`, the runtime exposes it as a
  callable tool automatically.

- **Allowlists only.** Agents not listed in `tools.agents` are rejected at
  call time. No denylists, no wildcards in v1.

- **Sub-agents are isolated.** Each sub-agent sees only its own tools. No
  tool inheritance from parent in v1.

- **Plain dataclasses.** `AgentSpec` and related types are dataclasses, not
  ORM models. No database awareness in this layer.

---

## Not Yet

- **`interaction.schema`** — structured I/O contract for the agent. When
  present, the runtime validates inputs and outputs against declared field
  types. Deferred; all agents default to unstructured chat I/O for now.

  Planned shape:
  ```yaml
  interaction:
    schema:
      types:                    # reusable custom type definitions
        my_type:
          field_a: string
          field_b: int?
      inputs:                   # input validation schema (field: type)
        message: string
      outputs:                  # output validation schema (field: type)
        reply: markdown
        sources: list[my_type]
  ```

  Builtin field types: `string`, `int`, `float`, `bool`, `markdown`, `url`,
  `datetime`, `code`, `json` (escape hatch), `list[T]`, `T?` (optional).
  Custom types defined under `schema.types` are reusable anywhere a builtin
  type is valid. The rationale for nesting under `interaction`: frontend and
  runtime need to read both execution semantics and I/O shape from one block.

- **Type inheritance** — `base: citation` to extend a builtin type within
  `schema.types`. All types are flat in v1.

- **`agent.map` / `agent.spawn`** — batch and parallel fan-out over sub-agents.
  Deferred to v2.

- **Builtin tools: `search.web`, `code.execute`, `memory.*`** — standardized
  runtime-provided tools. Interfaces and availability will be defined soon.

- **Memory policy declarations** — a `memory:` block for consent hints and
  scope declarations. Memory is purely a tool concern in v1.

- **`when:` routing hints on sub-agents** — declarative hints in `tools.agents`
  entries describing when to call each sub-agent. Skill content handles routing
  in v1.

- **Tool inheritance for sub-agents** — `inherit_tools: true` to pass a
  restricted tool allowlist down to a sub-agent. Isolation is the only model
  in v1.

- **Skill versioning** — version numbers on skills or the overall image beyond
  `spec_version: 1`.

- **`interruptible` flag** — all agents are interruptible in v1. A per-agent
  flag with partial-result semantics and resume-from-checkpoint may be added
  later.

- **`streaming` flag** — all agents support streaming in v1. A per-agent flag
  may be added later if non-streaming agents become a meaningful use case.

- **`conversational: false`** — stateless single-turn mode where the runtime
  does not maintain history across turns. All agents are conversational in v1;
  the field is defined in the spec but only `true` is supported for now.

- **`file` output modality** — agents generating downloadable files as output.
  Output modalities are limited to `text`, `image`, and `audio` in v1.

- **Flexible skill content sources** — similar to how `instructions` can be
  inline text or a file reference, skills could support an `instructions` key
  pointing to an arbitrary file instead of requiring `skills/<name>/SKILL.md`.
  Whether inlining skill text directly in `config.yaml` should also be
  supported is an open question — it trades discoverability and
  separation-of-concerns for convenience in simple single-skill agents. In v1,
  skills must live in `skills/<name>/SKILL.md`.

- **Tool environment declarations** — specifying dependencies for local tools,
  e.g. a `requirements.txt` for Python tools or `package.json` for TypeScript.
  The runtime currently assumes dependencies are pre-installed in the execution
  environment.
