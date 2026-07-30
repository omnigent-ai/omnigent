"""Fast smoke test for the HTTP-journey benchmark harness.

Runs the real harness (boots an ``omnigent server``, no runner / no LLM /
no Databricks) with tiny counts and asserts the report shape and threshold
logic. Runs on the normal CI lane — no creds, no ``databricks`` marker.

The measurement and schema layers also get direct unit checks so their logic
is covered without paying the server-boot cost.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import cast

import httpx
import pytest

from dev.benchmarks.omnigent import run as bench_run
from dev.benchmarks.omnigent.environment import BenchEnvironment, _sse_session_status
from dev.benchmarks.omnigent.journeys import ALL_JOURNEYS, Journey, run_latency, run_throughput
from dev.benchmarks.omnigent.measure import RunResult, aggregate, check_thresholds
from dev.benchmarks.omnigent.schema import SCHEMA_VERSION, build_report

_SMOKE_JOURNEYS = [
    "list_sessions",
    "create_session",
    "get_session",
    "load_conversation_history",
    "search_sessions",
    "list_projects",
    "list_project_sessions",
    "fork_session",
    "add_comment",
]


def _d(value: object) -> dict[str, object]:
    """Narrow an opaque report node to a dict for indexing (test-side JSON nav)."""
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _smoke_args(**overrides: object) -> argparse.Namespace:
    """Tiny-count args so the smoke run boots the server once and finishes fast."""
    base: dict[str, object] = {
        "journeys": _SMOKE_JOURNEYS,
        "database_uri": None,  # empty throwaway SQLite — journeys self-seed a fallback
        "iterations": 2,
        "requests": 5,
        "concurrency": 1,
        "runs": 1,
        "warmup": 1,
        "output": None,
        "network_delay_ms": 0.0,
        "min_rps": None,
        "max_p50_ms": None,
        "max_p99_ms": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# ── pure-layer unit checks (no server) ───────────────────────


def test_backend_of_classifies_uri_schemes() -> None:
    """The report's backend label is derived from the URI scheme."""
    assert bench_run._backend_of(None) == "sqlite"
    assert bench_run._backend_of("sqlite:////abs/bench.db") == "sqlite"
    assert bench_run._backend_of("postgresql+psycopg://u@h:5432/db") == "postgres"
    assert bench_run._backend_of("mysql+mysqldb://u@h:3306/db") == "mysql"


def test_percentile_and_throughput() -> None:
    r = RunResult(latencies_ms=[10.0, 20.0, 30.0, 40.0], wall_time=2.0)
    assert r.n_success == 4
    assert r.percentile(50) == 20.0  # ceil-index: idx = ceil(0.5*4)-1 = 1
    assert r.percentile(100) == 40.0
    assert r.throughput == 2.0  # 4 successes / 2.0s


def test_aggregate_summary_keys() -> None:
    runs = [RunResult(latencies_ms=[5.0, 15.0], wall_time=1.0) for _ in range(2)]
    block = aggregate(runs)
    run_rows = cast(list[dict[str, object]], block["runs"])
    assert len(run_rows) == 2
    # No http_requests recorded → no network key in the summary.
    assert set(_d(block["summary"])) == {
        "runs_total",
        "runs_ok",
        "avg_mean_ms",
        "avg_p50_ms",
        "avg_p95_ms",
        "avg_p99_ms",
        "avg_rps",
    }
    assert _d(block["summary"])["runs_total"] == 2
    assert _d(block["summary"])["runs_ok"] == 2
    assert run_rows[0]["n_success"] == 2
    # The per-run row still carries the network fields, as null when uncounted.
    assert run_rows[0]["http_requests"] is None
    assert run_rows[0]["http_requests_per_op"] is None


def test_aggregate_includes_network_block_when_counted() -> None:
    """When runs carry a server request count, the summary reports per-op volume."""
    # Two ops, four server requests → 2.0 requests/op.
    runs = [RunResult(latencies_ms=[5.0, 15.0], wall_time=1.0, http_requests=4) for _ in range(2)]
    block = aggregate(runs)
    summary = _d(block["summary"])
    assert summary["avg_http_requests_per_op"] == 2.0
    run_rows = cast(list[dict[str, object]], block["runs"])
    assert run_rows[0]["http_requests"] == 4
    assert run_rows[0]["http_requests_per_op"] == 2.0


