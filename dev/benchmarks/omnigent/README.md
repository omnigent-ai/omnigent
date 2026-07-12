# Omnigent performance benchmark

Baseline, repeatable latency/throughput numbers for key Omnigent user
journeys, so we can track them over time and catch regressions. Modeled on
MLflow's `dev/benchmarks/gateway/` workflow.

The harness boots a real `omnigent server`, drives the selected journeys under
load, prints latency/throughput tables, and writes a versioned JSON report.
Two families: **HTTP/API journeys** (server + DB, no runner/LLM — fast and
low-noise) and **full-turn journeys** (a real agent turn through the runner +
a zero-latency mock LLM). See *Journeys* below.

By default the server boots a fresh, empty SQLite DB, which gives best-case
numbers that don't move with load. For meaningful results, point it at a
**pre-seeded corpus** (`seed.py`) and, ideally, at **Postgres** — production
runs on Databricks Lakebase (Postgres), whose per-query round-trip + pooling
cost SQLite doesn't have. See *Seeding* and *Backends* below.

## Run it

```bash
# All journeys, sequential latency (100 iterations × 3 runs each).
uv run --no-sync dev/benchmarks/omnigent/run.py

# A subset, writing a report for CI artifact upload.
uv run --no-sync dev/benchmarks/omnigent/run.py \
    --journeys list_sessions,load_conversation_history \
    --iterations 200 --runs 3 --output bench.json

# Throughput mode: >1 concurrency drives concurrency-safe journeys as load.
uv run --no-sync dev/benchmarks/omnigent/run.py \
    --requests 500 --concurrency 25 --runs 3

# CI gating: exit 1 if a threshold is breached.
uv run --no-sync dev/benchmarks/omnigent/run.py --max-p50-ms 25 --max-p99-ms 100
```

`--no-sync` runs against the already-installed venv. (A bare `uv run` may try to
rebuild the project, which fails in a git worktree without a Node web-UI build;
`OMNIGENT_SKIP_WEB_UI=true uv sync` prepares the venv once, then use
`--no-sync`.)

Key flags (`--help` for all): `--journeys A,B`, `--database-uri URI` (seeded
corpus / Postgres; default: throwaway empty SQLite), `--iterations N` (per
latency run), `--requests N` / `--concurrency N` (throughput), `--runs N`,
`--warmup N`, `--output FILE`, `--min-rps` / `--max-p50-ms` / `--max-p99-ms`
(CI thresholds).

## Journeys

### HTTP/API (server + DB, runner-free)

| Journey | Operation timed | Stressed by |
| --- | --- | --- |
| `list_sessions` | `GET /v1/sessions` — session-list read | session count |
| `create_session` | `POST /v1/sessions` then `DELETE` — session create | write path |
| `get_session` | `GET /v1/sessions/{id}` — single-session snapshot | (O(1)) |
| `load_conversation_history` | `GET /v1/sessions/{id}/items` — history read | items/session |
| `search_sessions` | `GET /v1/sessions?search_query=` — unindexed `LIKE` | total item count |
| `fork_session` | `POST /v1/sessions/{id}/fork` — fork (deep-copy items); forks deleted in teardown, untimed | items/session |
| `add_comment` | `POST /v1/sessions/{id}/comments` — create a review comment | write path |

Read journeys target a **pre-seeded** session when the DB has a corpus; against
an empty DB they self-seed a small fallback session over HTTP (the
`external_conversation_item` event — appends items without starting a task), so
they still work with no runner or LLM.

### Full-turn (runner + mock LLM)

These drive a real agent turn end-to-end — `POST …/events` → server → **runner**
→ in-process executor → mock LLM → stream back → `idle`. Selecting any of them
boots `BenchEnvironment(with_runner=True)` automatically.

Each turn costs ~1 s+ (vs. the millisecond HTTP journeys), so these journeys
cap their latency iterations (`Journey.max_iterations`, currently 5) — a large
`--iterations` tuned for the HTTP journeys is clamped down for them so the run
stays within the CI time budget, with `--runs` providing the repeats. The cap
only lowers the count, never raises it. A cold start never deletes its session,
so sessions accumulate across a run; keeping the count small also keeps that
drift negligible (~2 ms/turn).

