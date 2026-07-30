# Omnigent load tests

[Locust](https://locust.io) load tests for the Omnigent server. Sibling to
`dev/benchmarks/`: benchmarks measure single-request latency in isolation; these
drive **concurrent load** against a running server.

## Setup

```bash
pip install -e '.[loadtest]'      # or: uv sync --extra loadtest
```

## Run

`run.py` is the entrypoint: give it a **server**, a **host**, and the **load
parameters**, and it runs.

```bash
# Local server, no auth:
python dev/loadtest/run.py \
    --server http://localhost:8000 \
    --users 50 --spawn-rate 5 --run-time 60s

# Remote deployment, scoped to a connected host, with a bearer token:
python dev/loadtest/run.py \
    --server https://my-omnigent.example.com \
    --host-id host_abc123 \
    --auth-token "$TOKEN" \
    --users 50 --spawn-rate 5 --run-time 5m
```

| Input | Flag | Meaning |
|---|---|---|
| Server | `--server` (required) | Omnigent server base URL to load. |
| Host | `--host-id` | Connected Omnigent host the load is scoped to. |
| Load | `--users` / `--spawn-rate` / `--run-time` | Concurrency, ramp, duration. |
| Auth | `--auth-token` | Bearer token (omit for a local single-user server). |
| Mount prefix | `--mount-prefix` | Path the app is served under on a fronted deployment. Empty for a plain server; `/api/2.0/omnigent` for a managed Databricks (MAS) deployment. |
| Scenario | `--locustfile` | Which test to run (default: `ws_load_test.py`). |
| UI | `--web` | Open Locust's web UI instead of a headless run. |

### Against a managed Databricks deployment

The embedded server is mounted under `/api/2.0/omnigent` (the browser UI route
`/omnigent` is SSO-gated and 303-redirects an API client to login — use the API
mount, not that). Point `--server` at the workspace root and set the prefix:

```bash
python dev/loadtest/run.py \
    --server https://<workspace>.cloud.databricks.com \
    --mount-prefix /api/2.0/omnigent \
    --auth-token "$WORKSPACE_PAT" \
    --users 50 --spawn-rate 10 --run-time 60s
```

`run.py` maps these to locust's `--host` / `-u` / `-r` / `-t` and threads the
host id + token through as the env vars the locustfile reads. To drive locust
directly instead, see the invocations under the scenario below.

### Results

Each headless run writes a timestamped directory under `results/` (override with
`--out-dir`):

| File | What it is |
|---|---|
| `summary.md` | Human-readable latency write-up — read this first. |
| `run_config.json` | Inputs + resolved locust argv + exit code (never the token). |
| `report_stats.csv` | Per-endpoint stats (raw). |
| `report_stats_history.csv` | Per-10s time series (raw). |
| `report_failures.csv` | Failure breakdown (raw). |
| `report.html` | Locust's own HTML report. |
| `console.log` | Full locust stdout/stderr. |

`summary.md` reports, per request type, the request count, failures, and the
latency distribution (avg / median / p95 / p99 / max) plus throughput, and
explains how to read them. `--web` (interactive UI) writes no result files.

The `results/` directory is git-ignored.

## `ws_load_test.py` — WebSocket fan-out

Opens **N concurrent WebSocket connections** to the client-facing live-updates
socket `WS /v1/sessions/updates` and holds them open, as N browser tabs would.
Pure server-side: handshake, origin/auth gating, per-connection watch-set
diffing, and the idle heartbeat — **no runner, no LLM, no agent turns**, so it
runs against any server (including a fresh local one) with no setup.

`-u N` is the number of concurrent sockets. Each user opens one connection in
`on_start`, holds it for the whole run, and the task loop drives `watch`
round-trips and drains pushed frames on it.

### Run against a local server (no auth)

```bash
omnigent server --port 8000 &
locust -f dev/loadtest/ws_load_test.py \
    --host http://localhost:8000 \
    --headless -u 50 -r 5 -t 60s
```

### Run against a remote deployment (bearer auth)

```bash
locust -f dev/loadtest/ws_load_test.py \
    --host https://my-omnigent.example.com \
    --headless -u 50 -r 5 -t 5m \
    -e AUTH_TOKEN "$TOKEN"
```

Drop `--headless` to open Locust's web UI and drive users interactively.

Flags: `-u` users (N concurrent sockets), `-r` spawn rate/s, `-t` run time,
`-e KEY VALUE` env.

### Environment variables

| Var | Required | Default | Meaning |
|---|---|---|---|
| `AUTH_TOKEN` | no | — | Bearer token for the handshake. Omit for a local single-user server. |
| `SESSION_IDS` | no | — | Comma-separated session ids to watch. Default: empty watch-set (a valid, dependency-free probe). |
| `WS_READ_TIMEOUT` | no | `35` | Per-recv timeout in seconds (> the 30s server heartbeat). |

### Metrics

Reported under the `WS` request type:

- **connect** — handshake + first snapshot latency (a socket came up).
- **watch roundtrip** — `watch` → `snapshot` round-trip on an open socket.
- **frames drained** — pushed heartbeat/delta frames consumed (count in the
  size column).
