# Contributing to Omnigent

Thanks for your interest in improving Omnigent. Issues and pull requests are
welcome. For larger changes, open an issue first so we can discuss the approach.

Please don't include secrets, internal URLs, customer data, or private
configuration in issues, tests, examples, or logs.

## Development setup

This is a Python package with an optional frontend under `ap-web/`. Use
[`uv`](https://docs.astral.sh/uv/) for local development:

Install local prerequisites first:

- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) for Python
  environments and dependency management.
- `tmux`, required for native Claude/Codex terminals launched by the local host
  (`brew install tmux` on macOS, or `apt install tmux` on Debian/Ubuntu).
- `bubblewrap` (`bwrap`), **Linux only**, used to OS-sandbox those native
  Claude/Codex/Pi terminals (`apt install bubblewrap` on Debian/Ubuntu). macOS
  uses the built-in `seatbelt` sandbox and needs nothing extra.
- Node.js 22 LTS or newer with `npm` when working on `ap-web/`.

```bash
git clone https://github.com/omnigent-ai/omnigent.git
cd omnigent

uv python install
uv venv --python "$(cat .python-version)"
uv sync --extra all --extra dev
source .venv/bin/activate    # or prefix commands with `uv run`
```

Common checks:

```bash
uv run pytest                      # Python tests (e2e/live skipped by default)
uv run ruff check . && uv run ruff format --check .
uv run pre-commit run --all-files
```

When touching `ap-web/`:

```bash
cd ap-web && npm install && npm run lint && npm run build
```

## Running locally

To try your changes, start a local server, register your machine as a host,
and run the frontend dev server. Use three separate terminals:

```bash
# Terminal 1: local server on :6767
omnigent server

# Terminal 2: register your machine as a host
omnigent host --server http://localhost:6767

# Terminal 3: frontend dev server
cd ap-web
npm run dev
```

Open the Vite URL from the frontend dev server, usually
`http://localhost:5173/`. The host registration is what lets the web UI browse
your filesystem and start new sessions on your machine — without it, the web UI
is read/continue-only.

`omni` is an alias for `omnigent`, so `omni host --server ...` works too.
The host URL can also be passed positionally (`omnigent host
http://localhost:6767`). See the [README](README.md) for more on hosts,
harnesses, and credentials.

### Backend-only local development validation

Use this recipe when you want to validate the Python backend and local API
server from a source checkout without building `ap-web`, configuring provider
credentials, creating sessions, or running agents. It is useful for quick
server/API smoke checks on current `main`.

Prerequisites:

- Linux or another POSIX-like shell environment.
- Python 3.12+ available as `python3`.
- `git`.
- Network access to the Python package index for dependency resolution.
- No provider credentials are required for this backend-only smoke test.

The example below keeps the toolchain, project environment, database,
artifacts, logs, and caches under one disposable runtime directory.

```bash
runtime_root="$(mktemp -d /tmp/omnigent_backend_XXXXXX)"
runtime_checkout="$runtime_root/omnigent"
toolchain_venv="$runtime_root/toolchain_venv"
project_venv="$runtime_root/project_venv"
runtime_home="$runtime_root/home"
runtime_tmp="$runtime_root/tmp"
runtime_config="$runtime_root/config"
runtime_data="$runtime_root/data"
runtime_cache="$runtime_root/cache"
runtime_pip_cache="$runtime_cache/pip"
runtime_uv_cache="$runtime_cache/uv"
runtime_logs="$runtime_root/logs"
runtime_artifacts="$runtime_root/artifacts"
runtime_db="$runtime_data/omnigent/chat.db"

mkdir -p \
  "$runtime_home" \
  "$runtime_tmp" \
  "$runtime_config/xdg" \
  "$runtime_data/xdg" \
  "$runtime_cache/xdg" \
  "$runtime_pip_cache" \
  "$runtime_uv_cache" \
  "$runtime_logs" \
  "$runtime_artifacts" \
  "$(dirname "$runtime_db")" \
  "$runtime_config/omnigent" \
  "$runtime_data/omnigent"

git clone https://github.com/omnigent-ai/omnigent.git "$runtime_checkout"

python3 -m venv "$toolchain_venv"
python3 -m venv "$project_venv"

HOME="$runtime_home" \
TMPDIR="$runtime_tmp" \
PIP_CACHE_DIR="$runtime_pip_cache" \
"$toolchain_venv/bin/python" -m pip install "uv>=0.11.8"

cd "$runtime_checkout"

HOME="$runtime_home" \
TMPDIR="$runtime_tmp" \
XDG_CONFIG_HOME="$runtime_config/xdg" \
XDG_DATA_HOME="$runtime_data/xdg" \
XDG_CACHE_HOME="$runtime_cache/xdg" \
PIP_CACHE_DIR="$runtime_pip_cache" \
UV_CACHE_DIR="$runtime_uv_cache" \
UV_PROJECT_ENVIRONMENT="$project_venv" \
UV_PYTHON_DOWNLOAD=never \
OMNIGENT_CONFIG_HOME="$runtime_config/omnigent" \
OMNIGENT_DATA_DIR="$runtime_data/omnigent" \
OMNIGENT_DATABASE_URI="sqlite:///$runtime_db" \
OMNIGENT_SKIP_WEB_UI=true \
"$toolchain_venv/bin/uv" sync --frozen
```

