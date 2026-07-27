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
(CI thresholds), `--network-delay-ms MS` (simulated client↔server latency,
see *Network* below).

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
| `list_projects` | `GET /v1/sessions/projects` — sidebar project list (dual-read union) | project count |
| `list_project_sessions` | `GET /v1/sessions?project=` — a project folder's sessions (dual-read filter) | sessions/project |

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
| `session_cold_start` | Create a new host-bound session and time its fresh runner launch through the first token — the full new-conversation cold path |
| `session_cold_restart` | With an existing session's runner stopped before the sample, post a user message and time the automatic runner relaunch to first token |
| `warm_turn` | Drive a turn on an already-warm session — steady-state dispatch overhead |
| `time_to_first_token` | Post a turn; time to the first streamed `output_text` delta |
| `interrupt` | Interrupt a running (gated) turn; time to cancellation |
| `read_runner_file` | `GET .../environments/default/filesystem/{path}` — server → runner filesystem read proxy |

The two cold journeys use a real `omni host` daemon. `session_cold_start`
creates a new host-bound session per sample, while `session_cold_restart`
creates one session up front and sends `stop_session` before each sample. That
control event preserves the conversation but stops its runner; the timed user
message then follows the production auto-relaunch path. In both cases the host
spawns a fresh runner with its own binding token and reverse tunnel, so the
latency includes process startup, tunnel registration, and first-token
dispatch. The daemon reaps any remaining runners when the benchmark exits.

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

## Network requests + simulated delay

Two related knobs for reasoning about **network cost** — the round-trips a
journey makes and what they'd cost over a real network, both of which loopback
otherwise hides.

### Requests-per-op (`http_requests` / `avg_http_requests_per_op`)

Every run reports how many HTTP requests **the server handled** during its
timed region, divided by successful ops → requests-per-op. This is the
deterministic, noise-free signal: a change that adds or removes a round-trip
moves the count directly, independent of timing jitter.

The value is the *server-side* count, not just what the benchmark process
issues — so for the full-turn (`needs_runner`) journeys it also captures the
cross-process traffic a client-side hook can't see (runner → server callbacks,
host → server). That's where the count is genuinely unknown and interesting; for
the HTTP/API journeys it's known by construction (`list_sessions` = 1,
`create_session` = 2 for the POST + inline DELETE, etc.).

How it works, and why it never ships in production:

- The server already tracks a cumulative request counter
  (`ServerPerformanceMetrics.total_started`), but it lives in the server
  subprocess's memory and is only pushed to OTel. The harness needs to *read*
  it, so a tiny router (`debug_router.py`) exposes it at
  `GET /debug/server-metrics`.
- That router lives under `dev/`, which `pyproject.toml` excludes from the wheel
  (`include = ["omnigent*"]`) — a production install can't even import it.
- It's mounted only via the `debug_router_modules` config key, which mirrors the
  existing `policy_modules` load-by-dotted-path seam (`create_app` →
  `_load_debug_routers`). The harness's generated `server.yaml` sets it;
  production config never does. A module that fails to import is logged and
  skipped, so a stray key is a no-op where `dev/` is absent.
- The harness (`environment.py`) reads the endpoint around each run's timed
  region and diffs it (subtracting its own closing poll). Counting is
  best-effort: if the endpoint is unreachable the run still reports latency,
  just with `http_requests: null`.

**Per-route appendix (`network_routes`).** Beyond the single per-op number, the
endpoint also returns a per-route tally (keyed by the low-cardinality FastAPI
template, e.g. `POST /v1/sessions`), so each journey's `summary` carries a
`network_routes` breakdown — every endpoint the journey hit, its total request
count, and per-op count, sorted chattiest-first. Since the count is near
identical across runs, it's summed across the summary runs and grouped by route.
This is what makes the count *actionable*: for `session_cold_start` it names
which endpoints the ~12 requests/op are spread across (including the
cross-process runner→server / host→server calls), not just the total. The
harness's own counter-poll route is filtered out. The raw per-run map is in each
run's `route_requests`.

**Tunnel round-trips are not counted.** Steady-state server↔runner traffic is
frames multiplexed over one long-lived WebSocket tunnel, not fresh HTTP requests
— so neither this counter nor an HTTP hook sees them as "requests." Counting
tunnel frames would need instrumenting the tunnel transport; it's out of scope
for v1.

### Simulated network delay (`--network-delay-ms`)

Loopback has ~zero latency, so the benchmark can't tell a chatty journey (many
round-trips) from a lean one on wall-clock alone. `--network-delay-ms MS`
(default `0`) injects an artificial sleep before **every request the benchmark
client sends**, via an httpx request event hook — modelling a real client↔server
network hop. Combined with the per-op request count, `delay × requests-per-op`
is the wall-clock cost those round-trips add, so the two features reinforce each
other when testing a network optimization.

