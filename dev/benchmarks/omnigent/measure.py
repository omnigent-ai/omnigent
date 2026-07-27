"""Latency/throughput measurement primitives.

Pure and I/O-free: a :class:`RunResult` accumulates per-operation latencies
and failures for one timed run, :func:`aggregate` folds several runs into the
``runs`` + ``summary`` shape the workspace ETL flattens, and
:func:`check_thresholds` gates a run in CI. Adapted from MLflow's
``dev/benchmarks/gateway/benchmark.py``.
"""

from __future__ import annotations

import math
import statistics
import sys
from dataclasses import dataclass, field

from rich.console import Console
from rich.table import Table

# Rich auto-detects terminal width, but falls back to 80 columns when stdout is
# not a TTY (CI logs, piped output) — which truncates the wider table columns
# (e.g. ``HTTP/op`` → ``HTTP…``). Give the non-interactive path a comfortable
# floor so every header renders in full; real terminals keep auto-detection.
_NONINTERACTIVE_WIDTH = 160
console = Console(width=None if sys.stdout.isatty() else _NONINTERACTIVE_WIDTH)


@dataclass
class RunResult:
    """Latencies and failures collected during one timed run.

    :param latencies_ms: Per-operation wall-clock latency in milliseconds,
        one entry per successful operation.
    :param failures: Failure reason (e.g. ``"HTTP 500"`` / an exception
        class name) mapped to how many times it occurred.
    :param wall_time: Total elapsed seconds for the run, used for throughput.
    :param http_requests: Total HTTP requests the server handled during this
        run's timed region (from the server-side counter, cross-process traffic
        included), or ``None`` when the counter was unavailable. Divided by
        successful ops to report requests-per-op.
    :param route_requests: Per-route breakdown of ``http_requests`` —
        ``"METHOD /route/{template}"`` mapped to how many the server handled
        during the timed region. Empty when uncounted. Feeds the report's
        per-journey network appendix.
    """

    latencies_ms: list[float] = field(default_factory=list)
    failures: dict[str, int] = field(default_factory=dict)
    wall_time: float = 0.0
    http_requests: int | None = None
    route_requests: dict[str, int] = field(default_factory=dict)

    @property
    def n_success(self) -> int:
        """Number of operations that completed without error."""
        return len(self.latencies_ms)

    @property
    def n_failures(self) -> int:
        """Total failed operations across all reasons."""
        return sum(self.failures.values())

    @property
    def throughput(self) -> float:
        """Successful operations per second over the run's wall time."""
        return self.n_success / self.wall_time if self.wall_time > 0 else 0.0

    def record_failure(self, reason: str) -> None:
        """Increment the count for one failure *reason*."""
        self.failures[reason] = self.failures.get(reason, 0) + 1

    def percentile(self, p: float) -> float:
        """Return the *p*-th percentile latency in ms (ceil-index method).

        :param p: Percentile in ``[0, 100]``, e.g. ``99`` for p99.
        :returns: The latency at that percentile, or ``0.0`` when no
            successful operation was recorded.
        """
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        idx = max(0, math.ceil(p / 100 * len(ordered)) - 1)
        return ordered[idx]

    def mean_ms(self) -> float:
        """Mean latency in ms, or ``0.0`` when no operation succeeded."""
        return statistics.mean(self.latencies_ms) if self.latencies_ms else 0.0

    def max_ms(self) -> float:
        """Maximum latency in ms, or ``0.0`` when no operation succeeded."""
        return max(self.latencies_ms) if self.latencies_ms else 0.0

    def requests_per_op(self) -> float | None:
        """Server HTTP requests per successful op, or ``None`` when uncounted.

        ``None`` when the server counter was unavailable or no op succeeded, so
        an uncounted run is distinguishable from a genuine zero.
        """
        if self.http_requests is None or self.n_success == 0:
            return None
        return self.http_requests / self.n_success


def _run_to_dict(result: RunResult) -> dict[str, object]:
    """Flatten one :class:`RunResult` into a JSON-serializable per-run row."""
    return {
        "n_success": result.n_success,
        "n_failures": result.n_failures,
        "failures": dict(result.failures),
        "wall_time_s": result.wall_time,
        "mean_ms": result.mean_ms(),
        "p50_ms": result.percentile(50),
        "p95_ms": result.percentile(95),
        "p99_ms": result.percentile(99),
        "max_ms": result.max_ms(),
        "rps": result.throughput,
        "http_requests": result.http_requests,
        "http_requests_per_op": result.requests_per_op(),
        "route_requests": dict(result.route_requests),
    }


