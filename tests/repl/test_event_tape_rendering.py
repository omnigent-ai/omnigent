"""Unit tests for the rendering and snapshot branches of ``omnigent.repl._event_tape``.

Complements ``test_event_tape.py`` with the sidebar labels, the detail panel's
less-travelled states (gaps, dropped/undelivered events, missing payloads), the
formatted-item preview, and payload truncation fallbacks.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from rich.console import Console, RenderableType
from rich.text import Text

from omnigent.repl._event_tape import (
    _DETAIL_PAYLOAD_MAX_LINES,
    _PAYLOAD_MAX_LINES,
    EventTape,
    Stage,
    TapeEntry,
    _format_payload,
    _format_payload_detail,
    _render_formatted_items,
    _snapshot_event,
    _stage_color,
    _stage_icon,
    build_tape_detail,
    build_tape_targets,
)


class _Fmt:
    muted = "dim"
    accent = "bold"


class _Ev:
    """Event stub; the tape only records its class name and ``vars``."""

    def __init__(self, **fields: object) -> None:
        self.__dict__.update(fields)


def _plain(renderable: RenderableType) -> str:
    console = Console(width=120, no_color=True, file=None)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


def _detail(entry: TapeEntry, tape: EventTape | None = None) -> str:
    tape = tape or EventTape()
    tape._buf.append(entry)  # detail is keyed by buffer index; bypass record_raw's clock
    return _plain(build_tape_detail(tape, str(len(tape.entries) - 1), _Fmt()))


def _entry(**overrides: object) -> TapeEntry:
    fields: dict[str, object] = {"ts": 1_700_000_000.0, "delta_ms": 0.0, "raw_event_type": "Ev"}
    fields.update(overrides)
    return TapeEntry(**fields)  # type: ignore[arg-type]


# ── sidebar ──────────────────────────────────────────────────


def test_build_tape_targets_labels_and_icons() -> None:
    tape = EventTape()
    dropped = _entry(raw_event_type="Short", delta_ms=0.0)
    slow = _entry(
        raw_event_type="A" * 30,
        delta_ms=1234.4,
        stage_reached=Stage.RENDERED,
    )
    for entry in (dropped, slow):
        tape._buf.append(entry)

    targets = build_tape_targets(tape)

    assert [t.key for t in targets] == ["0", "1"]
    assert targets[0].label == "Short "
    assert targets[0].icon == "🔴"
    # Long type names are cut to 20 chars plus an ellipsis, delta rounded to ms.
    assert targets[1].label == f"{'A' * 20}… +1234ms"
    assert targets[1].icon == "🟢"


def test_build_tape_targets_is_empty_for_empty_tape() -> None:
    assert build_tape_targets(EventTape()) == []


@pytest.mark.parametrize(
    ("stage", "color", "icon"),
    [
        (Stage.RENDERED, "green", "🟢"),
        (Stage.FORMATTED, "yellow", "🟡"),
        (Stage.TRANSLATED, "yellow", "🟡"),
        (Stage.RAW, "red", "🔴"),
    ],
)
def test_stage_color_and_icon(stage: str, color: str, icon: str) -> None:
    assert _stage_color(stage) == color
    assert _stage_icon(stage) == icon


# ── event snapshots ──────────────────────────────────────────


def test_snapshot_falls_back_to_dataclass_when_model_dump_raises() -> None:
    @dataclass
    class _Both:
        value: int = 3

        def model_dump(self) -> dict[str, object]:
            raise RuntimeError("boom")

    assert _snapshot_event(_Both()) == {"value": 3}


def test_snapshot_falls_back_to_vars_when_asdict_fails() -> None:
    @dataclass
    class _HoldsLock:
        # ``asdict`` deep-copies fields and locks cannot be copied.
        lock: object = field(default_factory=threading.Lock)

    snapshot = _snapshot_event(_HoldsLock())

    assert snapshot is not None and set(snapshot) == {"lock"}


def test_snapshot_returns_none_for_objects_without_fields() -> None:
    class _Slotted:
        __slots__ = ("x",)

    assert _snapshot_event(_Slotted()) is None
    assert _snapshot_event(42) is None


# ── detail panel ─────────────────────────────────────────────


def test_detail_flags_gaps_at_the_threshold() -> None:
    assert "⚠ GAP" in _detail(_entry(delta_ms=1000.0))
    assert "⚠ GAP" not in _detail(_entry(delta_ms=999.0))


def test_detail_formats_zero_and_positive_deltas() -> None:
    assert "+0ms" in _detail(_entry(delta_ms=0.0))
    assert "+12.5ms" in _detail(_entry(delta_ms=12.5))


def test_detail_reports_untranslated_event() -> None:
    text = _detail(_entry())

    assert "(not yet translated)" in text
    assert "(no output)" in text
    assert "not rendered — formatter produced nothing" in text
    assert "(payload not captured)" in text


def test_detail_reports_dropped_translation() -> None:
    text = _detail(_entry(sdk_translation="None (dropped)"))

    assert "Ev → None (dropped)" in text
    assert "not rendered — dropped at translation" in text


def test_detail_reports_translated_event_that_formatter_ignored() -> None:
    text = _detail(
        _entry(
            sdk_translation="TextDelta",
            formatter_result="[] empty",
            formatted_items=[],
            stage_reached=Stage.TRANSLATED,
        )
    )

    assert "Ev → TextDelta" in text
    assert "not rendered — formatter produced nothing" in text


def test_detail_marks_blank_formatter_output() -> None:
    text = _detail(
        _entry(
            formatter_result="StreamingText(3 chars)",
            formatted_items=[SimpleNamespace(text="   ")],
            stage_reached=Stage.FORMATTED,
        )
    )

    assert "(empty render)" in text


def test_detail_notes_history_render_without_formatter_capture() -> None:
    text = _detail(_entry(stage_reached=Stage.RENDERED))

    assert "rendered via _render_history_item" in text


def test_detail_escapes_markup_in_payload_and_shows_totals() -> None:
    tape = EventTape()
    entry = tape.record_raw(_Ev(text="[bold]literal[/bold]"))
    tape.mark_rendered(entry)

    text = _detail(_entry(raw_payload={"text": "[bold]literal[/bold]"}), tape)

    assert "[bold]literal[/bold]" in text
    assert "Pipeline totals: ev:1 tx:0 fmt:0 out:1" in text


def test_detail_handles_non_numeric_key() -> None:
    text = _plain(build_tape_detail(EventTape(), "not-an-index", _Fmt()))

    assert "No event selected." in text


# ── formatted-item preview ───────────────────────────────────


def test_render_formatted_items_handles_each_item_kind() -> None:
    items: list[object] = [
        SimpleNamespace(text="streamed\n"),
        SimpleNamespace(text="  "),  # whitespace-only deltas are skipped
        SimpleNamespace(renderable=Text("replaced")),
        SimpleNamespace(renderable=Text("   ")),  # renders blank, skipped
        Text("bare renderable"),
        Text(""),
    ]

    assert _render_formatted_items(items).splitlines() == [
        "streamed",
        "replaced",
        "bare renderable",
    ]


def test_render_formatted_items_survives_broken_renderable() -> None:
    class _Broken:
        def __rich_console__(self, console: Console, options: object) -> None:
            raise RuntimeError("cannot render")

    assert _render_formatted_items([_Broken()]) == "<_Broken: render failed>"


# ── payload truncation ───────────────────────────────────────


def test_format_payload_truncates_extra_lines() -> None:
    payload = {f"k{n}": n for n in range(_PAYLOAD_MAX_LINES * 3)}

    lines = _format_payload(payload).splitlines()

    assert len(lines) == _PAYLOAD_MAX_LINES + 1
    assert lines[-1].endswith("more lines)")


def test_format_payload_detail_truncates_values_and_extra_lines() -> None:
    payload: dict[str, object] = {f"k{n}": n for n in range(_DETAIL_PAYLOAD_MAX_LINES * 2)}
    payload["long"] = "x" * 2000

    lines = _format_payload_detail(payload).splitlines()

    assert len(lines) == _DETAIL_PAYLOAD_MAX_LINES + 1
    assert "more lines)" in lines[-1]


def test_format_payload_detail_truncates_long_values() -> None:
    rendered = _format_payload_detail({"long": "x" * 2000})

    # Values are cut to 500 characters (plus an ellipsis marker).
    assert "x" * 500 in rendered
    assert "x" * 501 not in rendered


@pytest.mark.parametrize("formatter", [_format_payload, _format_payload_detail])
def test_payload_formatters_fall_back_to_repr_for_unserializable_keys(formatter) -> None:
    payload = {(1, 2): "tuple keys are not valid JSON"}

    assert formatter(payload) == repr(payload)
