---
name: run-load-test
description: Run an Omnigent load test and produce a results file explaining the latencies. Load when the user wants to load-test / stress-test / benchmark concurrency against an Omnigent server ("load test omnigent", "stress test the server", "how many websocket connections can it handle", "run a load test against staging", "load test real agent turns / conversations", "how many concurrent conversations can the runner handle"). Two scenarios: WebSocket fan-out (dev/loadtest/run.py — sockets, any server) and runner turns (dev/loadtest/turn_load.py — real multi-turn conversations through the runner with a mocked LLM, boots its own local stack). Gathers inputs, runs the right one, then reads the generated summary.md and explains the latency distribution (avg/median/p95/p99, throughput, failures). NOT for single-request latency micro-benchmarks (that is dev/benchmarks/).
---

# Run an Omnigent load test

Drives a load test in `dev/loadtest/` end to end: collect inputs → run → read the
generated `summary.md` → explain the latencies. There are **two scenarios** —
pick by what the user wants to stress:

| If the user wants to test… | Use | Tool |
|---|---|---|
| WebSocket / connection fan-out, or load an existing/remote server | **Scenario A** | `dev/loadtest/run.py` (Locust) |
| Real agent **turns / conversations** through the runner | **Scenario B** | `dev/loadtest/turn_load.py` (asyncio) |

- **Scenario A** opens N concurrent WebSocket connections to `WS
  /v1/sessions/updates` and holds them open — control-plane fan-out only (**no
  runner, LLM, or agent turns**), so it runs against any server, local or remote.
- **Scenario B** drives N concurrent conversations of M sequential turns each
  through the runner with the **LLM mocked** (zero latency), measuring per-turn
  overhead as history grows. It **boots its own local stack** (server + mock LLM
  + runner) and runs from a repo checkout only — there is no server to point at.

If it's ambiguous which they want, ask (AskUserQuestion). For single-request
latency micro-benchmarks (not concurrency), that is a different tool:
`dev/benchmarks/`.

# Scenario A — WebSocket fan-out

## 1. Gather inputs

Ask the user for these (use AskUserQuestion when several are unknown). Only
`--server` is strictly required; everything else has a sensible default.

| Input | Flag | Default | Notes |
|---|---|---|---|
| Server URL | `--server` | — (required) | e.g. `http://localhost:8000` or a deployment URL. |
| Host id | `--host-id` | none | The connected Omnigent host the load is scoped to. Recorded as run context (the WS socket is user-scoped). |
| Users | `--users` | 50 | Concurrent WebSocket connections (N). |
| Spawn rate | `--spawn-rate` | 5 | Users started per second. |
| Run time | `--run-time` | 60s | `30s` / `5m` / `1h`. |
| Auth token | `--auth-token` | none | Bearer token for an authenticated deployment; omit for a local single-user server. **Never** print this or write it to a file. |
| Mount prefix | `--mount-prefix` | none | Path the app is served under when it sits behind a reverse proxy at a sub-path (e.g. `/omnigent`). Empty for a plain server. |

Scale guidance: start small on an unfamiliar target — `--users 10 --run-time
30s` — to confirm connectivity and auth before ramping to 50–100+.

### Behind a reverse proxy (path prefix)

If the server is fronted by a reverse proxy that serves it under a sub-path
rather than at the root, point `--server` at the proxy's base URL and pass the
sub-path as `--mount-prefix` (e.g. `--mount-prefix /omnigent`) so the WebSocket
URL resolves. Sanity-check the target + auth first with a plain request, e.g.
`curl -H "Authorization: Bearer $TOK" <server><prefix>/v1/info` — a 200 with a
JSON body means both are good. When sourcing a token from the shell env, pass
it as `--auth-token "$VAR"` and never echo the value.

## 2. Ensure the load-test deps are installed

The `locust` + `websocket-client` deps live in the `loadtest` extra:

```bash
pip install -e '.[loadtest]'      # or: uv sync --extra loadtest
```

If `locust` is missing the runner exits with that instruction.

## 3. (Local only) make sure a server is up

If the user is targeting a **local** server and none is running, start one:

```bash
omnigent server --port 8000 &
# wait for readiness:
until curl -sf http://localhost:8000/health >/dev/null; do sleep 0.5; done
```

