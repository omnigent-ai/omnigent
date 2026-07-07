"""Render a :class:`BenchMatrix` as a terminal table, Markdown, or JSON.

Three renderers for three audiences:

- :func:`render_table` — an aligned, optionally ANSI-colored grid for
  reading in a terminal (the CLI default). Pipes-and-dashes Markdown reads
  as raw noise in a shell, so this is what a human sees.
- :func:`render_markdown` — the GitHub-flavored table for docs / PRs,
  reproducing the hand-maintained support matrix.
- :func:`render_json` — the machine-readable form for regenerating docs or
  diffing runs.

Each verdict maps to a spreadsheet glyph plus a DRIFT callout the
spreadsheet cannot express.
"""

from __future__ import annotations

import json
from typing import Any

from tests.harness_bench.bench import BenchMatrix, CellResult, HarnessReport
from tests.harness_bench.probes import ALL_PROBES
from tests.harness_bench.verdict import Verdict

# ANSI colors per verdict, applied only when writing to a TTY.
_ANSI: dict[Verdict, str] = {
    Verdict.SUPPORTED: "32",  # green
    Verdict.PARTIAL: "33",  # yellow
    Verdict.UNSUPPORTED: "31",  # red
    Verdict.DRIFT: "1;31",  # bold red
    Verdict.NOT_APPLICABLE: "2",  # dim
    Verdict.UNKNOWN: "2",  # dim
    Verdict.SKIPPED: "2",  # dim
}

# The offline placeholder note; suppressed from the Notes section so a dry
# render is not 24 identical lines.
_OFFLINE_NOTE = "offline (declared shown)"

# Short transport labels for the harness column, so each row is self-describing
# about which transport produced it (e.g. `claude-sdk [full-server]`). The
# native-tui driver is abbreviated to `native` to match how it is spoken about.
_TRANSPORT_LABEL = {"native-tui": "native"}


def _harness_label(report: HarnessReport) -> str:
    """Harness name plus its resolved transport, e.g. ``codex [full-server]``.

    Uses the transport that actually ran (``report.transport``), which for an
    SDK harness on the default is ``full-server`` — not ``profile.transport``,
    the family marker. Falls back to the bare name when unknown (never resolved).
    """
    transport = report.transport
    if not transport:
        return report.profile.harness
    return f"{report.profile.harness} [{_TRANSPORT_LABEL.get(transport, transport)}]"


def _colorize(text: str, verdict: Verdict, color: bool) -> str:
    if not color:
        return text
    return f"\x1b[{_ANSI.get(verdict, '0')}m{text}\x1b[0m"


def _display_verdict(cell: CellResult | None, declared: bool) -> Verdict:
    if cell is None:
        return Verdict.UNKNOWN
    return cell.declared if declared else cell.verdict


def _cell_glyph_for_grid(cell: CellResult, declared: bool = False) -> str:
    """Compact glyph for a grid cell; drift shows the transition.

    When *declared* is set (offline mode), render the declared glyph so the
    dry matrix shows claimed capabilities instead of a grid of skips.
    """
    if declared:
        return cell.declared.glyph
    if cell.verdict is Verdict.DRIFT:
        return f"!!{cell.declared.glyph}>{cell.observed.glyph}"
    return cell.verdict.glyph


