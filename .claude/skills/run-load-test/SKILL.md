---
name: run-load-test
description: Run a Locust load test against an Omnigent server and produce a results file explaining the latencies. Load when the user wants to load-test / stress-test / benchmark concurrency against an Omnigent server or deployment ("load test omnigent", "stress test the server", "how many websocket connections can it handle", "run a load test against staging"). Gathers the inputs (server URL, host id, users / spawn-rate / run-time, auth token), runs dev/loadtest/run.py, then reads the generated summary.md and explains the latency distribution (avg/median/p95/p99, throughput, failures) back to the user. NOT for single-request latency micro-benchmarks (that is dev/benchmarks/).
---

# Run an Omnigent load test

Drives the load test in `dev/loadtest/` (Locust) end to end: collect inputs →
run → read the generated result set → explain the latencies. The scenario opens
**N concurrent WebSocket connections** to the client-facing live-updates socket
`WS /v1/sessions/updates` and holds them open, so it measures the server's
WebSocket fan-out under concurrency (handshake, auth/origin gating, watch-set
diffing, heartbeat). It needs **no runner, LLM, or agent turns**, so it runs
against any server — a fresh local one or a real deployment.

For single-request latency micro-benchmarks (not concurrency), that is a
different tool: `dev/benchmarks/`.

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

## Notes

- The scenario file is `dev/loadtest/ws_load_test.py`; run `--locustfile <other>`
  to point the runner at a different scenario if one is added later.
- Full reference: `dev/loadtest/README.md`.
