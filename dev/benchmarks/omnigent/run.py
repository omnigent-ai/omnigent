"""Omnigent user-journey benchmark runner.

Boots a real ``omnigent server`` against a SQLite DB (no runner, no LLM),
drives the selected HTTP journeys under load, prints per-journey latency /
throughput tables, and writes a versioned JSON report. Exits non-zero if any
supplied threshold is breached.

Runs in the project venv — it imports ``omnigent`` and ``tests._helpers`` and
spawns the real server, so it is NOT a standalone PEP 723 script. Invoke with
``--no-sync`` so ``uv`` uses the existing environment instead of rebuilding the
project (which triggers a web-UI build that fails in a worktree)::

    uv run --no-sync dev/benchmarks/omnigent/run.py
    uv run --no-sync dev/benchmarks/omnigent/run.py --journeys list_sessions,get_session
    uv run --no-sync dev/benchmarks/omnigent/run.py --requests 500 --concurrency 25 --runs 3
    uv run --no-sync dev/benchmarks/omnigent/run.py --output bench.json --max-p50-ms 25

The JSON is the contract consumed by the workspace Databricks ETL notebook —
see ``README.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import sys
from pathlib import Path

# Allow ``uv run <path>`` (no package context) to import the sibling modules.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from dev.benchmarks.omnigent.environment import BenchEnvironment
from dev.benchmarks.omnigent.journeys import (
    ALL_JOURNEYS,
    Journey,
    resolve_journeys,
    run_latency,
    run_throughput,
)
from dev.benchmarks.omnigent.measure import (
    RunResult,
    aggregate,
    check_thresholds,
    console,
    print_results,
)
from dev.benchmarks.omnigent.schema import build_report

# Harness label stamped in the report: HTTP/DB journeys drive no agent turn;
# runner journeys drive turns through the in-process openai-agents SDK harness.
_HTTP_HARNESS = "http-only"
_RUNNER_HARNESS = "openai-agents"


def _backend_of(database_uri: str | None) -> str:
    """Classify the DB URI into a coarse backend label for the report.

    ``None`` is the harness's throwaway SQLite temp file. Otherwise key off the
    URI scheme so the report (and the workspace dashboard) can group by backend.
    """
    if database_uri is None or database_uri.startswith("sqlite"):
        return "sqlite"
    if database_uri.startswith("postgres"):
        return "postgres"
    if database_uri.startswith("mysql"):
        return "mysql"
    return "other"


def _effective_iterations(journey: Journey, requested: int) -> int:
    """Clamp *requested* iterations down to the journey's ``max_iterations``.

    Full-turn journeys cost ~1s+ per op and cap themselves so a large
    ``--iterations`` (tuned for the millisecond HTTP journeys) doesn't overrun
    the CI time budget. The cap only ever lowers the count, never raises it.
    """
    if journey.max_iterations is not None:
        return min(requested, journey.max_iterations)
    return requested


async def _run_journey(
    journey: Journey, env: BenchEnvironment, args: argparse.Namespace
) -> tuple[str, list[RunResult]]:
    """Run one journey's timed runs, returning its report kind + per-run results.

    A journey runs as throughput when ``--concurrency > 1`` and it is
    concurrency-safe; otherwise as sequential latency.
    """
    as_throughput = args.concurrency > 1 and journey.concurrency_safe
    iterations = _effective_iterations(journey, args.iterations)
    results: list[RunResult] = []
    for _ in range(args.runs):
        if as_throughput:
            results.append(
                await run_throughput(
                    journey,
                    env,
                    requests=args.requests,
                    concurrency=args.concurrency,
                    warmup=args.warmup,
                )
            )
        else:
            results.append(
                await run_latency(journey, env, iterations=iterations, warmup=args.warmup)
            )
    return ("throughput" if as_throughput else "latency"), results


async def run_benchmark(args: argparse.Namespace) -> tuple[dict[str, object], bool]:
    """Run all selected journeys and build the report.

    :returns: ``(report, passed)`` where *passed* is ``False`` if any journey
        breached a supplied threshold.
    """
    journeys = resolve_journeys(args.journeys)
    journey_results: dict[str, dict[str, object]] = {}
    passed = True
    backend = _backend_of(args.database_uri)

    # Any full-turn journey needs the runner + mock LLM. A full env is a
    # superset — HTTP journeys still run against it — so a mixed selection just
    # boots with_runner=True. The harness label reflects what drove the turns.
    # A host-backed journey (session_cold_start) additionally needs a host
    # daemon; with_host is a further superset (it implies with_runner) so a
    # mixed selection that includes it boots the host too.
    with_runner = any(j.needs_runner for j in journeys)
    with_host = any(j.needs_host for j in journeys)
    harness = _RUNNER_HARNESS if with_runner else _HTTP_HARNESS

    async with BenchEnvironment(
        with_runner=with_runner, with_host=with_host, database_uri=args.database_uri
    ) as env:
        for journey in journeys:
            console.print(f"\n[bold]Benchmarking[/bold] {journey.name} [dim]({backend})[/dim]")
            kind, results = await _run_journey(journey, env, args)
            print_results(journey.name, results)
            block = aggregate(results)
            block["kind"] = kind
            block["backend"] = backend
            # Hardcoded per-journey mapping: HTTP journeys are False, full-turn
            # journeys True. Sourced from the journey itself, not the run-level
            # env, so it stays correct in a mixed selection (where with_runner
            # is True for the whole run because *some* journey needs it).
            block["needs_runner"] = journey.needs_runner
            journey_results[journey.name] = block
            if not check_thresholds(
                results,
                min_rps=args.min_rps,
                max_p50_ms=args.max_p50_ms,
                max_p99_ms=args.max_p99_ms,
            ):
                passed = False

    config = {
        "iterations": args.iterations,
        "requests": args.requests,
        "concurrency": args.concurrency,
        "runs": args.runs,
        "warmup": args.warmup,
        "with_runner": with_runner,
        "backend": backend,
    }
    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    report = build_report(
        journey_results,
        generated_at=generated_at,
        config=config,
        harness=harness,
    )
    return report, passed


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="omnigent-benchmark",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--journeys",
        type=lambda s: [p.strip() for p in s.split(",") if p.strip()],
        default=None,
        metavar="A,B,C",
        help=f"Comma-separated journeys to run. Default: all ({', '.join(ALL_JOURNEYS)}).",
    )
    parser.add_argument(
        "--database-uri",
        default=None,
        metavar="URI",
        help="DB the server boots against — a pre-seeded SQLite file, a "
        "postgresql+psycopg://… instance, or a mysql+mysqldb://… instance "
        "(see seed.py). Default: a fresh throwaway SQLite DB (empty — "
        "best-case numbers). The report's `backend` field is derived from this.",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=100,
        metavar="N",
        help="Sequential operations per latency run (default: 100).",
    )
    parser.add_argument(
        "--requests",
        type=int,
        default=500,
        metavar="N",
        help="Total operations per throughput run — used when --concurrency>1 (default: 500).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        metavar="N",
        help="Max in-flight operations. >1 runs concurrency-safe journeys as "
        "throughput (default: 1 = sequential latency).",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=3,
        metavar="N",
        help="Timed runs per journey; results are per-run and averaged (default: 3).",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=10,
        metavar="N",
        help="Warmup operations discarded before each run (default: 10).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        metavar="FILE",
        help="Write the JSON report to FILE (for CI artifact upload).",
    )
    parser.add_argument(
        "--min-rps",
        type=float,
        default=None,
        metavar="N",
        help="Exit 1 if any journey's avg throughput falls below N req/s.",
    )
    parser.add_argument(
        "--max-p50-ms",
        type=float,
        default=None,
        metavar="N",
        help="Exit 1 if any journey's avg P50 latency exceeds N ms.",
    )
    parser.add_argument(
        "--max-p99-ms",
        type=float,
        default=None,
        metavar="N",
        help="Exit 1 if any journey's avg P99 latency exceeds N ms.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    report, passed = asyncio.run(run_benchmark(args))
    if args.output is not None:
        args.output.write_text(json.dumps(report, indent=2))
        console.print(f"\n  Results written to [cyan]{args.output}[/cyan]")
    if not passed:
        console.print("\n[red]One or more thresholds failed.[/red]")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
