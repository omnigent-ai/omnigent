---
name: copilot-sdk-e2e-dev
description: Spin up a live local Omnigent server and exercise the GitHub Copilot SDK harness end-to-end — build copilot agents, run real turns, smoke-test, and bug-bash. Load when developing, testing, or debugging the copilot harness (omnigent/inner/copilot_executor.py, copilot_harness.py, omnigent/onboarding/copilot_auth.py) or its auth / model / tool-bridge behavior.
---

# Copilot SDK harness: end-to-end dev & testing

The `copilot` harness drives the **GitHub Copilot SDK** (`github-copilot-sdk`,
imported as `copilot`) — a persistent `CopilotClient` + `CopilotSession` per
Omnigent conversation — and bridges Omnigent's `sys_*` tools into Copilot as SDK
`Tool`s. The Python SDK **bundles the Copilot CLI binary it drives** as a backing
server, so there is no separate `@github/copilot` install. This skill is the
proven recipe for running it **for real** against a live local server — not just
the unit tests.

> The harness runs as a **local runner** from your current checkout, so
> `omni run <bundle> --server <url>` exercises exactly the code you're on.

## Prerequisites (check these first)

1. **You're on the branch you want to test.** The copilot harness is an
   optional extra — install it (without disturbing other extras) with
   `uv sync --frozen --extra dev --extra copilot`. NB: a bare
   `uv run --frozen --extra dev` re-syncs the venv and **prunes** the copilot
   SDK; for live testing call `.venv/bin/omni` / `.venv/bin/python` directly and
   avoid `uv run` mid-session.
2. **The SDK is installed:**
   `.venv/bin/python -c "import copilot; print(copilot.__file__)"`.
3. **A GitHub token with Copilot access is configured.** Copilot needs a
   fine-grained PAT with the "Copilot Requests" permission, or an OAuth token
   from the GitHub CLI / Copilot CLI app (classic `ghp_` PATs are rejected).
   Verify (booleans only — never print the token):
   ```bash
   .venv/bin/python -c "from omnigent.onboarding.copilot_auth import copilot_github_token_configured; import os; print('config:', copilot_github_token_configured(), 'env:', bool(os.environ.get('GH_TOKEN') or os.environ.get('COPILOT_GITHUB_TOKEN')))"
   ```
   If both are `False`, run `omni setup` and register a Copilot token, or
   `export GH_TOKEN=$(gh auth token)` (when `gh` is logged into an account with
   Copilot). Check the account's entitlement with
   `gh api /copilot_internal/user` (look for `chat_enabled`/`cli_enabled`).
4. **Network egress to GitHub's Copilot backend.** A turn that hangs or fails to
   connect on a locked-down host is usually egress, not a harness bug.

## Step 1 — start a local server

```bash
cd /path/to/omnigent
.venv/bin/omni server --port 7788 --no-open    # foreground; or `omni server start` for detached
curl -s http://127.0.0.1:7788/health           # {"status":"ok"}
```

Use the URL below as `$SERVER`.

## Step 2 — build a copilot agent bundle

A spec with `spec_version` **must be a directory containing `config.yaml`** —
not a single `.yaml` file. Minimal copilot agent:

```bash
mkdir -p /tmp/copilot-dev
cat > /tmp/copilot-dev/config.yaml <<'YAML'
spec_version: 1
name: copilot-dev
description: Copilot SDK dev/test agent.
executor:
  type: omnigent
  config:
    harness: copilot
    # model: gpt-5-mini      # optional; omit for Copilot auto-select
prompt: |
  You are a terse test agent. Answer in as few words as possible.
YAML
```

For sub-agents, tools, guardrails/policies, copy the field shapes from
`examples/polly/config.yaml` and `examples/debby/config.yaml`. (Declare policies
under `guardrails.policies:` — a top-level `policies:` key is silently dropped on
the `spec_version` + `config.yaml` path.)

## Step 3 — run a turn (and smoke-test)