**Scope note (v1):** the delay models the **client↔server** hop only — the hop
the benchmark process owns. The cross-process server↔runner tunnel and
server→mock-LLM hops are *not* delayed (they'd need injecting sleep into the
runner's client / the tunnel transport, in separate processes). Documented
follow-up. The nightly and PR workflows run at `0` for stable, comparable trend
data; dispatch the workflow with a higher `network_delay_ms` when investigating
a network optimization.

**Mind the CI time budget.** The delay applies to *every* client→server
request, so it multiplies across the full-turn journeys' round-trips — a cold
start makes ~12 requests/op. A large delay across the whole default journey set
can exceed the workflow's 30-min per-leg timeout (empirically, with the older
poll-based turn driver `network_delay_ms=100` over all journeys timed out; `10`
finishes in ~6 min). For a bigger delay, pair it with a `--journeys` subset of
the HTTP journeys, where the count is 1–2/op.

## Seeding a realistic corpus

`seed.py` writes a sizeable, deterministic corpus directly through the store
API (no HTTP, no runner) into the same DB the server then boots against:

```bash
# Seed 5000 sessions × 50 items into a SQLite file, then benchmark against it.
uv run --no-sync dev/benchmarks/omnigent/seed.py \
    --database-uri sqlite:////abs/path/bench.db --sessions 5000 --items-per-session 50 \
    --projects 20 --filed-fraction 0.5
uv run --no-sync dev/benchmarks/omnigent/run.py \
    --database-uri sqlite:////abs/path/bench.db --output bench.json
```

The corpus also seeds **first-class projects** and files a fraction of sessions
into them, so `list_projects` / `list_project_sessions` measure a realistic
sidebar instead of an empty project set. `--projects N` sets the folder count
(0 = none) and `--filed-fraction F` the fraction of sessions filed (round-robin
across the folders); the defaults (20 projects, 0.5) put ~1/40th of the corpus
in each folder. Projects are owned by the reserved `"local"` user the loopback
server resolves to, so the owner-scoped project reads see them.

Seeding is **idempotent**: a matching corpus (same sessions/items/projects/
schema) is detected and reused, so re-running is a fast no-op — pass `--reseed`
to force, or a differing config to be warned. SQLite absolute paths need four
slashes (`sqlite:////abs/...`). The reuse marker records the DB's Alembic head
read at seed time, so a corpus from an older schema is automatically reseeded —
no manual revision bookkeeping. `test_seed_creates_listable_corpus` (which seeds
through the store, running migrations to the current head) is the safety net
that a schema change hasn't broken seeding.

## Backends

`--database-uri` selects the DB; the report's `backend` field (`sqlite` /
`postgres` / `mysql`) is derived from the URI scheme so results group by
backend.

- **SQLite** (default) — in-process; fast, but not prod-representative.
- **Postgres** — `postgresql+psycopg://user@host:5432/db` (the fully-qualified
  `+psycopg` form; the server CLI does not normalize a bare `postgresql://`).
  Requires `psycopg[binary]` (the `databricks` extra). Matches prod's
  round-trip/pooling profile. Stand up a local one with
  `docker run -e POSTGRES_PASSWORD=… -p 5432:5432 postgres:16`.