| Journey | Operation timed |
| --- | --- |
| `session_cold_start` | Create+bind a fresh session and drive its first turn to `idle` (runner spawn + executor construction + turn) |
| `warm_turn` | Drive a turn on an already-warm session — steady-state dispatch overhead |
| `time_to_first_token` | Post a turn; time to the first streamed `output_text` delta |
| `interrupt` | Interrupt a running (gated) turn; time to cancellation |
| `read_runner_file` | `GET .../environments/default/filesystem/{path}` — server → runner filesystem read proxy |

`read_runner_file` needs a runner but does **not** drive a turn or call the LLM:
its setup plants a file via `PUT`, and the timed op is the proxied read (a
localhost round-trip). Being far cheaper than a turn, it uses a higher iteration
cap (50) than the full-turn journeys.

**Only measure what we control.** Full-turn journeys always use the
**`openai-agents`** SDK harness, which runs **in-process** (a call into the
`agents` library + an HTTP call to the mock LLM) — no vendor binary, no external
process. Native harnesses (e.g. `claude-native`) launch the real vendor CLI
into a tmux pane, whose startup we don't control, so they're deliberately
excluded. The mock LLM is zero-latency, so every number is omnigent
dispatch/streaming/cancel overhead, not model latency.

Add a journey by registering a `Journey` in `journeys.py` (set `needs_runner`
for full-turn journeys).

## Seeding a realistic corpus

`seed.py` writes a sizeable, deterministic corpus directly through the store
API (no HTTP, no runner) into the same DB the server then boots against:

```bash
# Seed 5000 sessions × 50 items into a SQLite file, then benchmark against it.
uv run --no-sync dev/benchmarks/omnigent/seed.py \
    --database-uri sqlite:////abs/path/bench.db --sessions 5000 --items-per-session 50
uv run --no-sync dev/benchmarks/omnigent/run.py \
    --database-uri sqlite:////abs/path/bench.db --output bench.json
```

Seeding is **idempotent**: a matching corpus (same sessions/items/schema) is
detected and reused, so re-running is a fast no-op — pass `--reseed` to force,
or a differing config to be warned. SQLite absolute paths need four slashes
(`sqlite:////abs/...`). The reuse marker records the DB's Alembic head read at
seed time, so a corpus from an older schema is automatically reseeded — no
manual revision bookkeeping. `test_seed_creates_listable_corpus` (which seeds
through the store, running migrations to the current head) is the safety net
that a schema change hasn't broken seeding.

## Backends

`--database-uri` selects the DB; the report's `backend` field (`sqlite` /
`postgres`) is derived from the URI scheme so results group by backend.

- **SQLite** (default) — in-process; fast, but not prod-representative.
- **Postgres** — `postgresql+psycopg://user@host:5432/db` (the fully-qualified
  `+psycopg` form; the server CLI does not normalize a bare `postgresql://`).
  Requires `psycopg[binary]` (the `databricks` extra). Matches prod's
  round-trip/pooling profile. Stand up a local one with
  `docker run -e POSTGRES_PASSWORD=… -p 5432:5432 postgres:16`.

## Output → Databricks → dashboard

The harness writes JSON only. Storage and charting live in Databricks:

```
run.py --output bench.json   →   GitHub Actions artifact   →   Databricks notebook (ETL)   →   Delta table   →   AI/BI dashboard
        (this repo)                    (CI, follow-up)              (workspace, yours)
```

The repo's contract is the **JSON schema** below. A workspace notebook (owned
outside this repo, modeled on MLflow's gateway ETL) pulls the CI artifacts via
the GitHub API, flattens each run's `summary` + `runs` + metadata, and
`saveAsTable`s into a Delta table the dashboard reads. `sample_output.json` is a
committed, faithful example so the notebook can be written against a real
document without running the harness.

### JSON schema (`schema.py`, `SCHEMA_VERSION`)

