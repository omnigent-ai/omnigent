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

## Executor

```yaml
executor:
  harness: claude-sdk        # claude-sdk, openai-agents, codex, cursor, devin-native, kiro-native, pi, antigravity, qwen, kimi, copilot, hermes, ...
  model: databricks-claude-opus-4-7
  reasoning_effort: high     # optional spec default: low | medium | high | xhigh (harness-dependent)
  auth:
    type: databricks
    profile: oss             # Databricks profile for model routing
```

Set the Databricks profile under `executor.auth`. The older top-level
`executor.profile` shorthand is legacy and should not be used in new specs.

`executor.reasoning_effort` sets the agent's default reasoning effort. It applies
to the main session and to every sub-agent dispatch that doesn't pass its own
per-dispatch effort, and is validated against the harness's effort vocabulary at
launch. The older `llm.reasoning_effort` is still accepted as a back-compat alias
(lifted to `executor.reasoning_effort` at parse time); when both are set,
`executor.reasoning_effort` takes precedence.

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
  harness: antigravity         # aliases: agy, google-antigravity (native: antigravity-native, agy-native)
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

To offer a curated model picker for a custom ACP agent, explicitly reference a
named provider in its agent spec:

```yaml
executor:
  harness: acp:helper
  auth:
    type: provider
    name: team-gateway
```

Configure at least two distinct model IDs in that provider's family `models:`
map in `config.yaml`, for example `models: {default: model-a, fast: model-b}`.
Tier aliases resolve to concrete IDs. The provider default leads the picker;
session selections cannot add models to the configured list. A default-only
map leaves model switching unrestricted, and an unrelated global default
provider does not change custom ACP agents. An explicitly selected provider
must resolve successfully; configuration errors do not remove model restrictions.

With curation enabled, a model pinned in the spec or ACP-agent configuration
must also appear in the list. An unlisted default prevents launch even when a
valid override is selected; clearing a selection restores the approved default.

The ACP command still owns its gateway URL and authentication; this provider
reference supplies model choices, not credentials. Configure matching provider
definitions on the server and execution host. Select a model from the session
composer and send a turn to apply it through ACP without losing the live
session, provided the command supports ACP model switching. If the switch fails,
the turn reports an error without sending the prompt on the previous model.
Retrying attempts the switch again in the same session.

Set `OMNIGENT_ACP_ENV_UNSET` on the execution host to a comma-separated list of
environment variable names to remove from the ACP command's environment. The
setting propagates through the runner and affects newly spawned commands.

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
the platform default (`linux_bwrap` on Linux, `darwin_seatbelt` on macOS, or
`windows_jobobject` on Windows), so the same YAML works across platforms. Use
`type: auto` to explicitly request the platform-default sandbox backend:

```yaml
os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: auto
```

`auto` and an omitted `type` resolve identically. `type: null` and `type: none`
both explicitly disable the sandbox. For the full set of sandbox options, how
to share one policy across `sys_os_*` and terminals, and how to set up network
egress rules, see the `sandbox:` examples below and the sandbox source under
`omnigent/inner/`.

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

### Refreshing proxy credentials

File and Unix socket credential sources can opt into renewal with
`refresh_interval_seconds`. The trusted parent re-reads the source on the first
request after that interval; sandbox placeholders stay the same. For example,
a local token broker can mint replacement GitHub App tokens before they expire:

```yaml
credential_proxy:
  - type: gh_basic
    source:
      unix_socket: /private/broker.sock
      refresh_interval_seconds: 60
```

The parent makes an HTTP `GET /token` directly over the Unix socket. The broker
must return HTTP 200 with a non-empty, single-line token (at most 64 KiB).
Redirects are not followed, no shell or external executable is involved, and
one five-second deadline covers connection setup, headers, and the response
body, including responses that trickle in slowly. Host bindings from one source
declaration share a cache and one in-flight refresh. Concurrent proxy requests
await the same result or failure without occupying additional worker threads;
cancelling one request does not cancel the refresh for other requests.

Keep the broker socket and its private key outside sandbox read/write paths.
Refresh sources require absolute paths and an active Linux bubblewrap or macOS
Seatbelt policy. The runtime rejects sources whose paths, symlink targets, or
parent directories fall inside sandbox read/write grants or the workspace, even
when the workspace is read-only. Hard-linked files and sockets are rejected.
Canonical source paths stay protected through launcher serialization: Linux
rejects mounts that expose them, including implicit toolchain mounts; macOS
denies file access and Unix-socket connections even under implicit read grants.
These protections also apply to startup-only Unix socket sources. Existing
startup-only file, environment, and command sources are unchanged.

Source checks run before startup resolution and every refresh. A symlink source
must retain its original canonical target for the session; replacing the file
atomically at the same canonical path remains supported. The trusted broker must keep its own code,
configuration, and dependencies outside sandbox-writable paths too.
Choose an interval shorter than the minimum remaining lifetime of tokens
returned by the source. A failed refresh fails the request; it does not reuse
an old credential. Proxy requests receive a sanitized HTTP 502 on source failure,
and a later request retries after the shared attempt finishes. The broker must
be ready before the helper starts: a source failure during startup prevents
launch, rather than producing a recoverable request-time 502. Without a refresh
interval, sources resolve once at startup.
Environment sources cannot refresh because a running process inherits a fixed
environment. Shell command sources remain startup-only: a sandbox might otherwise
replace a script or dependency before the trusted parent executes it again. Use
a private broker for renewable credentials instead.

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

### Linux desktop keyrings

The host and runner inherit `DBUS_SESSION_BUS_ADDRESS` and `XDG_RUNTIME_DIR`
to resolve credentials stored by `omnigent setup`. Restart the host from the
desktop session after changing these values. The keyring must be available
and unlocked.

Headless harnesses do not inherit these desktop variables by default. For a
trusted harness that performs its own keyring lookup, such as Goose configured
with `goose configure`, explicitly opt out of the sandbox and request them:

```yaml
os_env:
  type: caller_process
  sandbox:
    type: none
    env_passthrough: [DBUS_SESSION_BUS_ADDRESS, XDG_RUNTIME_DIR]
```

Unsandboxed terminals retain their declared/inherited desktop environment.
Active sandboxes remove the host bus address even when it appears in
`env_passthrough`, and supply a private, writable `XDG_RUNTIME_DIR` instead of
the desktop directory. Configure authentication separately for sandboxed
harnesses; the desktop keyring is not an available credential source there.

Environment filtering alone does not isolate the keyring. Desktop addresses
can be discovered without these variables; socket/filesystem access and process
isolation must enforce the boundary. Do not grant host desktop runtime paths to
untrusted sandboxes. `sandbox.type: none` deliberately provides no OS isolation.

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
