# Local setup for the routing MVP stack

How to bring up the isolated dev stack that `CUJ_STATUS.md` §1 recipe **R0**
assumes. Do this once per machine. Everything here runs against a private config
home inside the worktree, so your real `~/.omnigent` is never touched.

## 1. Prerequisites

| Tool | Version used | Notes |
| --- | --- | --- |
| `uv` | 0.12.1 | Manages Python. `pyproject.toml` requires Python >= 3.12. |
| `node` | v24.14.0 | Any recent LTS works. `run-frontend.sh` finds it. |
| `pnpm` | 11.15.1 | Pinned by `packageManager` in `package.json`. **Not npm.** |
| `databricks` | 1.2.1 | Needed for the OAuth profile the router uses. |
| `sqlite3` | any | Recipe **R1** reads the chat DB with it. |
| `tmux` | any | Recipe **R2** captures the claude pane through it. |

On macOS: `brew install uv pnpm node sqlite tmux databricks`.

## 2. Install dependencies

```sh
git clone git@github.com:omnigent-ai/omnigent.git
cd omnigent
git checkout routing-mvp

uv sync                 # the run-*.sh scripts pass --no-sync, so do this first
cd web && pnpm install && cd ..
```

`uv sync` rewrites `uv.lock` with registry churn on some machines. Run
`git checkout -- uv.lock` before committing if it does.

## 3. Authenticate

The router calls staging AI Gateway through a Databricks CLI profile:

```sh
databricks auth login --profile eng-ml-agent-platform
```

This is interactive, so run it in your own terminal. The session's model
catalogs come from the same workspace. If the profile lapses, the router
returns 403 and every routing decision fails open — the sessions still run,
they just do not route.

## 4. Create the isolated config

`.omnigent-local/` is gitignored, so a fresh clone has no config and
`run-server.sh` exits immediately. Create it:

```sh
mkdir -p .omnigent-local
cat > .omnigent-local/config.yaml <<'YAML'
# Isolated config for the routing-MVP worktree. Loaded via
# OMNIGENT_CONFIG_HOME=<worktree>/.omnigent-local so the global
# ~/.omnigent is never touched.

providers:
  eng-ml-agent-platform:
    default: true
    kind: databricks
    profile: eng-ml-agent-platform

routing:
  provider: external
  base_url: https://eng-ml-agent-platform.staging.cloud.databricks.com/ai-gateway/routing/v1
  router_name: task_v1
  model_prefix:
    - databricks-
    - system.ai.
YAML
```

Two details matter. `system.ai.` **keeps its trailing dot** — without it the
code strips ids to `.claude-opus-5` and sends malformed names to the router.
And `router_name` must be `task_v1`, because the arm menus in the code are that
router version's wire contract.

## 5. Start the stack

Three terminals, from the worktree root:

```sh
./run-server.sh      # :6868
./run-host.sh        # host daemon, registers against the server
./run-frontend.sh    # :5273
```

All three source `dev-env.sh`, which pins `OMNIGENT_CONFIG_HOME` and
`OMNIGENT_DATA_DIR` into `.omnigent-local/`. Never start these with your global
`omni` — it writes to the real `~/.omnigent`.

## 6. Confirm it works

```sh
# server is listening and the host registered
curl -s localhost:6868/v1/hosts | head -c 400

# the router answers (recipe R6)
./scripts/probe_routing_api.sh
```

A healthy host reports `gateway_inference` true for the claude and codex
families. If it reports false, Smart Routing is correctly hidden in the UI —
that is the gate in plan block `3f`, not a bug.

Then open `http://localhost:5273`.

## 7. Known local quirks

- **nvm's shell shim breaks in non-interactive shells** (`_load_nvm` FUNCNEST).
  `run-frontend.sh` works around it by resolving a real `node` binary. If you
  invoke node tooling by hand and it fails this way, use an absolute path.
- **`uv run` inside a worktree rewrites `uv.lock`.** Check it out before
  committing.
- **Pre-existing test failures are normal**: ~10 in `tests/cli`, and a set of
  Linux-only sandbox tests (bwrap/seccomp), AF_UNIX path length, tmux socket,
  and tty styling that fail on macOS. Compare failure counts against a clean
  main before blaming a change.
- **The `tests/e2e_ui` Playwright suite leaks seeded workspace files** into the
  main checkout root. Clean up after local runs.
- **`web` type-check has many pre-existing errors** repo-wide; the CI check for
  it is off. Do not treat them as yours.

## 8. Tearing down

```sh
uv run --no-sync omni host stop      # or kill the run-host.sh process
# then kill run-server.sh and run-frontend.sh
lsof -ti:6868,5273 | xargs -r kill   # confirm nothing is still listening
```

