---
name: cursor-sdk-e2e-dev
description: Spin up a live local Omnigent server and exercise the Cursor SDK harness end-to-end — build cursor agents, run real turns, smoke-test, and bug-bash. Load when developing, testing, or debugging the cursor harness (omnigent/inner/cursor_executor.py, cursor_harness.py, cursor_auth.py) or its auth / model / tool-bridge behavior.
---

# Cursor SDK harness: end-to-end dev & testing

The `cursor` harness drives the **Cursor Python SDK** (`cursor_sdk`, an
`AsyncAgent` over a local bridge) and bridges Omnigent's `sys_*` tools into
Cursor as SDK `custom_tools`. This skill is the proven recipe for running it
**for real** against a live local server — not just the unit tests.

> The harness runs as a **local runner** from your current checkout, so
> `omni run <bundle> --server <url>` exercises exactly the code you're on.

## Prerequisites (check these first)

1. **You're on the branch you want to test.** The cursor harness merged to
   `main` (#203/#204). Test on `main` unless validating a specific branch.
2. **A Cursor API key is configured.** The SDK *requires* an API key
   (`crsr_…`); there is no `cursor-agent login` path. Verify (booleans only —
   never print the key):
   ```bash
   .venv/bin/python -c "from omnigent.onboarding.cursor_auth import cursor_api_key_configured; import os; print('config:', cursor_api_key_configured(), 'env:', bool(os.environ.get('CURSOR_API_KEY')))"
   ```
   If both are `False`, run `omni setup` and register a Cursor key, or
   `export CURSOR_API_KEY=crsr_…`.
3. **`cursor-sdk` is installed** (a baseline dependency):
   `.venv/bin/python -c "import cursor_sdk; print(cursor_sdk.__file__)"`.
4. **Network egress to Cursor's backend.** The bridge subprocess talks to
   Cursor's own API; a turn that hangs or fails to connect on a locked-down
   host is usually an egress problem, not a harness bug.

## Step 1 — start a local server

```bash
cd /path/to/omnigent
.venv/bin/omni server --background          # spawns a detached server on a free loopback port
.venv/bin/omni server status         # prints the URL, e.g. http://127.0.0.1:6767
```

Use the **printed URL** below as `$SERVER`. (You can also run a foreground
server on a fixed port with `omnigent server --port 7777 --no-open`.)

## Step 2 — build a cursor agent bundle

A spec with `spec_version` **must be a directory containing `config.yaml`** —
not a single `.yaml` file. Minimal cursor agent:

```bash
mkdir -p /tmp/cursor-dev
cat > /tmp/cursor-dev/config.yaml <<'YAML'
spec_version: 1
name: cursor-dev
description: Cursor SDK dev/test agent.
executor:
  type: omnigent
  config:
    harness: cursor
    # model: gpt-5            # optional; omit for cursor "auto"
prompt: |
  You are a terse test agent. Answer in as few words as possible.
YAML
```

For sub-agents, tools, guardrails/policies, copy the field shapes from
`examples/polly/config.yaml` and `examples/debby/config.yaml`.

## Step 3 — run a turn (and smoke-test)

```bash
SERVER=http://127.0.0.1:6767   # the URL from `omni server status`
timeout 280 .venv/bin/omni run /tmp/cursor-dev \
  -p "Reply with exactly the single word: PONG" \
  --server "$SERVER" 2>&1
```

A healthy run prints connection lines then the assistant reply (`PONG`). If
that works, the full stack is good: key, egress, bridge, harness.

- **Shell / file tools:** add `--tools coding`.
- **Specific model:** add `--model gpt-5` (or `composer-1`, `auto`,
  `databricks-claude-opus-4-8`, …).

## Targeted scenarios

| Goal | How |
|------|-----|
| Native tools (shell/edit/read) | `--tools coding`, prompt to create→read→edit a file and run a shell command; confirm it actually touches disk |
| Bridged `sys_*` / sub-agent dispatch | declare a sub-agent (`tools.agents`/`spawn`), prompt the cursor agent to delegate — exercises the `custom_tools` daemon-thread bridge (`run_coroutine_threadsafe`) |
| Model routing | run the same bundle with several `--model` values; note which actually runs |
| Policy / guardrail | add a guardrail that denies a keyword; confirm `PHASE_LLM_REQUEST`/`PHASE_LLM_RESPONSE` blocks it |
| Concurrency / leaks | fire several `omni run … &` at once; then `pgrep -af "cursor-sdk-bridge|cursor_sdk"` to check for orphaned bridge subprocesses |

## Gotchas (these cost real time)

1. **`config.yaml`'s `server:` defaults to a *remote* server** (e.g. a
   Databricks Apps URL). Omitting `--server` sends your turn to that remote
   deploy — which may be **stale** and reject the cursor harness with
   `executor.config.harness: must be one of […], got 'cursor'`. **Always pass
   `--server http://127.0.0.1:<port>`** for local testing. (That allowlist is
   `omnigent/spec/_omnigent_compat.py`; if a *local* server rejects `cursor`,
   it's running stale code — restart it from your checkout.)
2. **A spec with `spec_version` must be a directory + `config.yaml`**, never a
   single `.yaml` file.
3. **Cursor needs a `crsr_` API key** (no CLI login). Resolution precedence:
   spec `executor.auth` (api_key) > stored `cursor:` config block (`omni
   setup`) > ambient `CURSOR_API_KEY`.
4. **No Databricks gateway.** Cursor talks only to Cursor's backend, so a
   `databricks-*` model is silently resolved to cursor `auto` — it will *not*
   route through the AI Gateway like claude-sdk/codex/pi.
5. **Use a model id from the account's catalog.** Bare `gpt-5` is **not** valid;
   the SDK rejects unknown ids. Valid examples seen live: `default`,
   `composer-2.5`, `claude-opus-4-8`, `gpt-5.5`. Run with `--model` and read the
   SDK's `Available models:` list to discover the live set.
5. **Turns take 30–90s** — always wrap in `timeout 280`.
6. **Local-runner topology:** `omni run <bundle> --server <url>` runs the
   harness from your **current checkout**; the server only holds state. The
   managed `omni server --background` server runs from whatever venv launched it.
7. **Never print/echo the Cursor key** in logs or commands.

## Code & tests

- **Executor (SDK bridge):** `omnigent/inner/cursor_executor.py`
- **Wrap (HARNESS_CURSOR_* env → executor):** `omnigent/inner/cursor_harness.py`
- **Auth / key resolution:** `omnigent/onboarding/cursor_auth.py`
- **Spawn env:** `_build_cursor_spawn_env` in `omnigent/runtime/workflow.py`

```bash
# Unit tests (use --frozen; the cwsandbox extra is unsatisfiable on public PyPI here)
uv run --frozen --extra dev python -m pytest \
  tests/inner/test_cursor_executor.py \
  tests/runtime/test_cursor_spawn_env.py \
  tests/onboarding/test_cursor_auth.py -q
# Gated end-to-end harness test
uv run --frozen --extra dev python -m pytest tests/e2e/omnigent/test_per_harness_cursor.py -q
```

## Bug-bash (fan out)

To stress the harness, run several scenario probes in parallel — each builds a
bundle and runs real turns against the same `$SERVER`, then reports what broke.
Highest-value targets: the `custom_tools` bridge (hangs / lost tool results /
errors reported as success), model routing, policy enforcement, streamed-output
rendering, and orphaned bridge processes after teardown.

## Known sharp edges (found via live bug-bash — "as of this writing")

Live-observed cursor-harness behaviors to watch for while testing (some may be
fixed by the time you read this — verify):

- **Start failures are swallowed.** An invalid/unavailable `--model` (or any
  bridge start error) makes `omni run -p` exit **0 with empty output**, while
  the server records a `failed` session + a `RuntimeError` item the user never
  sees. If a turn returns nothing, check the session status / items
  (`GET /v1/sessions/{id}/items`) — don't assume success. (claude-sdk surfaces
  such errors; cursor doesn't yet.)
- **Built-in coding tools bypass `on:[tool_call]` policies.** Cursor's native
  shell/file tools (`--tools coding`) don't emit `tool_call` events, so
  `on:[tool_call]` guardrails (e.g. `blast_radius`) never see them — a built-in
  shell can run `git push --force` even under a DENY policy. **Bridged `sys_*`
  tools *are* gated correctly.** Don't rely on `on:[tool_call]` guardrails for
  cursor built-in tools.
- **Run-on assistant text.** Adjacent assistant text blocks are concatenated
  with no separator, so pre-tool narration can glue onto the post-tool answer.
- **Non-graceful exit orphans the bridge.** Graceful teardown reaps it (the
  #221 `aclose` fix works), but a `SIGKILL`/hard-exit leaves an orphaned
  `cursor-sdk-bridge`. After hard kills, sweep `pgrep -af cursor-sdk-bridge`.

## Cleanup

```bash
.venv/bin/omni server stop      # stop the managed background server
rm -rf /tmp/cursor-dev          # remove scratch bundles
pgrep -af "cursor-sdk-bridge"   # confirm no orphaned bridge subprocesses linger
```