def _route_appendix(ok: list[RunResult]) -> list[dict[str, object]]:
    """Per-route request breakdown across the counted summary runs.

    Sums each route's requests over the runs that recorded a breakdown and
    divides by those runs' successful ops, giving per-op counts grouped by
    endpoint. Sorted by ``per_op`` descending (ties broken by route name) so
    the chattiest endpoints lead. Empty when no run recorded routes.

    :param ok: Summary-eligible runs (at least one success each).
    :returns: ``[{"route", "requests", "per_op"}]`` rows, or ``[]`` when
        uncounted.
    """
    counted = [r for r in ok if r.route_requests]
    if not counted:
        return []
    totals: dict[str, int] = {}
    for r in counted:
        for route, count in r.route_requests.items():
            totals[route] = totals.get(route, 0) + count
    ops = sum(r.n_success for r in counted)
    rows = [
        {"route": route, "requests": count, "per_op": (count / ops if ops else 0.0)}
        for route, count in totals.items()
    ]
    rows.sort(key=lambda row: (-float(row["per_op"]), str(row["route"])))
    return rows


def _summary_runs(results: list[RunResult]) -> list[RunResult]:
    """Runs eligible for the summary — those with at least one success.

    A run in which every operation failed records only ``0.0`` latencies /
    throughput. Averaging those zeros in would drag the summary toward zero
    (a fully-failed run looks like an infinitely fast one), so they are
    excluded from the averages while still kept in the per-run ``runs`` detail.
    """
    return [r for r in results if r.n_success > 0]


def aggregate(results: list[RunResult]) -> dict[str, object]:
    """Fold per-run results into ``{"runs": [...], "summary": {...}}``.

    The ``summary`` averages each metric across the runs that produced at
    least one successful sample (see :func:`_summary_runs`). Its metric keys
    mirror MLflow's gateway benchmark (``avg_mean_ms`` / ``avg_p50_ms`` /
    ``avg_p99_ms`` / ``avg_rps``) plus ``avg_p95_ms``, so the workspace ETL
    that flattens ``summary`` works unchanged; ``runs_total`` / ``runs_ok``
    record how many runs the averages are based on.

    :param results: One :class:`RunResult` per timed run (warmup excluded).
    :returns: A dict with a per-run ``runs`` list and an averaged
        ``summary``. ``summary`` is ``{}`` when *results* is empty; when runs
        exist but all failed, it carries only ``runs_total`` / ``runs_ok`` (no
        metric keys), so a fully-failed journey never fabricates fast numbers.
    """
    runs = [_run_to_dict(r) for r in results]
    if not results:
        return {"runs": runs, "summary": {}}
    ok = _summary_runs(results)
    summary: dict[str, object] = {"runs_total": len(results), "runs_ok": len(ok)}
    if ok:
        summary.update(
            {
                "avg_mean_ms": statistics.mean(r.mean_ms() for r in ok),
                "avg_p50_ms": statistics.mean(r.percentile(50) for r in ok),
                "avg_p95_ms": statistics.mean(r.percentile(95) for r in ok),
                "avg_p99_ms": statistics.mean(r.percentile(99) for r in ok),
                "avg_rps": statistics.mean(r.throughput for r in ok),
            }
        )
        # Requests-per-op, averaged over the runs whose server counter was
        # available. Only added when at least one such run exists, so journeys
        # run without the counter (or before it loaded) carry no network key
        # rather than a misleading zero.
        per_op = [rpo for r in ok if (rpo := r.requests_per_op()) is not None]
        if per_op:
            summary["avg_http_requests_per_op"] = statistics.mean(per_op)
        # Per-route appendix: which endpoints the journey's requests hit, per op.
        # Grouped by route since the count is near-identical across runs.
        appendix = _route_appendix(ok)
        if appendix:
            summary["network_routes"] = appendix
    return {"runs": runs, "summary": summary}