def test_aggregate_route_appendix_groups_and_orders() -> None:
    """The per-route appendix sums across runs, divides by ops, sorts by per_op."""
    # 2 ops/run × 2 runs = 4 ops. GET seen 2×/run (→4 total → 1.0/op),
    # POST 1×/run (→2 → 0.5/op).
    runs = [
        RunResult(
            latencies_ms=[5.0, 6.0],
            wall_time=1.0,
            http_requests=6,
            route_requests={"GET /v1/sessions/{id}": 2, "POST /v1/sessions": 1},
        )
        for _ in range(2)
    ]
    summary = _d(aggregate(runs)["summary"])
    appendix = cast(list[dict[str, object]], summary["network_routes"])
    assert [r["route"] for r in appendix] == ["GET /v1/sessions/{id}", "POST /v1/sessions"]
    assert appendix[0]["requests"] == 4 and appendix[0]["per_op"] == 1.0
    assert appendix[1]["requests"] == 2 and appendix[1]["per_op"] == 0.5
    # Per-run row carries its own raw breakdown.
    run_rows = cast(list[dict[str, object]], aggregate(runs)["runs"])
    assert run_rows[0]["route_requests"] == {"GET /v1/sessions/{id}": 2, "POST /v1/sessions": 1}


def test_aggregate_no_route_appendix_when_uncounted() -> None:
    """No route breakdown recorded → no network_routes key (not an empty list)."""
    runs = [RunResult(latencies_ms=[5.0], wall_time=1.0) for _ in range(2)]
    assert "network_routes" not in _d(aggregate(runs)["summary"])


def test_requests_per_op_none_when_uncounted_or_no_success() -> None:
    """requests_per_op distinguishes uncounted (None) from a real zero."""
    assert RunResult(latencies_ms=[5.0]).requests_per_op() is None  # http_requests unset
    # Counted but no successful op → None, not a divide-by-zero.
    failed = RunResult(wall_time=1.0, http_requests=3)
    failed.record_failure("HTTP 500")
    assert failed.requests_per_op() is None
    # Counted with successes → the ratio.
    assert RunResult(latencies_ms=[1.0, 1.0], http_requests=6).requests_per_op() == 3.0


def test_sse_session_status_parses_both_shapes_and_ignores_noise() -> None:
    """drive_turn's SSE completion parser: status shapes + non-status lines."""
    # Nested shape (API.md) and flat shape (observed on the wire) both work.
    assert _sse_session_status('{"type":"session.status","data":{"status":"idle"}}') == "idle"
    assert (
        _sse_session_status('{"type":"session.status","status":"running","conversation_id":"x"}')
        == "running"
    )
    # Non-status events, the [DONE] sentinel, empty, and non-JSON → None.
    assert _sse_session_status('{"type":"response.completed","response":{}}') is None
    assert _sse_session_status("[DONE]") is None
    assert _sse_session_status("") is None
    assert _sse_session_status("not json") is None
    # A session.status without a usable status field → None (not a crash).
    assert _sse_session_status('{"type":"session.status","data":{}}') is None


def test_aggregate_excludes_fully_failed_run_from_summary() -> None:
    """A run where every op failed doesn't drag the averages toward zero."""
    good = RunResult(latencies_ms=[10.0, 10.0], wall_time=1.0)
    failed = RunResult(wall_time=1.0)  # no successes
    failed.record_failure("HTTP 500")
    block = aggregate([good, failed])

    summary = _d(block["summary"])
    assert summary["runs_total"] == 2
    assert summary["runs_ok"] == 1
    # Averaged over the one successful run only — not (10 + 0) / 2 = 5.
    assert summary["avg_p50_ms"] == 10.0
    # The failed run is still visible in the per-run detail.
    run_rows = cast(list[dict[str, object]], block["runs"])
    assert run_rows[1]["n_failures"] == 1
    assert run_rows[1]["n_success"] == 0


