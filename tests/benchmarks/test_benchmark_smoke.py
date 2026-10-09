"""Fast smoke test for the HTTP-journey benchmark harness.

Runs the real harness (boots an ``omnigent server``, no runner / no LLM /
no Databricks) with tiny counts and asserts the report shape and threshold
logic. Runs on the normal CI lane — no creds, no ``databricks`` marker.

The measurement and schema layers also get direct unit checks so their logic
is covered without paying the server-boot cost.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

import httpx
import psutil
import pytest

from dev.benchmarks.omnigent import journeys as bench_journeys
from dev.benchmarks.omnigent import run as bench_run
from dev.benchmarks.omnigent.environment import (
    BenchEnvironment,
    GatedTurn,
    _close_reader,
    _sse_session_status,
    _stop_process_group,
    bench_child_environ,
)
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
    "native_hook_spawn",
    *(name for name in ALL_JOURNEYS if name.startswith("project_order_")),
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
    """Every full-turn journey caps its iterations; HTTP journeys do not.

    Non-runner journeys may also declare a cap when they are inherently slow
    (e.g. cli_startup which takes ~10s per iteration).
    """
    for journey in ALL_JOURNEYS.values():
        if journey.needs_runner:
            assert journey.max_iterations is not None, journey.name


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


@pytest.mark.asyncio
async def test_latency_max_warmup_clamps_requested_warmup() -> None:
    """``max_warmup`` lowers ``--warmup`` for journeys where repeats warm nothing."""
    calls: list[str] = []

    async def _measure(_env: BenchEnvironment, _ctx: object) -> None:
        calls.append("measure")

    journey = Journey(name="cold", kind="latency", measure=_measure, max_warmup=1)
    result = await run_latency(journey, cast(BenchEnvironment, object()), iterations=3, warmup=10)

    assert result.n_success == 3
    assert len(calls) == 4  # 1 warmup + 3 timed


@pytest.mark.asyncio
async def test_latency_validate_runs_off_the_clock() -> None:
    """Slow per-sample cleanup in ``validate`` (e.g. killing a CLI) is not timed."""

    async def _measure(_env: BenchEnvironment, _ctx: object) -> None:
        return None

    async def _validate(_env: BenchEnvironment, _ctx: object) -> None:
        await asyncio.sleep(0.5)

    journey = Journey(name="cleanup", kind="latency", measure=_measure, validate=_validate)
    result = await run_latency(journey, cast(BenchEnvironment, object()), iterations=2, warmup=0)

    assert result.n_success == 2
    assert max(result.latencies_ms) < 250  # timing validate's 0.5s sleep would exceed this


@pytest.mark.asyncio
async def test_turn_session_rotates_every_n_samples() -> None:
    """Turn journeys move to a fresh warmed session so history stays bounded."""
    created: list[str] = []
    warmed: list[str] = []

    class _Env:
        async def create_bound_session(self, agent_id: str) -> str:
            created.append(agent_id)
            return f"s{len(created)}"

        async def drive_turn(self, session_id: str, _text: str) -> None:
            warmed.append(session_id)

    ctx = bench_journeys._TurnSession(agent_id="a", session_id="s0")
    every = bench_journeys._TURN_SESSION_SAMPLES
    for _ in range(2 * every + 1):
        await bench_journeys._rotate_turn_session(cast(BenchEnvironment, _Env()), ctx)

    assert created == ["a", "a"]
    assert warmed == ["s1", "s2"]  # each fresh session gets its warm-up turn
    assert ctx.session_id == "s2"
    assert ctx.samples == 1


@pytest.mark.asyncio
async def test_interrupt_moves_to_a_fresh_session_after_a_failed_sample() -> None:
    """A parked turn left by a failed op could land its marker on the next sample."""
    calls: list[str] = []

    class _Env:
        async def finish_gated_turn(self, _turn: object) -> None:
            calls.append("finish")

        async def create_bound_session(self, _agent_id: str) -> str:
            calls.append("create")
            return "fresh"

        async def drive_turn(self, _session_id: str, _text: str) -> None:
            calls.append("warm")

        async def cancellation_markers(self, session_id: str) -> int:
            calls.append(f"markers:{session_id}")
            return 0

        async def start_gated_turn(self, session_id: str) -> str:
            calls.append(f"gate:{session_id}")
            return "turn"

    ctx = bench_journeys._TurnSession(agent_id="a", session_id="old", samples=3)
    ctx.gated = cast(GatedTurn, object())
    await bench_journeys._prepare_interrupt(cast(BenchEnvironment, _Env()), ctx)

    assert calls == ["finish", "create", "warm", "markers:fresh", "gate:fresh"]


def _status_line(status: str) -> bytes:
    return f'data: {{"type": "session.status", "status": "{status}"}}\n\n'.encode()


def _gated_env(statuses: list[str], end_stream: asyncio.Event) -> BenchEnvironment:
    """A runner-mode env whose SSE stream emits *statuses*, then ends on *end_stream*."""

    async def _stream():
        for status in statuses:
            yield _status_line(status)
        await end_stream.wait()

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, content=_stream())
        return httpx.Response(202)

    env = BenchEnvironment(with_runner=True)
    env.client = httpx.AsyncClient(
        base_url="http://bench", transport=httpx.MockTransport(_handler)
    )

    async def _no_mock(*_args: object, **_kwargs: object) -> None:
        return None

    env.configure_mock = _no_mock  # type: ignore[method-assign]
    env._wait_gate_pending = _no_mock  # type: ignore[method-assign]
    return env


@pytest.mark.asyncio
async def test_interrupt_fails_when_the_stream_ends_without_idle() -> None:
    """A stream closing after the interrupt is not a cancel: the sample must fail."""
    end_stream = asyncio.Event()
    env = _gated_env(["running"], end_stream)
    assert env.client is not None
    try:
        turn = await env.start_gated_turn("s1", timeout=5)
        end_stream.set()  # EOF with no idle status
        with pytest.raises(RuntimeError, match="did not settle cleanly"):
            await env.interrupt_gated_turn(turn, timeout=5)
        await env.finish_gated_turn(turn)
    finally:
        await env.client.aclose()


@pytest.mark.asyncio
async def test_gated_turn_fails_promptly_when_the_turn_settles_before_the_gate() -> None:
    """A turn that fails before calling the LLM must not wait out the gate timeout."""
    env = _gated_env(["running", "failed"], asyncio.Event())
    assert env.client is not None

    async def _never_pending(*, timeout: float) -> None:
        await asyncio.Event().wait()

    env._wait_gate_pending = _never_pending  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="settled before it was interrupted"):
            await asyncio.wait_for(env.start_gated_turn("s1", timeout=60), timeout=5)
    finally:
        await env.client.aclose()


@pytest.mark.asyncio
async def test_interrupt_sample_ends_at_idle_not_after_stream_cleanup() -> None:
    """Closing the session stream after ``idle`` must not count toward the sample."""
    interrupted, release = asyncio.Event(), asyncio.Event()

    class _SlowCloseStream(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[override]
            yield _status_line("running")
            await interrupted.wait()
            yield _status_line("idle")
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            await release.wait()  # response cleanup blocks until released

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, stream=_SlowCloseStream())
        if b'"interrupt"' in request.content:
            interrupted.set()
        return httpx.Response(202)

    env = _gated_env([], asyncio.Event())
    assert env.client is not None
    await env.client.aclose()
    env.client = httpx.AsyncClient(
        base_url="http://bench", transport=httpx.MockTransport(_handler)
    )
    try:
        turn = await env.start_gated_turn("s1", timeout=5)
        await asyncio.wait_for(env.interrupt_gated_turn(turn, timeout=5), timeout=2)
        release.set()
        await env.finish_gated_turn(turn)
    finally:
        release.set()
        await env.client.aclose()


@pytest.mark.asyncio
async def test_close_cli_startup_terminates_the_stashed_child_once() -> None:
    terminated: list[bool] = []

    class _Child:
        def terminate(self, force: bool) -> None:
            terminated.append(force)

    ctx: dict[str, object] = {"child": _Child()}
    env = cast(BenchEnvironment, object())
    await bench_journeys._close_cli_startup(env, ctx)
    await bench_journeys._close_cli_startup(env, ctx)  # a second cleanup is harmless

    assert terminated == [True]


@pytest.mark.asyncio
async def test_interrupt_refuses_a_turn_that_already_settled() -> None:
    """Timing an interrupt on a settled turn would measure nothing; it must fail."""
    posted: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        posted.append(request.url.path)
        return httpx.Response(202)

    env = BenchEnvironment(with_runner=True)
    env.client = httpx.AsyncClient(
        base_url="http://bench", transport=httpx.MockTransport(_handler)
    )
    idle = asyncio.Event()
    idle.set()
    turn = GatedTurn("s1", asyncio.create_task(asyncio.sleep(0)), idle, {})
    try:
        with pytest.raises(RuntimeError, match="settled before the interrupt"):
            await env.interrupt_gated_turn(turn)
    finally:
        await env.client.aclose()
        await turn.reader

    assert posted == []


@pytest.mark.asyncio
async def test_interrupt_returns_when_the_gated_turn_settles() -> None:
    idle = asyncio.Event()

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sessions/s1/events"
        idle.set()  # the stream reader would see the turn go idle
        return httpx.Response(202)

    env = BenchEnvironment(with_runner=True)
    env.client = httpx.AsyncClient(
        base_url="http://bench", transport=httpx.MockTransport(_handler)
    )
    turn = GatedTurn("s1", asyncio.create_task(asyncio.sleep(0)), idle, {})
    try:
        await env.interrupt_gated_turn(turn, timeout=5)
    finally:
        await env.client.aclose()
        await turn.reader


@pytest.mark.asyncio
@pytest.mark.parametrize("finished", [True, False])
async def test_close_reader_lets_a_finished_reader_close_its_stream(finished: bool) -> None:
    """Cancelling a reader mid-close leaked a pooled connection per turn."""
    saw_terminal = asyncio.Event()
    closed: list[bool] = []

    async def _reader() -> None:
        try:
            if not finished:
                await asyncio.Event().wait()  # still waiting on the stream
            saw_terminal.set()
            await asyncio.sleep(0.01)  # stands in for ``response.aclose()``
            closed.append(True)
        except asyncio.CancelledError:
            closed.append(False)
            raise

    reader = asyncio.create_task(_reader())
    if finished:
        await saw_terminal.wait()
    else:
        await asyncio.sleep(0)
    await _close_reader(reader, finished=finished)

    assert reader.done()
    assert closed == [finished]


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
        if name == "native_hook_spawn":
            # Subprocess-only by design: a nonzero request count or any
            # attributed route would mean the hook spawn path grew a server
            # round-trip.
            assert cast(float, per_op) == 0.0, f"{name}: {per_op} requests/op"
            assert not _d(block["summary"]).get("network_routes")
            continue
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
    projects = SqlAlchemyProjectStore(db_uri).list(user_id=RESERVED_USER_LOCAL)
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


# ── report_markdown: the CI job-summary matrix renderer ──────────────────────


def _markdown_report(journeys: dict[str, dict], backend: str = "sqlite") -> dict:
    """A minimal run.py-shaped report carrying just what the renderer reads."""
    return {
        "git_sha": "abcdef1234567890",
        "harness": "openai-agents",
        "config": {"iterations": 100, "runs": 3, "warmup": 10, "backend": backend},
        "journeys": journeys,
    }


def _journey_block(**summary: object) -> dict:
    """A measured journey block with the given summary metrics."""
    return {"kind": "latency", "runs": [], "summary": {"runs_total": 3, "runs_ok": 3, **summary}}


def test_report_markdown_renders_journey_matrix() -> None:
    """One report renders a journey × metric table with formatted values.

    The renderer feeds $GITHUB_STEP_SUMMARY; a broken cell silently degrades
    the CI matrix, so assert the exact row content, not just "contains name".
    """
    from dev.benchmarks.omnigent.report_markdown import build_markdown

    report = _markdown_report(
        {
            "session_cold_start": _journey_block(
                avg_mean_ms=1234.5,
                avg_p50_ms=1200.0,
                avg_p95_ms=1500.25,
                avg_p99_ms=1600.0,
                avg_rps=0.8,
            ),
        }
    )
    md = build_markdown([("sqlite", report)], title="Host session benchmark")

    assert "## Host session benchmark" in md
    assert "### sqlite" in md
    # Caption line carries the run context.
    assert "_100 iterations × 3 runs · warmup 10 · openai-agents · `abcdef12`_" in md
    assert "| session_cold_start | 1234.5 | 1200.0 | 1500.2 | 1600.0 | 1 | 3/3 |  |" in md


def test_report_markdown_marks_skipped_and_failed_journeys() -> None:
    """Skipped journeys carry the skip reason; all-failed journeys are flagged.

    A skipped journey has an empty summary, and a fully-failed one has counts
    but no metric keys — both must render as explicit markers, never as fast
    zeros.
    """
    from dev.benchmarks.omnigent.report_markdown import build_markdown

    report = _markdown_report(
        {
            "session_cold_start": {
                "kind": "latency",
                "runs": [],
                "summary": {},
                "skipped": True,
                "error": "RuntimeError: host not\nready",
            },
            "warm_turn": {
                "kind": "latency",
                "runs": [],
                "summary": {"runs_total": 3, "runs_ok": 0},
            },
        }
    )
    md = build_markdown([("sqlite", report)])

    assert (
        "| session_cold_start | — | — | — | — | — | — | ⚠️ skipped — RuntimeError: host not ready |"
        in md
    )
    assert "| warm_turn | — | — | — | — | — | 0/3 | ❌ every run failed |" in md


def test_report_markdown_cross_report_matrix() -> None:
    """Multiple reports lead with a journey × report P50 matrix.

    The nightly renders one report per backend; the cross matrix is what
    makes them comparable at a glance, including journeys missing from one
    backend (rendered as —).
    """
    from dev.benchmarks.omnigent.report_markdown import build_markdown

    sqlite = _markdown_report(
        {
            "create_session": _journey_block(avg_p50_ms=12.5),
            "session_cold_start": _journey_block(avg_p50_ms=1200.0),
        },
        backend="sqlite",
    )
    postgres = _markdown_report(
        {"create_session": _journey_block(avg_p50_ms=20.0)}, backend="postgresql"
    )
    md = build_markdown([("sqlite", sqlite), ("postgresql", postgres)])

    assert "### P50 across reports" in md
    assert "| Journey | sqlite P50 ms | postgresql P50 ms |" in md
    assert "| create_session | 12.5 | 20.0 |" in md
    assert "| session_cold_start | 1200.0 | — |" in md
    # Per-report sections still follow the cross matrix.
    assert "### sqlite" in md
    assert "### postgresql" in md


# ── harness hygiene (process groups, caller env, terminals) ──


def test_bench_child_environ_drops_caller_session_vars() -> None:
    """Bench processes must not inherit the enclosing Omnigent session's identity."""
    caller = {
        "PATH": "/usr/bin",
        "OMNIGENT_SKIP_WEB_UI": "true",
        "OMNIGENT_RUNNER_ZYGOTE": "0",  # a tuning knob, kept
        "OMNIGENT_RUNNER_ID": "runner_caller",
        "OMNIGENT_RUNNER_DELEGATED_AUTH": "token",
        "RUNNER_SERVER_URL": "http://caller:6767",
        "OMNIGENT_PROCESS_LOG_FILE": "/home/u/.omnigent/logs/runner/runner-caller.log",
        "OMNIGENT_TERMINAL_LAUNCH_ID": "launch_caller",
        "OMNIGENT_REMOTE_AUTH_TOKEN": "caller-bearer",
        "OMNIGENT_DATABRICKS_EXTRA_HEADERS": '{"X-Routing": "caller"}',
        "OMNIGENT_HARNESS_AUTH_TOKEN": "caller-harness-token",
        "OMNIGENT_SESSION_ID": "conv_caller",
        "OMNIGENT_POLICY_URL": "http://caller:6767",
    }

    assert bench_child_environ(caller) == {
        "PATH": "/usr/bin",
        "OMNIGENT_SKIP_WEB_UI": "true",
        "OMNIGENT_RUNNER_ZYGOTE": "0",
    }


