# omnidev

Dev tooling for Omnigent, in one binary with two independent capabilities:

1. A per-repo dev **pod supervisor** (bare `omnidev`) — the default.
2. **Install management** (`omnidev install`/`update`/`check`) — install and
   keep a git-based omnigent up to date. See
   [Managing your omnigent install](#managing-your-omnigent-install). These
   subcommands need no checkout and run anywhere.

## Pod supervisor

A per-repo dev **pod** supervisor, as a single long-running terminal UI. It
replaces the three-terminal local dev flow (`omnigent server`, `omnigent host`,
`npm run dev`) with one process that:

- runs each checkout in an **isolated pod** — its own state dir, database,
  artifacts, logs, and auto-allocated ports — so multiple worktrees never
  collide;
- **supervises** the backend server, the host daemon, and the Vite frontend,
  restarting any that crash (with backoff);
- **reloads the backend** (server → host) when you edit `omnigent/**/*.py`;
  gitignored files under `omnigent/` (e.g. the build-time `_build_info.py`) are
  skipped so generated churn doesn't reload; the frontend self-reloads through
  Vite HMR;
- gives you **per-process log panes** plus a combined view, each a `less`-style
  pager with wrap and search (see [Keys](#keys)).

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
| server | `uv run omnigent --log-to-stderr server --host 127.0.0.1 --port <p> --database-uri … --artifact-location …` | Waited on via `GET /health`. |
| host   | `uv run omnigent --log-to-stderr host --server http://127.0.0.1:<p>` | Started once the server is healthy. |
| vite   | `npm run dev -- --host <host> --port <p> --strictPort` (cwd `web/`) | `OMNIGENT_URL` points its proxy at the pod's server. |

Before Vite starts (and on a manual Vite restart), omnidev runs `npm install`
in `web/` when needed — `node_modules/` is missing, or `package.json` /
`package-lock.json` is newer than it — so a fresh checkout or a new dependency
doesn't make Vite fail its dependency scan. Output streams into the `vite` pane.

Open the UI at the `ui` URL shown in the header (the Vite dev server).

## Isolation

Only Omnigent's own state is isolated per pod — enough that concurrent pods
never share a database, server pidfile, or `config.yaml` — via
`OMNIGENT_DATA_DIR`, `OMNIGENT_DATABASE_URI`, `OMNIGENT_URL`, and
`OMNIGENT_CONFIG_HOME`. Everything else (your real `HOME`, credentials, and
uv/npm caches) is inherited, because the agents Omnigent runs need it. This is
deliberately lighter than the hermetic `scripts/backend-smoke.sh` sandbox,
which repoints `HOME`/`XDG_*` to touch nothing real.

Each pod gets its own `config.yaml` under `<pod>/config/`, pointed to by
`OMNIGENT_CONFIG_HOME`. On first create it's **seeded** from your real
`~/.omnigent/config.yaml` (if present) so the pod works out of the box — it
keeps your providers — after which the two are independent: server-config edits
inside a pod (via the UI or `omnigent config`) don't touch your real config.
`--clean` wipes the pod dir, so the next run re-seeds from your real config.

The pod dir defaults to
`${XDG_CACHE_HOME:-~/.cache}/omnidev/<repo-name>-<hash>/`, keyed to the
canonical checkout path. Per-process logs are written through to
`<pod>/logs/{server,host,vite}.log` for inspection outside the TUI.

## Options

```
--server-port <N>   Force the backend port (default: probe from 6767)
--vite-port <N>     Force the Vite port (default: probe from 5173)
--vite-host <ADDR>  Vite bind host (default: 127.0.0.1; use 0.0.0.0 for LAN access)
--trust-lan-origins Trust this machine's LAN origins (for device testing)
--pod-dir <PATH>    Use a specific pod dir instead of the per-repo default
--no-vite           Backend + host only (no frontend)
--clean             Wipe the pod dir before starting
--debug             Log each watched file change and whether it reloads
```

`--vite-host 0.0.0.0` exposes the Vite dev server on all interfaces for device
testing. Vite still proxies API traffic to the pod backend through `127.0.0.1`.

### Testing from a phone or tablet

`--vite-host 0.0.0.0` alone lets a device load the UI, but the backend runs in
single-user local mode, where its CSRF/CSWSH guard trusts only loopback
origins. A device loads the UI at `http://<your-lan-ip>:<vite-port>`, so its
browser stamps that non-loopback origin on every request — and the guard then
rejects multipart uploads (403) and refuses the live WebSocket stream.

`--trust-lan-origins` fixes that: omnidev enumerates this machine's LAN IPv4
addresses and trusts the matching `http://<ip>:<vite-port>` origins via the
server's `OMNIGENT_WS_ALLOWED_ORIGINS` allowlist (merged with any value you
already export). It stays exact-match — only those origins are trusted, nothing
is disabled — so it's for dev pods, not deployed servers. The trusted origins
are printed in the combined log at startup.

```bash
omnidev --vite-host 0.0.0.0 --trust-lan-origins
```

This covers IPv4 LAN addresses; mDNS `.local` hostnames and HTTPS origins are
not auto-trusted (add those to `OMNIGENT_WS_ALLOWED_ORIGINS` yourself).

## Keys

The log pane is a `less`-style pager, so the movement and search keys should
feel familiar.

| Key | Action |
|---|---|
| `1` / `2` / `3` / `0` | Focus server / host / vite / combined pane |
| `Tab` | Cycle panes |
| `j` / `k` (or `↓` / `↑`) | Scroll one line |
| `f` / `Space` / `PgDn` (or `b` / `PgUp`) | Page forward / back one window |
| `d` / `u` | Half-page forward / back |
| `g` / `G` | Jump to top / bottom (bottom re-follows the tail) |
| `F` | Toggle follow-tail (like `less +F`) |
| `w` | Toggle line wrap (on by default) |
| `/` `?` | Search forward / back — type, `Enter` to jump, `Esc` to cancel |
| `n` / `N` | Next / previous match |
| `r` | Restart the focused process (server/host restart as a pair) |
| `R` | Restart the backend (server then host) |
| `c` | Clear the focused pane |
| `q` / `Ctrl-C` | Quit and tear down all processes |

## Managing your omnigent install

For people who *run* omnigent (installed from git via `uv tool install`) rather
than develop it. This wraps the fiddly PEP 508 install syntax and adds a daily
update check — filling a gap, since omnigent's own update notice only works for
PyPI-wheel installs and skips git installs.

These subcommands manage the global tool and work from **any directory** (no
checkout needed).

```
omnidev install     # uv tool install omnigent from git (databricks extra, main)
omnidev update      # reinstall the latest of the tracked ref/extras
omnidev check       # check for an update; prompt to update on a TTY
omnidev refresh     # refresh the check cache from the network (usually detached)
omnidev shell-hook  # print the daily-check snippet for your shell rc
```

`install` options: `--ref <branch/tag/sha>` (default `main`), `--extra <name>`
(repeatable; defaults to `databricks`), `--no-default-extra` (install with no
extras), `--repo <url>`. The choice is saved to
`${XDG_CONFIG_HOME:-~/.config}/omnidev/install.toml` so `update` reuses it.

Installing from git **builds the web UI from source**, so Node 22+/npm must be
on PATH (the PyPI wheel ships the UI prebuilt; the git install does not).
`omnidev install` fails early with a clear message if `uv` or `npm` is missing.

### Daily update check

Append the hook to your shell rc once to be told, at most once a day, when a
newer `main` commit is available — and be offered to update on the spot:

```bash
omnidev shell-hook >> ~/.zshrc     # or ~/.bashrc
```

The snippet itself guards on `command -v omnidev`, so it's a no-op in shells
where omnidev isn't on PATH — nothing to fail. (Appending the snippet is
preferred over `eval "$(omnidev shell-hook)"`: the latter would run omnidev on
every shell startup and print a "command not found" error whenever omnidev is
absent.)

On each interactive shell it runs `omnidev check --quiet`, which reads a cached
result (`${XDG_CACHE_HOME:-~/.cache}/omnidev/omnigent-check.json`) and, when
stale (>24h), refreshes it in a detached background process — so shell startup
never blocks on the network. When a newer commit is available it prints a notice
and, on a terminal, prompts `Update omnigent now? [y/N]`; on yes it runs
`omnidev update` in the foreground. Declining suppresses that same commit until a
newer one lands. Set `OMNIGENT_NO_UPDATE_CHECK` in your environment if you want
to silence omnigent's own separate notice.