def test_aggregate_all_failed_runs_has_no_metric_keys() -> None:
    """When every run failed, the summary carries counts but no fake metrics."""
    failed = RunResult(wall_time=1.0)
    failed.record_failure("HTTP 500")
    block = aggregate([failed])
    summary = _d(block["summary"])
    assert summary == {"runs_total": 1, "runs_ok": 0}


def test_check_thresholds_pass_and_fail() -> None:
    runs = [RunResult(latencies_ms=[10.0, 10.0], wall_time=1.0)]
    assert check_thresholds(runs, min_rps=None, max_p50_ms=1000.0, max_p99_ms=None)
    assert not check_thresholds(runs, min_rps=None, max_p50_ms=0.001, max_p99_ms=None)


def test_check_thresholds_ignores_failed_run() -> None:
    """A fully-failed run's zeros don't fabricate a passing (or failing) p50."""
    good = RunResult(latencies_ms=[10.0, 10.0], wall_time=1.0)
    failed = RunResult(wall_time=1.0)
    failed.record_failure("HTTP 500")
    # p50 over the successful run is 10ms — a 20ms bound passes despite the
    # failed run's 0.0 (which would otherwise pull the average to 5ms).
    assert check_thresholds([good, failed], min_rps=None, max_p50_ms=20.0, max_p99_ms=None)


def test_check_thresholds_all_failed_fails_when_gated() -> None:
    """No successful sample + a supplied threshold can't be verified → fail."""
    failed = RunResult(wall_time=1.0)
    failed.record_failure("HTTP 500")
    # With a threshold supplied, an all-failed journey fails the gate.
    assert not check_thresholds([failed], min_rps=None, max_p50_ms=1000.0, max_p99_ms=None)
    # With no threshold supplied, resilience wins — it's vacuously fine.
    assert check_thresholds([failed], min_rps=None, max_p50_ms=None, max_p99_ms=None)


def test_skipped_block_shape() -> None:
    """A skipped journey keeps the report shape but carries no fake metrics."""
    journey = ALL_JOURNEYS["fork_session"]
    block = bench_run._skipped_block(journey, "postgres", RuntimeError("boom"))
    assert block["skipped"] is True
    assert block["runs"] == []
    assert block["summary"] == {}
    assert block["backend"] == "postgres"
    assert block["needs_runner"] is journey.needs_runner
    assert block["error"] == "RuntimeError: boom"


def test_thresholds_supplied() -> None:
    assert not bench_run._thresholds_supplied(
        argparse.Namespace(min_rps=None, max_p50_ms=None, max_p99_ms=None)
    )
    assert bench_run._thresholds_supplied(
        argparse.Namespace(min_rps=None, max_p50_ms=25.0, max_p99_ms=None)
    )


def test_build_report_shape() -> None:
    block = aggregate([RunResult(latencies_ms=[1.0], wall_time=1.0)])
    block["kind"] = "latency"
    report = build_report(
        {"list_sessions": block},
        generated_at="2026-07-08T00:00:00+00:00",
        config={"iterations": 2},
        harness="http-only",
    )
    assert report["schema_version"] == SCHEMA_VERSION
    assert report["generated_at"] == "2026-07-08T00:00:00+00:00"
    assert set(report) >= {
        "schema_version",
        "generated_at",
        "git_sha",
        "git_branch",
        "host",
        "harness",
        "config",
        "journeys",
    }
    assert "list_sessions" in _d(report["journeys"])


# ── per-journey iteration cap (no server) ────────────────────


def test_effective_iterations_clamps_capped_journey() -> None:
    """A journey with ``max_iterations`` clamps a larger request down, not up."""
    capped = ALL_JOURNEYS["session_cold_start"]
    assert capped.max_iterations is not None
    # Requesting more than the cap is clamped to the cap; less is left alone.
    assert bench_run._effective_iterations(capped, 200) == capped.max_iterations
    assert bench_run._effective_iterations(capped, 1) == 1