def test_caller_session_env_literals_match_the_runtime() -> None:
    """Names kept as literals (no cheap public constant) must track the runtime."""
    from dev.benchmarks.omnigent.environment import _CALLER_SESSION_ENV_VARS
    from omnigent.chat import _REMOTE_AUTH_TOKEN_ENV
    from omnigent.runner.native import orchestration
    from omnigent.runtime.harnesses.paths import HARNESS_TMP_PARENT_ENV_VAR
    from omnigent.runtime.harnesses.process_manager import _HARNESS_AUTH_TOKEN_ENV

    assert {
        _REMOTE_AUTH_TOKEN_ENV,
        HARNESS_TMP_PARENT_ENV_VAR,
        _HARNESS_AUTH_TOKEN_ENV,
    } <= _CALLER_SESSION_ENV_VARS
    # The native policy hooks' env is built from literals in orchestration.
    source = Path(orchestration.__file__).read_text()
    for name in ("OMNIGENT_SESSION_ID", "OMNIGENT_POLICY_URL"):
        assert f'policy_env["{name}"]' in source
        assert name in _CALLER_SESSION_ENV_VARS


def _wait_gone(proc: psutil.Process, timeout: float = 5.0) -> bool:
    """Wait for *proc* to exit; a recycled pid doesn't count as it running."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX-only")
def test_stop_process_group_kills_descendants_that_ignore_sigterm(tmp_path: Path) -> None:
    """Stopping a bench child also kills what it forked, even past SIGTERM.

    That includes a child its SIGTERM handler forks just before it exits.
    """
    late_pid_file = tmp_path / "late.pid"
    ignore_term = '(trap "" TERM; exec sleep 60) &'
    script = (
        f"trap '{ignore_term} echo $! > \"$1\"; exit 0' TERM; "
        f"{ignore_term} echo $!; "
        "while :; do sleep 0.1; done"
    )
    proc = subprocess.Popen(
        ["sh", "-c", script, "sh", str(late_pid_file)],
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    assert proc.stdout is not None
    descendants = [psutil.Process(int(proc.stdout.readline()))]
    try:
        _stop_process_group(proc)

        assert proc.poll() is not None
        with contextlib.suppress(psutil.NoSuchProcess):  # already killed and reaped
            descendants.append(psutil.Process(int(late_pid_file.read_text())))
        for descendant in descendants:
            assert _wait_gone(descendant)
    finally:
        for descendant in descendants:
            with contextlib.suppress(psutil.Error):
                descendant.kill()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")
def test_teardown_kills_terminals_under_the_bench_tmpdir() -> None:
    """A runner's leftover REPL tmux server under the bench TMPDIR is killed."""
    env = BenchEnvironment()
    terminal_dir = env._child_tmp / "omnigent-terminal-test"
    terminal_dir.mkdir(parents=True)
    socket_path = terminal_dir / "tmux.sock"
    subprocess.run(["tmux", "-S", str(socket_path), "new-session", "-d", "sleep 60"], check=True)
    # Teardown deletes the socket, so only the server's pid can show it exited.
    server = psutil.Process(
        int(
            subprocess.run(
                ["tmux", "-S", str(socket_path), "display-message", "-p", "#{pid}"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
    )
    try:
        env._stop()

        assert _wait_gone(server)
    finally:
        with contextlib.suppress(psutil.Error):
            server.kill()
        shutil.rmtree(env._tmp, ignore_errors=True)


@pytest.mark.asyncio
async def test_failed_start_stops_what_it_started() -> None:
    """``__aexit__`` never runs when ``__aenter__`` raises, so it must clean up itself."""
    env = BenchEnvironment()
    stopped: list[bool] = []

    def _start() -> None:
        raise RuntimeError("server never became healthy")

    env._start = _start  # type: ignore[method-assign]
    env._stop = lambda: stopped.append(True)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="never became healthy"):
        await env.__aenter__()
    assert stopped == [True]


@pytest.mark.asyncio
async def test_failure_after_start_still_tears_down() -> None:
    """A failure later in ``__aenter__`` (here the mock call) stops what started."""
    env = BenchEnvironment(with_runner=True)
    stopped: list[bool] = []

    async def _mock_post(_path: str, _body: dict[str, object]) -> None:
        raise httpx.ConnectError("mock LLM crashed")

    env._start = lambda: None  # type: ignore[method-assign]
    env._stop = lambda: stopped.append(True)  # type: ignore[method-assign]
    env._mock_post = _mock_post  # type: ignore[method-assign]

    with pytest.raises(httpx.ConnectError):
        await env.__aenter__()
    assert stopped == [True]
    assert env.client is not None and env.client.is_closed


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX-only")
@pytest.mark.asyncio
async def test_cancelled_start_cannot_leave_a_child_behind() -> None:
    """Cancelling startup mid-spawn: teardown stops what started, refuses the rest."""
    env = BenchEnvironment()
    spawned = threading.Event()
    release = threading.Event()
    later_spawn: list[str] = []

    def _start() -> None:
        env._spawn(["sleep", "60"])
        spawned.set()
        release.wait(timeout=30)
        try:
            env._spawn(["sleep", "60"])
            later_spawn.append("spawned")
        except RuntimeError:
            later_spawn.append("refused")

    env._start = _start  # type: ignore[method-assign]
    task = asyncio.create_task(env.__aenter__())
    try:
        await asyncio.to_thread(spawned.wait, 30)
        first_child = env._children[0]
        task.cancel()
        await _wait_until(lambda: env._stopping)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The thread running _start outlives the cancelled await; let it finish.
        await _wait_until(lambda: bool(later_spawn))

        assert later_spawn == ["refused"]
        assert len(env._children) == 1
        assert first_child.poll() is not None
    finally:
        release.set()
        for child in env._children:
            # An unreaped leader keeps its group id from being recycled.
            if child.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)


@pytest.mark.asyncio
async def test_start_cancelled_before_it_begins_creates_nothing() -> None:
    """Teardown that runs first must not leave a temp dir for the late start to fill."""
    env = BenchEnvironment()
    entered = threading.Event()
    release = threading.Event()
    outcome: list[str] = []
    real_start = env._start

    def _paused_start() -> None:
        entered.set()
        release.wait(timeout=30)
        try:
            real_start()
        except RuntimeError as exc:
            outcome.append(str(exc))
            raise

    env._start = _paused_start  # type: ignore[method-assign]
    task = asyncio.create_task(env.__aenter__())
    try:
        await asyncio.to_thread(entered.wait, 30)
        task.cancel()
        await _wait_until(lambda: env._stopping)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _wait_until(lambda: bool(outcome))

        assert outcome == ["benchmark environment is stopping"]
        assert not env._tmp.exists()
    finally:
        release.set()
        shutil.rmtree(env._tmp, ignore_errors=True)


async def _wait_until(condition: Callable[[], bool], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX-only")
@pytest.mark.timeout(180)
def test_sigterm_mid_run_leaves_no_processes(tmp_path: Path) -> None:
    """``timeout``/``kill`` on run.py still tears down the server it booted."""
    proc = subprocess.Popen(
        [
            sys.executable,
            str(Path(bench_run.__file__)),
            "--journeys",
            "list_sessions",
            "--iterations",
            "100000",
            "--runs",
            "1",
        ],
        env={**bench_child_environ(), "HOME": str(tmp_path)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    descendants: list[psutil.Process] = []
    try:
        for line in proc.stdout:
            if "Benchmarking" in line:
                break
        descendants = psutil.Process(proc.pid).children(recursive=True)
        assert descendants, "run.py booted no server"
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=60)
        assert proc.returncode == 128 + signal.SIGTERM
        assert all(_wait_gone(child) for child in descendants)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        for child in descendants:
            with contextlib.suppress(psutil.Error):
                child.kill()


class _CliStubEnv:
    """The slice of BenchEnvironment the cli_startup journey uses."""

    base_url = "http://127.0.0.1:9"

    def __init__(self, tmp: Path) -> None:
        self._tmp = tmp

    def child_env(self) -> dict[str, str]:
        return {"PATH": os.environ.get("PATH", ""), "TMPDIR": str(self._tmp)}


# What the REPL paints once ready: the toolbar, or the prompt where it's suppressed.
_TOOLBAR_READY = " polly \\302\\267  ready \\n"
_PROMPT_READY = "\\342\\235\\257 "


def _fake_cli(
    tmp_path: Path, calls: Path, *, drain: str = "exit 0", ready: str = _TOOLBAR_READY
) -> Path:
    """A stand-in `omnigent`: logs each call, and prints a slow REPL for `polly`.

    :param drain: Shell run for a draining `host stop --all` (no `--daemon-only`).
    :param ready: What `polly` prints once its REPL is ready.
    """
    fake = tmp_path / "omnigent"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "$* | $OMNIGENT_DATA_DIR | $OMNIGENT_CONFIG_HOME" >> {calls}\n'
        f'if [ "$*" = "host stop --all" ]; then {drain}; fi\n'
        'if [ "$1" = polly ]; then\n'
        "  printf 'Launching your agent\\n'; sleep 0.5\n"
        f"  printf '{ready}'; sleep 30\n"
        "fi\n"
    )
    fake.chmod(0o755)
    return fake


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="fake CLI is a POSIX shell script")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("drain", "falls_back"),
    [
        pytest.param("exit 0", False, id="drained"),
        pytest.param("exit 1", True, id="drain-failed"),
        pytest.param("exec sleep 5", True, id="drain-timed-out"),
    ],
)
async def test_cli_startup_stops_only_its_own_daemons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drain: str, falls_back: bool
) -> None:
    """cli_startup must never run `omnigent stop`, which kills any local server.

    Teardown drains the last session first; if that fails or hangs, it still
    stops the journey's daemons.
    """
    calls = tmp_path / "calls.txt"
    monkeypatch.setenv("OMNIGENT_BIN", str(_fake_cli(tmp_path, calls, drain=drain)))
    monkeypatch.setattr(bench_journeys, "_CLI_DRAIN_TIMEOUT_S", 0.5)
    env = cast(BenchEnvironment, _CliStubEnv(tmp_path))
    journey = ALL_JOURNEYS["cli_startup"]

    ctx = await journey.run_setup(env)
    cli_env = cast(dict[str, dict[str, str]], ctx)["env"]
    await journey.run_prepare(env, ctx)
    await journey.run_teardown(env, ctx)

    scoped = f"{cli_env['OMNIGENT_DATA_DIR']} | {cli_env['OMNIGENT_CONFIG_HOME']}"
    assert calls.read_text().splitlines() == [
        f"host stop --all --daemon-only | {scoped}",  # prepare: daemons only, fast
        f"host stop --all | {scoped}",  # teardown: drain the last session first
        *([f"host stop --all --daemon-only | {scoped}"] if falls_back else []),
    ]
    assert Path(cli_env["OMNIGENT_DATA_DIR"]).is_relative_to(tmp_path)
    # Pre-set theme, so the first-run picker doesn't stand in for the REPL.
    assert "theme: light" in (Path(cli_env["OMNIGENT_CONFIG_HOME"]) / "config.yaml").read_text()


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="fake CLI is a POSIX shell script")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ready", [pytest.param(_TOOLBAR_READY, id="toolbar"), pytest.param(_PROMPT_READY, id="prompt")]
)
async def test_cli_startup_times_until_the_repl_is_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ready: str
) -> None:
    """The spinner line is not readiness; the timed CLI runs in the scoped dirs."""
    calls = tmp_path / "calls.txt"
    monkeypatch.setenv("OMNIGENT_BIN", str(_fake_cli(tmp_path, calls, ready=ready)))
    env = cast(BenchEnvironment, _CliStubEnv(tmp_path))
    journey = ALL_JOURNEYS["cli_startup"]

    result = await run_latency(journey, env, iterations=1, warmup=0)

    assert result.n_success == 1, result.failures
    assert result.latencies_ms[0] >= 500  # waited past "Launching your agent"
    polly = [line for line in calls.read_text().splitlines() if line.startswith("polly")]
    assert len(polly) == 1
    assert str(tmp_path) in polly[0].split(" | ")[1]
