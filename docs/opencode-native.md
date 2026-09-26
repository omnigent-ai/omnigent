# The `opencode-native` harness

`omnigent opencode` runs the real **OpenCode** TUI in a runner-owned tmux pane
and mirrors it into an Omnigent conversation. The runner starts one private
`opencode serve` per conversation. An SSE forwarder mirrors OpenCode's events
into the session, and web turns are injected over loopback HTTP.

| Harness id | Aliases | Integration |
|---|---|---|
| `opencode-native` | `opencode`, `native-opencode` | Native server: `opencode serve` + `opencode --server` TUI |

## Supported version

OpenCode **2.0.x** (`>=2.0.0,<3.0.0`), published on npm as `@opencode/cli`.
OpenCode 1.x (`opencode-ai`) is no longer supported. A host running it reports
the harness as *outdated* until it is upgraded.
`OMNIGENT_OPENCODE_SKIP_VERSION_CHECK=1` bypasses the gate at your own risk.

## Install and sign in

    npm rm -g opencode-ai            # only if OpenCode 1.x is installed
    npm i -g @opencode/cli@~2.0.18
    opencode auth login
    omnigent opencode

`omni setup` → OpenCode does the same install and shows the login step. The web
UI's setup dialog shows the same checklist for a remote host.

Omnigent stores no OpenCode credential. At launch, the runner writes a
per-conversation `auth.json` merged from your legacy `auth.json` and your
OpenCode 2.x credential store (`~/.local/share/opencode/opencode.db`; the store
wins on conflicts), and OpenCode imports it once into the conversation's
private database. Provider keys that exist only in the runner's environment
(for example `ANTHROPIC_API_KEY`) are then connected through OpenCode's
integration API at launch. If none of these are present, the host shows the
`opencode auth login` hint.

## Agent YAML

```yaml
spec_version: 1
name: my-opencode
description: OpenCode with Omnigent policies and tools.
executor:
  type: omnigent
  config:
    harness: opencode-native
    model: anthropic/<model-id>   # optional; OpenCode's provider/model form
prompt: |
  You are a careful coding agent. Keep changes scoped to the task.
```

- **Model override:** a model id is OpenCode's `provider/model`, for example
  `anthropic/<model-id>` or `openai/<model-id>`. When the agent is bound to the
  Omnigent or Databricks gateway, give a bare endpoint name and Omnigent
  qualifies it as `<gateway-provider>/<endpoint>`. `omnigent opencode --model`
  and `omni setup` → OpenCode → "Set default model" (`opencode_model`) set the
  same value. `/model <id>` in the web composer switches the running session.
- **Instructions:** the agent's `prompt` / `instructions`, plus Omnigent's
  framework instructions, are written to the conversation's private `AGENTS.md`
  once per launch. The config's `instructions` key lists that file, but
  OpenCode 2.x does not read config `instructions` directly — it always reads
  the per-session `AGENTS.md` from its config dir.

## What is mirrored

| OpenCode | Omnigent |
|---|---|
| Text and reasoning (`session.text.delta`, `session.reasoning.delta`) | Live streaming in the chat |
| Tool calls (`shell`, `edit`, `subagent`, MCP tools) | Tool cards with output |
| Permission requests (`permission.asked`) | Omnigent policies first; ASK becomes a web approval card. First answer wins between the web card and the TUI |
| Question forms (`form.created`) | Web question card (single/multi select, boolean, number, text) |
| Compaction (`session.compaction.*`, `/compact`) | Compaction status in the chat |
| Step cost and tokens (`session.step.ended`, `session.usage.updated`) | Session cost and usage |
| Model changes (TUI picker or `/model`) | Model badge |
| Child sessions (`subagent`) | Sub-agent sessions |

Every tool call asks: the runner writes a `permissions` rule
`{action: "*", resource: "*", effect: "ask"}` into the per-conversation config
and never passes `--auto`. This routes every call through the Omnigent policy
engine. Omnigent's relay tools, and any MCP servers merged from your own
OpenCode config, run with Code Mode off, so each call is asked under its own
`<server>_<tool>` name and a policy can gate it by name. OpenCode's own MCP
builtins (`opencode_list_mcp_resources`, `opencode_read_mcp_resource`) can
still reach any configured server and have no config switch to turn them off.
They are gated as OS tools: `ask_on_os_tools` asks before each one.

Images attached in the web UI are sent as `data:` URIs. Other attachments are
flattened to text. A message sent during a running turn is delivered as a
*steer*. Queued messages use OpenCode's own queue.

## One server per conversation

Each conversation gets its own `opencode serve --stdio` process, its own
password (`OPENCODE_PASSWORD`) and its own data directory under
`~/.omnigent/opencode-native/`. Model, gateway, MCP servers, the policy plugin
and the policy plugin's session binding are all per process, and credentials and
history stay isolated. Resume reattaches to the same data directory on the same
host. A resume on a different host, or of a lost session, starts a new OpenCode
session seeded with the Omnigent transcript. A same-agent fork into the same
workspace clones the source OpenCode session natively from a snapshot of the
source conversation's database (in-flight claims cleared); a fork from an
earlier message, into another workspace, or whose source is unreachable
replays the history as a text preamble instead.

## Known limits

- **Policy token snapshot.** The policy plugin gets its Omnigent credentials
  when the server launches. A session that outlives that token's expiry fails
  open on the request/tool-result phases until it is relaunched. Tool-call
  approvals are unaffected, because they go through `permission.asked`.
- **No shared server.** OpenCode's background `--service` server, pairing, and
  one-server-for-many-sessions are not used.
- **No Direct mode.** `opencode acp` is not wired as an ACP harness.
- **Session import** starts a short-lived `opencode serve` on a snapshot copy
  of your OpenCode 2.x database (never the live store) and reads sessions over
  its HTTP API. There is no `opencode export`.

## Troubleshooting

- *Harness is outdated*: install `@opencode/cli@~2.0.18` as shown above.
- *Needs auth / auth-shaped turn error*: run `opencode auth login` on the host.
- Diagnostics: see [harness-diagnostics.md](harness-diagnostics.md). The
  per-conversation bridge directory holds `opencode.json`, the policy plugin, and
  `opencode-serve.log` (the server's stderr from the latest launch).