def render_table(
    matrix: BenchMatrix, *, color: bool = False, declared: bool = False, grid: bool = True
) -> str:
    """Render *matrix* as an aligned column grid for terminal reading.

    :param color: When true, colorize each glyph with ANSI (green supported,
        red drift, dim skipped, ...). The CLI passes the TTY state so piped
        output stays plain.
    :param declared: When true (offline mode), render each cell's *declared*
        verdict glyph instead of the observed/reconciled one, so the dry
        matrix shows the capabilities the profile claims rather than ``·``.
    :param grid: When false, omit the heading + glyph grid and emit only the
        footer (legend, drift, notes, skips). The CLI uses this when the rich
        live table already painted the grid to the same terminal, so the report
        adds the per-cell explanations without re-printing the grid.
    """
    titles = [p.title for p in ALL_PROBES]
    names = [p.name for p in ALL_PROBES]

    # Column widths from the visible (uncolored) content.
    labels = {id(r): _harness_label(r) for r in matrix.reports}
    harness_w = max(len("Harness"), *(len(v) for v in labels.values()))
    glyphs: dict[tuple[int, str], str] = {}
    verdicts: dict[tuple[int, str], Verdict] = {}
    for r in matrix.reports:
        by_name = {c.probe_name: c for c in r.cells}
        for n in names:
            cell = by_name.get(n)
            glyphs[(id(r), n)] = _cell_glyph_for_grid(cell, declared) if cell else "?"
            verdicts[(id(r), n)] = _display_verdict(cell, declared)
    col_w = [
        max(len(t), *(len(glyphs[(id(r), n)]) for r in matrix.reports))
        for t, n in zip(titles, names, strict=False)
    ]

    def _center(text: str, width: int, verdict: Verdict | None) -> str:
        total = width - len(text)
        left, right = total // 2, total - total // 2
        inner = _colorize(text, verdict, color) if verdict is not None else text
        return " " * left + inner + " " * right

    header = "  ".join(
        [
            "Harness".ljust(harness_w),
            *(_center(t, w, None) for t, w in zip(titles, col_w, strict=False)),
        ]
    )
    rule = "  ".join(["-" * harness_w, *["-" * w for w in col_w]])
    lines = [header, rule]
    for r in matrix.reports:
        row = [labels[id(r)].ljust(harness_w)]
        row += [
            _center(glyphs[(id(r), n)], w, verdicts[(id(r), n)])
            for n, w in zip(names, col_w, strict=False)
        ]
        lines.append("  ".join(row))

    if grid:
        heading = "Harness capability matrix" + (" (declared, not observed)" if declared else "")
        out = [heading, "", *lines, "", _legend()]
    else:
        # The rich live table already showed the grid on this terminal; emit
        # only the footer so we add the legend + explanations, not a duplicate.
        out = [_legend()]

    drift = _drift_lines(matrix)
    if drift:
        out += ["", "Drift (observed disagrees with declared):", *drift]

    notes = _note_lines(matrix)
    if notes:
        out += ["", "Notes:", *notes]

    skips = _skip_lines(matrix)
    if skips:
        out += ["", "Skipped harnesses:", *skips]

    return "\n".join(out) + "\n"


def _note_lines(matrix: BenchMatrix) -> list[str]:
    """Explain every non-supported, non-trivial cell so a glyph isn't opaque."""
    explain = {Verdict.SKIPPED, Verdict.PARTIAL, Verdict.UNKNOWN, Verdict.UNSUPPORTED}
    lines: list[str] = []
    for report in matrix.reports:
        if report.skipped_reason:
            continue  # whole-harness skip is listed separately
        for cell in report.cells:
            if cell.verdict in explain and cell.note and cell.note != _OFFLINE_NOTE:
                lines.append(
                    f"- {report.profile.harness} / {cell.title}: {cell.verdict.glyph} {cell.note}"
                )
    return lines


def render_markdown(matrix: BenchMatrix, *, declared: bool = False) -> str:
    """Render *matrix* as a Markdown capability grid with a legend and drift list.

    :param declared: When true (offline mode), render declared glyphs so the
        table shows the claimed matrix rather than a grid of skips.
    """
    columns = [p.title for p in ALL_PROBES]
    names = [p.name for p in ALL_PROBES]

    header = "| Harness | " + " | ".join(columns) + " |"
    sep = "| --- | " + " | ".join("---" for _ in columns) + " |"
    lines = [header, sep]

    for report in matrix.reports:
        by_name = {c.probe_name: c for c in report.cells}
        cells = [_cell_glyph(by_name[n], declared) if n in by_name else "?" for n in names]
        lines.append(f"| `{_harness_label(report)}` | " + " | ".join(cells) + " |")

    heading = "# Harness capability matrix" + (" (declared, not observed)" if declared else "")
    out = [heading, "", *lines, "", _legend()]

    drift = _drift_lines(matrix)
    if drift:
        out += ["", "## Drift (observed disagrees with declared)", "", *drift]

    skips = _skip_lines(matrix)
    if skips:
        out += ["", "## Skipped harnesses", "", *skips]

    return "\n".join(out) + "\n"


def _cell_glyph(cell: Any, declared: bool = False) -> str:
    """Glyph for a cell; a drift cell shows the alarm plus what changed."""
    if declared:
        return cell.declared.glyph
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
        # The family marker the profile declares, plus the transport that
        # actually ran (differs for an SDK harness on the full-server default).
        "transport": report.profile.transport,
        "resolved_transport": report.transport,
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