For a **remote** deployment, do NOT start a server — just point `--server` at
it and pass `--auth-token`.

## 4. Run

```bash
python dev/loadtest/run.py \
    --server <SERVER_URL> \
    [--host-id <HOST_ID>] \
    [--auth-token <TOKEN>] \
    --users <N> --spawn-rate <R> --run-time <T>
```

The runner prints the result directory it created:
`dev/loadtest/results/ws_load_test-<timestamp>/` (override with `--out-dir`).

## 5. Read and explain the results

The run writes a result set into the out-dir:

| File | What it is |
|---|---|
| `summary.md` | Human-readable latency write-up (**read this first**). |
| `run_config.json` | Inputs + resolved locust argv + exit code (no token). |
| `report_stats.csv` | Per-endpoint stats (raw). |
| `report_stats_history.csv` | Per-10s time series (raw). |
| `report_failures.csv` | Failure breakdown (raw). |
| `report.html` | Locust's own HTML report. |
| `console.log` | Full locust stdout/stderr. |

`Read` `summary.md` and relay it to the user. Explain, don't just paste:

- **Outcome / failures** — lead with whether it passed (exit 0, 0 failures). Any
  non-zero failure count is the headline; open `console.log` / `report_failures.csv`
  for the error and diagnose (auth 401/403, origin 403, connection refused,
  timeouts under load).
- **Latency** — focus on **p95 / p99**, not the average: those are what a loaded
  client actually feels. Note if the median is far below the average (a few slow
  outliers) or if `connect` latency dominates (handshake/TLS/auth cost per new
  socket).
- **Throughput** — `Req/s` per request type at this concurrency.
- **Metrics glossary** (WS request type): `connect` = handshake + first snapshot
  (a socket came up); `watch roundtrip` = `watch`→`snapshot` on an open socket;
  `frames drained` = pushed heartbeat/delta frames consumed.

If failures appeared or tail latency looks high, suggest a concrete next step
(ramp users up/down, lengthen run-time to see steady state, check the server's
own logs/metrics).

# Scenario B — Runner turns (real conversations, mocked LLM)

Use this when the user wants to load-test **actual agent turns / conversations**
through the runner. It boots its own local stack, so it needs no `--server`.

## 1. Ensure deps

Needs the harness + bench deps on top of the load-test extra:

```bash
pip install -e '.[loadtest,dev,agents-sdk]'   # or: uv sync --extra loadtest --extra dev --extra agents-sdk
```

Must run **from a repo checkout** (imports `dev.benchmarks` and `tests`), with
the same interpreter the deps are installed in.

## 2. Gather inputs

| Input | Flag | Default | Notes |
|---|---|---|---|
| Conversations | `--conversations` | 10 | Concurrent conversations (N). |
| Turns | `--turns` | 8 | Sequential turns per conversation (M) — history grows across them. |
| Reply length | `--reply-words` | 80 | Words in the mocked (streamed) reply per turn. |
| Turn timeout | `--turn-timeout` | 180 | Per-turn timeout (s). |

Scale guidance: start at `--conversations 2 --turns 3` to confirm the stack
boots on this machine (first boot spawns server + mock + runner, ~10-30s), then
ramp. Booting can take a bit; don't treat a slow first run as a failure.

## 3. Run

```bash
python dev/loadtest/turn_load.py --conversations 10 --turns 8
```

It prints the result dir (`dev/loadtest/results/turn_load-<timestamp>/`).

## 4. Read and explain

`Read` the `summary.md` and relay it. Focus on:

- **Outcome / failures** first. A boot failure says "FAILED TO BOOT — see
  console.log" (usually a missing dep — check the `agents-sdk` / `dev` extras).
- **turn** latency — the headline: one full post→idle turn through the runner
  with the model mocked, so it is Omnigent's own per-turn overhead. Note that it
  **grows across a conversation** as history accumulates, so a rising p95/p99 as
  `--turns` increases is expected and the interesting signal.
- **Ops/s** — the runner's concurrent turn throughput (one runner serves all N).
- **session create** — create + runner-bind cost, once per conversation.

## Notes

- Scenario A file: `dev/loadtest/ws_load_test.py` (`--locustfile <other>` points
  `run.py` at a different locustfile). Scenario B file: `dev/loadtest/turn_load.py`.
- Full reference: `dev/loadtest/README.md`.
