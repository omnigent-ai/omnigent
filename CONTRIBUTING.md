# Contributing to Omnigent

Thanks for your interest in improving Omnigent. Issues and pull requests are
welcome. For larger changes, open an issue first so we can discuss the approach.

Please don't include secrets, internal URLs, customer data, or private
configuration in issues, tests, examples, or logs.

## Development setup

This is a Python package with an optional frontend under `web/`. Use
[`uv`](https://docs.astral.sh/uv/) for local development:

**Supported dev OS: macOS or Linux.** Native Windows is not supported for
development — some test dependencies are POSIX-only (`pexpect`/`pyte` are
excluded on Windows), a few modules import POSIX stdlib or call `os.getuid()`
at import time, and the `pre-commit` hooks assume the Unix `.venv/bin/` layout,
so `pytest` and `pre-commit` cannot pass natively. On Windows, use
**WSL2 (Ubuntu)** and clone into the **Linux** filesystem (`~/…`, not `/mnt/c`);
this matches CI. Git Bash is not sufficient — it runs native-Windows Python.

Install local prerequisites first:

- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) for Python
  environments and dependency management.
- `tmux`, required for native Claude/Codex terminals launched by the local host
  (`brew install tmux` on macOS, or `apt install tmux` on Debian/Ubuntu).
- `bubblewrap` (`bwrap`), **Linux only**, used to OS-sandbox those native
  Claude/Codex/Pi terminals (`apt install bubblewrap` on Debian/Ubuntu). macOS
  uses the built-in `seatbelt` sandbox and needs nothing extra.
- Node.js 22 LTS or newer with `pnpm` (install via `corepack enable` or
  `npm install -g pnpm`) when working on `web/`.
- A Rust toolchain for the recommended `omnidev` local development supervisor.

```bash
git clone https://github.com/omnigent-ai/omnigent.git
cd omnigent

uv python install
uv venv --python "$(cat .python-version)"
uv sync --extra all --extra dev
source .venv/bin/activate    # or prefix commands with `uv run`
```

Common checks:

Pyrefly is the canonical Python type checker for the repository.

```bash
uv run pytest                      # Python tests (e2e/live skipped by default)
uv run ruff check . && uv run ruff format --check .
uv run --no-sync pyrefly check     # Python type checking (omnigent/)
uv run pre-commit run --all-files
```

When touching `web/`:

```bash
cd web && pnpm install && pnpm run lint && pnpm run type-check && pnpm run build
```

When touching `editors/vscode/`:

```bash
cd editors/vscode && pnpm install && pnpm run type-check && pnpm run test && pnpm run build
```

## Running locally

