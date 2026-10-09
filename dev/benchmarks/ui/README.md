# OSS UI performance benchmark

Runs the **production SPA in real Chromium against an isolated OSS server**.
The fixture uses the public history-ingestion API to create a conversation
with Markdown tables, links, and formatting. It requires at least 2,500 mounted
DOM elements, even with transcript virtualization, so document-wide style
invalidation is visible. No model, runner, external server, or credentials are
needed. Server state and browser contexts are disposable.

This complements the [server/runner/host benchmarks](../omnigent/README.md).
DOM-only component tests cannot measure style recalculation, layout, or paint
opportunities.

## Run locally

```bash
OMNIGENT_SKIP_WEB_UI=true uv sync --locked --group test
pnpm install --frozen-lockfile --filter web
pnpm --filter web run build
uv run --no-sync playwright install chromium
uv run --no-sync dev/benchmarks/ui/run.py --output-dir artifacts/ui-benchmark
```

On Linux, install Chromium's system libraries with
`uv run --no-sync playwright install --with-deps chromium` if needed.

For a quick functional smoke run (not statistically useful):

```bash
uv run --no-sync dev/benchmarks/ui/run.py --runs 1 --iterations 10 --warmup 3
```

To compare two revisions, build each checkout's SPA to a separate directory
(`pnpm --filter web run build --outDir /absolute/output/path`), then run the
candidate's driver:

```bash
uv run --no-sync dev/benchmarks/ui/run.py \
  --web-dist /tmp/ui-candidate --revision CANDIDATE_SHA \
  --baseline-dist /tmp/ui-base --baseline-revision BASE_SHA \
  --output-dir artifacts/ui-benchmark
```

Both bundles use the candidate's backend, fixture, and Playwright version.
Only one page is measured at a time; the order alternates base/candidate,
candidate/base, base/candidate across the three runs. Use the **same bundle**
for both paths to check A/A noise before changing thresholds.

## Journeys and gates

Each fresh context opens a populated session, warms up 10 keystrokes, and
measures 80 real keyboard presses. Every character must appear exactly once
in the composer. Runs cover ordinary browser CSS and the macOS Electron CSS
scope (`data-electron-mac` on `<html>`). The latter exercises the stylesheet
in Chromium; it does not test a native Electron window or OS input handling.

| Metric | Measurement |
| --- | --- |
| `session_open` | Navigation through editable composer and rendered final transcript message, with fonts ready |
| `key_to_frame` | Browser keydown timestamp through two animation frames, including React's update and a rendering opportunity |
| `style_layout` | CDP `RecalcStyleDuration` + `LayoutDuration` delta for each keystroke |

Key-to-frame is a rendering proxy, **not INP**. It includes frame scheduling;
it excludes Python and Playwright transport time. Style/layout measures the
browser's actual rendering work, which reveals expensive restyles even if
the text still arrives correctly. Per-keystroke script duration is recorded
as a diagnostic alongside the gated metrics. Screenshots happen after timing.

Chromium runs at a fixed 1440×900 viewport and **4× CPU throttling** by default.
The candidate must stay within run-median P95 budgets of **100 ms key-to-frame**
and **16 ms style/layout** (`--max-key-to-frame-ms`, `--max-style-layout-ms`).
With a baseline, the existing benchmark comparator also gates run-median P50
and P95: both a **100% relative increase** and a **5 ms absolute increase** are
required (`--threshold`, `--min-regression-ms`). Session-open has one sample
per run, so it gates on P50 only. Thresholds aim to catch substantial UX
regressions without treating small renderer timing differences as failures.

Missing assets, browser errors, lost keystrokes, an undersized fixture,
missing measurements, and exceeded budgets all exit nonzero. The benchmark
never silently skips an unavailable browser or missing baseline.

## CI and artifacts

[`benchmark-ui.yml`](../../../.github/workflows/benchmark-ui.yml) runs on UI
PRs (including forks), nightly, and by manual dispatch. PRs compare the exact
base of the test merge against the candidate on **one runner**. Filtering is
at the job level, so `UI performance regression check` can be made a required
check without leaving unrelated PRs pending. Nightlies enforce absolute
budgets and publish trend data; manual dispatch accepts `baseline_ref` for
comparisons or A/A checks. No write token or model secrets are used.

The `benchmark-results-ui-<run_id>` artifact contains `candidate.json`,
optional `baseline.json` and `comparison.json`, `summary.md`, and screenshots.
Reports reuse the existing versioned benchmark schema (`harness=chromium-ui`,
`backend=chromium`) and retain **all raw keystroke samples**, DOM counts,
browser version, throttle settings, fixture size, and UI/backend revisions.
An interrupted run may leave partial JSON; incomplete data cannot pass.

To verify sensitivity to the document-wide restyle regression, copy a built
bundle and replace its compiled `html[data-electron-mac] :is(` selector with
`html:has([data-electron-mac]) :is(`. Compare that copy as the candidate against
the original. The style/layout gate should fail in the ordinary browser case
as well as the desktop CSS case. Keep these temporary builds outside the tree.