def test_effective_iterations_uncapped_journey_passthrough() -> None:
    """An HTTP journey (no cap) uses the requested count verbatim."""
    uncapped = ALL_JOURNEYS["list_sessions"]
    assert uncapped.max_iterations is None
    assert bench_run._effective_iterations(uncapped, 200) == 200


def test_runner_journeys_are_capped() -> None:
    """Every full-turn journey caps its iterations; HTTP journeys do not."""
    for journey in ALL_JOURNEYS.values():
        if journey.needs_runner:
            assert journey.max_iterations is not None, journey.name
        else:
            assert journey.max_iterations is None, journey.name


@pytest.mark.asyncio
async def test_latency_prepare_runs_before_warmup_and_timed_operations() -> None:
    """Per-sample preparation is outside measure but runs for every sample."""
    calls: list[str] = []

    async def _setup(_env: BenchEnvironment) -> object:
        calls.append("setup")
        return object()

    async def _prepare(_env: BenchEnvironment, _ctx: object) -> None:
        calls.append("prepare")

    async def _measure(_env: BenchEnvironment, _ctx: object) -> None:
        calls.append("measure")

    async def _teardown(_env: BenchEnvironment, _ctx: object) -> None:
        calls.append("teardown")

    journey = Journey(
        name="prepared",
        kind="latency",
        setup=_setup,
        prepare=_prepare,
        measure=_measure,
        teardown=_teardown,
    )
    result = await run_latency(
        journey,
        cast(BenchEnvironment, object()),
        iterations=2,
        warmup=1,
    )

    assert result.n_success == 2
    assert calls == [
        "setup",
        "prepare",
        "measure",
        "prepare",
        "measure",
        "prepare",
        "measure",
        "teardown",
    ]


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """Build an ``HTTPStatusError`` like ``raise_for_status`` raises."""
    request = httpx.Request("GET", "http://localhost/v1/sessions")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


@pytest.mark.asyncio
@pytest.mark.parametrize("runner", [run_latency, run_throughput])
async def test_setup_failure_is_recorded_not_raised(runner: object) -> None:
    """A journey whose setup raises yields a failed run, not a crash.

    This is the exact failure from the field: ``_setup_target_session`` calling
    ``raise_for_status()`` on a 500. Before the fix it propagated and aborted
    the whole suite; now it is recorded as a failed run.
    """

    async def _setup(_env: BenchEnvironment) -> object:
        raise _http_status_error(500)

    async def _measure(_env: BenchEnvironment, _ctx: object) -> None:  # pragma: no cover
        raise AssertionError("measure must not run when setup failed")

    journey = Journey(name="broken-setup", kind="latency", setup=_setup, measure=_measure)
    result = await runner(  # type: ignore[operator]
        journey,
        cast(BenchEnvironment, object()),
        **({"iterations": 3} if runner is run_latency else {"requests": 3, "concurrency": 2}),
        warmup=1,
    )

    assert result.n_success == 0
    assert result.n_failures == 1  # one failed run, not one-per-iteration
    assert result.failures == {"setup: HTTP 500": 1}


@pytest.mark.asyncio
async def test_teardown_failure_does_not_mask_results() -> None:
    """A teardown that raises is suppressed — the run's results still return."""

    async def _measure(_env: BenchEnvironment, _ctx: object) -> None:
        return None

    async def _teardown(_env: BenchEnvironment, _ctx: object) -> None:
        raise RuntimeError("teardown blew up")

    journey = Journey(name="broken-teardown", kind="latency", measure=_measure, teardown=_teardown)
    result = await run_latency(journey, cast(BenchEnvironment, object()), iterations=2, warmup=0)
    assert result.n_success == 2


@pytest.mark.asyncio
async def test_operation_failure_is_recorded_per_op() -> None:
    """A measure op that raises records one failure per op and keeps timing."""
    calls = {"n": 0}

    async def _measure(_env: BenchEnvironment, _ctx: object) -> None:
        calls["n"] += 1
        if calls["n"] == 2:  # fail exactly one timed op (warmup=0)
            raise _http_status_error(503)

    journey = Journey(name="flaky", kind="latency", measure=_measure)
    result = await run_latency(journey, cast(BenchEnvironment, object()), iterations=3, warmup=0)
    assert result.n_success == 2
    assert result.failures == {"HTTP 503": 1}


