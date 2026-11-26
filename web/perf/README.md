# Web performance harness

This local benchmark serves a built web app with synthetic data. It never starts
an application backend or reads a local database. Fixtures, builds and results
are ignored by git. Use Node 22 or later, pnpm and Chrome or Chromium.

## Build and run

From the repository root:

```sh
pnpm install --frozen-lockfile --filter web
node web/perf/fixture-gen.mjs
(cd web && pnpm exec vite build --config perf/vite.perf.config.ts)
node web/perf/server.mjs --port 5299
```

In another terminal, run one scenario at a time:

```sh
node web/perf/bench.mjs --scenarios switch --cycles 10 --chrome /path/to/chrome --out web/perf/out/switch
node web/perf/summarize.mjs --files web/perf/out/switch/results-v2.json --out web/perf/out/switch/summary.json
node --test web/perf/*.test.mjs
```

Stop the mock server with Ctrl-C. The benchmark owns its headless browser and
profile. Use `--cdp-port` to choose a free debugging port; the default is 9336.
The mock binds to loopback only. Run serially on an otherwise quiet machine.

Before timing, a separate browser expands the hydrated transcript and compares
rendered tool cards, reasoning and code blocks against fixture counts. It also
checks expanded Files rows, Markdown Preview headings, lists, tables and fenced
blocks, the Monaco Edit line count, and Changes rows. A mismatch aborts the run;
`fixture-check-a.json` records expected and rendered counts for each arm.

Scenarios:

- `switch` rotates retained-live sessions A/B/D; `--cycles 10` gives 30 samples.
- `switchEvicting` rotates five sessions, deliberately exceeding the mock's
  HTTP/1.1 limit of three retained streams. It records placeholder visibility.
- `switchFiles` and `switchChanges` keep those workspace tabs open.
- `switchCollapsedFile` keeps D's large Markdown file in its default Preview
  while the rail is collapsed. `switchCollapsedFileEdit` measures Monaco Edit.
- `cold`, `idle`, and `stream` measure cold loads, 10-second idle windows and
  10-second streams. `deepHistory` pages in the full history and measures both
  idle and streaming with it loaded. Use `--cycles 15` for 30 page-ins.

Use `--throttle 4` for fourfold CPU throttling. Every retained-live switch checks
that the target stream remained open without rebinding. A full-history sample
fails if the oldest or latest marker cannot be reached. Pass
`--watchdog-minutes` for longer runs. The server accepts `--latency-ms 50-150`
or a fixed nonnegative delay; it delays API responses, not assets, SSE or
harness-control requests.

## Interleaved comparisons

```sh
node web/perf/ab.mjs --a origin/main --b ../candidate-worktree --samples 30 --out web/perf/out/compare
node web/perf/ab.mjs --a origin/main --b ../candidate-worktree --samples 30 --throttle 4 --out web/perf/out/compare-4x
```

Each arm may be a git ref or a worktree with dependencies installed. Refs use
temporary worktrees with locked dependencies. Each arm is built using its own
Vite config; `--a-dist` and `--b-dist` use existing builds instead. Two mock
servers and two isolated browsers measure matched switches in ABBA order. Each
arm passes preflight. A pair is discarded and retried if a retained stream is
lost. Results include raw samples and `ab-summary.json`; processes and temporary
worktrees are cleaned up on completion or interruption. Use separate output
folders and distinct `--port-a`, `--port-b`, `--cdp-port` and `--cdp-port-b`
when running alongside other processes. `--latency-ms` applies to both servers.
Use `--features-a` and `--features-b` for comma-separated mock server feature
names, or `server.mjs --features` for a single server.

The summary uses nearest-rank p50/p75/p90 and paired bootstrap 95% intervals
for B-minus-A deltas, with commit, long-task, layout, style and heap guardrails.
Run A/A first to estimate the noise floor. An interval excluding zero is
necessary, not sufficient, evidence of a win.

## Interpreting timings

`clickToPaintedMs` approximates click to content painted: a mutation-driven
marker check runs up to 40 ms apart, then records an animation frame and a
macrotask. It is not a pixel-level paint timestamp. `clickToWorkDoneMs` is the
later of that estimate and the last observed long-task or long-animation-frame
end. `clickToSettledMs` includes a 250 ms quiet window. Cold-load times use
in-page performance timestamps, not CDP counters across navigation.

Commit counts cover click to paint and click to click + 1.5 seconds separately;
pointer-down-to-click commits are separate. Placeholder visibility is estimated
from DOM mutations and a frame callback, not screenshots. A placeholder can mount
without painting; if mount and removal share a mutation batch its mount duration
is unavailable.
