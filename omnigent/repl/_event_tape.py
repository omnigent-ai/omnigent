"""SSE-to-UI debug tooling — event tape, pipeline counters, and JSONL logger.

Activated by ``--debug-events`` on the CLI. Three layered features:

1. **Event Tape** (``Ctrl+E`` overlay): a scrollable, color-coded ring
   buffer of every event that passed through the rendering pipeline.
2. **JSONL Event Log**: same data written to
   ``~/.omnigent/debug/events-<session_id>.jsonl`` for offline analysis.
3. **Pipeline Stage Counters**: compact ``ev:N tx:N fmt:N out:N``
   readout appended to the toolbar while streaming.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import pathlib
import time
from collections import deque
from dataclasses import dataclass
from typing import Protocol, TextIO, runtime_checkable

from rich.console import Group, RenderableType
from rich.text import Text

# ── Constants ──────────────────────────────────────────────────────────

# Maximum entries the ring buffer retains. Old entries are evicted
# FIFO when the buffer is full. 500 keeps ~200 KB of memory at
# ~400 bytes/entry — enough to hold a full multi-turn conversation
# while bounding peak usage.
_TAPE_CAPACITY = 500

# Inter-event gaps exceeding this threshold (milliseconds) are flagged
# with ``<<GAP>>`` in the tape overlay to surface latency spikes.
_GAP_THRESHOLD_MS = 1000.0

# Detail-panel payload limits. More generous than the sidebar limits
# (_PAYLOAD_VALUE_MAX_CHARS / _PAYLOAD_MAX_LINES) because the detail
# panel is the primary inspection surface.
_DETAIL_PAYLOAD_MAX_CHARS = 500
_DETAIL_PAYLOAD_MAX_LINES = 60


@runtime_checkable
class _ModelDumpEvent(Protocol):
    def model_dump(self) -> dict[str, object]: ...


# ── Stage enumeration ─────────────────────────────────────────────────


class Stage:
    """Pipeline stage identifiers for :class:`TapeEntry`.

    Not an ``enum.Enum`` because we never iterate or reverse-lookup —
    plain class attributes are simpler and import-free.

    :param RAW: Event received from the SSE stream.
    :param TRANSLATED: ``_server_event_to_sdk_event`` returned a
        non-``None`` result.
    :param FORMATTED: The formatter produced at least one
        :class:`FormattedItem`.
    :param RENDERED: ``host.output()`` was called with the item.
    """

    RAW = "raw"
    TRANSLATED = "translated"
    FORMATTED = "formatted"
    RENDERED = "rendered"


def _snapshot_event(event: object) -> dict[str, object] | None:
    """Capture a JSON-serializable snapshot of an event's fields.

    Tries three strategies in order:

    1. Pydantic ``model_dump()`` — server-side SSE events are
       :class:`BaseModel` subclasses.
    2. :func:`dataclasses.asdict` — SDK events are dataclasses.
    3. ``vars()`` — fallback for plain objects with ``__dict__``.

    Returns ``None`` if the event cannot be serialized (shouldn't
    happen with known event types, but safe for forward-compat).

    :param event: The raw event object to snapshot.
    :returns: A dict of the event's fields, or ``None`` on failure.
    """
    # Pydantic models (server-side events).
    if isinstance(event, _ModelDumpEvent):
        try:
            return event.model_dump()
        except Exception:  # noqa: BLE001 — best-effort snapshot
            pass

    # Dataclasses (SDK events).
    if dataclasses.is_dataclass(event) and not isinstance(event, type):
        try:
            return dataclasses.asdict(event)
        except Exception:  # noqa: BLE001 — best-effort snapshot
            pass

    # Plain objects with __dict__.
    try:
        d = vars(event)
        # Shallow-copy to avoid mutation if the event is reused.
        return dict(d)
    except TypeError:
        pass

    return None


# ── TapeEntry ──────────────────────────────────────────────────────────


@dataclass
class TapeEntry:
    """One event's journey through the SSE → Block → Render pipeline.

    :param ts: Wall-clock :func:`time.time` when the raw event arrived.
    :param delta_ms: Milliseconds elapsed since the *previous* entry's
        ``ts``, e.g. ``3.2``. ``0.0`` for the first entry in a turn.
    :param raw_event_type: ``type(event).__name__`` of the raw SSE
        event, e.g. ``"OutputTextDeltaEvent"``.
    :param sdk_translation: Short label for the SDK-shape event
        produced by ``_server_event_to_sdk_event``, e.g.
        ``"TextDelta"``. ``"None (dropped)"`` when the translator
        returned ``None``. ``None`` until
        :meth:`EventTape.update_translation` is called.
    :param formatter_result: Human-readable summary of the formatter's
        output, e.g. ``"StreamingText(5 chars)"`` or ``"[] empty"``.
        ``None`` until :meth:`EventTape.update_format` is called.
    :param stage_reached: Furthest :class:`Stage` this event reached,
        e.g. ``"rendered"``.
    :param path: Which REPL code path handled the event —
        ``"sessions"`` or ``"blockstream"``.
    :param raw_payload: JSON-serializable snapshot of the raw event's
        fields, e.g. ``{"delta": "hello", "type": "output_text.delta"}``.
        Captured at :meth:`EventTape.record_raw` time so the overlay
        can show the full event payload on inspection.
    :param formatted_items: The actual :class:`FormattedItem` objects
        the formatter produced for this event. Stored so the detail
        panel can re-render them to show the user exactly how the
        TUI would display the event. ``None`` until
        :meth:`EventTape.update_format` is called.
    """

    ts: float
    delta_ms: float
    raw_event_type: str
    sdk_translation: str | None = None
    formatter_result: str | None = None
    stage_reached: str = Stage.RAW
    path: str = "sessions"
    raw_payload: dict[str, object] | None = None
    formatted_items: list[object] | None = None


# ── PipelineCounters ──────────────────────────────────────────────────


@dataclass
class PipelineCounters:
    """Compact per-turn counters for the toolbar readout.

    Reset at the start of each user turn via :meth:`reset`.

    :param raw: Raw events received from the SSE stream.
    :param translated: Events that ``_server_event_to_sdk_event``
        translated successfully (non-``None``).
    :param formatted: Events where the formatter produced at least
        one :class:`FormattedItem`.
    :param rendered: Events where ``host.output()`` was called.
    :param max_gap_ms: Largest inter-event gap (milliseconds) seen
        this turn, e.g. ``2100.5``.
    :param max_gap_event_type: The ``raw_event_type`` of the event
        that *followed* the largest gap, e.g.
        ``"OutputItemDoneEvent"``. ``None`` until a gap is recorded.
    """

    raw: int = 0
    translated: int = 0
    formatted: int = 0
    rendered: int = 0
    max_gap_ms: float = 0.0
    max_gap_event_type: str | None = None

    def reset(self) -> None:
        """Zero all counters for a new turn."""
        self.raw = 0
        self.translated = 0
        self.formatted = 0
        self.rendered = 0
        self.max_gap_ms = 0.0
        self.max_gap_event_type = None

    def toolbar_text(self) -> str:
        """Format the compact toolbar readout.

        :returns: A string like
            ``"ev:142 tx:138 fmt:120 out:120  gap:2.1s@OutputItemDoneEvent"``.
        """
        parts = f"ev:{self.raw} tx:{self.translated} fmt:{self.formatted} out:{self.rendered}"
        if self.max_gap_ms >= _GAP_THRESHOLD_MS and self.max_gap_event_type is not None:
            gap_s = self.max_gap_ms / 1000.0
            parts += f"  gap:{gap_s:.1f}s@{self.max_gap_event_type}"
        return parts


# ── EventTape ──────────────────────────────────────────────────────────


class EventTape:
    """Ring-buffer of :class:`TapeEntry` objects with pipeline instrumentation.

    Thread-safety: the REPL is single-threaded (asyncio event loop) so no
    locking is needed. All mutation methods are called from the main task.

    :param capacity: Maximum number of entries before eviction,
        e.g. ``500``.
    :param counters: Shared :class:`PipelineCounters` instance that the
        toolbar reads. Mutated in-place by the tape's recording methods.
    """

    def __init__(
        self,
        capacity: int = _TAPE_CAPACITY,
        counters: PipelineCounters | None = None,
    ) -> None:
        """
        Construct the event tape.

        :param capacity: Maximum ring-buffer size, e.g. ``500``.
        :param counters: Optional shared :class:`PipelineCounters`
            for toolbar integration. ``None`` disables counter updates.
        """
        self._buf: deque[TapeEntry] = deque(maxlen=capacity)
        self._counters = counters or PipelineCounters()
        self._last_ts: float = 0.0

    @property
    def counters(self) -> PipelineCounters:
        """The shared pipeline counters instance.

        :returns: The :class:`PipelineCounters` used by this tape.
        """
        return self._counters

    def record_raw(self, event: object, *, path: str = "sessions") -> TapeEntry:
        """Record a raw SSE event arriving at the pipeline.

        Creates a new :class:`TapeEntry`, appends it to the ring
        buffer, and increments :attr:`PipelineCounters.raw`.

        :param event: The raw event object, e.g. an
            ``OutputTextDeltaEvent`` instance.
        :param path: Which code path — ``"sessions"`` or
            ``"blockstream"``.
        :returns: The newly created :class:`TapeEntry` (callers
            pass it to :meth:`update_translation` /
            :meth:`update_format` / :meth:`mark_rendered` as the
            event progresses).
        """
        now = time.time()
        delta_ms = (now - self._last_ts) * 1000.0 if self._last_ts > 0 else 0.0
        self._last_ts = now

        entry = TapeEntry(
            ts=now,
            delta_ms=delta_ms,
            raw_event_type=type(event).__name__,
            stage_reached=Stage.RAW,
            path=path,
            raw_payload=_snapshot_event(event),
        )
        self._buf.append(entry)
        self._counters.raw += 1

        # Track the largest gap this turn.
        if delta_ms > self._counters.max_gap_ms:
            self._counters.max_gap_ms = delta_ms
            self._counters.max_gap_event_type = entry.raw_event_type

        return entry

    def update_translation(self, entry: TapeEntry, sdk_event: object | None) -> None:
        """Record the translation result from ``_server_event_to_sdk_event``.

        :param entry: The :class:`TapeEntry` returned by
            :meth:`record_raw`.
        :param sdk_event: The translated event, or ``None`` if the
            translator dropped it.
        """
        if sdk_event is None:
            entry.sdk_translation = "None (dropped)"
        else:
            entry.sdk_translation = type(sdk_event).__name__
            entry.stage_reached = Stage.TRANSLATED
            self._counters.translated += 1

    def update_format(self, entry: TapeEntry, items: list[object]) -> None:
        """Record the formatter's output for this event.

        :param entry: The :class:`TapeEntry` returned by
            :meth:`record_raw`.
        :param items: The list of :class:`FormattedItem` objects the
            formatter produced. Empty list means the formatter
            produced nothing.
        """
        if not items:
            entry.formatter_result = "[] empty"
            entry.formatted_items = []
        else:
            summaries = [_summarize_formatted_item(it) for it in items]
            entry.formatter_result = ", ".join(summaries)
            entry.formatted_items = list(items)
            entry.stage_reached = Stage.FORMATTED
            self._counters.formatted += 1

    def mark_rendered(self, entry: TapeEntry, count: int = 1) -> None:
        """Record that ``host.output()`` was called for this event.

        :param entry: The :class:`TapeEntry` returned by
            :meth:`record_raw`.
        :param count: Number of ``host.output()`` calls made,
            e.g. ``1``.
        """
        entry.stage_reached = Stage.RENDERED
        self._counters.rendered += count

    @property
    def entries(self) -> list[TapeEntry]:
        """Return a snapshot of all buffered entries (oldest first).

        :returns: A list copy of the ring buffer contents.
        """
        return list(self._buf)

    def summary_counts(self) -> dict[str, int]:
        """Count events grouped by ``raw_event_type``.

        :returns: A dict mapping event-type names to occurrence
            counts, e.g. ``{"OutputTextDeltaEvent": 120,
            "CompletedEvent": 1}``.
        """
        counts: dict[str, int] = {}
        for entry in self._buf:
            counts[entry.raw_event_type] = counts.get(entry.raw_event_type, 0) + 1
        return counts

    def reset_turn(self) -> None:
        """Reset per-turn state (counters) but keep the tape buffer.

        Called at the start of each user turn so toolbar counters
        reflect the current turn only.
        """
        self._counters.reset()
        self._last_ts = 0.0


# ── Overlay builders ───────────────────────────────────────────────────
# The Ctrl+E overlay uses the two-pane Overlay mode:
# * **Sidebar** (``targets_builder``): one row per tape entry — shows
#   index, delta, event type, and a stage-color icon so the user can
#   scan for dropped/delayed events at a glance.
# * **Detail panel** (``builder``): for the selected entry, shows the
#   full pipeline journey (translation, formatter, render) AND the
#   raw JSON payload so the user can inspect exactly what the server
#   sent and how the REPL handled it.


def build_tape_targets(tape: EventTape) -> list[_OverlayTargetLike]:
    """Build the sidebar target list — one per tape entry.

    Each target's ``key`` is the stringified index into the tape's
    entry list; ``label`` is a compact summary for the sidebar row.

    :param tape: The :class:`EventTape` holding buffered entries.
    :returns: A list of sidebar target objects. Empty when no events
        have been recorded.
    """
    entries = tape.entries
    targets: list[_OverlayTargetLike] = []
    for i, e in enumerate(entries):
        icon = _stage_icon(e.stage_reached)
        delta_str = f"+{e.delta_ms:.0f}ms" if e.delta_ms > 0 else ""
        # Truncate long type names to fit the sidebar width.
        short_type = e.raw_event_type
        if len(short_type) > 22:
            short_type = short_type[:20] + "…"
        label = f"{short_type} {delta_str}"
        targets.append(_OverlayTargetLike(key=str(i), label=label, icon=icon))
    return targets


def build_tape_detail(
    tape: EventTape,
    target_key: str,
    fmt: object,
) -> RenderableType:
    """Build the detail panel for a selected tape entry.

    Shows four sections:

    1. **Header** — event index, type, timestamp, delta.
    2. **Pipeline journey** — stage reached, translation result,
       formatter output, render status.
    3. **Raw JSON payload** — the event's fields as pretty-printed
       JSON, enabling full inspection of what the server sent.
    4. **Summary footer** — pipeline-wide counters.

    :param tape: The :class:`EventTape` holding buffered entries.
    :param target_key: The ``key`` from the selected
        :class:`OverlayTarget` — a stringified index into the
        tape's entry list, e.g. ``"42"``.
    :param fmt: The :class:`RichBlockFormatter` (used for its
        ``muted`` and ``accent`` color attributes).
    :returns: A Rich :class:`Group` renderable for the detail panel.
    """
    muted = getattr(fmt, "muted", "dim")
    accent = getattr(fmt, "accent", "bold")
    entries = tape.entries
    parts: list[RenderableType] = []

    try:
        idx = int(target_key)
        entry = entries[idx]
    except (ValueError, IndexError):
        parts.append(Text.from_markup("[dim]No event selected.[/dim]"))
        return Group(*parts)

    # ── 1. Header ───────────────────────────────────────────────
    wall = datetime.datetime.fromtimestamp(entry.ts, tz=datetime.timezone.utc).strftime(
        "%H:%M:%S.%f"
    )[:-3]
    color = _stage_color(entry.stage_reached)

    parts.append(
        Text.from_markup(
            f"[bold]Event [{idx + 1}][/bold]  [{color}]{entry.raw_event_type}[/{color}]"
        )
    )
    parts.append(Text(""))

    # ── 2. Pipeline journey ─────────────────────────────────────
    delta_str = f"+{entry.delta_ms:.1f}ms" if entry.delta_ms > 0 else "+0ms"
    gap_flag = "  ⚠ GAP" if entry.delta_ms >= _GAP_THRESHOLD_MS else ""
    parts.append(Text.from_markup(f"  [{muted}]Time[/{muted}]: {wall}  {delta_str}{gap_flag}"))
    parts.append(Text.from_markup(f"  [{muted}]Path[/{muted}]: {entry.path}"))
    parts.append(
        Text.from_markup(f"  [{muted}]Stage[/{muted}]: [{color}]{entry.stage_reached}[/{color}]")
    )
    parts.append(Text(""))

    # Translation
    parts.append(Text.from_markup(f"  [{accent}]Translation[/{accent}]"))
    if entry.sdk_translation is not None:
        tx_color = "red" if entry.sdk_translation == "None (dropped)" else "green"
        parts.append(
            Text.from_markup(
                f"    {entry.raw_event_type} → [{tx_color}]{entry.sdk_translation}[/{tx_color}]"
            )
        )
    else:
        parts.append(Text.from_markup(f"    [{muted}](not yet translated)[/{muted}]"))
    parts.append(Text(""))

    # Formatter summary
    parts.append(Text.from_markup(f"  [{accent}]Formatter Output[/{accent}]"))
    if entry.formatter_result is not None:
        parts.append(Text.from_markup(f"    {entry.formatter_result}"))
    else:
        parts.append(Text.from_markup(f"    [{muted}](no output)[/{muted}]"))
    parts.append(Text(""))

    # Rendered output — show what the TUI actually displays.
    # Re-render the stored FormattedItem objects through a Rich
    # Console capture so the user sees the same output the REPL
    # would produce. This is the primary debugging surface for
    # "why does this event look wrong on screen?"
    parts.append(Text.from_markup(f"  [{accent}]Rendered As[/{accent}]"))
    if entry.formatted_items:
        rendered_preview = _render_formatted_items(entry.formatted_items)
        if rendered_preview:
            for rl in rendered_preview.splitlines():
                parts.append(Text.from_markup(f"    [{muted}]│[/{muted}] {_escape_markup(rl)}"))
        else:
            parts.append(Text.from_markup(f"    [{muted}](empty render)[/{muted}]"))
    elif entry.stage_reached == Stage.RENDERED:
        # Rendered via _render_history_item (no formatter items stored).
        parts.append(
            Text.from_markup(
                f"    [{muted}](rendered via _render_history_item — "
                f"no formatter capture)[/{muted}]"
            )
        )
    else:
        if entry.sdk_translation == "None (dropped)":
            reason = "dropped at translation"
        else:
            reason = "formatter produced nothing"
        parts.append(Text.from_markup(f"    [red]✗ not rendered — {reason}[/red]"))
    parts.append(Text(""))

    # ── 3. Raw JSON payload ─────────────────────────────────────
    parts.append(Text.from_markup(f"  [{accent}]Raw Event Payload[/{accent}]"))
    if entry.raw_payload is not None:
        # Use generous limits for the detail panel — this is the
        # primary inspection surface.
        payload_str = _format_payload_detail(entry.raw_payload)
        for pl_line in payload_str.splitlines():
            parts.append(Text.from_markup(f"    [{muted}]{_escape_markup(pl_line)}[/{muted}]"))
    else:
        parts.append(Text.from_markup(f"    [{muted}](payload not captured)[/{muted}]"))
    parts.append(Text(""))

    # ── 4. Summary footer ───────────────────────────────────────
    counters = tape.counters
    parts.append(
        Text.from_markup(
            f"  [{muted}]Pipeline totals: "
            f"ev:{counters.raw} tx:{counters.translated} "
            f"fmt:{counters.formatted} out:{counters.rendered}[/{muted}]"
        )
    )

    return Group(*parts)


def _render_formatted_items(items: list[object]) -> str:
    """Render stored FormattedItem objects to plain text via Rich Console.

    Each item type is handled according to how ``TerminalHost.output()``
    would display it:

    * ``StreamingText`` → raw ``.text`` content (streaming delta).
    * ``StreamReplace`` / ``StreamLive`` → render the inner
      ``.renderable`` through a Rich Console to produce the final
      visual output (markdown, panels, etc.).
    * Other ``RenderableType`` → render through Rich Console.

    :param items: The stored :attr:`TapeEntry.formatted_items` list.
    :returns: Multi-line plain-text preview of the rendered output.
        Empty string if nothing renders.
    """
    from rich.console import Console

    lines: list[str] = []
    # Width matches a typical overlay content pane.
    console = Console(width=80, no_color=True, file=None)

    for item in items:
        # StreamingText: raw text delta — show as-is.
        text_attr = getattr(item, "text", None)
        if text_attr is not None and isinstance(text_attr, str):
            if text_attr.strip():
                lines.append(text_attr.rstrip("\n"))
            continue

        # StreamReplace / StreamLive: inner renderable.
        renderable = getattr(item, "renderable", None)
        if renderable is not None:
            with console.capture() as cap:
                console.print(renderable)
            rendered = cap.get().rstrip("\n")
            if rendered.strip():
                lines.append(rendered)
            continue

        # Bare RenderableType (Rich Text, Panel, etc.).
        try:
            with console.capture() as cap:
                console.print(item)
            rendered = cap.get().rstrip("\n")
            if rendered.strip():
                lines.append(rendered)
        except Exception:  # noqa: BLE001 — best-effort preview
            lines.append(f"<{type(item).__name__}: render failed>")

    return "\n".join(lines)


def _escape_markup(text: str) -> str:
    """Escape Rich markup characters in user-controlled text.

    Payload JSON may contain ``[`` and ``]`` that Rich would
    interpret as style tags. Escaping prevents rendering errors.

    :param text: Raw text that may contain Rich markup characters.
    :returns: Text with ``[`` escaped to ``\\[`` so Rich renders
        it literally.
    """
    return text.replace("[", "\\[")


def _format_payload_detail(payload: dict[str, object]) -> str:
    """Format a payload for the detail panel with generous limits.

    Unlike :func:`_format_payload` (used in the JSONL log summary),
    this version allows more lines and wider values — the detail
    panel is where the user actually inspects the full event.

    :param payload: The event's field dict from
        :func:`_snapshot_event`.
    :returns: Pretty-printed JSON string.
    """
    truncated = _truncate_deep(payload, max_chars=_DETAIL_PAYLOAD_MAX_CHARS)
    try:
        raw = json.dumps(truncated, indent=2, default=str)
    except (TypeError, ValueError):
        raw = repr(payload)
    lines = raw.splitlines()
    if len(lines) > _DETAIL_PAYLOAD_MAX_LINES:
        overflow = len(lines) - _DETAIL_PAYLOAD_MAX_LINES
        lines = [*lines[:_DETAIL_PAYLOAD_MAX_LINES], f"  ... ({overflow} more lines)"]
    return "\n".join(lines)


@dataclass
class _OverlayTargetLike:
    """Lightweight stand-in for :class:`OverlayTarget`.

    Avoids importing the SDK at module scope — the real
    :class:`OverlayTarget` is constructed in the REPL layer
    that already imports the SDK. This dataclass has the same
    three fields the SDK expects (``key``, ``label``, ``icon``)
    so the REPL can trivially convert it.

    :param key: Stable identifier, e.g. ``"42"`` (entry index).
    :param label: Sidebar display label, e.g. ``"TextDelta +3ms"``.
    :param icon: Stage icon, e.g. ``"🟢"`` for rendered.
    """

    key: str
    label: str
    icon: str


def _stage_color(stage: str) -> str:
    """Map a :class:`Stage` value to a Rich markup color.

    :param stage: One of the :class:`Stage` constants.
    :returns: A Rich color string — green for rendered, yellow for
        translated/formatted, red for raw (dropped).
    """
    if stage == Stage.RENDERED:
        return "green"
    if stage in (Stage.TRANSLATED, Stage.FORMATTED):
        return "yellow"
    return "red"


def _stage_icon(stage: str) -> str:
    """Map a :class:`Stage` value to a sidebar icon.

    :param stage: One of the :class:`Stage` constants.
    :returns: A colored circle emoji — green for rendered, yellow
        for translated/formatted, red for dropped.
    """
    if stage == Stage.RENDERED:
        return "🟢"
    if stage in (Stage.TRANSLATED, Stage.FORMATTED):
        return "🟡"
    return "🔴"


# Maximum number of characters to show for a single JSON payload
# field value in the overlay. Long text deltas or tool arguments
# are truncated to keep the overlay scannable.
_PAYLOAD_VALUE_MAX_CHARS = 120

# Maximum number of lines for the indented JSON block per entry.
# Deep Pydantic models (e.g. CompletedEvent with nested response
# objects) can produce hundreds of lines; cap to keep the overlay
# navigable.
_PAYLOAD_MAX_LINES = 12


def _format_payload(payload: dict[str, object]) -> str:
    """Format an event payload dict as indented JSON for the overlay.

    Long string values are truncated to :data:`_PAYLOAD_VALUE_MAX_CHARS`.
    The entire output is capped at :data:`_PAYLOAD_MAX_LINES` lines.

    :param payload: The event's field dict from
        :func:`_snapshot_event`.
    :returns: A multi-line string of pretty-printed JSON, suitable
        for indented display in the overlay.
    """
    truncated = _truncate_deep(payload)
    try:
        raw = json.dumps(truncated, indent=2, default=str)
    except (TypeError, ValueError):
        raw = repr(payload)
    lines = raw.splitlines()
    if len(lines) > _PAYLOAD_MAX_LINES:
        overflow = len(lines) - _PAYLOAD_MAX_LINES
        lines = [*lines[:_PAYLOAD_MAX_LINES], f"  ... ({overflow} more lines)"]
    return "\n".join(lines)


def _truncate_deep(obj: object, max_chars: int = _PAYLOAD_VALUE_MAX_CHARS) -> object:
    """Recursively truncate long string values in a nested structure.

    :param obj: The object to truncate (dict, list, or scalar).
    :param max_chars: Maximum characters for string values before
        truncation, e.g. ``120``.
    :returns: A copy with long strings shortened to
        ``value[:max_chars] + "…"``.
    """
    if isinstance(obj, str):
        return obj[:max_chars] + "…" if len(obj) > max_chars else obj
    if isinstance(obj, dict):
        return {k: _truncate_deep(v, max_chars) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_truncate_deep(item, max_chars) for item in obj]
    return obj


# ── Item summarizer ────────────────────────────────────────────────────


def _summarize_formatted_item(item: object) -> str:
    """One-line summary of a formatted output item.

    :param item: A :class:`FormattedItem` (``StreamingText``,
        ``StreamReplace``, or a Rich ``RenderableType``).
    :returns: A human-readable label, e.g. ``"StreamingText(12 chars)"``.
    """
    cls_name = type(item).__name__
    # StreamingText has a .text attribute with the delta content.
    text_attr = getattr(item, "text", None)
    if text_attr is not None and isinstance(text_attr, str):
        return f"{cls_name}({len(text_attr)} chars)"
    return cls_name


# ── JSONL logger ───────────────────────────────────────────────────────

# Directory under ``~/.omnigent/`` where event logs are written.
_DEBUG_DIR_NAME = "debug"


def open_event_log(session_id: str) -> pathlib.Path:
    """Create (if needed) and return the JSONL log path for a session.

    :param session_id: The session id, e.g. ``"sess_abc123"``.
        Sanitized for filesystem safety.
    :returns: Absolute path to the log file, e.g.
        ``~/.omnigent/debug/events-sess_abc123.jsonl``.
    """
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
    debug_dir = pathlib.Path.home() / ".omnigent" / _DEBUG_DIR_NAME
    debug_dir.mkdir(parents=True, exist_ok=True)
    return debug_dir / f"events-{safe_id}.jsonl"


def log_entry_jsonl(fh: TextIO, entry: TapeEntry) -> None:
    """Write one :class:`TapeEntry` as a JSON line.

    :param fh: A file handle opened for writing (``open(..., "a")``).
    :param entry: The :class:`TapeEntry` to serialize.
    """
    record = {
        "ts": entry.ts,
        "wall": datetime.datetime.fromtimestamp(entry.ts, tz=datetime.timezone.utc).isoformat(),
        "delta_ms": round(entry.delta_ms, 2),
        "path": entry.path,
        "raw_type": entry.raw_event_type,
        "sdk_type": entry.sdk_translation,
        "fmt_items": entry.formatter_result,
        "stage": entry.stage_reached,
        "rendered": entry.stage_reached == Stage.RENDERED,
        "payload": entry.raw_payload,
    }
    fh.write(json.dumps(record, default=str) + "\n")
    fh.flush()