# ── end-to-end smoke (boots the server) ──────────────────────


@pytest.mark.timeout(180)
async def test_benchmark_smoke_end_to_end() -> None:
    """Boot the server, run every HTTP journey once, validate the report."""
    report, passed = await bench_run.run_benchmark(_smoke_args())

    assert passed  # no thresholds supplied → vacuously passes
    assert report["schema_version"] == SCHEMA_VERSION
    assert _d(report["config"])["with_runner"] is False
    # No --database-uri → the throwaway-SQLite path, labelled "sqlite".
    assert _d(report["config"])["backend"] == "sqlite"
    # The delay knob is recorded in config; default run injects none.
    assert _d(report["config"])["network_delay_ms"] == 0.0

    journeys = _d(report["journeys"])
    for name in _SMOKE_JOURNEYS:
        assert name in ALL_JOURNEYS
        block = _d(journeys[name])
        assert block["kind"] == "latency"
        assert block["backend"] == "sqlite"
        # Hardcoded per-journey flag: HTTP journeys are always False, even in a
        # run whose config.with_runner is True because a runner journey rode along.
        assert block["needs_runner"] is False
        run_rows = cast(list[dict[str, object]], block["runs"])
        assert run_rows, f"{name} produced no runs"
        # Zero failures — a failure here means the HTTP path itself broke.
        assert run_rows[0]["n_failures"] == 0, f"{name}: {run_rows[0]['failures']}"
        assert cast(float, _d(block["summary"])["avg_p50_ms"]) >= 0.0
        # The CI-only debug router loaded, so the server request counter was
        # readable: each HTTP journey issues at least one request per op.
        per_op = _d(block["summary"]).get("avg_http_requests_per_op")
        assert per_op is not None, f"{name} produced no request count"
        assert cast(float, per_op) >= 1.0, f"{name}: {per_op} requests/op"
        # Per-route appendix: at least one endpoint attributed, and its
        # per-op figures sum to roughly the aggregate per-op count.
        routes = cast(list[dict[str, object]], _d(block["summary"]).get("network_routes", []))
        assert routes, f"{name} produced no route breakdown"
        assert all(cast(float, r["per_op"]) > 0.0 for r in routes)


@pytest.mark.timeout(180)
async def test_benchmark_smoke_threshold_failure_exits_nonzero() -> None:
    """An impossible p50 bound trips the threshold gate (passed=False)."""
    _, passed = await bench_run.run_benchmark(
        _smoke_args(journeys=["list_sessions"], max_p50_ms=0.0001)
    )
    assert not passed


@pytest.mark.timeout(180)
async def test_benchmark_smoke_erroring_journey_is_skipped_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected per-journey error is recorded as skipped; the suite continues.

    Guards the outer safety net in ``run_benchmark``: the field crash was a
    setup 500 aborting the whole process. Here a middle journey raises and the
    journeys on either side still run and report.
    """
    real_run_journey = bench_run._run_journey
    calls: list[str] = []

    async def _fake_run_journey(journey: Journey, env: object, args: object) -> object:
        calls.append(journey.name)
        if journey.name == "get_session":
            raise RuntimeError("kaboom")
        return await real_run_journey(journey, env, args)  # type: ignore[arg-type]

    monkeypatch.setattr(bench_run, "_run_journey", _fake_run_journey)

    report, passed = await bench_run.run_benchmark(
        _smoke_args(journeys=["list_sessions", "get_session", "add_comment"])
    )

    # Every journey was attempted despite the middle one erroring.
    assert calls == ["list_sessions", "get_session", "add_comment"]
    journeys = _d(report["journeys"])
    assert _d(journeys["get_session"])["skipped"] is True
    assert _d(journeys["get_session"])["summary"] == {}
    assert _d(journeys["list_sessions"]).get("skipped") is None
    assert _d(journeys["add_comment"]).get("skipped") is None
    # No thresholds supplied → a skip is non-fatal.
    assert passed


@pytest.mark.timeout(180)
async def test_benchmark_smoke_skip_fails_gate_when_thresholds_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A skipped journey fails the CI gate when a threshold was requested."""

    async def _fake_run_journey(journey: Journey, env: object, args: object) -> object:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(bench_run, "_run_journey", _fake_run_journey)

    _, passed = await bench_run.run_benchmark(
        _smoke_args(journeys=["list_sessions"], max_p50_ms=1000.0)
    )
    assert not passed


