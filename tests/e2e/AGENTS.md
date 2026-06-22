# `tests/e2e/` — prerequisites & how to run

These tests start a real `omnigent` server subprocess, upload real agent bundles, and call real LLM APIs. They are **excluded from the default `pytest` run** via `addopts = --ignore=tests/e2e` in `pyproject.toml`. To exercise them you must opt in with `--llm-api-key` (and optionally `--profile`).

## ALWAYS RUN INTEGRATION + UNIT TESTS IN THE BACKGROUND

The e2e suite takes 5–10 minutes even fully parallel; the unit suite takes 5–7 minutes parallel. **Never block your terminal (or an interactive Claude Code session) on a foreground run** — kick them off backgrounded and monitor:

```bash
# Pattern: launch with `&`, tee to a known log path. The `env -u`
# strips host shell vars so the spawned server only sees the
# `--llm-api-key` value (which `live_server` re-injects into the
# subprocess env as OPENAI_API_KEY). Without the strip, a stale
# `OPENAI_API_KEY` in the parent shell silently shadows the test
# flag's value.
#
# Set $PROFILE to your Databricks profile name (no default — pick
# one from ~/.databrickscfg that's authorized for the serving
# endpoints you want to hit), and $TOKEN to a fresh workspace PAT
# for that profile (e.g. `TOKEN=$(databricks auth token --profile
# "$PROFILE" | jq -r .access_token)`).
(env -u DATABRICKS_TOKEN -u OPENAI_API_KEY \
  uv run --no-sync pytest tests/e2e/ \
    --llm-api-key="$TOKEN" --profile="$PROFILE" \
    -n 8 --dist=loadscope \
    --tb=line -q -rfs 2>&1 | tee /tmp/e2e.log) &

# Monitor: poll the log for the terminal summary line, OR tail it
until grep -qE "passed in [0-9]|failed in [0-9]|short test summary" /tmp/e2e.log 2>/dev/null; do sleep 10; done
grep -E "passed in [0-9]|failed in [0-9]" /tmp/e2e.log | tail -1
```

This applies to **both** the unit suite (`uv run pytest -n 8 --dist=loadfile`) and the e2e suite. Inside Claude Code, use `Bash(run_in_background=true)` plus a separate `Bash` polling loop with `until grep -q ...` to wait for the terminal marker — that pattern frees the assistant to continue with other work and surfaces the summary as a single notification.

## Prerequisites

### LLM credentials — pick ONE

**Option A (preferred): Databricks profile.** Recommended on machines that already have `~/.databrickscfg` set up. Routes the spawned server through `<workspace-host>/serving-endpoints` and rewrites bundle `llm.model` values to their Databricks-served equivalents (see `_DATABRICKS_MODEL_MAP` in `tests/e2e/conftest.py`).

```bash
export PROFILE=<your-profile>
TOKEN=$(databricks auth token --profile "$PROFILE" | jq -r .access_token)
uv run pytest tests/e2e/ \
  --llm-api-key="$TOKEN" \
  --profile="$PROFILE" \
  -n 8 --dist=loadscope
```

**Option B: OpenAI API key.** Routes the spawned server at `api.openai.com` with the bare key. Bundles use their original `llm.model` values (`gpt-5.4`, `gpt-4o`, `claude-sonnet-4-20250514`, …).

```bash
export OPENAI_API_KEY=sk-...
uv run pytest tests/e2e/ \
  --llm-api-key="$OPENAI_API_KEY" \
  -n 8 --dist=loadscope
```

### Binaries on `PATH`

Some tests gate on local binaries. Install whichever you need; tests for missing binaries skip individually rather than failing the suite.

| Binary  | Required by                                           | Install                                                |
|---------|-------------------------------------------------------|--------------------------------------------------------|
| `tmux`  | `test_sys_terminal_e2e.py`, `test_repl_terminal_overview_e2e.py`, `tests/unit/inner/test_terminal*.py`, `tests/unit/terminals/`, `tests/unit/tools/builtins/test_sys_terminal.py` | `brew install tmux` (macOS) / `apt install tmux` (Debian) |
| `claude` | `claude-sdk` harness rows (`test_per_harness_claude_sdk.py` and any `[claude-sdk]` parametrize) | Anthropic Claude CLI — see `claude-agent-sdk` docs    |
| `codex`  | `codex` harness rows (`test_per_harness_codex.py`, `test_run_omnigent_coding_supervisor.py`)                              | OpenAI Codex CLI                                       |
| `pi`     | `pi` harness rows (`test_per_harness_pi.py`)                                                            | Internal CLI — see project docs                        |
| `databricks` | Required only when using `--profile <name>`                                                         | `brew install databricks` (macOS) / official installer |
| `omnigent` (formerly `ap`) | A handful of legacy tests (`test_repl_approval_e2e.py`, `test_dispatch_fork_repl_e2e.py`)        | `uv sync` makes the CLI available via `uv run omnigent …`. Tests checking for a standalone `ap` binary on PATH currently skip — that's pre-existing infra debt unrelated to this directory. |

### Python environment

- `uv sync --extra dev --extra claude-sdk --extra openai-agents` from the repo root to install pytest, pytest-xdist, filelock, and harness SDKs.
- The `databricks-sdk` is a runtime dep, so `databricks auth token` works from any shell once `~/.databrickscfg` is set up.

## Recommended invocation

```bash
# Parallel + Databricks profile (the fastest, most representative path).
# Export PROFILE first; the snippet reuses it twice.
export PROFILE=<your-profile>
TOKEN=$(databricks auth token --profile "$PROFILE" | jq -r .access_token)
uv run pytest tests/e2e/ \
  --llm-api-key="$TOKEN" \
  --profile="$PROFILE" \
  -n 8 --dist=loadscope
```

- `-n 8` is the empirical sweet spot on a 12-core laptop — fastest wall time and the same flake count as `-n auto` (12). `-n 4` is more stable (zero timing-ordering flakes; matches main's failure set exactly) but ~25% slower. Bump up if your host has more cores.
- `--dist=loadscope` keeps tests within one file on a single worker so the session-scoped `live_server` + agent-upload fixtures only spawn once per worker per file.
- `--profile <name>` (optional) routes through the named Databricks workspace's serving-endpoints and rewrites bundle `llm.model` values to Databricks equivalents. Without it the spawned server hits `api.openai.com`.

## Skip-reason cheat-sheet

When tests skip, check the `-rs` summary for one of these reasons:

| Reason                                         | Fix                                                      |
|------------------------------------------------|----------------------------------------------------------|
| `tmux not installed; …`                        | Install tmux                                             |
| `tmux, pi, and provider API key required`      | Install tmux + pi + set `OPENAI_API_KEY` (or `--profile`)|
| `OPENAI_API_KEY not set`                        | `export OPENAI_API_KEY=…` or pass `--llm-api-key`        |
| `Integration tests require --integration flag` | Add `--integration` to the pytest invocation             |
| `<harness>'s CLI not on PATH`                  | Install the named binary (`claude` / `codex` / `pi`)     |
| `requires --profile <name>`                    | Pass `--profile <name>`                                  |
| `test uses an LLM judge that hits api.openai.com directly` | Either drop `--profile` (so `--llm-api-key` is used as the OpenAI key), or keep `--profile` and also export a real `OPENAI_API_KEY=sk-…` in your shell (don't strip it via `env -u`). Skipping is the default to avoid spending OpenAI quota on every CI run. |

## Parallel-safety notes

- `patched_databrickscfg` (in `tests/e2e/omnigent/conftest.py`) acquires a cross-process `FileLock` on `~/.databrickscfg.e2e-lock` for the backup → patch → restore sequence, so xdist workers serialize on the rewrite. Tests not using that fixture parallelize freely.
- A few tests still write to fixed `/tmp/...` paths (`test_harness_wrap_e2e.py`, `test_example_agent_with_os_env_fork.py`). They serialize naturally because each is a single test, but if you add new tests that also write to those paths you'll need to coordinate.
