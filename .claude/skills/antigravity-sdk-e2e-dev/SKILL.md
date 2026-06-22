---
name: antigravity-sdk-e2e-dev
description: Spin up a live local Omnigent server and exercise the Antigravity (Gemini) SDK harness end-to-end — build antigravity agents, run real turns, smoke-test, and bug-bash. Load when developing, testing, or debugging the antigravity harness (omnigent/inner/antigravity_executor.py, antigravity_harness.py, omnigent/onboarding/antigravity_auth.py) or its auth / model / tool-bridge behavior.
---

# Antigravity SDK harness: end-to-end dev & testing

The `antigravity` harness drives Google's **Antigravity Python SDK**
(`google-antigravity`, an in-process `Agent`/`Conversation`) and bridges
Omnigent's `sys_*` tools into the SDK as `custom_tools`. It is **Gemini-native**:
it authenticates with a Gemini / Antigravity API key (or Vertex AI) and has **no
OpenAI-compatible gateway / Databricks path**. This skill is the proven recipe
for running it **for real** against a live local server — not just the unit
tests.

> The harness runs as a **local runner** from your current checkout, so
> `omni run <bundle> --server <url>` exercises exactly the code you're on.

## Prerequisites (check these first)

1. **You're on the branch you want to test.** The antigravity harness merged to
   `main` (#194). Test on `main` unless validating a specific branch.
2. **A Gemini API key is configured.** The SDK *requires* one (`AIza…`); there
   is no login flow. Verify (booleans only — never print the key):
   ```bash
   .venv/bin/python -c "from omnigent.onboarding.antigravity_auth import antigravity_api_key_configured as c; import os; print('config:', c(), 'env:', bool(os.environ.get('GEMINI_API_KEY') or os.environ.get('ANTIGRAVITY_API_KEY')))"
   ```
   If both are `False`, run `omni setup` → **Antigravity** and paste a key, or
   `export GEMINI_API_KEY=AIza…`.
3. **`google-antigravity` is installed** (the `antigravity` extra —
   `pip install "omnigent[antigravity]"`):
   `.venv/bin/python -c "import google.antigravity as a; print(a.__file__)"`.
4. **glibc ≥ ~2.36.** The SDK spawns a **native `localharness` binary** that
   needs a recent glibc (`GLIBC_ABI_DT_RELR`). Check `ldd --version | head -1`.
   On an older host the turn fails at setup with
   `RuntimeError: … localharness: … version 'GLIBC_ABI_DT_RELR' not found`. Dev
   workaround on a glibc-2.31 box: point the SDK at a loader-shim via
   `ANTIGRAVITY_HARNESS_PATH=/path/to/shim` that runs the *untouched* bundled
   binary through a newer glibc's loader (see the auto-memory note
   `antigravity-harness-glibc-native-binary.md`). The shim is dev-only — the
   real fix is a glibc-≥2.36 host.
5. **Network egress to the Gemini backend.** The native binary talks to
   Google's API; a turn that hangs or fails to connect on a locked-down host is
   usually an egress problem, not a harness bug.

## Step 1 — start a local server

```bash
cd /path/to/omnigent
.venv/bin/omni server start          # spawns a detached server on a free loopback port
.venv/bin/omni server status         # prints the URL, e.g. http://127.0.0.1:6767
```

Use the **printed URL** below as `$SERVER`. (You can also run a foreground
server on a fixed port with `omnigent server --port 7777 --no-open`.)

## Step 2 — build an antigravity agent bundle

A spec with `spec_version` **must be a directory containing `config.yaml`** —
not a single `.yaml` file. Minimal antigravity agent (no `auth:` block → it
resolves the key from the `antigravity:` config / ambient env):

```bash
mkdir -p /tmp/agy-dev
cat > /tmp/agy-dev/config.yaml <<'YAML'
spec_version: 1
name: agy-dev
description: Antigravity SDK dev/test agent.
executor:
  type: omnigent
  config:
    harness: antigravity
    model: gemini-3.5-flash      # default; gemini-3-pro 404s on a plain AI-Studio key
prompt: |
  You are a terse test agent. Answer in as few words as possible.
YAML
```

For sub-agents, tools, guardrails/policies, copy the field shapes from
`examples/polly/config.yaml` and `examples/debby/config.yaml`.

## Step 3 — run a turn (and smoke-test)

```bash
SERVER=http://127.0.0.1:6767   # the URL from `omni server status`
timeout 280 .venv/bin/omni run /tmp/agy-dev \
  -p "Reply with exactly the single word: PONG" \
  --server "$SERVER" 2>&1
```

A healthy run prints connection lines then the assistant reply (`PONG`). If
that works, the full stack is good: Gemini key, glibc/native binary, egress,
streaming, harness.

- **Shell / file tools:** add `--tools coding`.
- **Specific model:** add `--model gemini-2.5-flash` (or another Gemini id).

## Targeted scenarios

