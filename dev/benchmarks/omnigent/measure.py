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
from dataclasses import dataclass, field

from rich.console import Console
from rich.table import Table

console = Console()


@dataclass
class RunResult:
    """Latencies and failures collected during one timed run.

    :param latencies_ms: Per-operation wall-clock latency in milliseconds,
        one entry per successful operation.
    :param failures: Failure reason (e.g. ``"HTTP 500"`` / an exception
        class name) mapped to how many times it occurred.
    :param wall_time: Total elapsed seconds for the run, used for throughput.
    """

    latencies_ms: list[float] = field(default_factory=list)
    failures: dict[str, int] = field(default_factory=dict)
    wall_time: float = 0.0

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
    }


def aggregate(results: list[RunResult]) -> dict[str, object]:
    """Fold per-run results into ``{"runs": [...], "summary": {...}}``.

    The ``summary`` averages each metric across runs. Its keys mirror
    MLflow's gateway benchmark (``avg_mean_ms`` / ``avg_p50_ms`` /
    ``avg_p99_ms`` / ``avg_rps``) plus ``avg_p95_ms``, so the workspace ETL
    that flattens ``summary`` works unchanged.

    :param results: One :class:`RunResult` per timed run (warmup excluded).
    :returns: A dict with a per-run ``runs`` list and an averaged
        ``summary`` (empty ``summary`` when *results* is empty).
    """
    runs = [_run_to_dict(r) for r in results]
    if not results:
        return {"runs": runs, "summary": {}}
    summary = {
        "avg_mean_ms": statistics.mean(r.mean_ms() for r in results),
        "avg_p50_ms": statistics.mean(r.percentile(50) for r in results),
        "avg_p95_ms": statistics.mean(r.percentile(95) for r in results),
        "avg_p99_ms": statistics.mean(r.percentile(99) for r in results),
        "avg_rps": statistics.mean(r.throughput for r in results),
    }
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
        true when none are supplied or *results* is empty).
    """
    if not results:
        return True
    avg_rps = statistics.mean(r.throughput for r in results)
    avg_p50 = statistics.mean(r.percentile(50) for r in results)
    avg_p99 = statistics.mean(r.percentile(99) for r in results)
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
    table.add_column("Failures", justify="right")

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
            fail_str,
        )

    if len(results) > 1:
        table.add_section()
        table.add_row(
            "[bold]avg[/bold]",
            f"[bold]{statistics.mean(r.mean_ms() for r in results):.1f}[/bold]",
            f"[bold]{statistics.mean(r.percentile(50) for r in results):.1f}[/bold]",
            f"[bold]{statistics.mean(r.percentile(95) for r in results):.1f}[/bold]",
            f"[bold]{statistics.mean(r.percentile(99) for r in results):.1f}[/bold]",
            f"[bold]{statistics.mean(r.max_ms() for r in results):.1f}[/bold]",
            f"[bold]{statistics.mean(r.throughput for r in results):.0f}[/bold]",
            "",
        )

    console.print()
    console.print(table)

    combined: dict[str, int] = {}
    for r in results:
        for reason, count in r.failures.items():
            combined[reason] = combined.get(reason, 0) + count
    if combined:
        console.print("  [red]Failure breakdown:[/red]")
        for reason, count in sorted(combined.items(), key=lambda kv: -kv[1]):
            console.print(f"    {reason}: {count}")
