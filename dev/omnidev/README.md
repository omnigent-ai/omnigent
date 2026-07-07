# omnidev

A per-repo dev **pod** supervisor for the Omnigent repo, as a single
long-running terminal UI. It replaces the three-terminal local dev flow
(`omnigent server`, `omnigent host`, `npm run dev`) with one process that:

- runs each checkout in an **isolated pod** — its own state dir, database,
  artifacts, logs, and auto-allocated ports — so multiple worktrees never
  collide;
- **supervises** the backend server, the host daemon, and the Vite frontend,
  restarting any that crash (with backoff);
- **reloads the backend** (server → host) when you edit `omnigent/**/*.py`;
  the frontend self-reloads through Vite HMR;
- gives you **scrollable per-process log panes** plus a combined view.

## Build & run

Requires the repo's usual dev prerequisites (`uv` for Python, `npm` for the
web UI) plus a Rust toolchain.

```bash
cd dev/omnidev
cargo run            # launches the TUI for the surrounding checkout
```

Run it from anywhere inside the checkout — it walks up to the repo root
(the `.jj`/`.git` marker) and requires `omnigent/` and
`web/` to be present. Build a release binary with `cargo build --release`
(lands at `target/release/omnidev`).

## What it starts

| Process | Command | Notes |
|---|---|---|
| server | `uv run omnigent server --host 127.0.0.1 --port <p> --database-uri … --artifact-location …` | Waited on via `GET /health`. |
| host   | `uv run omnigent host --server http://127.0.0.1:<p>` | Started once the server is healthy. |
| vite   | `npm run dev -- --port <p> --strictPort` (cwd `web/`) | `OMNIGENT_URL` points its proxy at the pod's server. |

Open the UI at the `ui` URL shown in the header (the Vite dev server).

## Isolation

All Omnigent state is redirected into the pod dir via environment variables —
the same pattern `scripts/backend-smoke.sh` uses:
`HOME`, `TMPDIR`, `XDG_*`, `OMNIGENT_CONFIG_HOME`, `OMNIGENT_DATA_DIR`,
`OMNIGENT_DATABASE_URI`, and `OMNIGENT_URL`.

The pod dir defaults to
`${XDG_CACHE_HOME:-~/.cache}/omnidev/<repo-name>-<hash>/`, keyed to the
canonical checkout path. Per-process logs are written through to
`<pod>/logs/{server,host,vite}.log` for inspection outside the TUI.

## Options

```
--server-port <N>   Force the backend port (default: probe from 6767)
--vite-port <N>     Force the Vite port (default: probe from 5173)
--pod-dir <PATH>    Use a specific pod dir instead of the per-repo default
--no-vite           Backend + host only (no frontend)
--clean             Wipe the pod dir before starting
```

## Keys

| Key | Action |
|---|---|
| `1` / `2` / `3` / `0` | Focus server / host / vite / combined pane |
| `Tab` | Cycle panes |
| `↑` `↓` `PgUp` `PgDn` | Scroll (detaches from tail) |
| `f` | Toggle follow-tail |
| `r` | Restart the focused process (server/host restart as a pair) |
| `R` | Restart the backend (server then host) |
| `c` | Clear the focused pane |
| `q` / `Ctrl-C` | Quit and tear down all processes |