| Goal | How |
|------|-----|
| Native tools (shell/edit/read) | `--tools coding`, prompt to create→read→edit a file and run a shell command; confirm it actually touches disk |
| Bridged `sys_*` / sub-agent dispatch | declare a sub-agent (`tools.agents`/`spawn`), prompt the agent to delegate — exercises the `custom_tools` bridge + `PostToolCallHook` |
| Model routing | run the same bundle with several `--model` Gemini ids; note which actually runs |
| Vertex AI auth | set `executor.config.vertex: true` + `project`/`location` and use GCP application-default creds instead of an API key |
| Policy / guardrail | add a guardrail that denies a keyword; confirm it blocks (see the **sharp edges** below — LLM-phase + tool-call enforcement was incomplete at merge) |
| Per-session brain override | run a bundle agent (polly/debby) and select `antigravity` as the brain harness (it's in `BRAIN_HARNESS_LABELS`) |
| Concurrency / leaks | fire several `omni run … &` at once; then `pgrep -af localharness` to check for orphaned native subprocesses |

## Gotchas (these cost real time)

1. **`config.yaml`'s `server:` defaults to a *remote* server.** Omitting
   `--server` sends your turn to that remote deploy — which may be **stale** and
   reject the antigravity harness with `executor.config.harness: must be one of
   […], got 'antigravity'`. **Always pass `--server http://127.0.0.1:<port>`**
   for local testing. (That allowlist is `omnigent/spec/_omnigent_compat.py`; if
   a *local* server rejects `antigravity`, it's running stale code — restart it
   from your checkout.)
2. **A spec with `spec_version` must be a directory + `config.yaml`**, never a
   single `.yaml` file.
3. **Antigravity needs a Gemini key** (no login). Resolution precedence: spec
   `executor.auth` (api_key) > stored `antigravity:` config block (`omni setup`)
   > ambient `GEMINI_API_KEY` / `ANTIGRAVITY_API_KEY`. Vertex AI is opt-in via
   `executor.config` `vertex`/`project`/`location`.
4. **No OpenAI gateway / Databricks.** The SDK has no `base_url`; a `databricks`
   or generic-`provider` auth is **warned and ignored**, and the run falls back
   to ambient Gemini creds. Don't expect `databricks-*` models to route through
   the AI Gateway like claude-sdk/codex/pi.
5. **Model ids are Gemini ids.** Default `gemini-3.5-flash`. `gemini-3-pro`
   **404s on a plain AI-Studio key** — use `gemini-2.5-flash` / `gemini-3.5-flash`
   unless your key has Pro access.
6. **The native binary needs glibc ≥ ~2.36** (see Prereq 4). This is the most
   common "it won't even start" cause; check it before assuming a harness bug.
7. **Turns take ~10–60s** — always wrap in `timeout 280`.
8. **Local-runner topology:** `omni run <bundle> --server <url>` runs the
   harness from your **current checkout**; the server only holds state. The
   managed `omni server start` server runs from whatever venv launched it.
9. **Never print/echo the Gemini key** in logs or commands.

## Code & tests

- **Executor (SDK driver):** `omnigent/inner/antigravity_executor.py`
- **Wrap (HARNESS_ANTIGRAVITY_* env → executor):** `omnigent/inner/antigravity_harness.py`
- **Auth / key resolution:** `omnigent/onboarding/antigravity_auth.py`
- **Spawn env:** `_build_antigravity_spawn_env` in `omnigent/runtime/workflow.py`

```bash
# Unit tests (use --frozen; the cwsandbox extra is unsatisfiable on public PyPI here)
uv run --frozen --extra dev python -m pytest \
  tests/unit/inner/test_antigravity_executor.py \
  tests/unit/inner/test_antigravity_harness.py \
  tests/unit/runtime/test_antigravity_spawn_env.py \
  tests/unit/onboarding/test_antigravity_auth.py -q
# (or, if uv re-resolve is blocked on your host: .venv/bin/python -m pytest <same paths> -q)
```

There is no gated per-harness antigravity e2e test yet (it is deliberately
excluded from the live no-AGENT harness matrix in
`tests/e2e/omnigent/test_run_harness_without_agent_e2e.py`, because that matrix
authenticates through the Databricks gateway and antigravity is Gemini-native).
This skill IS the live coverage.

## Bug-bash (fan out)

To stress the harness, run several scenario probes in parallel — each builds a
bundle and runs real turns against the same `$SERVER`, then reports what broke.
Highest-value targets: the `custom_tools` bridge (hangs / lost tool results /
errors reported as success), model routing, policy enforcement, streamed-output
rendering, history retention across turns, and orphaned `localharness`
processes after teardown.

## Known sharp edges (found via the merge review — "as of this writing")

Several were merged as-is and have **fix PRs in flight (#276–#281)** — verify
against your checkout:

- **Native/built-in tools bypass the TOOL_CALL policy.** Only a
  `PostToolCallHook` (post-execution, can't block) was installed at merge, so a
  DENY/ASK guardrail doesn't gate the SDK's native shell/file tools before they
  run. Bridged `sys_*` tools route through the server. *(Fix: policy-enforcement PR.)*
- **LLM_REQUEST / LLM_RESPONSE policies aren't evaluated** in `run_turn` (prompt-
  deny / output-block silently ignored). *(Fix: policy-enforcement PR.)*
- **History on a fresh/rebuilt session.** The SDK has no history-injection API,
  so prior turns are replayed as a plain-text `"Conversation so far: …"` prefix
  (user/assistant text only; tool calls aren't reconstructed). *(PR #278.)*
- **`sys_list_models` can over-report OpenAI-family models** for antigravity
  (it was mapped to the openai family for shared lookups); the worker only runs
  Gemini. *(Fix: openai-family-cleanup PR.)*
- **Per-session `/model` override** was rejected with a false "no plumbing"
  error. *(PR #276.)*  **Global `auth:` (an OpenAI key)** could be adopted as a
  Gemini key. *(PR #277.)*  **Tool parameter schemas** were dropped (model flew
  blind on arg shapes). *(PR #279.)*
- **A failed turn** (e.g. the glibc error, a bad model) surfaces as a `failed`
  session + an error item — if a turn returns little, check
  `GET /v1/sessions/{id}` status and `…/items` rather than assuming success.

## Cleanup

```bash
.venv/bin/omni server stop      # stop the managed background server
rm -rf /tmp/agy-dev             # remove scratch bundles
pgrep -af "localharness"        # confirm no orphaned native subprocesses linger
```
