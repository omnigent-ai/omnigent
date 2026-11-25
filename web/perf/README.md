# Web performance harness

A local, mocked-backend benchmark for the built web app. It does not start a
real server or use local application data. Generated fixtures, builds and results
are ignored by git. Use Node 22 or later, pnpm, and a local Chrome or Chromium.
Chrome is detected automatically; pass `--chrome` or set `CHROME` to select it.

## Build and run

From the repository root:

```sh
pnpm install --frozen-lockfile --filter web
node web/perf/fixture-gen.mjs
(cd web && pnpm exec vite build --config perf/vite.perf.config.ts)
node web/perf/server.mjs --port 5299
```

In another terminal, run one scenario per invocation so workspace and history
state from a previous scenario cannot leak into the next one:

```sh
node web/perf/bench.mjs --scenarios switch --cycles 10 --out web/perf/out/switch
node web/perf/summarize.mjs --files web/perf/out/switch/results-v2.json --out web/perf/out/switch/summary.json
node --test web/perf/*.test.mjs
```

Stop the mock server with Ctrl-C. `--out` selects an output directory for the
server and benchmark, or an output file for the summarizer. Default output is
under `web/perf/out/`. The benchmark launches its own headless browser on CDP
port 9336; select a different port with `--cdp-port` if needed. The server binds
only to loopback. Run measurements serially on an otherwise quiet machine.

Scenarios are `switch` (retained-live rotation and reload-then-switch),
`switchEvicting` (five-session rotation, HTTP/1.1 cap = three live),
`switchFiles`, `switchChanges`, `switchCollapsedFile` (session D's large file
open in the collapsed rail), `cold`, `idle`, `stream`, and `deepHistory`.
Use `--throttle 4` for fourfold CPU throttling. Warm switches rotate over three
sessions; `--cycles 10` yields 30 samples. The mock uses HTTP/1.1, for which
the app retains at most three live sessions. Each measured warm switch checks
that the target stream remains open and was not rebound. Idle samples last 10
seconds, and streaming injects 20 events per second for 10 seconds.
Deep-history results are only recorded when the oldest marker is reached.

To add mock request latency, start the server with `--latency-ms 50-150` or a
fixed value such as `--latency-ms 100`. REST and Connect requests are delayed;
static assets, streaming connections and harness control requests are not.

## Interleaved comparisons

```sh
node web/perf/ab.mjs --a origin/main --b ../candidate-worktree --samples 30 --out web/perf/out/compare
node web/perf/ab.mjs --a origin/main --b ../candidate-worktree --samples 30 --throttle 4 --out web/perf/out/compare-4x
```

Each arm may be a worktree path or a git ref. Refs are checked out in temporary
worktrees and have locked web dependencies installed; existing worktrees must
already have their dependencies installed. Each arm is built with that tree's
own Vite config, overriding only the output directory. To use existing builds,
pass `--a-dist` and `--b-dist` instead. The driver starts two mock servers and
two browser processes with separate profiles, then measures matched switches
in ABBA order, alternating which arm goes first. Process isolation avoids shared
renderer heaps and browser caches. It writes raw samples and `ab-summary.json`,
then stops the processes it started. A pair is discarded and retried if either
arm loses its retained stream; the discarded count is reported. Use distinct
output directories for separate runs. `--port-a`, `--port-b`, `--cdp-port`, and `--cdp-port-b` select ports;
the defaults are 5299, 5300, 9336 and 9337. `--latency-ms` applies to both
servers.

The summary reports nearest-rank p50/p75/p90, B-minus-A deltas and paired
bootstrap 95% intervals, plus commit, long-task, layout, style and heap
guardrails. It also reports timing and heap deltas separately by arm order.
First run A/A (the same build in both arms) to estimate the noise floor. An
interval excluding zero is necessary, not sufficient, evidence of a win;
counterbalancing does not eliminate every source of bias.

## Interpretation and profiling

`clickToPaintedMs` approximates click to target content painted using a
mutation-driven marker check followed by an animation frame and a macrotask.
The marker poll is at most 40 ms apart, so this is not a pixel-level paint
timestamp. `clickToWorkDoneMs` is the later of that paint estimate and the last
observed long-task or long-animation-frame end. `clickToSettledMs` includes a
250 ms quiet window and is not a headline latency metric.

React commits are counted from click to paint and separately from click to
click + 1.5 seconds; pointer-down-to-click commits are recorded separately.
Placeholder visibility is estimated from DOM mutations and a frame callback,
not from screenshots. A placeholder can mount without ever painting; when
mount and removal share a mutation batch, its mount duration is unavailable.

For component attribution and traces, make a separate unminified build:

```sh
(cd web && pnpm exec vite build --config perf/vite.perf.config.ts --mode nomin)
node web/perf/server.mjs --port 5299 --dist dist-nomin
# In another terminal:
node web/perf/bench.mjs --scenarios switch --cycles 10 --attribution --out web/perf/out/attribution
node web/perf/profile.mjs --out web/perf/out/traces
```

The profiler captures a CPU profile and timeline phases. Attribution compares
fiber identities with the previous root and records commit and request stacks.
It is intrusive: use it for component and commit counts, not headline timings.
The summarizer ranks components and counts uniquely identified root commits per
phase. Phase labels can overlap; stacks may not preserve asynchronous causes,
and a component present in a commit does not prove what caused the commit.
