# Omnigent load tests

Load tests for the Omnigent server. Sibling to `dev/benchmarks/`: benchmarks
measure single-request latency in isolation; these drive **concurrent load**.
Two scenarios, each standalone:

| Scenario | File | Driver | Exercises | Target |
|---|---|---|---|---|
| **WebSocket fan-out** | `ws_load_test.py` (via `run.py`) | Locust | Control-plane socket fan-out — no runner/LLM/turns | Any server (local or remote) |
| **Runner turns** | `turn_load.py` | asyncio | Real multi-turn agent conversations through the runner, model mocked | Local stack it boots itself |

## Setup

```bash
# WebSocket fan-out (ws_load_test.py):
pip install -e '.[loadtest]'                 # locust + websocket-client

# Runner turns (turn_load.py): also needs the harness + bench deps
pip install -e '.[loadtest,dev,agents-sdk]'  # + BenchEnvironment + openai-agents
```

## Scenario 1 — WebSocket fan-out (`ws_load_test.py`)

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
| Mount prefix | `--mount-prefix` | Path the app is served under when it sits behind a reverse proxy at a sub-path. Empty for a plain server. |
| Scenario | `--locustfile` | Which test to run (default: `ws_load_test.py`). |
| UI | `--web` | Open Locust's web UI instead of a headless run. |

### Behind a reverse proxy (path prefix)

If the server is fronted by a reverse proxy that serves it under a sub-path
rather than at the root, point `--server` at the proxy's base URL and pass the
sub-path as `--mount-prefix` so the WebSocket URL resolves. Example for a
server mounted under `/omnigent`:

```bash
python dev/loadtest/run.py \
    --server https://proxy.example.com \
    --mount-prefix /omnigent \
    --auth-token "$TOKEN" \
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

### What it does

Opens **N concurrent WebSocket connections** to the client-facing live-updates
socket `WS /v1/sessions/updates` and holds them open, as N browser tabs would.
Pure server-side: handshake, origin/auth gating, per-connection watch-set
diffing, and the idle heartbeat — **no runner, no LLM, no agent turns**, so it
runs against any server (including a fresh local one) with no setup.

`-u N` is the number of concurrent sockets. Each user opens one connection in
`on_start`, holds it for the whole run, and the task loop drives `watch`
round-trips and drains pushed frames on it.

### Drive locust directly (instead of run.py)

#### Run against a local server (no auth)

```bash
omnigent server --port 8000 &
locust -f dev/loadtest/ws_load_test.py \
    --host http://localhost:8000 \
    --headless -u 50 -r 5 -t 60s
```

#### Run against a remote deployment (bearer auth)

```bash
AUTH_TOKEN="$TOKEN" locust -f dev/loadtest/ws_load_test.py \
    --host https://my-omnigent.example.com \
    --headless -u 50 -r 5 -t 5m
```

Drop `--headless` to open Locust's web UI and drive users interactively.

Flags: `-u` users (N concurrent sockets), `-r` spawn rate/s, `-t` run time.
The scenario reads `AUTH_TOKEN` / `SESSION_IDS` / `WS_READ_TIMEOUT` / `HOST_ID` /
`MOUNT_PREFIX` from the environment (set them before `locust`, as above);
`run.py` sets them for you from its flags.

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

## Scenario 2 — Runner turns (`turn_load.py`)

Drives **real agent turns through the runner** — the full `POST .../events` →
server → runner → in-process executor → LLM → stream → `idle` loop — under
concurrency, with the **LLM mocked** (zero latency) so the numbers isolate
Omnigent's own dispatch / streaming / history-handling overhead, not provider
time. Runs **N concurrent conversations, each M sequential turns** on one
durable session, so history grows across the turns (a real long conversation,
not N one-shots).

Unlike scenario 1, this **boots the whole stack itself** — a real `omnigent
server` + a zero-latency mock LLM + a runner (via the benchmark harness's
`BenchEnvironment`) — so there is no `--server` to point at, and it runs **from
a repo checkout only** (it imports `dev.benchmarks` and `tests`). The agent uses
the in-process `openai-agents` harness, so no vendor CLI or real API key is
needed.

```bash
pip install -e '.[loadtest,dev,agents-sdk]'
python dev/loadtest/turn_load.py --conversations 10 --turns 8
```

| Flag | Default | Meaning |
|---|---|---|
| `--conversations` | 10 | Concurrent conversations (N). |
| `--turns` | 8 | Sequential turns per conversation (M) — history grows across them. |
| `--reply-words` | 80 | Word count of the mocked (streamed) assistant reply each turn. |
| `--turn-timeout` | 180 | Per-turn timeout (s) before a turn counts as failed. |
| `--out-dir` | — | Result directory (default `results/turn_load-<timestamp>/`). |

### Results

Writes a timestamped `results/` directory with `summary.md` (a latency table for
`session create` and `turn`, with avg / median / p95 / p99 / max + throughput)
and `run_config.json`. Key signals:

- **turn** — one full post→idle agent turn. The headline number; with the model
  mocked at zero latency it is Omnigent's per-turn overhead.
- Turn latency **grows across a conversation** as history accumulates, so the
  tail (p95/p99/max) reflects the later, longer turns — watch it as `--turns`
  rises.
- **Ops/s** — a single runner services all N conversations, so this is the
  runner's concurrent turn throughput.
