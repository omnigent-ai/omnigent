"""Omnigent end-to-end UI benchmark runner.

Boots a real ``omnigent server`` + runner against a zero-latency mock LLM,
builds and serves the web SPA, opens a Playwright Chromium browser, drives the
selected browser journeys, prints per-journey latency tables, and writes a
versioned JSON report. Each journey block carries the shared ``runs``/``summary``
latency shape PLUS a ``network`` sub-object counting the requests the journey
issued (by resource type and by method+normalized-path).

Runs in the project venv — it imports ``omnigent`` and ``tests._helpers`` and
spawns the real server. Invoke with ``--no-sync`` so ``uv`` uses the existing
environment (a plain ``uv run`` would trigger a web-UI build inside the sync
that fails in a worktree). Chromium must be installed
(``uv run playwright install --with-deps chromium``)::

    uv run --no-sync dev/benchmarks/omnigent_ui/run.py
    uv run --no-sync dev/benchmarks/omnigent_ui/run.py --journeys landing_load
    uv run --no-sync dev/benchmarks/omnigent_ui/run.py --runs 3 --iterations 8 --output ui.json
    uv run --no-sync dev/benchmarks/omnigent_ui/run.py --headed --skip-build   # local debug

The JSON is the same contract the HTTP benchmark emits (see
``dev/benchmarks/omnigent/README.md``), with ``harness="web-ui-playwright"`` and
an added per-journey ``network`` block.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import sys
from pathlib import Path

# Allow ``uv run <path>`` (no package context) to import the sibling modules and
# the shared HTTP-benchmark report primitives.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from dev.benchmarks.omnigent.measure import (
    aggregate,
    check_thresholds,
    console,
    print_results,
)
from dev.benchmarks.omnigent.schema import build_report
from dev.benchmarks.omnigent_ui.environment import UIEnvironment
from dev.benchmarks.omnigent_ui.journeys import (
    ALL_UI_JOURNEYS,
    UIJourney,
    build_network_block,
    resolve_ui_journeys,
    run_ui_journey,
    summarize_browser_timing,
)
from dev.benchmarks.omnigent_ui.ui_driver import LaunchOptions, UIDriver

_HARNESS = "web-ui-playwright"


def _backend_of(database_uri: str | None) -> str:
    """Classify the DB URI into a coarse backend label for the report."""
    if database_uri is None or database_uri.startswith("sqlite"):
        return "sqlite"
    if database_uri.startswith("postgres"):
        return "postgres"
    if database_uri.startswith("mysql"):
        return "mysql"
    return "other"


def _effective_iterations(journey: UIJourney, requested: int) -> int:
    """Clamp *requested* iterations down to the journey's ``max_iterations``."""
    return min(requested, journey.max_iterations)


async def run_benchmark(args: argparse.Namespace) -> tuple[dict[str, object], bool]:
    """Run all selected UI journeys and build the report.

    :returns: ``(report, passed)`` where *passed* is ``False`` if any journey
        breached a supplied threshold.
    """
    journeys = resolve_ui_journeys(args.journeys)
    journey_results: dict[str, dict[str, object]] = {}
    passed = True
    backend = _backend_of(args.database_uri)

    async with UIEnvironment(database_uri=args.database_uri, skip_build=args.skip_build) as env:
        launch = LaunchOptions.from_env(headed=args.headed)
        async with UIDriver(env.base_url, launch) as driver:
            for journey in journeys:
                console.print(f"\n[bold]Benchmarking[/bold] {journey.name} [dim]({backend})[/dim]")
                iterations = _effective_iterations(journey, args.iterations)
                results, net_reps, timings = await run_ui_journey(
                    journey,
                    env,
                    driver,
                    runs=args.runs,
                    iterations=iterations,
                    warmup=args.warmup,
                )
                print_results(journey.name, results)

                block = aggregate(results)
                block["kind"] = "ui-latency"
                block["backend"] = backend
                block["network"] = build_network_block(net_reps)
                browser_timing = summarize_browser_timing(timings)
                if browser_timing:
                    block["browser_timing"] = browser_timing
                journey_results[journey.name] = block

                if not check_thresholds(
                    results,
                    min_rps=None,
                    max_p50_ms=args.max_p50_ms,
                    max_p99_ms=args.max_p99_ms,
                ):
                    passed = False
        resource_usage = env.resource_usage

    config = {
        "iterations": args.iterations,
        "runs": args.runs,
        "warmup": args.warmup,
        "with_runner": True,
        "with_host": False,
        "backend": backend,
        "headed": args.headed,
        "network_grouping": ["resource_type", "method_path"],
    }
    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    report = build_report(
        journey_results,
        generated_at=generated_at,
        config=config,
        harness=_HARNESS,
        resource_usage=resource_usage,
    )
    return report, passed


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="omnigent-ui-benchmark",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--journeys",
        type=lambda s: [p.strip() for p in s.split(",") if p.strip()],
        default=None,
        metavar="A,B,C",
        help=f"Comma-separated journeys to run. Default: all ({', '.join(ALL_UI_JOURNEYS)}).",
    )
    parser.add_argument(
        "--database-uri",
        default=None,
        metavar="URI",
        help="DB the server boots against. Default: a fresh throwaway SQLite DB. "
        "The report's `backend` field is derived from this.",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=8,
        metavar="N",
        help="Timed browser operations per run, clamped per journey (default: 8).",
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
        default=2,
        metavar="N",
        help="Warmup operations discarded before each journey's timed runs (default: 2).",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        default=False,
        help="Launch a visible browser (local debugging only; CI runs headless).",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        default=False,
        help="Reuse the existing SPA build in omnigent/server/static/web-ui/ "
        "instead of rebuilding. Fails if no build is present.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        metavar="FILE",
        help="Write the JSON report to FILE (for CI artifact upload).",
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