Test sessions spawn real agents that do real work in whatever workspace they
are pointed at. Check `git status` in that workspace before and after a matrix
run.

## 9. Personal CLI setup (for moving machines)

Everything above is what the *repo* needs. This section is the personal Claude
Code and Codex configuration that lives in `$HOME` — none of it is in this
directory, which is why a fresh clone alone does not reproduce the environment.
Replace `<user>` with your macOS username in any absolute path.

### 9.1 What this directory actually contributes

Nothing personal. It carries `CLAUDE.md` (a symlink to `AGENTS.md`) and
`.claude/skills/` (eight tracked harness e2e skills). There is no
`.claude/settings.json`, no `.mcp.json`, and no `.envrc`. The only untracked
local state is `.omnigent-local/` (§4) and the staged prompts in `/tmp/p_*.txt`.

### 9.2 Claude Code

Three files are personal and worth copying verbatim:

- `~/.claude/settings.json` — see below.
- `~/.claude/CLAUDE.md` — global agent instructions.
- `~/.claude/keybindings.json` — key bindings.

```json
{
  "env": { "CLAUDE_CODE_NO_FLICKER": "1", "CLAUDE_CODE_SCROLL_SPEED": "5" },
  "permissions": {
    "allow": ["Read","Write","Edit","Glob","Grep","Agent","WebFetch","WebSearch",
      "NotebookEdit","Bash","mcp__github__github_read_api_call",
      "mcp__github__github_write_api_call","Edit(.claude/**)","Write(.claude/**)",
      "Edit(**/CLAUDE.md)","Write(**/CLAUDE.md)","Edit(**/AGENTS.md)","Write(**/AGENTS.md)"],
    "defaultMode": "dontAsk"
  },
  "model": "claude-fable-5[1m]",
  "alwaysThinkingEnabled": true,
  "effortLevel": "medium",
  "theme": "light-daltonized",
  "switchModelsOnFlag": true,
  "teammateMode": "in-process",
  "remoteControlAtStartup": true,
  "agentPushNotifEnabled": true,
  "skipAutoPermissionPrompt": true,
  "useAutoModeDuringPlan": false,
  "voiceEnabled": true,
  "autoUpdater": { "enabled": true },
  "statusLine": {
    "type": "command",
    "command": "bash /Users/<user>/.cache/spec-driven-development/statusline/wrapper.sh"
  },
  "enabledPlugins": {
    "plugin-builder@experimental-plugin-marketplace": true,
    "lite@experimental-plugin-marketplace": true,
    "liteswap@experimental-plugin-marketplace": true,
    "codex@openai-codex": true,
    "security-review-assistant@production-plugin-marketplace": true,
    "dev-productivity@experimental-plugin-marketplace": true,
    "swift-lsp@claude-plugins-official": true,
    "debug-copilot@experimental-plugin-marketplace": true,
    "spec-driven-development@experimental-plugin-marketplace": true,
    "session-report@experimental-plugin-marketplace": true
  },
  "extraKnownMarketplaces": {
    "openai-codex": { "source": { "source": "github", "repo": "openai/codex-plugin-cc" } }
  }
}
```

Do **not** hand-copy `/Library/Application Support/ClaudeCode/managed-settings.json`.
It is root-owned, the Databricks install writes it, and it carries only OTEL and
telemetry env — no `apiKeyHelper` and no `ANTHROPIC_BASE_URL`.

Auth comes from `~/.config/llm-cli/config.json`:

```json
{ "claude_code_proxy_mode": "model-serving", "claude_code_power_user": false,
  "preferred_agent": "claude_code", "mcp_safe_auto_enabled": true,
  "mcp_web_search_auto_enabled": true }
```

The bearer token lives in `~/.databricks/model-serving-token.json` and an
isaac-managed `UserPromptSubmit` hook
(`~/.config/llm-cli/hooks/refresh_model_serving_token.sh`) refreshes it when it
nears expiry. Binary: `~/.local/bin/claude`, version 2.1.220.

### 9.3 Codex

`~/.codex/config.toml`, minus the machine-local blocks:

```toml
model_provider = "Databricks"
model = "gpt-5.6-sol"
model_reasoning_effort = "xhigh"
project_doc_fallback_filenames = ["CLAUDE.md", "COPILOT.md"]

[model_providers.Databricks]
name = "Databricks AI Gateway"
base_url = "https://1965859176160743.ai-gateway.cloud.databricks.com/codex/v1"
wire_api = "responses"

[model_providers.Databricks.auth]
command = "jq"
args = ["-r", ".access_token", "/Users/<user>/.databricks/model-serving-token.json"]
timeout_ms = 5000
refresh_interval_ms = 1500000

[model_providers.Databricks.http_headers]
Databricks-Ai-Gateway-Request-Tags = "{\"source\": \"isaac-cli\"}"

[features]
remote_connections = true
hooks = true
js_repl = false

[profiles.default]
model_provider = "Databricks"
```