Start with the smallest relevant automated test described in [Tests](#tests).
For full-stack manual testing, use `omnidev`.

### Recommended: worktree-safe testing with `omnidev`

`omnidev` runs the current checkout's server, host, and Vite frontend in one
terminal. Each checkout path, including each worktree, gets isolated state,
configuration, database, artifacts, logs, and automatically allocated ports,
so it can run alongside your normal Omnigent installation and other worktrees.

Install the supervisor once from an up-to-date checkout:

```bash
cargo install --path dev/omnidev --force
```

Then run it from anywhere inside the branch checkout or worktree you want to
test. A fresh worktree needs its own Python environment first:

```bash
cd /path/to/omnigent-worktree
uv sync --extra all --extra dev
omnidev
```

Open the exact `ui` URL displayed in the header; do not assume the Vite port is
`5173`. Python changes under `omnigent/` reload the server and host, while
frontend changes use Vite HMR.

Run CLI commands against the development pod through the passthrough so they
use that checkout and its isolated state instead of a globally installed
`omnigent`:

```bash
omnidev omnigent config show
omnidev omnigent agent list
```

Keep `omnidev` in the foreground and quit with `q` or `Ctrl-C` so it tears down
all three processes. An interactive terminal inside an existing Omnigent
session also works; use `git rev-parse --show-toplevel` to confirm that its
current checkout is the one you intend to test.

See [`dev/omnidev/README.md`](dev/omnidev/README.md) for log controls,
clean-state testing, backend-only and LAN modes, and other options.

### Manual three-terminal fallback

Use the manual flow when you need to run or debug each component separately.
Unlike `omnidev`, it does not isolate state or allocate ports. These commands
assume the default ports are free:

```bash
# Terminal 1: local server on :6767
uv run omnigent server

# Terminal 2: register your machine as a host
uv run omnigent host --server http://localhost:6767

# Terminal 3: frontend dev server
cd web
pnpm run dev
```

Open the Vite URL from the frontend dev server, usually
`http://localhost:5173/`. The host registration is what lets the web UI browse
your filesystem and start new sessions on your machine — without it, the web UI
is read/continue-only.

`omni` is an alias for `omnigent`, so `omni host --server ...` works too.
The host URL can also be passed positionally (`omnigent host
http://localhost:6767`). See the [README](README.md) for more on hosts,
harnesses, and credentials.

### Disposable backend-only validation

Use this when you want to validate the Python backend and local API server from
a source checkout without building the web UI, configuring provider
credentials, creating sessions, or running agents -- a quick server/API smoke
check on your working copy or current `main`.

[`scripts/backend-smoke.sh`](scripts/backend-smoke.sh) automates it:

```bash
scripts/backend-smoke.sh              # boots on port 18080
PORT=18090 scripts/backend-smoke.sh   # override the port if 18080 is busy
```

It installs `uv` into a throwaway toolchain venv, runs `uv sync --frozen`,
starts the server in API-only mode (`OMNIGENT_SKIP_WEB_UI=true`), waits for
`/health`, and smoke-tests `/`, `/health`, `/docs`, `/v1/agents`, and
`/v1/sessions` -- expecting HTTP `200` from all five. It exits non-zero if any
check fails.

Notes:

- **Requires `bash` or `zsh`** (the script's `#!/usr/bin/env bash` shebang
  guarantees this); it is not POSIX-`sh` portable. **Also needs** Python 3.12+
  as `python3`, `git`, `curl`, and network access to PyPI. No provider
  credentials are needed. **Works on Linux and macOS.**
- **Fully isolated, disposable:** every artifact -- the toolchain and project
  venvs, config, data, the SQLite database, artifacts, logs, and `pip`/`uv`
  caches -- lives under one `mktemp -d` runtime directory removed on exit, so
  the run never touches your real `~/.omnigent`, `~/.config` / `~/Library`, or
  package caches. `HOME` is the primary isolation lever (it redirects
  `~/.config` on Linux and `~/Library` on macOS); the explicit `UV_*` / `PIP_*`
  / `OMNIGENT_*` overrides pin the toolchain and app state regardless of OS,
  and `XDG_*` are set so an `XDG_*` already exported in your shell cannot
  redirect state back to your real home.
- **What it does not cover:** the web UI, mobile access, human-in-the-loop
  approval flows, provider-backed sessions, or agent execution. Use the full
  local development flow above when working on those areas.

## Tests

A change that alters behaviour under `omnigent/` should ship with a test, and a
bug fix should add a test that fails before the fix. Pure refactors, renames,
type-only changes, dependency bumps, and edits with no observable behaviour
change don't need a new test.

Prefer the smallest test that covers the change. A fast, focused **unit test**
in the area suite is the default and what most changes need. Reach for
`tests/integration/` only when behaviour genuinely spans components, and for
`tests/e2e/` only for full-stack flows that a unit test can't capture — these
are slower and (for e2e) gateway-bound, so don't use them where a unit test
would do.

Put the test in the suite that matches the area you changed — most backend
areas mirror their source directory under `tests/`:

| Area changed (`omnigent/…`) | Test suite (`tests/…`) |
| --- | --- |
| `server/` | `server/` |
| `runner/` | `runner/` |
| `runtime/` | `runtime/` |
| `tools/` | `tools/` |
| `inner/` | `inner/` |
| `llms/` | `llms/` |
| `db/` | `db/` (a schema migration especially warrants one) |
| `policies/` | `policies/` |
| `repl/` | `repl/` |
| `entities/` | `entities/` |
| `stores/` | `stores/` |
| `host/` | `host/` |
| `spec/` | `spec/` |

Two cross-cutting suites sit on top of these:

- `tests/integration/` — behaviour that spans several components (e.g. server +
  runtime) and isn't captured by any single area's unit test.
- `tests/e2e/` — full-stack flows driven against a live LLM (sessions, the
  runtime, sub-agent dispatch, client-tool tunneling, transports, native
  harness bridges, steering/cancellation). These are slow and gateway-bound, so
  reserve them for genuine end-to-end behaviour — but a PR that adds new
  user-facing functionality **must** include at least one e2e happy-path test
  (see `.github/copilot-instructions.md`).

### Frontend (`web/`)

Frontend changes follow the same expectation with a different toolchain:

- Add or update a **colocated Vitest test** — a `*.test.ts`/`*.test.tsx` file
  next to the component or module you changed — and run it with `pnpm test`.
- A change to **user-facing UI behaviour** also needs a Playwright test under
  `tests/e2e_ui/`. This one is enforced mechanically by the `E2E UI Required`
  check, so a UI PR won't merge without a covering test (or a maintainer
  waiver) — see `.github/workflows/e2e-ui-required.yml`.
- Styling/formatting-only changes, copy tweaks with no flow change, and
  refactors with no behaviour change are exempt, same as the backend.

## Developer Certificate of Origin

To contribute to this repository, you must sign off your commits to certify
that you have the right to contribute the code and that it complies with the
open source license. If you can certify the contents of the [DCO](DCO), add a
`Signed-off-by` line to each commit message:

```
Signed-off-by: Joe Smith <joe.smith@email.com>
```

Please use your real name — pseudonymous/anonymous contributions are not
accepted. If your `user.name` and `user.email` git configs are set, `git
commit -s` adds the sign-off automatically. The DCO check on every pull
request enforces this, so unsigned commits will block merging.

## Pull requests

- Branch from `main`, keep changes focused, and include tests or docs when relevant.
- Sign off your commits with `git commit -s` (see
  [Developer Certificate of Origin](#developer-certificate-of-origin) above).
- Fill in the PR template. For **UI / frontend changes**, check the
  "UI / frontend change" box and attach a **video or images** in the `Demo`
  section showing the new behaviour, so reviewers can see it without checking
  out the branch.
