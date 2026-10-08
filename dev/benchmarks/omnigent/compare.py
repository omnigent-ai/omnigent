#!/usr/bin/env python3
"""Compare two benchmark JSON reports for performance regressions.

Usage:
    uv run --no-sync dev/benchmarks/omnigent/compare.py \\
        --baseline nightly.json --candidate pr.json [--threshold 0.20] \\
        [--output-markdown report.md] [--backend sqlite]

Exits 0 if no regression, 1 if regression detected, 3 if a ``--require``d
journey was not measured on both sides.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console()

# Below this many samples per run, a ceil-index P95 is exactly the slowest
# sample, so it tracks one-off outliers instead of a tail. Such journeys gate on P50 only.
_MIN_P95_SAMPLES = 20
_UNGATED_P95_NOTE = (
    f"† P95 not gated: fewer than {_MIN_P95_SAMPLES} samples per run, so it is the slowest sample."
)


def _min_run_samples(data: dict) -> int | None:
    """Return the smallest per-run sample count, or ``None`` if no run records it."""
    counts = [
        run["n_success"]
        for run in (data.get("runs") or [])
        if isinstance(run.get("n_success"), int)
    ]
    return min(counts) if counts else None


def _comparison_metric(data: dict, run_key: str, summary_key: str) -> float | None:
    """Return the median run metric, falling back for summary-only reports."""
    values = [
        float(value)
        for run in (data.get("runs") or [])
        if isinstance((value := run.get(run_key)), (int, float)) and math.isfinite(value)
    ]
    if values:
        return statistics.median(values)
    value = data.get("summary", {}).get(summary_key)
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


def _fmt_ms(v: float | None) -> str:
    return f"{v:.1f}" if v is not None else "—"


def _fmt_delta(v: float | None) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v * 100:.1f}%"


def _fmt_req(row: dict) -> str:
    """Format a journey's requests-per-op as ``base→cand`` (or a single value).

    ``—`` when neither side counted; a bare value when only one side has it
    (a new journey, or a baseline predating request counting). A change in the
    count means a round-trip was added or removed — a deterministic signal
    independent of the latency deltas.
    """
    b, c = row.get("b_req"), row.get("c_req")
    if b is None and c is None:
        return "—"
    if b is None:
        return f"{c:.1f}"
    if c is None:
        return f"{b:.1f}"
    return f"{b:.1f}" if b == c else f"{b:.1f}→{c:.1f}"


def compare_reports(
    baseline: dict,
    candidate: dict,
    threshold: float,
    backend: str | None = None,
) -> tuple[bool, list[dict]]:
    """Compare journeys between two reports.

    :param baseline: Parsed baseline JSON report.
    :param candidate: Parsed candidate JSON report.
    :param threshold: Regression threshold as a fraction (e.g. 0.20 = 20%).
    :param backend: If set, only compare journeys whose ``backend`` key matches.
    Run-level medians drive latency comparisons so one noisy timed run cannot
    dominate a three-run report. Summary averages remain the fallback for
    legacy reports that did not retain per-run metrics. P95 is reported but not
    gated when either side has fewer than ``_MIN_P95_SAMPLES`` samples per run.

    :returns: ``(passed, rows)`` where *rows* hold per-journey comparison data.
    """
    baseline_journeys = baseline.get("journeys", {})
    candidate_journeys = candidate.get("journeys", {})
    rows: list[dict] = []
    passed = True

    for name, c_data in candidate_journeys.items():
        if backend is not None and c_data.get("backend") != backend:
            continue

        c_summary = c_data.get("summary", {})
        c_p50 = _comparison_metric(c_data, "p50_ms", "avg_p50_ms")
        c_p95 = _comparison_metric(c_data, "p95_ms", "avg_p95_ms")

        # A skipped journey (or one whose runs all failed) carries no metric
        # keys. Report it as its own status instead of computing a delta off a
        # missing value (which would read as a spurious -100% improvement).
        c_req = c_summary.get("avg_http_requests_per_op")
        if c_p50 is None:
            b_journey = baseline_journeys.get(name, {})
            b_j_summary = b_journey.get("summary", {})
            rows.append(
                {
                    "journey": name,
                    "status": "skipped",
                    "b_p50": _comparison_metric(b_journey, "p50_ms", "avg_p50_ms"),
                    "c_p50": None,
                    "b_p95": _comparison_metric(b_journey, "p95_ms", "avg_p95_ms"),
                    "c_p95": None,
                    "delta_p50": None,
                    "delta_p95": None,
                    "b_req": b_j_summary.get("avg_http_requests_per_op"),
                    "c_req": c_req,
                }
            )
            continue

        if name not in baseline_journeys:
            rows.append(
                {
                    "journey": name,
                    "status": "new",
                    "b_p50": None,
                    "c_p50": c_p50,
                    "b_p95": None,
                    "c_p95": c_p95,
                    "delta_p50": None,
                    "delta_p95": None,
                    "b_req": None,
                    "c_req": c_req,
                }
            )
            continue

        b_data = baseline_journeys[name]
        if backend is not None and b_data.get("backend") != backend:
            # Baseline journey exists but for a different backend — treat as new.
            rows.append(
                {
                    "journey": name,
                    "status": "new",
                    "b_p50": None,
                    "c_p50": c_p50,
                    "b_p95": None,
                    "c_p95": c_p95,
                    "delta_p50": None,
                    "delta_p95": None,
                    "b_req": None,
                    "c_req": c_req,
                }
            )
            continue

        b_summary = b_data.get("summary", {})
        b_p50 = _comparison_metric(b_data, "p50_ms", "avg_p50_ms") or 0.0
        b_p95 = _comparison_metric(b_data, "p95_ms", "avg_p95_ms") or 0.0
        b_req = b_summary.get("avg_http_requests_per_op")

        c_p50 = c_p50 or 0.0
        c_p95 = c_p95 or 0.0
        delta_p50 = (c_p50 - b_p50) / b_p50 if b_p50 > 0 else 0.0
        delta_p95 = (c_p95 - b_p95) / b_p95 if b_p95 > 0 else 0.0

        p95_gated = not any(
            n is not None and n < _MIN_P95_SAMPLES
            for n in (_min_run_samples(b_data), _min_run_samples(c_data))
        )
        regression = delta_p50 > threshold or (p95_gated and delta_p95 > threshold)
        if regression:
            passed = False

        rows.append(
            {
                "journey": name,
                "status": "regression" if regression else "ok",
                "b_p50": b_p50,
                "c_p50": c_p50,
                "delta_p50": delta_p50,
                "b_p95": b_p95,
                "c_p95": c_p95,
                "delta_p95": delta_p95,
                "p95_gated": p95_gated,
                "b_req": b_req,
                "c_req": c_req,
            }
        )

    return passed, rows


def _measured(row: dict) -> bool:
    """Whether both sides produced a real P50 (0.0 is a no-success placeholder)."""
    if row["status"] == "failed":  # the candidate was measured as unusable
        return True
    return row["status"] in ("ok", "regression") and bool(row["b_p50"]) and bool(row["c_p50"])


def unmeasured_journeys(rows: list[dict], required: list[str]) -> list[str]:
    """Return the *required* journeys this comparison could not measure.

    Used to confirm a regression flagged elsewhere: a re-check that never
    measured the flagged journey on both sides cannot clear it.
    """
    by_name = {row["journey"]: row for row in rows}
    return [name for name in required if name not in by_name or not _measured(by_name[name])]


def _status_style(status: str) -> str:
    return {"regression": "red", "new": "cyan", "ok": "green", "skipped": "yellow"}.get(status, "")


def print_table(rows: list[dict], threshold: float) -> None:
    """Render the comparison rows as a rich table."""
    table = Table(
        title=f"Benchmark comparison (regression threshold: {threshold * 100:.0f}%)",
        show_header=True,
        header_style="bold cyan",
        box=None,
        padding=(0, 2),
        title_justify="left",
    )
    table.add_column("Journey", no_wrap=True)
    table.add_column("Status", justify="center")
    table.add_column("Base run-med P50 ms", justify="right")
    table.add_column("Cand run-med P50 ms", justify="right")
    table.add_column("Δ P50", justify="right")
    table.add_column("Base run-med P95 ms", justify="right")
    table.add_column("Cand run-med P95 ms", justify="right")
    table.add_column("Δ P95", justify="right")
    table.add_column("Req/op", justify="right")

    for row in rows:
        style = _status_style(row["status"])
        delta_p50_str = _fmt_delta(row["delta_p50"])
        delta_p95_str = _fmt_delta(row["delta_p95"])

        p95_gated = row.get("p95_gated", True)
        if row["status"] == "regression":
            if row["delta_p50"] is not None and row["delta_p50"] > threshold:
                delta_p50_str = f"[red]{delta_p50_str}[/red]"
            if p95_gated and row["delta_p95"] is not None and row["delta_p95"] > threshold:
                delta_p95_str = f"[red]{delta_p95_str}[/red]"
        if not p95_gated:
            delta_p95_str = f"[dim]{delta_p95_str} †[/dim]"

        table.add_row(
            row["journey"],
            f"[{style}]{row['status']}[/{style}]" if style else row["status"],
            _fmt_ms(row["b_p50"]),
            _fmt_ms(row["c_p50"]),
            delta_p50_str,
            _fmt_ms(row["b_p95"]),
            _fmt_ms(row["c_p95"]),
            delta_p95_str,
            _fmt_req(row),
        )

    console.print()
    console.print(table)
    if any(not row.get("p95_gated", True) for row in rows):
        console.print(f"[dim]{_UNGATED_P95_NOTE}[/dim]")
    console.print()


def build_markdown(rows: list[dict], threshold: float, passed: bool) -> str:
    """Render the comparison rows as a GitHub-flavoured markdown table."""
    lines = [
        "## Benchmark comparison",
        "",
        f"Regression threshold: **{threshold * 100:.0f}%** on run-median P50 or P95.",
        "",
        "| Journey | Status | Base run-med P50 ms | Cand run-med P50 ms | Δ P50"
        " | Base run-med P95 ms | Cand run-med P95 ms | Δ P95 | Req/op |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]

    for row in rows:
        status = row["status"]
        emoji = {"regression": "🔴", "new": "🆕", "ok": "✅", "skipped": "⚠️"}.get(status, status)
        b_p50 = _fmt_ms(row["b_p50"])
        c_p50 = _fmt_ms(row["c_p50"])
        d_p50 = _fmt_delta(row["delta_p50"])
        b_p95 = _fmt_ms(row["b_p95"])
        c_p95 = _fmt_ms(row["c_p95"])
        d_p95 = _fmt_delta(row["delta_p95"])
        if not row.get("p95_gated", True):
            d_p95 += " †"
        lines.append(
            f"| {row['journey']} | {emoji} {status} "
            f"| {b_p50} | {c_p50} | {d_p50} "
            f"| {b_p95} | {c_p95} | {d_p95} | {_fmt_req(row)} |"
        )

    if any(not row.get("p95_gated", True) for row in rows):
        lines += ["", _UNGATED_P95_NOTE]
    lines.append("")
    verdict = (
        "**PASS** — no regressions detected." if passed else "**FAIL** — regression(s) detected."
    )
    lines.append(verdict)
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare benchmark JSON reports for performance regressions."
    )
    parser.add_argument("--baseline", required=True, type=Path, help="Baseline JSON report")
    parser.add_argument("--candidate", required=True, type=Path, help="Candidate JSON report")
    parser.add_argument(
        "--threshold",
        type=float,
        default=1.0,
        help="Regression threshold as a fraction (default 1.0 = 100%%, checks P50 and P95)",
    )
    parser.add_argument(
        "--output-markdown",
        type=Path,
        metavar="FILE",
        help="Write markdown comparison table to FILE",
    )
    parser.add_argument(
        "--backend",
        help="Filter to journeys for this backend only (e.g. sqlite, postgres)",
    )
    parser.add_argument(
        "--require",
        type=lambda s: [p.strip() for p in s.split(",") if p.strip()],
        default=[],
        metavar="A,B",
        help="Journeys that must be measured on both sides; exit 3 if any is not",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        metavar="FILE",
        help="Write {passed, rows, unmeasured} as JSON to FILE",
    )
    args = parser.parse_args(argv)

    baseline = json.loads(args.baseline.read_text())
    candidate = json.loads(args.candidate.read_text())

    console.print(
        f"[bold]Baseline:[/bold]  {args.baseline} (git: {baseline.get('git_sha', 'unknown')[:12]})"
    )
    sha = candidate.get("git_sha", "unknown")[:12]
    console.print(f"[bold]Candidate:[/bold] {args.candidate} (git: {sha})")
    if args.backend:
        console.print(f"[bold]Backend filter:[/bold] {args.backend}")

    passed, rows = compare_reports(baseline, candidate, args.threshold, backend=args.backend)
    unmeasured = unmeasured_journeys(rows, args.require)

    if args.output_json:
        args.output_json.write_text(
            json.dumps({"passed": passed, "rows": rows, "unmeasured": unmeasured}, indent=2)
        )

    if not rows:
        console.print("[yellow]No journeys found to compare.[/yellow]")
        if unmeasured and args.output_markdown:
            args.output_markdown.write_text(
                "No journeys found to compare.\n\n"
                f"**INCOMPLETE** — not measured on both sides: {', '.join(unmeasured)}.\n"
            )
        return 3 if unmeasured else 0

    print_table(rows, args.threshold)

    regressions = [r for r in rows if r["status"] == "regression"]
    new_journeys = [r for r in rows if r["status"] == "new"]
    skipped = [r for r in rows if r["status"] == "skipped"]

    if new_journeys:
        names = ", ".join(r["journey"] for r in new_journeys)
        console.print(f"[cyan]New journeys (no baseline):[/cyan] {names}")

    if skipped:
        names = ", ".join(r["journey"] for r in skipped)
        console.print(f"[yellow]Skipped (no candidate metrics):[/yellow] {names}")

    if regressions:
        console.print(
            f"[red bold]REGRESSION DETECTED[/red bold] in "
            f"{len(regressions)} journey(s): "
            f"{', '.join(r['journey'] for r in regressions)}"
        )
    else:
        console.print("[green bold]PASS[/green bold] — no regressions detected.")

    if unmeasured:
        console.print(
            f"[red bold]INCOMPLETE[/red bold] — required journey(s) not measured on both sides: "
            f"{', '.join(unmeasured)}"
        )

    if args.output_markdown:
        md = build_markdown(rows, args.threshold, passed)
        if unmeasured:
            md += f"\n**INCOMPLETE** — not measured on both sides: {', '.join(unmeasured)}.\n"
        args.output_markdown.write_text(md)
        console.print(f"Markdown report written to {args.output_markdown}")

    if not passed:
        return 1
    return 3 if unmeasured else 0


if __name__ == "__main__":
    sys.exit(main())