- **MySQL** — `mysql+mysqldb://user@host:3306/db`. Requires the `mysqlclient`
  driver (`pip install mysqlclient`, which needs the `libmysqlclient-dev`
  system library) — it is not in any extra. A supported backend, though prod
  runs on Postgres. Stand up a local one with
  `docker run -e MYSQL_ROOT_PASSWORD=… -e MYSQL_DATABASE=benchdb -p 3306:3306 mysql:8.0`.

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
  "schema_version": 6,
  "generated_at": "<ISO-8601 UTC>",
  "git_sha": "<HEAD sha>",
  "git_branch": "<branch>",
  "host": {"platform": "...", "python": "...", "cpu_count": 12},
  "harness": "http-only",
  "config": {"iterations": 100, "requests": 500, "concurrency": 1,
             "runs": 3, "warmup": 10, "with_runner": false,
             "backend": "sqlite", "network_delay_ms": 0.0},
  "journeys": {
    "<journey name>": {
      "kind": "latency" | "throughput",
      "backend": "sqlite" | "postgres" | "mysql",
      "needs_runner": false,          // hardcoded per journey: HTTP=false, full-turn=true
      "runs": [                       // one per --runs
        {"n_success": N, "n_failures": N, "failures": {"HTTP 500": 1},
         "wall_time_s": …, "mean_ms": …, "p50_ms": …, "p95_ms": …,
         "p99_ms": …, "max_ms": …, "rps": …,
         "http_requests": N,          // server HTTP requests during the timed region; null if uncounted
         "http_requests_per_op": …,   // http_requests / n_success; null if uncounted
         "route_requests": {"POST /v1/sessions": N, ...}}  // per-route breakdown; {} if uncounted
      ],
      "summary": {"runs_total": 3, "runs_ok": 3,   // how many runs the averages cover
                  "avg_mean_ms": …, "avg_p50_ms": …, "avg_p95_ms": …,
                  "avg_p99_ms": …, "avg_rps": …,   // averaged over the runs_ok runs
                  "avg_http_requests_per_op": …,   // present only when a run was counted
                  "network_routes": [              // per-route appendix, sorted by per_op desc
                    {"route": "POST /v1/sessions", "requests": N, "per_op": …}
                  ]}                               // present only when a run recorded routes
    }
    // A journey that errored out of measurement entirely instead carries:
    //   {"kind", "backend", "needs_runner", "runs": [], "summary": {},
    //    "skipped": true, "error": "HTTPStatusError: ..."}
  }
}
```

The `http_requests*` / `route_requests` / `network_routes` fields are the
server-side request count and its per-endpoint breakdown (see *Network* above);
`network_delay_ms` records the simulated client↔server latency the run used.

The per-journey `summary` + `runs` shape mirrors MLflow's gateway benchmark, so
the same ETL flatten works — keyed by `journey` and `backend`. Bump
`SCHEMA_VERSION` on any breaking shape change so the notebook can branch on it.

**Failures never abort the run.** A per-operation error is recorded in that
run's `failures` breakdown (keyed `HTTP 500` etc.); a run in which *every*
operation failed keeps its per-run row but is excluded from the `summary`
averages (`runs_ok` < `runs_total`) so a failed run can't masquerade as an
infinitely fast one. A journey whose `setup` fails (e.g. a 500 resolving a
target session — the exact crash this harness used to die on) records a single
`setup: HTTP 500` failed run and moves on. Any other unexpected per-journey
error is caught in `run.py`, recorded as `"skipped": true` with the `error`
string, and the remaining journeys still run. Skips/all-failed journeys are
non-fatal on their own, but if any CI threshold (`--max-p50-ms` etc.) is
supplied, a journey with no successful sample fails the gate — the guarantee
couldn't be verified.

## Layout

| File | Role |
| --- | --- |
| `run.py` | CLI orchestrator + entrypoint |
| `seed.py` | deterministic corpus seeder (store API) |
| `journeys.py` | `Journey` dataclass, latency/throughput runners, registry |
| `environment.py` | server (± runner + mock LLM) lifecycle; `--database-uri`; request-count read + network-delay hook |
| `measure.py` | `RunResult`, percentile, aggregation, thresholds, tables |
| `schema.py` | `SCHEMA_VERSION`, `build_report`, git/host metadata |
| `debug_router.py` | CI-only `GET /debug/server-metrics` plugin router (never shipped in the wheel) |
| `sample_output.json` | committed example of the JSON contract |

The smoke test is `tests/benchmarks/test_benchmark_smoke.py` (boots the server
with tiny counts + a seeded-corpus unit test; runs on the normal CI lane, no
creds).

## CI

`.github/workflows/benchmark.yml` runs nightly (and on dispatch) as a backend
matrix — `sqlite`, `postgres` (a `postgres:16` service container), and `mysql`
(a `mysql:8.0` service container; the `mysqlclient` driver is installed on that
leg only). Each leg seeds a corpus (SQLite reuses a cache keyed on the schema
head + `seed.py` + corpus config, so a migration busts the cache and forces a
reseed; Postgres and MySQL are fresh per run), runs the benchmark, and uploads
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
  `configure_mock` / `set_mock_fallback` seam if that's ever wanted. (Distinct
  from `--network-delay-ms`, which models the *client↔server* hop — see
  *Network* above.)
- **Wider network-delay coverage.** `--network-delay-ms` v1 delays only the
  client↔server hop (the one the benchmark process owns). Extending it to the
  server↔runner tunnel and server→mock-LLM hops would need injecting the delay
  into the runner's httpx client and the tunnel transport in their own
  processes.
- **Tunnel round-trip counting.** `http_requests` counts HTTP the server
  handles, not frames on the persistent server↔runner WebSocket tunnel.
  Counting those (for a true per-turn round-trip figure) would mean
  instrumenting the tunnel transport's `RequestFrame` dispatch.