# ── runner (full-turn) journeys ──────────────────────────────

_RUNNER_JOURNEYS = [
    "session_cold_start",
    "session_cold_restart",
    "warm_turn",
    "time_to_first_token",
    "interrupt",
    "read_runner_file",
]


@pytest.mark.timeout(300)
async def test_benchmark_smoke_runner_journeys() -> None:
    """Run each full-turn journey once through server + runner + mock LLM.

    First exercise of the ``with_runner=True`` path end-to-end. Tiny counts —
    each cold-journey iteration spawns a runner, so this is the slow smoke.
    """
    report, passed = await bench_run.run_benchmark(
        _smoke_args(journeys=_RUNNER_JOURNEYS, iterations=1, warmup=1)
    )

    assert passed
    # A runner journey was selected → env booted with_runner, harness stamped.
    assert _d(report["config"])["with_runner"] is True
    assert report["harness"] == "openai-agents"

    journeys = _d(report["journeys"])
    for name in _RUNNER_JOURNEYS:
        assert ALL_JOURNEYS[name].needs_runner
        block = _d(journeys[name])
        # The hardcoded per-journey flag surfaces in the report block.
        assert block["needs_runner"] is True
        run_rows = cast(list[dict[str, object]], block["runs"])
        assert run_rows, f"{name} produced no runs"
        # Zero failures — a failure here means the full-turn path broke.
        assert run_rows[0]["n_failures"] == 0, f"{name}: {run_rows[0]['failures']}"


# ── seeder (direct store, no server) ─────────────────────────


def test_seed_creates_listable_corpus(tmp_path: Path) -> None:
    """Seed a tiny corpus and confirm it is listable as "local" with history."""
    from dev.benchmarks.omnigent import seed as seed_mod
    from omnigent.server.auth import RESERVED_USER_LOCAL
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

    db_uri = f"sqlite:///{tmp_path / 'seed.db'}"

    created = seed_mod.seed(
        db_uri, sessions=6, items_per_session=4, projects=2, filed_fraction=0.5
    )
    assert created == 6

    conv = SqlAlchemyConversationStore(db_uri)
    listing = conv.list_conversations(
        limit=100,
        agent_name="bench-agent",
        accessible_by=RESERVED_USER_LOCAL,
        has_agent_id=True,
    )
    assert len(listing.data) == 6  # all seeded sessions listable as "local"
    assert len(conv.list_items(listing.data[0].id, limit=100).data) == 4

    # Projects were seeded (owner-scoped to "local") and sessions filed into them.
    projects = SqlAlchemyProjectStore(db_uri).list(owner_user_id=RESERVED_USER_LOCAL)
    assert len(projects) == 2
    # 3 of 6 sessions filed (0.5), listable via the owner-scoped ?project= filter.
    filed = conv.list_conversations(
        limit=100,
        accessible_by=RESERVED_USER_LOCAL,
        owned_by=RESERVED_USER_LOCAL,
        project=projects[0].name,
    )
    assert len(filed.data) >= 1  # round-robin puts ~1-2 sessions in each folder

    # Idempotent: a matching re-seed is a no-op.
    assert (
        seed_mod.seed(db_uri, sessions=6, items_per_session=4, projects=2, filed_fraction=0.5) == 0
    )

    # NOTE: seed() builds the store, which runs migrations to the current head,
    # so this test always exercises the live schema — it is the safety net that
    # a schema change hasn't broken seeding (no revision constant to maintain).
