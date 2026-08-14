---
name: run-load-test
description: Run the Omnigent load test and produce a results file explaining the latencies. Load when the user wants to load-test / stress-test / benchmark Omnigent under concurrency ("load test omnigent", "stress test the server", "how many hosts/sessions/turns can it handle", "load test real agent turns / conversations", "run a load test"). The test makes each simulated user a real omnigent host that creates host-bound sessions and drives real multi-turn conversations with a mocked LLM; it boots its own local stack (dev/loadtest/run.py). Gather inputs, run it, then read the generated summary.md and explain the latency distribution (avg/median/p95/p99, throughput, failures). NOT for single-request latency micro-benchmarks (that is dev/benchmarks/).
---

# Run the Omnigent load test

Drives `dev/loadtest/` end to end: collect inputs → run → read `summary.md` →
explain the latencies. **Each Locust user is a real `omnigent host`** that
registers over the host tunnel, creates host-bound sessions, and drives **real
multi-turn conversations** — every turn is a genuine post→idle loop through the
host's runner, with the **LLM mocked** (zero latency) so the numbers are
Omnigent's own overhead. `-u N` scales the number of hosts.

It **boots its own local stack** (server + mock LLM), so there is no server to
point at, and it runs **from a repo checkout** only. For single-request latency
micro-benchmarks (not concurrency), that is a different tool: `dev/benchmarks/`.

## 1. Ensure deps (repo checkout)

```bash
uv sync --extra loadtest --extra agents-sdk
```

Run with that same interpreter (e.g. `.venv/bin/python`), from the repo root.

## 2. Gather inputs

Ask the user (AskUserQuestion when several are unknown); all have defaults.

| Input | Flag | Default | Notes |
|---|---|---|---|
| Hosts | `--users` | 4 | Concurrent hosts (N) — the main scale knob. |
| Spawn rate | `--spawn-rate` | 1 | Hosts started per second. |
| Run time | `--run-time` | 120s | `40s` / `5m` / `1h`. |
| Sessions/host | `--sessions-per-user` | 2 | Host-bound sessions each host drives. |
| Turns/session | `--turns-per-session` | 4 | Turns per session — history grows across them. |
| Reply length | `--reply-words` | 60 | Words in the mocked (streamed) reply per turn. |

**Capacity caveat — say this to the user if they ask for large N:** turns run on
real host + runner subprocesses, so N hosts × M sessions = N×M runner processes
on *this* box. It is capacity-limited by design (real turns, not faked). Start at
`--users 2 --sessions-per-user 1 --turns-per-session 2 --run-time 40s` to confirm
the stack boots (~10-30s), then ramp to a few dozen hosts at most. At high N the
load box saturates before the server (Locust warns about CPU).

## 3. Run

```bash
python dev/loadtest/run.py \
    --users <N> --spawn-rate <R> --run-time <T> \
    --sessions-per-user <S> --turns-per-session <TU>
```

It boots the stack, prints the server URL + registered agent, runs Locust, and
writes `dev/loadtest/results/omnigent_load_test-<timestamp>/`.

## 4. Read and explain

`Read` the `summary.md` and relay it. Focus on:

- **Outcome / failures** first. Exit 0 + 0 failures = PASS. Non-zero failures are
  the headline — check `console.log` and, for a host that failed to register,
  the per-host `results/.../host-workspaces/<name>/host.log`. At high N, failures
  usually mean the *load box* saturated, not the server.
- **turn** — the headline latency: one full post→idle agent turn on a host's
  runner (mocked LLM), so it is Omnigent's per-turn overhead. It **grows across a
  conversation** as history accumulates, so a rising p95/p99 with larger
  `--turns-per-session` is expected and is the interesting signal.
- **host online** — host tunnel registration cost; **session create** — the
  host-bound create; **Ops/s** — aggregate throughput at this concurrency.

If failures appeared or the tail looks high, suggest a concrete next step (lower
N if the load box is saturated, raise `--turns-per-session` to study history
growth, lengthen `--run-time` for steady state, or check server logs/metrics).

## Notes

- Scenario file: `dev/loadtest/omnigent_load_test.py`; driver + report:
  `dev/loadtest/run.py`. Full reference: `dev/loadtest/README.md`.
