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

import pytest

from dev.benchmarks.omnigent import run as bench_run
from dev.benchmarks.omnigent.journeys import ALL_JOURNEYS
from dev.benchmarks.omnigent.measure import RunResult, aggregate, check_thresholds
from dev.benchmarks.omnigent.schema import SCHEMA_VERSION, build_report

_SMOKE_JOURNEYS = [
    "list_sessions",
    "create_session",
    "get_session",
    "load_conversation_history",
    "search_sessions",
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
        "min_rps": None,
        "max_p50_ms": None,
        "max_p99_ms": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# ── pure-layer unit checks (no server) ───────────────────────


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
    assert set(_d(block["summary"])) == {
        "avg_mean_ms",
        "avg_p50_ms",
        "avg_p95_ms",
        "avg_p99_ms",
        "avg_rps",
    }
    assert run_rows[0]["n_success"] == 2


def test_check_thresholds_pass_and_fail() -> None:
    runs = [RunResult(latencies_ms=[10.0, 10.0], wall_time=1.0)]
    assert check_thresholds(runs, min_rps=None, max_p50_ms=1000.0, max_p99_ms=None)
    assert not check_thresholds(runs, min_rps=None, max_p50_ms=0.001, max_p99_ms=None)


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

    journeys = _d(report["journeys"])
    for name in _SMOKE_JOURNEYS:
        assert name in ALL_JOURNEYS
        block = _d(journeys[name])
        assert block["kind"] == "latency"
        assert block["backend"] == "sqlite"
        run_rows = cast(list[dict[str, object]], block["runs"])
        assert run_rows, f"{name} produced no runs"
        # Zero failures — a failure here means the HTTP path itself broke.
        assert run_rows[0]["n_failures"] == 0, f"{name}: {run_rows[0]['failures']}"
        assert cast(float, _d(block["summary"])["avg_p50_ms"]) >= 0.0


@pytest.mark.timeout(180)
async def test_benchmark_smoke_threshold_failure_exits_nonzero() -> None:
    """An impossible p50 bound trips the threshold gate (passed=False)."""
    _, passed = await bench_run.run_benchmark(
        _smoke_args(journeys=["list_sessions"], max_p50_ms=0.0001)
    )
    assert not passed


# ── runner (full-turn) journeys ──────────────────────────────

_RUNNER_JOURNEYS = [
    "session_cold_start",
    "warm_turn",
    "time_to_first_token",
    "interrupt",
    "read_runner_file",
]


@pytest.mark.timeout(300)
async def test_benchmark_smoke_runner_journeys() -> None:
    """Run each full-turn journey once through server + runner + mock LLM.

    First exercise of the ``with_runner=True`` path end-to-end. Tiny counts —
    each cold-start iteration spawns a runner, so this is the slow smoke.
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

    db_uri = f"sqlite:///{tmp_path / 'seed.db'}"

    created = seed_mod.seed(db_uri, sessions=6, items_per_session=4)
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

    # Idempotent: a matching re-seed is a no-op.
    assert seed_mod.seed(db_uri, sessions=6, items_per_session=4) == 0

    # NOTE: seed() builds the store, which runs migrations to the current head,
    # so this test always exercises the live schema — it is the safety net that
    # a schema change hasn't broken seeding (no revision constant to maintain).