```jsonc
{
  "schema_version": 1,
  "generated_at": "<ISO-8601 UTC>",
  "git_sha": "<HEAD sha>",
  "git_branch": "<branch>",
  "host": {"platform": "...", "python": "...", "cpu_count": 12},
  "harness": "http-only",
  "config": {"iterations": 100, "requests": 500, "concurrency": 1,
             "runs": 3, "warmup": 10, "with_runner": false,
             "backend": "sqlite"},
  "journeys": {
    "<journey name>": {
      "kind": "latency" | "throughput",
      "backend": "sqlite" | "postgres",
      "runs": [                       // one per --runs
        {"n_success": N, "n_failures": N, "failures": {"HTTP 500": 1},
         "wall_time_s": …, "mean_ms": …, "p50_ms": …, "p95_ms": …,
         "p99_ms": …, "max_ms": …, "rps": …}
      ],
      "summary": {"avg_mean_ms": …, "avg_p50_ms": …, "avg_p95_ms": …,
                  "avg_p99_ms": …, "avg_rps": …}    // averaged across runs
    }
  }
}
```

The per-journey `summary` + `runs` shape mirrors MLflow's gateway benchmark, so
the same ETL flatten works — keyed by `journey` and `backend`. Bump
`SCHEMA_VERSION` on any breaking shape change so the notebook can branch on it.

## Layout

| File | Role |
| --- | --- |
| `run.py` | CLI orchestrator + entrypoint |
| `seed.py` | deterministic corpus seeder (store API) |
| `journeys.py` | `Journey` dataclass, latency/throughput runners, registry |
| `environment.py` | server (± runner + mock LLM) lifecycle; `--database-uri` |
| `measure.py` | `RunResult`, percentile, aggregation, thresholds, tables |
| `schema.py` | `SCHEMA_VERSION`, `build_report`, git/host metadata |
| `sample_output.json` | committed example of the JSON contract |

The smoke test is `tests/benchmarks/test_benchmark_smoke.py` (boots the server
with tiny counts + a seeded-corpus unit test; runs on the normal CI lane, no
creds).

## CI

`.github/workflows/benchmark.yml` runs nightly (and on dispatch) as a backend
matrix — `sqlite` and `postgres` (a `postgres:16` service container). Each leg
seeds a corpus (SQLite reuses a cache keyed on the schema head + `seed.py` +
corpus config, so a migration busts the cache and forces a reseed; Postgres is
fresh per run), runs the benchmark, and uploads
`benchmark-results-<backend>-<run_id>.json`. The workspace notebook pulls those
artifacts.

Schema changes need no manual step: the seed always targets the current
migrated schema (migrations run when the store is constructed), the reuse
marker records the head read at seed time (so old corpora auto-reseed), and
`test_seed_creates_listable_corpus` fails if a migration genuinely breaks
seeding.

## Follow-ups

- **Subagent spawn.** A planned full-turn journey (`needs_runner=True`): the
  parent agent emits a `sys_session_send` tool call, the runner dispatches a
  child session, and the parent auto-wakes with the collected result. It's
  fully mockable with the zero-latency mock LLM (no real model) — script the
  parent's queue to emit the tool call and the child's queue to return a short
  reply, then poll for the child's marker. It needs the parent bundle to declare
  a sub-agent under `tools:` (extend `_agent_bundle`); the pattern is in
  `tests/e2e/test_coder_subagent.py`.
- **Excluded journeys** (agent-behaviour-dependent, deliberately not measured):
  multi-turn and tool-calling turns (dominated by the agent's own choices) and
  large-history turns (the O(N) `history_to_input_items` conversion is real app
  work but only fires on a cold runner cache, so isolating it entangles with
  cold-start cost).
- **CI matrix.** Runner journeys are backend-agnostic (they exercise runner
  dispatch, not big DB reads), so the nightly workflow can run them on the
  SQLite leg only rather than both — wire a runner `--journeys` set into
  `benchmark.yml` when desired.
- **Simulated provider latency.** The mock LLM returns at ~zero latency, which
  is what isolates omnigent overhead. A fixed per-response delay knob would let
  turns model end-user wall-clock instead; it's a small change behind the
  `configure_mock` / `set_mock_fallback` seam if that's ever wanted.
