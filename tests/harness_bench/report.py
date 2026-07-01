"""Render a :class:`BenchMatrix` as Markdown or JSON.

The Markdown grid reproduces the hand-maintained support matrix — one row
per harness, one column per dimension, spreadsheet glyphs in the cells —
plus a DRIFT callout the spreadsheet cannot express. JSON is the
machine-readable form for regenerating docs or diffing runs.
"""

from __future__ import annotations

import json
from typing import Any

from tests.harness_bench.bench import BenchMatrix, HarnessReport
from tests.harness_bench.probes import ALL_PROBES
from tests.harness_bench.verdict import Verdict


def render_markdown(matrix: BenchMatrix) -> str:
    """Render *matrix* as a Markdown capability grid with a legend and drift list."""
    columns = [p.title for p in ALL_PROBES]
    names = [p.name for p in ALL_PROBES]

    header = "| Harness | " + " | ".join(columns) + " |"
    sep = "| --- | " + " | ".join("---" for _ in columns) + " |"
    lines = [header, sep]

    for report in matrix.reports:
        by_name = {c.probe_name: c for c in report.cells}
        cells = [_cell_glyph(by_name[n]) if n in by_name else "?" for n in names]
        lines.append(f"| `{report.profile.harness}` | " + " | ".join(cells) + " |")

    out = ["# Harness capability matrix", "", *lines, "", _legend()]

    drift = _drift_lines(matrix)
    if drift:
        out += ["", "## Drift (observed disagrees with declared)", "", *drift]

    skips = _skip_lines(matrix)
    if skips:
        out += ["", "## Skipped harnesses", "", *skips]

    return "\n".join(out) + "\n"


def _cell_glyph(cell: Any) -> str:
    """Glyph for a cell; a drift cell shows the alarm plus what changed."""
    if cell.verdict is Verdict.DRIFT:
        return f"!! ({cell.declared.glyph}->{cell.observed.glyph})"
    return cell.verdict.glyph


def _legend() -> str:
    parts = [
        f"`{v.glyph}` {v.name}"
        for v in (
            Verdict.SUPPORTED,
            Verdict.PARTIAL,
            Verdict.UNSUPPORTED,
            Verdict.NOT_APPLICABLE,
            Verdict.UNKNOWN,
            Verdict.SKIPPED,
            Verdict.DRIFT,
        )
    ]
    return "Legend: " + " · ".join(parts)


def _drift_lines(matrix: BenchMatrix) -> list[str]:
    lines: list[str] = []
    for report in matrix.reports:
        for cell in report.cells:
            if cell.is_drift:
                lines.append(
                    f"- `{report.profile.harness}` / {cell.title}: "
                    f"declared {cell.declared.glyph} ({cell.declared.name}), "
                    f"observed {cell.observed.glyph} ({cell.observed.name})"
                    + (f" — {cell.note}" if cell.note else "")
                )
    return lines


def _skip_lines(matrix: BenchMatrix) -> list[str]:
    return [
        f"- `{r.profile.harness}`: {r.skipped_reason}" for r in matrix.reports if r.skipped_reason
    ]


def render_json(matrix: BenchMatrix) -> str:
    """Render *matrix* as indented JSON (stable key order) for tooling."""
    payload = {
        "harnesses": [_report_json(r) for r in matrix.reports],
        "has_drift": matrix.has_drift,
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _report_json(report: HarnessReport) -> dict[str, Any]:
    return {
        "harness": report.profile.harness,
        "transport": report.profile.transport,
        "model": report.profile.model,
        "owner": report.profile.owner,
        "auth": report.profile.auth,
        "implementation": report.profile.implementation,
        "skipped_reason": report.skipped_reason,
        "cells": [
            {
                "dimension": c.probe_name,
                "priority": c.priority.value,
                "observed": c.observed.value,
                "declared": c.declared.value,
                "verdict": c.verdict.value,
                "note": c.note,
            }
            for c in report.cells
        ],
    }