```bash
SERVER=http://127.0.0.1:7788
timeout 280 .venv/bin/omni run /tmp/copilot-dev \
  -p "Reply with exactly the single word: PONG" \
  --server "$SERVER" 2>&1
```

A healthy run prints connection lines then the reply (`PONG`). If that works,
the full stack is good: token, egress, bundled CLI, harness.

- **Shell / file tools:** add `--tools coding`.
- **Specific model:** add `--model gpt-5-mini` (or `claude-haiku-4.5`, `auto`).

## Targeted scenarios

| Goal | How |
|------|-----|
| Native tools (shell/edit/read) | `--tools coding`, prompt to create→read→edit a file; confirm it actually touches disk |
| Bridged `sys_*` / sub-agent dispatch | declare a sub-agent (harness `copilot` so auth is satisfied), prompt the parent to delegate — exercises the SDK `Tool` async-handler bridge into `_tool_executor` |
| Model routing | run the same bundle with several `--model` values; an unknown id fails **loud**, a `databricks-*` id is dropped to auto with a warning |
| LLM-phase policy | add a guardrail that denies a keyword; confirm `PHASE_LLM_REQUEST`/`PHASE_LLM_RESPONSE` blocks it |
| Concurrency / leaks | fire several `omni run … &` at once; then `pgrep -af "copilot/bin/copilot"` to check for orphaned bundled-CLI subprocesses |

## Running polly (or any orchestrator) on a copilot brain

The copilot harness can serve as an **async orchestrator** brain (polly / debby),
not just a standalone agent — it dispatches to sub-agents via the bridged
`sys_*` tools and synthesizes their results. Two ways to exercise it:

**1. Committed regression guard (brain smoke).**
`tests/e2e/test_polly_copilot_e2e.py` boots a local server from your checkout and
runs `examples/polly` with `--harness copilot --model auto`, asserting the brain
boots and replies. It is **skipped** unless a Copilot token is configured (so CI
without one skips it). Run it with:

```bash
.venv/bin/python -m pytest -o addopts="" tests/e2e/test_polly_copilot_e2e.py -v
```

**2. Full orchestration (dispatch → collect → synthesize).** Use the
`polly-e2e-dev` driver (in the internal `agent-framework` clone) — it boots a
local server, polls the AP API, auto-answers elicitations, and asserts the
fan-out. Drive the brain on copilot with `--brain-harness copilot`, and **always
pass a Copilot-catalog `--brain-model`** (`auto`, `claude-haiku-4.5`,
`gpt-5-mini`): the driver's default `--brain-model` is a Claude id that Copilot
(no Databricks gateway) can't route. From the agent-framework clone:

```bash
.venv/bin/python .claude/skills/polly-e2e-dev/polly_driver.py \
  --local --code-dir <this-worktree> \
  --cuj smoke --brain-harness copilot --brain-model auto      # brain only
# --cuj fanout  …  and  --cuj review-pr --repo omnigent-ai/omnigent --pr <n>  …
#   exercise real sub-agent dispatch (claude_code + codex) under a copilot brain.
```

All three CUJs (smoke / fanout / review-pr) pass on a copilot brain (verified
live: fanout dispatched 8 sub-agents, 8/8 OK + a synthesis). Note `omni run -p`
exits after the dispatch turn (the brain parks until woken), so a sub-agent's
final answer lands server-side — read it over the AP API
(`GET /v1/sessions/{id}/items`, child sessions), not just stdout.

## Gotchas (these cost real time)

