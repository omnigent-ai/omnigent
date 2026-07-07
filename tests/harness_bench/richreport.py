"""A rich live-progress sink for a bench run.

Draws a table that updates in place as events arrive: one row per harness,
one column per capability dimension, each cell showing the probe's live state
(a spinner while running, then the verdict glyph). Under a parallel run
(``--jobs`` > 1) several rows advance at once, which is exactly what the live
table is for.

Only usable on a TTY with ``rich`` installed. :func:`rich_sink_or_none`
returns ``None`` when either precondition is missing, so the CLI falls back to
the plain :class:`~tests.harness_bench.events.LineSink`.
"""

from __future__ import annotations

from tests.harness_bench.events import (
    BenchEvent,
    HarnessSkipped,
    HarnessStarted,
    ProbeFinished,
    ProbeStarted,
)
from tests.harness_bench.probes import ALL_PROBES
from tests.harness_bench.verdict import Verdict

# Cell state → what the table shows. Verdicts reuse the report glyphs; the two
# transient states (pending/running) are bench-live only.
_VERDICT_GLYPH: dict[Verdict, str] = {
    Verdict.SUPPORTED: "[green]✓[/green]",
    Verdict.PARTIAL: "[yellow]~[/yellow]",
    Verdict.UNSUPPORTED: "[red]✗[/red]",
    Verdict.NOT_APPLICABLE: "[dim]—[/dim]",
    Verdict.UNKNOWN: "[dim]?[/dim]",
    Verdict.SKIPPED: "[dim]·[/dim]",
    Verdict.DRIFT: "[bold red]!![/bold red]",
}
_PENDING = "[dim]·[/dim]"
_RUNNING = "[cyan]…[/cyan]"

# Short transport labels for the harness column (native-tui → native), matching
# the static report renderer.
_TRANSPORT_LABEL = {"native-tui": "native"}


def rich_sink_or_none(*, force: bool = False):
    """Return a rich live sink, or ``None`` if rich/TTY is unavailable.

    :param force: Build the sink even if stdout is not a TTY (for tests /
        explicit ``--rich``). Normally the caller only asks for this when a
        terminal is detected.
    """
    try:
        from rich.console import Console
    except ImportError:
        return None
    console = Console(stderr=True)
    if not force and not console.is_terminal:
        return None
    return _RichLiveSink(console)


class _RichLiveSink:
    """A :class:`~tests.harness_bench.events.ProgressSink` backed by ``rich.Live``.

    Holds a ``{harness: {dimension: cell-markup}}`` grid and re-renders a table
    on every event. Rows appear as harnesses start; a whole-harness skip marks
    every cell ``·`` with the reason as a trailing note.
    """

    # This sink paints the full glyph grid to the terminal, so the CLI can skip
    # re-printing it in the stdout report (see __main__._grid_already_shown).
    drew_grid = True

    def __init__(self, console) -> None:
        from rich.live import Live

        self._console = console
        self._dimensions = [p.title for p in ALL_PROBES]
        self._dim_by_name = {p.name: p.title for p in ALL_PROBES}
        # harness → {dim_title: markup}; insertion order = display order.
        self._rows: dict[str, dict[str, str]] = {}
        self._notes: dict[str, str] = {}
        self._transport: dict[str, str] = {}  # harness → resolved transport label
        self._live = Live(self._render(), console=console, refresh_per_second=8)
        self._live.start()

    def _blank_row(self) -> dict[str, str]:
        return dict.fromkeys(self._dimensions, _PENDING)

    def _render(self):
        from rich.table import Table

        table = Table(title="Harness capability matrix (live)", expand=False)
        table.add_column("Harness", no_wrap=True)
        for dim in self._dimensions:
            table.add_column(dim, justify="center")
        for harness, cells in self._rows.items():
            note = self._notes.get(harness)
            transport = self._transport.get(harness)
            label = harness
            if transport:
                label += f" [dim]\\[{_TRANSPORT_LABEL.get(transport, transport)}][/dim]"
            if note:
                label += f"  [dim]({note})[/dim]"
            table.add_row(label, *(cells[dim] for dim in self._dimensions))
        return table

    def emit(self, event: BenchEvent) -> None:
        if isinstance(event, HarnessStarted):
            self._rows.setdefault(event.harness, self._blank_row())
            self._transport[event.harness] = event.transport
        elif isinstance(event, HarnessSkipped):
            self._rows.setdefault(event.harness, self._blank_row())
            # A whole-harness skip: every dimension is ·, reason on the label.
            self._notes[event.harness] = event.reason.split(";")[0][:60]
            if event.transport:
                self._transport[event.harness] = event.transport
        elif isinstance(event, ProbeStarted):
            row = self._rows.setdefault(event.harness, self._blank_row())
            row[self._dim_by_name.get(event.probe, event.title)] = _RUNNING
        elif isinstance(event, ProbeFinished):
            row = self._rows.setdefault(event.harness, self._blank_row())
            row[self._dim_by_name.get(event.probe, event.title)] = _VERDICT_GLYPH.get(
                event.verdict, _PENDING
            )
        # HarnessFinished needs no cell change (all its probes already landed).
        self._live.update(self._render())

    def close(self) -> None:
        self._live.update(self._render())
        self._live.stop()


__all__ = ["rich_sink_or_none"]