Skip the `[otel.*]` and `[marketplaces.*]` blocks: the marketplace entries are
machine-local cache paths that the installer recreates, and the otel blocks
belong to isaac (and hold a token — see §9.4).

`~/.codex/hooks.json` holds five personal hooks, all under
`~/.config/llm-cli/hooks/`:

| Event | Matcher | Script |
| --- | --- | --- |
| `UserPromptSubmit` | — | `handle_prompt_submit.sh`, `refresh_model_serving_token.sh` |
| `PreToolUse` | `^Bash$` | `guard_high_risk_commands.sh` |
| `PreToolUse` | — | `handle_secrets_pre_tool_use.sh` |
| `PreToolUse` | `^mcp__toolproxy__run_command$` | `guard_toolproxy_commands.sh` |
| `PostToolUse` | — | `handle_secrets_post_tool_use.sh` |

These matter for the routing work: they are the user-owned half that Omnigent's
generated `hooks.json` has to merge with in one atomic write.

Binary: `codex-cli 0.145.0` from `~/.nvm/versions/node/v24.14.0/bin/codex`. Keep
that version — 0.145 refuses to load a config that contains `wire_api = "chat"`.

### 9.4 Secrets — move these out of band

Never copy these through git or a chat window:

1. `~/.databricks/model-serving-token.json` — OAuth access plus refresh token.
   Regenerate on the new machine instead of copying.
2. A live `dapi…` PAT sits in the `[otel.*.headers]` blocks of
   `~/.codex/config.toml`. Let isaac rewrite those blocks rather than copying
   the file whole.

### 9.5 Provider topology: three different workspaces

This is the part that is easy to misread. The global Omnigent config and this
worktree's config point somewhere different, and only one of them is
gateway-backed.

`~/.omnigent/config.yaml` (the global host, server on `:8000`):

| Provider | Kind | Resolves to |
| --- | --- | --- |
| `claude` (**default**) | `subscription` | the Claude subscription login — no gateway |
| `codex-databricks` | `cli-config` | defers to `~/.codex/config.toml` → AIGW workspace `1965859176160743` |
| `isaac-databricks-ai-gateway` | `gateway` | AIGW workspace `dbc-a5d4177a-49dc`, `/ai-gateway/anthropic`, default model `system.ai.claude-opus-4-8[1m]` |

`.omnigent-local/config.yaml` (this worktree, server on `:6868`): one provider,
`eng-ml-agent-platform`, `kind: databricks`, `default: true` — staging AIGW —
plus the `routing:` block from §4.

Two things follow. The `/ai-gateway/anthropic` URL in the global config **is**
the AI Gateway, despite "anthropic" in the path; that is the Gateway's
Anthropic-wire route, not `api.anthropic.com`. But that provider is not the
default, so it is not what a launch resolves.

Measured on 2026-08-02 with `omnigent.gateway_inference`:

| Config | claude family | codex family |
| --- | --- | --- |
| global `~/.omnigent` | `False` (config resolves to `None`) | `False` (launch `base_url` is `None`) |
| worktree `.omnigent-local` | `True` | `True` |

So the claude and codex processes that Omnigent spawns during a matrix run in
this worktree are **not** using the personal CLI auth. They are configured by
`.omnigent-local/config.yaml`. The personal setup and the routing stack are
meant to be separate, and the gateway-inference gate reports them differently.

The codex `False` on the global row is a **known false negative**: a
`kind: cli-config` provider makes Omnigent defer to `~/.codex/config.toml`
without resolving a base URL of its own, so the check sees `None` and reports
"not backed" even though that codex install does route through the AI Gateway.
`PR_REWRITE_PLAN.md` block `3f` records it for the rewrite.

### 9.6 Order of operations on a new machine

1. Install isaac. It writes the managed settings, the `~/.config/llm-cli` hooks,
   the Codex Databricks provider, and the token refresher.
2. Copy the personal files only: `~/.claude/settings.json`, `~/.claude/CLAUDE.md`,
   `~/.claude/keybindings.json`, and your Codex `model`,
   `model_reasoning_effort`, and `project_doc_fallback_filenames` preferences.
3. Run `isaac auth login` (or let the refresh hook fire) to mint
   `~/.databricks/model-serving-token.json`.
4. Run `databricks auth login --profile eng-ml-agent-platform` — separate from
   step 3, and needed only for this worktree's stack.
5. Follow §1–§6 above for the routing stack itself.
