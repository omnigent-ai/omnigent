# Agent YAML spec

Omnigent can run an agent from a single YAML file:

```bash
omnigent run path/to/agent.yaml
```

Use this file to choose the harness/model, write the agent-owned system
instructions, and declare which tools, sub-agents, OS access, and policies the
agent can use.

## Minimal agent

```yaml
name: hello_agent
prompt: |
  You are a concise assistant. Answer directly and ask a follow-up question when
  the request is ambiguous.

executor:
  harness: claude-sdk
  model: databricks-claude-sonnet-4-6
  auth:
    type: databricks
    profile: oss
```

`prompt` may also be replaced by `instructions: AGENTS.md`; relative paths are
resolved from the YAML file's directory.

These fields define the portable, agent-authored portion of the system prompt.
Omnigent may append framework-owned lifecycle or metadata instructions at
runtime after agent and per-request instructions; those additions are not part
of the agent YAML.

Whether and how that composed text actually reaches the vendor agent varies
by harness — see [Instruction delivery](#instruction-delivery) below.

## Common top-level fields

| Field | Required? | Purpose |
| --- | --- | --- |
| `name` | Recommended | Stable identifier shown in sessions and logs. |
| `prompt` | Usually | Inline agent-owned system instructions. |
| `instructions` | Optional | Inline instructions or a path to an instructions file. If set, it takes precedence over `prompt`. |
| `executor` | Recommended | Harness, model, and auth settings. |
| `tools` | Optional | MCP tools, Python function tools, sub-agents, handoffs, or inherited tools. |
| `policies` | Optional | Guardrails that inspect requests, responses, tool calls, or tool results. |
| `params` | Optional | Typed user parameters available to tools/skills. |
| `os_env` | Optional | Enables local OS tools such as file reads, writes, edits, and shell commands. |
| `terminals` | Optional | Named interactive terminal environments the agent can launch. |
| `async` | Optional | Whether async work tools are exposed. Defaults to `true`. |
| `cancellable` | Optional | Whether the session can be cancelled. Defaults to `true`. |
| `timers` | Optional | Whether timer tools are exposed. Defaults to `false`. |

## Instruction delivery

`prompt`/`instructions` (plus any per-request instructions and framework-owned
additions) are composed into a single system-prompt string internally, but
**not every harness delivers that composed string to the vendor agent the
same way — and a few do not deliver it at all.** `InstructionDelivery`
(`omnigent/harness_capabilities.py`) declares, per canonical harness, which of
six lifecycle shapes applies. Aliases inherit their canonical harness's
declared value, but the registry itself is keyed by canonical id only — an
alias must be resolved to its canonical id (`canonicalize_harness()` from
`omnigent/harness_aliases.py`) *before* looking it up, exactly as the runner's
own delivery-gap warn check does. `harness_capabilities().get(harness_id)` is
a plain `dict.get` with no default: passing a raw, unresolved id (an alias,
or an undeclared/third-party harness) returns `None`, not an
`InstructionDelivery.UNKNOWN`-valued capability object — callers that need
the "unknown" fallback check for `None` explicitly and substitute
`InstructionDelivery.UNKNOWN` themselves, e.g.:

```python
caps = harness_capabilities().get(canonicalize_harness(harness_id) or harness_id)
delivery = caps.instruction_delivery if caps is not None else InstructionDelivery.UNKNOWN
```

Alternatively, use the `/v1/harnesses` catalog: it is keyed by canonical
harness id (aliases don't get their own row — the same inheritance rule
applies) and exposes `instruction_delivery` in each row's `capabilities`
object.

| Value | Meaning |
| --- | --- |
| `composed-per-turn` | The full framework-composed prompt is effective for every turn. An unchanged value may remain resident in a cached SDK agent or session, but a changed value is applied before the next turn — either resent, or by rebuilding/respawning the underlying session. |
| `composed-session-snapshot` | The full framework-composed prompt is captured once, when a persistent vendor client/session object is created, and is **not** refreshed if the composed text changes afterward while that client/session continues to be reused. |
| `agent-startup-additive` | The vendor channel is additive and startup/session-creation scoped — not a per-message system field or a user-turn prefix. Carries **raw author instructions only** (`AgentSpec.instructions`), not the fully framework-composed per-turn string. Startup is not tied to any one turn, whereas the composed string is assembled per conversation for the turn about to run — binding a startup channel to one turn's composition would leave every later turn addressed by a value chosen for an earlier one. |
| `first-user-prefix` | Instructions are prepended to the user's own first turn in a fresh vendor session — not a separate synthetic turn. |
| `not-delivered` | No wired channel exists from the Omnigent prompt to that executor/vendor program today. |
| `unknown` | Reserved for plugin/third-party executors that do not declare the capability. Treated like `not-delivered` for warning purposes. |

### Per-harness matrix

| Harness | `InstructionDelivery` |
| --- | --- |
| `acp` | `first-user-prefix` |
| `antigravity` | `composed-per-turn` |
| `antigravity-native` | `not-delivered` |
| `claude-native` | `agent-startup-additive` |
| `claude-sdk` | `composed-session-snapshot` (see below) |
| `codex` | `composed-per-turn` |
| `codex-native` | `agent-startup-additive` |
| `copilot` | `composed-per-turn` |
| `cursor` | `first-user-prefix` |
| `cursor-native` | `not-delivered` |
| `goose` | `first-user-prefix` |
| `goose-native` | `not-delivered` |
| `grok` | `first-user-prefix` (builtin ACP CLI catalog row; see below) |
| `hermes` | `first-user-prefix` (see below — do not confuse with `hermes-native`) |
| `hermes-native` | `not-delivered` (distinct from non-native `hermes` above) |
| `kimi` | `not-delivered` (Kimi's own agent spec carries instructions; deliberate deferral tied to tool-bridge immaturity, not an oversight) |
| `kimi-native` | `not-delivered` |
| `kiro-native` | `not-delivered` (see below) |
| `open-responses` | `composed-per-turn` |
| `openai-agents` | `composed-per-turn` |
| `opencode-native` | `composed-per-turn` |
| `pi` | `composed-per-turn` (see below — not `agent-startup-additive`) |
| `pi-native` | `not-delivered` |
| `qwen` | `first-user-prefix` |
| `qwen-native` | `not-delivered` |

**Pi vs. claude-native/codex-native.** Pi's launch flag looks superficially
like claude-native/codex-native's startup mechanics
(`--append-system-prompt` supplied when its RPC subprocess starts), but
`_ensure_rpc()` kills and respawns that subprocess with the current composed
value before every `run_turn()` call whenever the value has changed — an
operational per-turn guarantee, not a startup-only additive channel. Pi is
therefore `composed-per-turn`, and receives the fully composed value, unlike
claude-native/codex-native's raw-author-only channel.

**`claude-sdk` known limitation.** `system_prompt` is placed into
`ClaudeAgentOptions` only at client construction; the cached client is reused
across turns with only its model updated, so composed-text changes made
after client creation are silently dropped, not re-applied. This is a
distinct, honestly named lifecycle (`composed-session-snapshot`), not a
footnote on `composed-per-turn`. Follow-up tracked as
[issue #3558](https://github.com/omnigent-ai/omnigent/issues/3558), covering
rebuilding the client when late-bound framework instructions change; a
merge-gated strict-xfail conformance test in
`tests/inner/test_claude_sdk_executor.py` keeps this classification honest
until #3558 lands — when it lands, the xfail is removed and this row flips
to `composed-per-turn` in the same commit.

Follow-up ticket scope
([issue #3558](https://github.com/omnigent-ai/omnigent/issues/3558), not
implemented in issue #3530): include the
effective composed prompt in the persistent Claude SDK client's identity, or
provide a supported in-place prompt update; rebuild the client when
late-bound framework instructions change; replay prior history exactly once
into a rebuilt client; preserve MCP/tool bridge callbacks, policy/elicitation
wiring, model switching, and interrupt/cancellation/steering/close-session
semantics; framework instructions must become active after the first turn
(flip the registry row in the same commit); decide whether the rebuilt client
reuses or replaces the vendor session identifier.

**`kiro-native` deferred channel.** A first-user-prefix-*shaped* launch
channel exists (`build_kiro_launch`'s positional `prompt` argv) but is
intentionally left unwired, for two independent reasons: the launcher would
create/replay a standalone synthetic turn visible in a resident TUI the user
co-drives, and the executor has no freshness ground truth — it doesn't create
the vendor session, doesn't capture a vendor session id, and has no
fresh-vs-resumed signal at the executor boundary to gate a safe
implementation. `kiro-native` is declared `not-delivered`, describing current
wired behavior only; declaring `first-user-prefix` for an unwired channel
would make delivery-gap warnings stay silent for the one harness this
distinction most matters for.

**Builtin ACP CLI harnesses.** Rows in the ACP CLI catalog
(`omnigent/acp_cli_harnesses.py`, currently `grok`) are not separate
integrations: `harness_modules` points every one of them at the same generic
`omnigent.inner.acp_harness` wrap as the plain `acp` harness, and
`harness_plugins.py` copies the `acp` capability row onto each. They therefore
share `acp`'s delivery channel — `first-user-prefix` — for as long as they run
that shared wrap. A catalog row that grows its own executor stops inheriting
and needs its own declaration and matrix row.

**Hermes instruction delivery.** `HermesExecutor` tracks a captured
Hermes-native session id per Omnigent session key (`_session_map`, keyed off
the `session_id` stamped on the first message). Composed instructions are
prefixed onto the user's own first turn — `f"{system_prompt}\n\n{user_text}"`
— only when that session id is genuinely unset (`hermes_sid is None`), i.e.
this is the first turn of a fresh Hermes session; every subsequent turn on
the same session key passes `--resume <hermes_sid>` instead, so the prefix
is never repeated. The session id is captured once, from the first turn's
own `hermes chat -q` output, and only if a prior value wasn't already
recorded. `close_session` drops the mapping for that one session key
(`_session_map.pop`); `close` drops all of them. Either reset clears the
freshness gate, so the NEXT turn on that session key is treated as a fresh
Hermes session again and re-prefixes the composed instructions — matching
`first-user-prefix`'s "restart re-delivers" semantics.

## Executor

```yaml
executor:
  harness: claude-sdk        # claude-sdk, openai-agents, codex, cursor, kiro-native, pi, antigravity, qwen, kimi, copilot, hermes, ...
  model: databricks-claude-opus-4-7
  auth:
    type: databricks
    profile: oss             # Databricks profile for model routing
```

Set the Databricks profile under `executor.auth`. The older top-level
`executor.profile` shorthand is legacy and should not be used in new specs.

The `cursor` harness (Cursor's `cursor-agent`) is the exception: it talks
only to Cursor's own backend and has no custom API base-URL, so the Databricks
gateway / `auth.type: databricks` does not apply. Authenticate it with
`CURSOR_API_KEY` (or a prior `cursor-agent login`), optionally pinned via
`auth: {type: api_key, api_key: ${CURSOR_API_KEY}}`, and choose a Cursor model
id (e.g. `auto`, `gpt-5`) rather than a `databricks-*` id.

The `kiro-native` harness is the native Kiro CLI terminal path used by
`omnigent kiro`. It requires `kiro-cli` on `PATH` and Kiro's own login/auth; it
does not use Databricks, OpenAI, or Anthropic provider credentials. Plain
`harness: kiro` is not a generic Omnigent harness id. Kiro's TUI remains the
authoritative approval surface; supported one-time tool approvals can also be
mirrored into Chat cards, while persistent trust choices remain explicit Kiro
TUI/flag actions. See `kiro-native-elicitation.md`.

### Antigravity (Gemini)

`harness: antigravity` runs the agent through Google's
[Antigravity SDK](https://pypi.org/project/google-antigravity/)
(`pip install "omnigent[antigravity]"`). It defaults to **Gemini 3.5 Flash**
and can also drive Claude / GPT-OSS. Authenticate with an Antigravity /
Gemini API key, or Vertex AI (`project` / `location`) — the SDK is
Gemini-native and has no OpenAI-compatible gateway / Databricks path.

```yaml
executor:
  harness: antigravity         # aliases: agy, google-antigravity
  model: gemini-3.5-flash
  auth:
    type: api_key
    api_key: ${GEMINI_API_KEY}     # or ANTIGRAVITY_API_KEY
```

### GitHub Copilot

`harness: copilot` runs the agent through the
[GitHub Copilot SDK](https://pypi.org/project/github-copilot-sdk/)
(`pip install "omnigent[copilot]"`). The SDK bundles the Copilot CLI it drives
as a backing server, so no separate CLI install is needed. Like cursor and
antigravity it talks only to GitHub's Copilot backend — there is no Databricks
gateway / `auth.type: databricks` path. Authenticate with a **GitHub token** that
carries Copilot access: a fine-grained PAT with the "Copilot Requests"
permission, or an OAuth token from the GitHub CLI (`gh auth token`) / Copilot
CLI. Resolution: spec `auth.api_key` → a token registered via `omnigent setup`
(the `copilot:` config block) → ambient `COPILOT_GITHUB_TOKEN` / `GH_TOKEN` /
`GITHUB_TOKEN`. Choose a Copilot model id (e.g. `claude-haiku-4.5`, `gpt-5-mini`,
or omit for auto-select) rather than a `databricks-*` id. Classic `ghp_` PATs are
not accepted by Copilot.

```yaml
executor:
  harness: copilot             # alias: github-copilot
  model: claude-haiku-4.5      # a Copilot model id; omit for auto-select
  auth:
    type: api_key
    api_key: ${GH_TOKEN}       # a GitHub token with Copilot access
```

To route through OpenRouter / a gateway, declare a key/gateway provider in
`~/.omnigent/config.yaml` and reference it (`auth: {type: provider, name: …}`),
or set `auth.base_url` to the OpenAI-compatible endpoint alongside the key.
For Databricks, use `auth: {type: databricks, profile: …}`.

### Kimi Code

`harness: kimi` runs the agent through Moonshot AI's
[Kimi Code CLI](https://github.com/MoonshotAI/Kimi-Code) headlessly via
`kimi --print --output-format stream-json` per turn. Install the binary
with `curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash`
and authenticate once with `kimi login` (OAuth or a Moonshot API key).

```yaml
executor:
  harness: kimi               # alias: kimi-code
  model: kimi-k2-turbo
```

By default Kimi authenticates against Moonshot AI's backend — Omnigent
declares no `executor.auth` block. To route through a gateway, either set
`HARNESS_KIMI_GATEWAY_BASE_URL` + `HARNESS_KIMI_GATEWAY_API_KEY` in the
shell, declare a key/gateway provider in `~/.omnigent/config.yaml`, or use
`executor.auth: {type: databricks, profile: …}` and let Omnigent resolve
the workspace.

CLI flags such as `--harness` and `--model` can override or supply missing
executor values for a run. Databricks credentials come from the spec's
`executor.auth` block or your `omnigent setup` provider config — there is
no profile flag.

## Qwen Code

`harness: qwen` runs the agent through [Qwen Code](https://github.com/QwenLM/qwen)
(`npm install -g @qwen-code/qwen-code`). It drives the `qwen` CLI in ACP mode
(`qwen --acp`).

```yaml
executor:
  harness: qwen                # aliases: qwen-code
  model: qwen/qwen-2.5-coder
```

CLI flags such as `--harness qwen` and `--model <id>` can override or supply
missing executor values.

## Custom ACP agents

`harness: acp:<slug>` runs any configured Agent Client Protocol server command.
Register commands in `~/.omnigent/config.yaml` under `acp.agents`; the slug is
derived from the agent name.

OpenClaw's Gateway ACP bridge is one such server. It rejects per-session
`mcpServers`, so disable Omnigent's MCP relay for that entry and let OpenClaw
use its own tools, routing, memory, and channels:

```yaml
acp:
  agents:
    - name: OpenClaw
      command: openclaw acp --url <gateway-url> --token-file <token-file>
      omnigent_mcp: false
```

Then run it with `omni run --harness acp:openclaw` or select `OpenClaw` in the
app. See the [OpenClaw integration guide](openclaw.md) for registry import,
Gateway setup, and compatibility details.

## Local OS access

Declare `os_env` only for agents that need local file/shell tools.

```yaml
os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: linux_bwrap
    write_paths:
      - .
    allow_network: true
```

For trusted local development, examples may use `sandbox.type: none`:

```yaml
os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
```

Prefer the narrowest filesystem and network access that supports the task. Do
not pass secrets through the environment unless the tool genuinely needs them.

You usually don't need to choose a `sandbox.type` — omit it and Omnigent picks
the platform default (`linux_bwrap` on Linux, `darwin_seatbelt` on macOS), so the
same YAML works across platforms. For the full set of sandbox options, how to
share one policy across `sys_os_*` and terminals, and how to set up network
egress rules, see the `sandbox:` examples below and the sandbox source under `omnigent/inner/`.

### Secretless credential proxy

`sandbox.credential_proxy` lets sandboxed tools authenticate to external hosts
without the real secret ever entering the sandbox: the mandatory L7 egress proxy
attaches the credential on the way out. It requires `egress_rules` and a
network-isolating backend (`linux_bwrap` or `darwin_seatbelt`). See
`designs/SANDBOX_CREDENTIAL_PROXY.md` for the full type table.

The `databricks_cli` type proxies the Databricks CLI. List the profiles to
proxy; only those are materialized into the sandbox (with placeholder tokens)
and swapped by the proxy. As with every other credential-proxy type, you must
list each workspace host in `egress_rules` yourself — the proxy does not widen
egress on its own. OAuth tokens are refreshed for the life of the session.
Requires the `databricks` extra and `linux_bwrap` (the Go CLI ignores
`SSL_CERT_FILE` on macOS, so `darwin_seatbelt` is rejected).

```yaml
os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: linux_bwrap
    egress_rules:
      - "* pypi.org/**"                              # your other egress needs
      - "* dbc-adb7b1a3-9097.cloud.databricks.com/**"  # the proxied workspace
    credential_proxy:
      - type: databricks_cli
        profiles: [dbc-adb7b1a3-9097, oss]
        default: dbc-adb7b1a3-9097   # optional; sets DATABRICKS_CONFIG_PROFILE
```

Inside the sandbox, `databricks --profile dbc-adb7b1a3-9097 current-user me`
works; the sandbox holds only `oa_cred_*` placeholders, never a live token.

## Tools

Tools are declared under `tools` by name.

### MCP server

```yaml
tools:
  github:
    type: mcp
    command: uv
    args:
      - run
      - python
      - -m
      - my_package.github_mcp
    tools:
      - search_issues
      - get_pull_request
```

MCP tools can also point at a remote URL:

```yaml
tools:
  docs:
    type: mcp
    url: https://example.com/mcp
    headers:
      Authorization: Bearer ${TOKEN}
```

### Python function tool

```yaml
tools:
  summarize_file:
    type: function
    description: Summarize a local text file.
    callable: my_package.tools.summarize_file
    parameters:
      type: object
      properties:
        path:
          type: string
      required: [path]
```

For client-provided tools, use `runtime: client` and do not set `callable`.

### Tool sandbox containers

Local Python tools can run inside a container image by declaring a sandbox image.
Use `container_image` for new specs; `docker_image` remains accepted as a
deprecated alias for backwards compatibility. Set `container_runtime: podman` to
run the image with Podman instead of Docker.

The runtime can also be set globally via the `OMNIGENT_CONTAINER_RUNTIME`
environment variable (accepted values: `docker`, `podman`). The per-agent
`container_runtime` YAML key takes precedence over the environment variable.

```yaml
tools:
  sandbox:
    container_image: python:3.12-slim
    container_runtime: podman  # optional; defaults to docker (or OMNIGENT_CONTAINER_RUNTIME)
```

### Sub-agent tool

```yaml
tools:
  reviewer:
    type: agent
    description: Review proposed code changes.
    prompt: |
      You are a careful code reviewer. Focus on correctness, tests, security,
      and maintainability.
    executor:
      harness: claude-sdk
      model: databricks-claude-sonnet-4-6
    os_env: inherit
    pass_history: true
    max_sessions: 2
```

Each sub-agent picks its own `executor.harness` and `model`, so an orchestrator
can mix harnesses by role — e.g. a `cursor` coder with a `claude-sdk`
reviewer:

```yaml
tools:
  coder:
    type: agent
    executor:
      harness: cursor      # Cursor model id (e.g. gpt-5, auto), not a databricks-* id
      model: gpt-5
```

Use `tools.<name>: inherit` to inherit a tool from a parent agent, or
`tools.<name>: self` / `spec: self` for a sub-agent that clones the parent spec.

## Policies

Policies can inspect requests, responses, tool calls, and tool results.

```yaml
policies:
  pii_guard:
    type: function
    handler: my_package.policies.pii_guard
    on: [request, response]
```

A factory can be configured with `factory_params`:

```yaml
policies:
  workspace_policy:
    type: function
    handler: my_package.policies.make_workspace_policy
    factory_params:
      allowed_hosts:
        - example.cloud.databricks.com
```

## Terminals

Terminals are named interactive shell environments that the agent can launch.

```yaml
terminals:
  bash:
    command: bash
    args: [-l]
    os_env: inherit
    allow_cwd_override: true
    allow_sandbox_override: false
    scrollback: 10000
```

Use `os_env: inherit` to give the terminal the same sandbox as the agent, or
alias a shared `sandbox:` block so `sys_os_*` and the terminal enforce the same
policy. Keep `allow_sandbox_override: false` unless you intend to let the
launcher weaken the sandbox at launch time.

## Complete example

```yaml
name: coding_agent
prompt: |
  You are a coding agent. Inspect files before editing, run targeted tests, and
  summarize changes with validation results.

executor:
  harness: claude-sdk
  model: databricks-claude-sonnet-4-6
  auth:
    type: databricks
    profile: oss

async: true
cancellable: true

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: linux_bwrap
    write_paths: [.]
    allow_network: true

terminals:
  zsh:
    command: zsh
    args: [-l]
    os_env: inherit
    allow_cwd_override: true

tools:
  repo_search:
    type: function
    description: Search repository files for a pattern.
    callable: my_package.tools.repo_search
    parameters:
      type: object
      properties:
        query:
          type: string
      required: [query]
```

## Validation tips

- Keep examples free of secrets, workspace URLs, customer data, and private
  Databricks-only configuration unless the example is explicitly internal.
- Prefer `instructions: AGENTS.md` for long prompts that are shared with other
  tooling.
- Start from a bundled example such as `examples/polly/config.yaml` or
  `examples/debby/config.yaml` and remove tools you do not need.
- Run the YAML before publishing it:

  ```bash
  omnigent run path/to/agent.yaml -p "Say hello"
  ```