1. **`config.yaml`'s `server:` defaults to a *remote* server.** Omitting
   `--server` sends your turn to that remote deploy — which may be **stale** and
   reject the copilot harness with `executor.config.harness: must be one of […]`.
   **Always pass `--server http://127.0.0.1:<port>`.** (If a *local* server
   rejects `copilot`, it's running stale code — restart it from your checkout.)
2. **A spec with `spec_version` must be a directory + `config.yaml`**, never a
   single `.yaml` file.
3. **Copilot needs a GitHub token** (fine-grained PAT w/ Copilot Requests, or a
   gh/Copilot-CLI OAuth token). Resolution precedence: spec `executor.auth`
   (api_key) > stored `copilot:` config block (`omni setup`) > ambient
   `COPILOT_GITHUB_TOKEN` / `GH_TOKEN` / `GITHUB_TOKEN`. Classic `ghp_` rejected.
4. **No Databricks gateway.** Copilot talks only to GitHub's backend, so a
   `databricks-*` model is silently resolved to Copilot's auto-select — it will
   *not* route through the AI Gateway like claude-sdk/codex/pi.
5. **Use a model id from the account's catalog.** free_limited offers `auto`,
   `claude-haiku-4.5`, `gpt-5-mini`. Run `.venv/bin/python` + `client.list_models()`
   to discover the live set; an unknown id fails loud (server-side failed session).
6. **Turns take 30–90s** — always wrap in `timeout 280`.
7. **Never print/echo the GitHub token** in logs or commands.

## Code & tests

- **Executor (SDK bridge):** `omnigent/inner/copilot_executor.py`
- **Wrap (HARNESS_COPILOT_* env → executor):** `omnigent/inner/copilot_harness.py`
- **Auth / token resolution:** `omnigent/onboarding/copilot_auth.py`
- **Spawn env:** `_build_copilot_spawn_env` in `omnigent/runtime/workflow.py`

```bash
uv run --frozen --extra dev python -m pytest \
  tests/inner/test_copilot_executor.py \
  tests/inner/test_copilot_harness.py \
  tests/runtime/test_copilot_spawn_env.py \
  tests/onboarding/test_copilot_auth.py -q
```

## Bug-bash (fan out)

To stress the harness, run several scenario probes in parallel — each builds a
bundle and runs real turns against the same `$SERVER`, then reports what broke.
Highest-value targets: the `Tool` async-handler bridge (hangs / lost tool
results / errors reported as success), model routing, policy enforcement,
streamed-output rendering, and orphaned bundled-CLI processes after teardown.
Cross-check the AP API (`GET /v1/sessions/{id}/items`) — a start failure can exit
0 with empty stdout while the server records a `failed` session.

## Known sharp edges (found via live bug-bash — "as of this writing")

- **Native tools bypass `on:[tool_call]` policies and aren't recorded.** Copilot's
  built-in `create`/`view`/`edit`/`bash` run inside the SDK, so an
  `on:[tool_call]` DENY guardrail (e.g. `blast_radius`) never sees them, and they
  leave no `function_call` item in the transcript (only streamed narration).
  **Bridged `sys_*` tools ARE gated and recorded.** Gate Copilot's built-ins at
  the LLM phase (`PHASE_LLM_REQUEST`/`RESPONSE`, which fire) or via the OS-env
  sandbox — not `on:[tool_call]`. (Same shape as the cursor harness.)
- **Copilot fails loud (unlike cursor's swallowed start failures).** Bad token,
  empty/invalid model, and unknown model ids all exit non-zero with a clear error
  AND a server-side failed session + error item — verified, not swallowed.
- **`omni run -p` against an async orchestrator exits after the dispatch turn**,
  so a delegated sub-agent's final answer is persisted server-side but may not
  reach stdout in one-shot mode. Read the session over the AP API to see it.
- **Non-graceful exit can orphan the bundled CLI.** Graceful teardown reaps it
  (`client.stop()`); after a `SIGKILL`/hard-exit, sweep
  `pgrep -af "copilot/bin/copilot"`.

## Cleanup

```bash
.venv/bin/omni server stop        # or kill the foreground `omni server`
rm -rf /tmp/copilot-dev           # remove scratch bundles
pgrep -af "copilot/bin/copilot"   # confirm no orphaned bundled-CLI subprocesses linger
```