def check_thresholds(
    results: list[RunResult],
    *,
    min_rps: float | None = None,
    max_p50_ms: float | None = None,
    max_p99_ms: float | None = None,
) -> bool:
    """Check averaged results against optional CI thresholds.

    :param results: Timed runs for one journey.
    :param min_rps: Fail if average throughput is below this (req/s).
    :param max_p50_ms: Fail if average p50 latency exceeds this (ms).
    :param max_p99_ms: Fail if average p99 latency exceeds this (ms).
    :returns: ``True`` when every supplied threshold passes (vacuously
        true when none are supplied or *results* is empty). Fully-failed runs
        are excluded from the averages; if a threshold is supplied but no run
        produced a successful sample, the guarantee can't be verified, so this
        fails rather than passing on fabricated zeros.
    """
    if not results:
        return True
    have_thresholds = min_rps is not None or max_p50_ms is not None or max_p99_ms is not None
    ok = _summary_runs(results)
    if not ok:
        if have_thresholds:
            console.print(
                "  [red]THRESHOLD FAILED:[/red] every run failed — no successful"
                " sample to check thresholds against."
            )
            return False
        return True
    avg_rps = statistics.mean(r.throughput for r in ok)
    avg_p50 = statistics.mean(r.percentile(50) for r in ok)
    avg_p99 = statistics.mean(r.percentile(99) for r in ok)
    passed = True

    if min_rps is not None and avg_rps < min_rps:
        console.print(
            f"  [red]THRESHOLD FAILED:[/red] avg throughput {avg_rps:.0f} req/s"
            f" < minimum {min_rps:.0f} req/s"
        )
        passed = False
    if max_p50_ms is not None and avg_p50 > max_p50_ms:
        console.print(
            f"  [red]THRESHOLD FAILED:[/red] avg P50 {avg_p50:.1f} ms"
            f" > maximum {max_p50_ms:.1f} ms"
        )
        passed = False
    if max_p99_ms is not None and avg_p99 > max_p99_ms:
        console.print(
            f"  [red]THRESHOLD FAILED:[/red] avg P99 {avg_p99:.1f} ms"
            f" > maximum {max_p99_ms:.1f} ms"
        )
        passed = False
    return passed


def print_results(journey_name: str, results: list[RunResult]) -> None:
    """Render per-run and averaged metrics for one journey as a rich table.

    :param journey_name: Journey label used as the table title.
    :param results: Timed runs to display.
    """
    table = Table(
        title=journey_name,
        show_header=True,
        header_style="bold cyan",
        box=None,
        padding=(0, 2),
        title_justify="left",
    )
    table.add_column("Run", style="dim", width=5)
    table.add_column("Mean ms", justify="right")
    table.add_column("P50 ms", justify="right")
    table.add_column("P95 ms", justify="right")
    table.add_column("P99 ms", justify="right")
    table.add_column("Max ms", justify="right")
    table.add_column("Req/s", justify="right")
    table.add_column("HTTP/op", justify="right")
    table.add_column("Failures", justify="right")

    def _per_op_str(r: RunResult) -> str:
        rpo = r.requests_per_op()
        return f"{rpo:.1f}" if rpo is not None else "—"

    for i, r in enumerate(results):
        fail_str = f"[red]{r.n_failures}[/red]" if r.n_failures else "0"
        table.add_row(
            str(i + 1),
            f"{r.mean_ms():.1f}",
            f"{r.percentile(50):.1f}",
            f"{r.percentile(95):.1f}",
            f"{r.percentile(99):.1f}",
            f"{r.max_ms():.1f}",
            f"{r.throughput:.0f}",
            _per_op_str(r),
            fail_str,
        )

    # Average only the runs that produced a successful sample, so a
    # fully-failed run doesn't drag the row toward zero (it matches the
    # summary in aggregate()).
    ok = _summary_runs(results)
    if len(results) > 1 and ok:
        per_op = [rpo for r in ok if (rpo := r.requests_per_op()) is not None]
        per_op_avg = f"[bold]{statistics.mean(per_op):.1f}[/bold]" if per_op else "—"
        table.add_section()
        table.add_row(
            "[bold]avg[/bold]",
            f"[bold]{statistics.mean(r.mean_ms() for r in ok):.1f}[/bold]",
            f"[bold]{statistics.mean(r.percentile(50) for r in ok):.1f}[/bold]",
            f"[bold]{statistics.mean(r.percentile(95) for r in ok):.1f}[/bold]",
            f"[bold]{statistics.mean(r.percentile(99) for r in ok):.1f}[/bold]",
            f"[bold]{statistics.mean(r.max_ms() for r in ok):.1f}[/bold]",
            f"[bold]{statistics.mean(r.throughput for r in ok):.0f}[/bold]",
            per_op_avg,
            "",
        )

    console.print()
    console.print(table)
    if len(ok) < len(results):
        console.print(
            f"  [yellow]avg over {len(ok)}/{len(results)} runs[/yellow]"
            " — fully-failed runs excluded."
        )

    combined: dict[str, int] = {}
    for r in results:
        for reason, count in r.failures.items():
            combined[reason] = combined.get(reason, 0) + count
    if combined:
        console.print("  [red]Failure breakdown:[/red]")
        for reason, count in sorted(combined.items(), key=lambda kv: -kv[1]):
            console.print(f"    {reason}: {count}")
