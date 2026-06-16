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

## Tests

A change that alters behaviour under `omnigent/` should ship with a test, and a
bug fix should add a test that fails before the fix. Pure refactors, renames,
type-only changes, dependency bumps, and edits with no observable behaviour
change don't need a new test.

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
