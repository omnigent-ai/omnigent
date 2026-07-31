# Omnigent load test

A load test where **each simulated user is a real Omnigent host**. Sibling to
`dev/benchmarks/`: benchmarks measure single-request latency in isolation; this
drives **concurrent, end-to-end load** — hosts → sessions → real multi-turn
conversations.

One unit of load = one **host**. Each Locust user spawns a real `omnigent host`
subprocess (unique identity), registers it over the host tunnel, then
repeatedly creates a **host-bound session** and drives a **real multi-turn
conversation** on it. Every turn is a genuine
`POST .../events` → server → the user's host → a runner subprocess it spawns →
LLM → stream → `idle` loop. The **LLM is mocked** (zero latency), so the numbers
isolate Omnigent's own dispatch / streaming / history-handling overhead rather
than provider latency. `-u N` scales the number of hosts.

## Capacity note (read this)

Turns really execute on the host, so each host spawns a **real runner
subprocess per session**, and each Locust user runs a **real host subprocess**.
`-u N` hosts × `--sessions-per-user M` therefore puts **N×M runner processes on
this (load-generating) machine**. This is deliberate — it drives genuine
end-to-end turns instead of faking the runner — but it is **capacity-limited**:
keep N modest (a few dozen). At high N the *load box*, not the server, saturates
first (Locust will warn about CPU). To stress the server harder, run this from a
beefier box or point the driver at a bigger server (see "Against another
server").

## Setup

Runs from a **repo checkout** (it imports `dev.benchmarks` + `tests`), with the
harness + bench deps:

```bash
pip install -e '.[loadtest,dev,agents-sdk]'
# or: uv sync --extra loadtest --extra dev --extra agents-sdk
```

## Run

`run.py` boots the whole stack (server + zero-latency mock LLM), registers one
agent, then runs Locust against it. There is **no `--server` to pass** — mocking
the LLM requires a stack the test controls.

```bash
python dev/loadtest/run.py --users 8 --sessions-per-user 2 --turns-per-session 4
```

| Flag | Default | Meaning |
|---|---|---|
| `--users` | 4 | Concurrent hosts (N) — the main scale knob. |
| `--spawn-rate` | 1 | Hosts started per second. |
| `--run-time` | 120s | Run duration (`120s` / `5m` / `1h`). |
| `--sessions-per-user` | 2 | Host-bound sessions each host drives (sequentially). |
| `--turns-per-session` | 4 | Turns per session — history grows across them. |
| `--reply-words` | 60 | Word count of the mocked (streamed) reply per turn. |
| `--out-dir` | — | Result directory (default `results/omnigent_load_test-<timestamp>/`). |

Start small to confirm the stack boots on your machine — `--users 2
--sessions-per-user 1 --turns-per-session 2 --run-time 40s` — then ramp.

## Results

Writes a timestamped `results/` directory (git-ignored):

| File | What it is |
|---|---|
| `summary.md` | Human-readable latency write-up — read this first. |
| `run_config.json` | Inputs + resolved locust argv + exit code. |
| `report_stats.csv` | Per-operation stats (raw). |
| `report_stats_history.csv` | Per-10s time series (raw). |
| `report_failures.csv` | Failure breakdown (raw). |
| `report.html` | Locust's own HTML report. |
| `console.log` | Full locust stdout/stderr. |

`summary.md` reports, per operation, count / failures / latency
(avg / median / p95 / p99 / max) + throughput. Key operations:

- **turn** — the headline: one full post→idle agent turn on a host's runner
  (mocked LLM). Latency **grows across a conversation** as history accumulates,
  so a rising p95/p99 with larger `--turns-per-session` is expected.
- **host online** — cost of a host subprocess registering over the host tunnel.
- **session create** — the host-bound `POST /v1/sessions`.
- **Ops/s** — aggregate throughput at this concurrency.

Each host's own log (useful when a host fails to register) is at
`results/.../host-workspaces/<host-name>/host.log`.

## Against another server

There is intentionally no `--server` flag: the mocked LLM only works on a stack
the test owns, and the whole point is real turns without token cost. To load a
different server you would need real runners + a real (or separately-mocked) LLM
on that side — out of scope here; use this to profile the host↔server↔runner
path locally.
