# Omnigent end-to-end UI benchmark

Baseline, repeatable numbers for real **browser** user journeys through the web
SPA, so we can track UI latency and network chattiness over time and catch
regressions the HTTP benchmark (`dev/benchmarks/omnigent/`) can't see — SPA boot,
SSE streaming to first paint, client-side navigation, and asset/API request
counts.

The harness boots a real `omnigent server` + runner against a zero-latency mock
LLM, builds and serves the web SPA, opens a Playwright Chromium browser, drives
the selected journeys, prints per-journey latency tables, and writes a
versioned JSON report. Each journey block carries the same `runs`/`summary`
latency shape as the HTTP benchmark **plus** a `network` sub-object counting the
requests the journey issued.

Because the mock LLM is zero-latency, the numbers are Omnigent + SSE + browser
render overhead, not model latency — exactly what a UI regression benchmark
wants.

## Run it

One-time setup (in a worktree the venv + node deps may be missing):

```bash
OMNIGENT_SKIP_WEB_UI=true uv sync --extra dev --extra e2e-ui   # venv (once)
uv run playwright install --with-deps chromium                  # browser (once)
```

Then:

```bash
# All journeys (8 iterations × 3 runs each), writing a report.
uv run --no-sync dev/benchmarks/omnigent_ui/run.py --output ui-benchmark.json

# A single journey, quick.
uv run --no-sync dev/benchmarks/omnigent_ui/run.py \
    --journeys landing_load --runs 1 --iterations 2

# Local debugging: visible browser, reuse an existing SPA build.
uv run --no-sync dev/benchmarks/omnigent_ui/run.py --headed --skip-build

# CI gating: exit 1 if a threshold is breached.
uv run --no-sync dev/benchmarks/omnigent_ui/run.py --max-p50-ms 2000
```

`--no-sync` runs against the already-installed venv. The harness builds the SPA
into `omnigent/server/static/web-ui/` on startup (serialized by
`web/.build.lock`); pass `--skip-build` to reuse a build already there.

On a root/container runner where Chromium refuses to start without
`--no-sandbox`, set `OMNIGENT_PW_NO_SANDBOX=1` (same knob the e2e_ui suite
uses).

Key flags (`--help` for all): `--journeys A,B`, `--database-uri URI` (default:
throwaway empty SQLite), `--iterations N` (per run, clamped per journey),
`--runs N`, `--warmup N`, `--headed`, `--skip-build`, `--output FILE`,
`--max-p50-ms` / `--max-p99-ms` (CI thresholds).

## Journeys

| Journey | What it times | Isolation |
| --- | --- | --- |
| `landing_load` | `goto /` → landing composer visible (cold first paint) | fresh context per rep |
| `new_session_first_token` | fresh bound session → type in composer → Send → first streamed assistant token | fresh context per rep |
| `switch_sessions` | click between two seeded sessions in the sidebar (client-side nav) → target conversation renders | one shared page (JS state must survive) |
| `fork_session` | fork from an assistant response → forked conversation renders | fresh context per rep |

`landing_load` and `new_session_first_token` use a fresh browser context each
repetition so they measure a true cold visit. `switch_sessions` reuses one page
because a client-side sidebar switch depends on the SPA's in-memory store —
a full reload would reset it, so it must not `goto`.

Determinism: driven turns use a reset-surviving streaming mock fallback so every
turn streams the same reply; the server's policy-classifier and generic-turn
fallbacks (`_policy_llm_`, `gpt-4o-mini`) are set by the base environment so no
turn blocks or reaches a real provider. Sessions created/forked during a run are
deleted in each journey's teardown to bound DB drift.

## Report

Same JSON contract as the HTTP benchmark (`schema.py` `build_report`,
`schema_version` 5), with `harness: "web-ui-playwright"`. Each journey block:

```jsonc
{
  "kind": "ui-latency",
  "backend": "sqlite",
  "runs": [ { "n_success": 8, "p50_ms": 412.0, "p95_ms": 480.0, ... } ],
  "summary": { "avg_p50_ms": 412.0, "avg_p95_ms": 480.0, "avg_p99_ms": 495.0, ... },
  "network": {
    "aggregation": "median_per_rep",   // counts are the median across reps
    "reps": 8,
    "by_resource_type": { "document": 1, "script": 6, "fetch": 4, "websocket": 1 },
    "by_endpoint": { "GET /v1/sessions/:id": 2, "POST /v1/sessions/:id/events": 1 },
    "median_total_requests": 14,
    "max_total_requests": 16,
    "per_rep_total_requests": [14, 14, 15, 14, 16, 14, 14, 14]
  },
  "browser_timing": { "first_contentful_paint_ms": 210.4, "dom_content_loaded_ms": 180.2 }
}
```

Network counts are grouped **two** ways: by Playwright resource type
(`document`/`script`/`stylesheet`/`xhr`/`fetch`/`websocket`/`image`/`font`) and
by `"{METHOD} {normalized_path}"`, where session/agent ids and asset hashes are
collapsed to `:id` / `:n` / stem so counts aggregate across reps. Counts are the
**median per rep** (iteration-count independent); `per_rep_total_requests` and
`max_total_requests` expose any nondeterminism. `browser_timing` (Navigation
Timing + FCP) is a **secondary** signal only — the primary latency number is the
wall-clock span around each journey's awaited stop-assertion.

Latency-regression comparison reuses the HTTP benchmark's comparator unchanged
(it reads only `summary.avg_p50_ms`/`avg_p95_ms`):

```bash
uv run --no-sync dev/benchmarks/omnigent/compare.py \
    --baseline nightly-ui.json --candidate pr-ui.json --threshold 0.20
```

## CI

`.github/workflows/e2e-ui-benchmark.yml` runs this nightly (and on
`workflow_dispatch`) and uploads `ui-benchmark-results.json` as an artifact. No
PR gate for now.
