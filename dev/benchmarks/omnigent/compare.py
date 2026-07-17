#!/usr/bin/env python3
"""Compare two benchmark JSON reports for performance regressions.

Usage:
    uv run --no-sync dev/benchmarks/omnigent/compare.py \\
        --baseline nightly.json --candidate pr.json [--threshold 0.20] \\
        [--output-markdown report.md] [--backend sqlite]

Exits 0 if no regression, 1 if regression detected.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console()


def _fmt_ms(v: float | None) -> str:
    return f"{v:.1f}" if v is not None else "—"


def _fmt_delta(v: float | None) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v * 100:.1f}%"


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
        c_p50 = c_summary.get("avg_p50_ms")
        c_p95 = c_summary.get("avg_p95_ms")

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
                }
            )
            continue

        b_summary = b_data.get("summary", {})
        b_p50 = b_summary.get("avg_p50_ms", 0.0)
        b_p95 = b_summary.get("avg_p95_ms", 0.0)

        c_p50 = c_p50 or 0.0
        c_p95 = c_p95 or 0.0
        delta_p50 = (c_p50 - b_p50) / b_p50 if b_p50 > 0 else 0.0
        delta_p95 = (c_p95 - b_p95) / b_p95 if b_p95 > 0 else 0.0

        regression = delta_p50 > threshold or delta_p95 > threshold
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
            }
        )

    return passed, rows


def _status_style(status: str) -> str:
    return {"regression": "red", "new": "cyan", "ok": "green"}.get(status, "")


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
    table.add_column("Base P50 ms", justify="right")
    table.add_column("Cand P50 ms", justify="right")
    table.add_column("Δ P50", justify="right")
    table.add_column("Base P95 ms", justify="right")
    table.add_column("Cand P95 ms", justify="right")
    table.add_column("Δ P95", justify="right")

    for row in rows:
        style = _status_style(row["status"])
        delta_p50_str = _fmt_delta(row["delta_p50"])
        delta_p95_str = _fmt_delta(row["delta_p95"])

        if row["status"] == "regression":
            if row["delta_p50"] is not None and row["delta_p50"] > threshold:
                delta_p50_str = f"[red]{delta_p50_str}[/red]"
            if row["delta_p95"] is not None and row["delta_p95"] > threshold:
                delta_p95_str = f"[red]{delta_p95_str}[/red]"

        table.add_row(
            row["journey"],
            f"[{style}]{row['status']}[/{style}]" if style else row["status"],
            _fmt_ms(row["b_p50"]),
            _fmt_ms(row["c_p50"]),
            delta_p50_str,
            _fmt_ms(row["b_p95"]),
            _fmt_ms(row["c_p95"]),
            delta_p95_str,
        )

    console.print()
    console.print(table)
    console.print()


def build_markdown(rows: list[dict], threshold: float, passed: bool) -> str:
    """Render the comparison rows as a GitHub-flavoured markdown table."""
    lines = [
        "## Benchmark comparison",
        "",
        f"Regression threshold: **{threshold * 100:.0f}%** on avg P50 or avg P95.",
        "",
        "| Journey | Status | Base P50 ms | Cand P50 ms | Δ P50"
        " | Base P95 ms | Cand P95 ms | Δ P95 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]

    for row in rows:
        status = row["status"]
        emoji = {"regression": "🔴", "new": "🆕", "ok": "✅"}.get(status, status)
        b_p50 = _fmt_ms(row["b_p50"])
        c_p50 = _fmt_ms(row["c_p50"])
        d_p50 = _fmt_delta(row["delta_p50"])
        b_p95 = _fmt_ms(row["b_p95"])
        c_p95 = _fmt_ms(row["c_p95"])
        d_p95 = _fmt_delta(row["delta_p95"])
        lines.append(
            f"| {row['journey']} | {emoji} {status} "
            f"| {b_p50} | {c_p50} | {d_p50} "
            f"| {b_p95} | {c_p95} | {d_p95} |"
        )

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

    if not rows:
        console.print("[yellow]No journeys found to compare.[/yellow]")
        return 0

    print_table(rows, args.threshold)

    regressions = [r for r in rows if r["status"] == "regression"]
    new_journeys = [r for r in rows if r["status"] == "new"]

    if new_journeys:
        names = ", ".join(r["journey"] for r in new_journeys)
        console.print(f"[cyan]New journeys (no baseline):[/cyan] {names}")

    if regressions:
        console.print(
            f"[red bold]REGRESSION DETECTED[/red bold] in "
            f"{len(regressions)} journey(s): "
            f"{', '.join(r['journey'] for r in regressions)}"
        )
    else:
        console.print("[green bold]PASS[/green bold] — no regressions detected.")

    if args.output_markdown:
        md = build_markdown(rows, args.threshold, passed)
        args.output_markdown.write_text(md)
        console.print(f"Markdown report written to {args.output_markdown}")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
