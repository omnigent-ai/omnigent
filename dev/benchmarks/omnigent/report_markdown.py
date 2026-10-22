"""Render ``run.py`` JSON reports as GitHub-flavoured markdown result matrices.

Where ``compare.py`` renders a baseline-vs-candidate regression table, this
renders the absolute numbers of one or more standalone reports — the
journey × metric matrix a CI job appends to ``$GITHUB_STEP_SUMMARY``. Given
several reports (e.g. the nightly's sqlite / postgres / mysql legs) it also
leads with a cross-report P50 matrix so the backends can be compared side by
side.

Usage::

    report_markdown.py REPORT.json [REPORT.json ...] [--title TEXT]

Prints markdown to stdout. Report labels come from each report's
``config.backend``; when two reports share a backend the filename stem is
appended to keep the columns distinguishable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Keep table cells one-line and skimmable: a skip reason is an exception
# rendering, which can run long and embed newlines that would break the row.
_MAX_NOTE_LEN = 100


def _fmt_ms(value: object) -> str:
    """Format a millisecond metric, or ``—`` when it is absent."""
    return f"{value:.1f}" if isinstance(value, (int, float)) else "—"


def _fmt_rps(value: object) -> str:
    """Format a requests-per-second metric, or ``—`` when it is absent."""
    return f"{value:.0f}" if isinstance(value, (int, float)) else "—"


def _one_line(text: str) -> str:
    """Collapse *text* onto one bounded line so it can live in a table cell."""
    flattened = " ".join(str(text).split())
    if len(flattened) > _MAX_NOTE_LEN:
        return flattened[: _MAX_NOTE_LEN - 1] + "…"
    return flattened


def _journey_note(block: dict) -> str:
    """The Notes cell for one journey block: skip reason / failure marker."""
    if block.get("skipped"):
        return _one_line(f"⚠️ skipped — {block.get('error', 'unknown error')}")
    summary = block.get("summary") or {}
    if summary and not summary.get("runs_ok"):
        return "❌ every run failed"
    return ""


def _runs_cell(block: dict) -> str:
    """The Runs cell: ``ok/total``, or ``—`` for a skipped journey."""
    summary = block.get("summary") or {}
    total = summary.get("runs_total")
    if not isinstance(total, int):
        return "—"
    return f"{summary.get('runs_ok', 0)}/{total}"


def _caption(report: dict) -> str:
    """One italic line of run context under a section heading."""
    config = report.get("config") or {}
    parts: list[str] = []
    iterations = config.get("iterations")
    runs = config.get("runs")
    if iterations is not None and runs is not None:
        parts.append(f"{iterations} iterations × {runs} runs")
    warmup = config.get("warmup")
    if warmup is not None:
        parts.append(f"warmup {warmup}")
    harness = report.get("harness")
    if harness:
        parts.append(str(harness))
    sha = report.get("git_sha")
    if sha:
        parts.append(f"`{str(sha)[:8]}`")
    return f"_{' · '.join(parts)}_" if parts else ""


def _report_section(label: str, report: dict) -> list[str]:
    """Markdown lines for one report: heading, caption, journey × metric table."""
    lines = [f"### {label}", ""]
    caption = _caption(report)
    if caption:
        lines.extend([caption, ""])
    lines.extend(
        [
            "| Journey | Mean ms | P50 ms | P95 ms | P99 ms | Req/s | Runs | Notes |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for name, block in (report.get("journeys") or {}).items():
        summary = block.get("summary") or {}
        lines.append(
            f"| {name} "
            f"| {_fmt_ms(summary.get('avg_mean_ms'))} "
            f"| {_fmt_ms(summary.get('avg_p50_ms'))} "
            f"| {_fmt_ms(summary.get('avg_p95_ms'))} "
            f"| {_fmt_ms(summary.get('avg_p99_ms'))} "
            f"| {_fmt_rps(summary.get('avg_rps'))} "
            f"| {_runs_cell(block)} "
            f"| {_journey_note(block)} |"
        )
    lines.append("")
    return lines


def _journey_order(labeled_reports: list[tuple[str, dict]]) -> list[str]:
    """Union of journey names, keeping each report's insertion order."""
    ordered: list[str] = []
    for _, report in labeled_reports:
        for name in report.get("journeys") or {}:
            if name not in ordered:
                ordered.append(name)
    return ordered


def _cross_matrix(labeled_reports: list[tuple[str, dict]]) -> list[str]:
    """Journey × report P50 matrix so several reports compare side by side."""
    labels = [label for label, _ in labeled_reports]
    lines = [
        "### P50 across reports",
        "",
        "| Journey | " + " | ".join(f"{label} P50 ms" for label in labels) + " |",
        "| --- | " + " | ".join("---:" for _ in labels) + " |",
    ]
    for name in _journey_order(labeled_reports):
        cells = []
        for _, report in labeled_reports:
            block = (report.get("journeys") or {}).get(name) or {}
            cells.append(_fmt_ms((block.get("summary") or {}).get("avg_p50_ms")))
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines.append("")
    return lines


def build_markdown(labeled_reports: list[tuple[str, dict]], title: str | None = None) -> str:
    """Render *labeled_reports* as one markdown document.

    :param labeled_reports: ``(label, report)`` pairs, where *report* is a
        parsed ``run.py`` JSON report and *label* names it (e.g. its backend).
    :param title: Optional top-level heading, e.g. ``"Benchmark results"``.
    :returns: GitHub-flavoured markdown ending in a newline.
    """
    lines: list[str] = []
    if title:
        lines.extend([f"## {title}", ""])
    if len(labeled_reports) > 1:
        lines.extend(_cross_matrix(labeled_reports))
    for label, report in labeled_reports:
        lines.extend(_report_section(label, report))
    return "\n".join(lines).rstrip("\n") + "\n"


def _label_for(path: Path, report: dict, seen: set[str]) -> str:
    """Label a report by backend, disambiguating duplicates with the filename."""
    backend = (report.get("config") or {}).get("backend")
    label = str(backend) if backend else path.stem
    if label in seen:
        label = f"{label} ({path.stem})"
    seen.add(label)
    return label


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: render the given report files to stdout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path, help="run.py JSON report file(s).")
    parser.add_argument("--title", default=None, help="Optional top-level heading.")
    args = parser.parse_args(argv)

    labeled: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for path in args.reports:
        report = json.loads(path.read_text())
        labeled.append((_label_for(path, report, seen), report))
    sys.stdout.write(build_markdown(labeled, title=args.title))
    return 0


if __name__ == "__main__":
    sys.exit(main())