`UV_PROJECT_ENVIRONMENT` tells `uv` to use the already-created project
virtualenv. `UV_PYTHON_DOWNLOAD=never` keeps `uv` from downloading Python
interpreters. `OMNIGENT_SKIP_WEB_UI=true` skips the web UI build and leaves the
server in API-only mode.

Start the backend server with an explicit detached lifecycle. If port 18080 is
already in use, choose another free local port and reuse it in the smoke
checks.

```bash
selected_port=18080

cd "$runtime_checkout"

nohup env \
  HOME="$runtime_home" \
  TMPDIR="$runtime_tmp" \
  XDG_CONFIG_HOME="$runtime_config/xdg" \
  XDG_DATA_HOME="$runtime_data/xdg" \
  XDG_CACHE_HOME="$runtime_cache/xdg" \
  PIP_CACHE_DIR="$runtime_pip_cache" \
  UV_CACHE_DIR="$runtime_uv_cache" \
  UV_PROJECT_ENVIRONMENT="$project_venv" \
  UV_PYTHON_DOWNLOAD=never \
  OMNIGENT_CONFIG_HOME="$runtime_config/omnigent" \
  OMNIGENT_DATA_DIR="$runtime_data/omnigent" \
  OMNIGENT_DATABASE_URI="sqlite:///$runtime_db" \
  OMNIGENT_SKIP_WEB_UI=true \
  "$project_venv/bin/omnigent" server \
    --host 127.0.0.1 \
    --port "$selected_port" \
    --database-uri "sqlite:///$runtime_db" \
    --artifact-location "$runtime_artifacts" \
  < /dev/null > "$runtime_logs/omnigent_server.log" 2>&1 &

server_pid="$!"
echo "$server_pid"
```

The important lifecycle details are `nohup`, stdin redirected from
`/dev/null`, stdout/stderr redirected to a log file, and exact PID capture.

Wait for the health endpoint before running smoke checks:

```bash
for i in {1..60}; do
  code="$(curl -s -o /dev/null -w "%{http_code}" \
    --max-time 2 "http://127.0.0.1:${selected_port}/health" || true)"
  [ "$code" = "200" ] && break
  sleep 0.5
done
```

Smoke-test local API-only endpoints:

```bash
for path in / /health /docs /v1/agents /v1/sessions; do
  printf "%s " "$path"
  curl -s -o /dev/null -w "%{http_code}\n" \
    --max-time 5 "http://127.0.0.1:${selected_port}${path}"
done
```

Expected result: HTTP `200` for all five paths.

Clean up only the process started by this recipe:

```bash
kill -TERM "$server_pid" 2>/dev/null || true
wait "$server_pid" 2>/dev/null || true
```

This backend-only path does not validate the web UI, mobile access,
human-in-the-loop approval flows, provider-backed sessions, or agent execution.
Use the full local development flow above when working on those areas.

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

### Frontend (`ap-web/`)

Frontend changes follow the same expectation with a different toolchain:

- Add or update a **colocated Vitest test** — a `*.test.ts`/`*.test.tsx` file
  next to the component or module you changed — and run it with `npm test`.
- A change to **user-facing UI behaviour** also needs a Playwright test under
  `tests/e2e_ui/`. This one is enforced mechanically by the `E2E UI Required`
  check, so a UI PR won't merge without a covering test (or a maintainer
  waiver) — see `.github/workflows/e2e-ui-required.yml`.
- Styling/formatting-only changes, copy tweaks with no flow change, and
  refactors with no behaviour change are exempt, same as the backend.

## Pull requests

- Branch from `main`, keep changes focused, and include tests or docs when relevant.
- Sign off your commits with `git commit -s` (Developer Certificate of Origin).
