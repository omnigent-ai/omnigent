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
