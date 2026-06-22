"""Unit tests for the SSE-to-UI debug tooling in ``_event_tape.py``.

Exercises ``EventTape``, ``TapeEntry``, ``PipelineCounters``,
``build_tape_targets``, ``build_tape_detail``, ``_summarize_formatted_item``,
``open_event_log``, and ``log_entry_jsonl``.
"""

from __future__ import annotations

import json
import pathlib
import time
from dataclasses import dataclass

import pytest

from omnigent.repl._event_tape import (
    EventTape,
    PipelineCounters,
    Stage,
    TapeEntry,
    _format_payload,
    _snapshot_event,
    _summarize_formatted_item,
    _truncate_deep,
    build_tape_detail,
    build_tape_targets,
    log_entry_jsonl,
    open_event_log,
)

# ── Synthetic event stubs ──────────────────────────────────────────────
# Real types where the production code only inspects ``type().__name__``
# and doesn't do ``isinstance`` checks on the raw events. Using
# lightweight stubs avoids importing server/SDK internals into a
# self-contained unit-test module.


@dataclass
class _FakeTextDelta:
    """Stub for ``OutputTextDeltaEvent`` — carries a text delta.

    :param delta: The text chunk, e.g. ``"hello"``.
    """

    delta: str


@dataclass
class _FakeCompletedEvent:
    """Stub for ``CompletedEvent`` — signals response completion."""


@dataclass
class _FakeDroppedEvent:
    """Stub for an event that ``_server_event_to_sdk_event`` would drop."""


@dataclass
class _FakeSDKTextDelta:
    """Stub for the translated SDK ``TextDelta`` event.

    :param delta: The text chunk, e.g. ``"hello"``.
    """

    delta: str


# ── StreamingText stub ─────────────────────────────────────────────────


@dataclass
class _FakeStreamingText:
    """Stub for ``omnigent_ui_sdk.StreamingText``.

    :param text: The text content, e.g. ``"Hello world!"``.
    """

    text: str


# ── Formatter stub ─────────────────────────────────────────────────────


class _FakeFmt:
    """Minimal formatter stub exposing ``muted`` and ``accent`` attrs.

    :param muted: Rich markup style for muted text, e.g. ``"dim"``.
    :param accent: Rich markup style for accent text, e.g. ``"bold"``.
    """

    muted: str = "dim"
    accent: str = "bold"


# ── PipelineCounters ───────────────────────────────────────────────────


def test_pipeline_counters_initial_state() -> None:
    """All counters start at zero."""
    counters = PipelineCounters()
    # Every counter field must be zero at construction — a non-zero
    # default would cause the toolbar to show stale data before any
    # events arrive.
    assert counters.raw == 0
    assert counters.translated == 0
    assert counters.formatted == 0
    assert counters.rendered == 0
    assert counters.max_gap_ms == 0.0
    assert counters.max_gap_event_type is None


def test_pipeline_counters_reset() -> None:
    """``reset()`` zeros all fields."""
    counters = PipelineCounters()
    counters.raw = 10
    counters.translated = 8
    counters.formatted = 6
    counters.rendered = 6
    counters.max_gap_ms = 2500.0
    counters.max_gap_event_type = "CompletedEvent"

    counters.reset()

    # After reset, every field should be back to its initial value.
    # If any field survives reset, the toolbar would carry over
    # stale counts from a previous turn.
    assert counters.raw == 0
    assert counters.translated == 0
    assert counters.formatted == 0
    assert counters.rendered == 0
    assert counters.max_gap_ms == 0.0
    assert counters.max_gap_event_type is None


def test_pipeline_counters_toolbar_text_no_gap() -> None:
    """``toolbar_text()`` omits the gap segment when below threshold."""
    counters = PipelineCounters()
    counters.raw = 5
    counters.translated = 4
    counters.formatted = 3
    counters.rendered = 3
    counters.max_gap_ms = 50.0  # Well below _GAP_THRESHOLD_MS

    text = counters.toolbar_text()

    # The output must contain all four counter labels with their
    # exact values. No gap segment because max_gap_ms < threshold.
    assert "ev:5" in text
    assert "tx:4" in text
    assert "fmt:3" in text
    assert "out:3" in text
    assert "gap:" not in text


def test_pipeline_counters_toolbar_text_with_gap() -> None:
    """``toolbar_text()`` includes the gap segment when at or above threshold."""
    counters = PipelineCounters()
    counters.raw = 10
    counters.translated = 10
    counters.formatted = 10
    counters.rendered = 10
    counters.max_gap_ms = 2100.0
    counters.max_gap_event_type = "_FakeCompletedEvent"

    text = counters.toolbar_text()

    # Gap >= threshold triggers the "gap:Xs@EventType" suffix.
    # If missing, the toolbar would hide latency spikes from the user.
    assert "gap:2.1s@_FakeCompletedEvent" in text
    assert "ev:10" in text


# ── EventTape: record_raw ──────────────────────────────────────────────


def test_record_raw_creates_entry() -> None:
    """``record_raw`` appends a TapeEntry and increments ``counters.raw``."""
    counters = PipelineCounters()
    tape = EventTape(counters=counters)
    event = _FakeTextDelta(delta="hi")

    entry = tape.record_raw(event, path="sessions")

    # The entry must capture the event's class name and the path.
    # If raw_event_type is wrong, the overlay would display incorrect
    # event labels.
    assert entry.raw_event_type == "_FakeTextDelta"
    assert entry.path == "sessions"
    assert entry.stage_reached == Stage.RAW
    # Counter must be incremented exactly once per record_raw call.
    assert counters.raw == 1, (
        f"Expected raw counter 1 after one record_raw, got {counters.raw}. "
        "If 0, record_raw failed to increment the counter."
    )


def test_record_raw_delta_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    """First entry has delta_ms=0; subsequent entries have positive deltas."""
    # Use controlled timestamps: first call at t=1000.0, second at t=1000.050
    # (50ms gap). Avoids time.sleep and makes assertions deterministic.
    timestamps = iter([1000.0, 1000.050])
    monkeypatch.setattr(time, "time", lambda: next(timestamps))

    tape = EventTape()

    e1 = tape.record_raw(_FakeTextDelta(delta="a"))
    # First entry: no previous timestamp to diff against.
    assert e1.delta_ms == 0.0, (
        f"First entry should have delta_ms=0.0 (no prior event). Got {e1.delta_ms}."
    )

    e2 = tape.record_raw(_FakeTextDelta(delta="b"))
    # Second entry: delta should be exactly 50ms (1000.050 - 1000.0).
    assert e2.delta_ms == pytest.approx(50.0, abs=0.1), (
        f"Second entry should have delta_ms=50.0 from the mocked timestamps. Got {e2.delta_ms}."
    )


def test_record_raw_tracks_max_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """``record_raw`` updates ``counters.max_gap_ms`` when a larger gap is seen."""
    # Use controlled timestamps: first call at t=1000.0, second at t=1002.0
    # (2-second gap). Avoids touching private ``_last_ts`` attribute.
    timestamps = iter([1000.0, 1002.0])
    monkeypatch.setattr(time, "time", lambda: next(timestamps))

    counters = PipelineCounters()
    tape = EventTape(counters=counters)

    tape.record_raw(_FakeTextDelta(delta="a"))
    tape.record_raw(_FakeCompletedEvent())

    # The delta for the second entry should be exactly 2000ms.
    assert counters.max_gap_ms == pytest.approx(2000.0, abs=0.1), (
        f"Expected max_gap_ms ~2000 after a 2s simulated gap, got {counters.max_gap_ms}."
    )
    assert counters.max_gap_event_type == "_FakeCompletedEvent", (
        "max_gap_event_type should be the event that followed the gap."
    )


# ── EventTape: update_translation ──────────────────────────────────────


def test_update_translation_non_none() -> None:
    """Non-None translation advances stage to TRANSLATED."""
    tape = EventTape()
    entry = tape.record_raw(_FakeTextDelta(delta="x"))

    tape.update_translation(entry, _FakeSDKTextDelta(delta="x"))

    assert entry.sdk_translation == "_FakeSDKTextDelta"
    assert entry.stage_reached == Stage.TRANSLATED
    # Counter must be incremented.
    assert tape.counters.translated == 1


def test_update_translation_none() -> None:
    """None translation marks as dropped, stage stays RAW."""
    tape = EventTape()
    entry = tape.record_raw(_FakeDroppedEvent())

    tape.update_translation(entry, None)

    assert entry.sdk_translation == "None (dropped)"
    # Stage must NOT advance — the event was dropped.
    assert entry.stage_reached == Stage.RAW
    # Translated counter must NOT be incremented for dropped events.
    assert tape.counters.translated == 0, (
        "Dropped events (None translation) must not increment the translated counter."
    )


# ── EventTape: update_format ──────────────────────────────────────────


def test_update_format_with_items() -> None:
    """Non-empty formatter output advances stage to FORMATTED."""
    tape = EventTape()
    entry = tape.record_raw(_FakeTextDelta(delta="x"))
    tape.update_translation(entry, _FakeSDKTextDelta(delta="x"))

    items = [_FakeStreamingText(text="Hello")]
    tape.update_format(entry, items)

    assert entry.stage_reached == Stage.FORMATTED
    assert "_FakeStreamingText(5 chars)" in entry.formatter_result
    assert tape.counters.formatted == 1


def test_update_format_empty_list() -> None:
    """Empty formatter output keeps stage at TRANSLATED, records '[] empty'."""
    tape = EventTape()
    entry = tape.record_raw(_FakeTextDelta(delta="x"))
    tape.update_translation(entry, _FakeSDKTextDelta(delta="x"))

    tape.update_format(entry, [])

    assert entry.formatter_result == "[] empty"
    # Stage must NOT advance past TRANSLATED for empty output.
    assert entry.stage_reached == Stage.TRANSLATED
    assert tape.counters.formatted == 0, (
        "Empty formatter output must not increment the formatted counter."
    )


# ── EventTape: mark_rendered ──────────────────────────────────────────


def test_mark_rendered() -> None:
    """``mark_rendered`` advances stage to RENDERED and increments counter."""
    tape = EventTape()
    entry = tape.record_raw(_FakeTextDelta(delta="x"))
    tape.update_translation(entry, _FakeSDKTextDelta(delta="x"))
    tape.update_format(entry, [_FakeStreamingText(text="hi")])

    tape.mark_rendered(entry, count=1)

    assert entry.stage_reached == Stage.RENDERED
    assert tape.counters.rendered == 1


# ── EventTape: entries / summary_counts ───────────────────────────────


def test_entries_returns_snapshot() -> None:
    """``entries`` returns a list copy of the ring buffer."""
    tape = EventTape()
    tape.record_raw(_FakeTextDelta(delta="a"))
    tape.record_raw(_FakeCompletedEvent())

    entries = tape.entries
    # Two events recorded → two entries in the snapshot.
    assert len(entries) == 2, (
        f"Expected 2 entries, got {len(entries)}. If 0, record_raw failed to append to the buffer."
    )
    assert entries[0].raw_event_type == "_FakeTextDelta"
    assert entries[1].raw_event_type == "_FakeCompletedEvent"


def test_summary_counts() -> None:
    """``summary_counts`` groups entries by raw_event_type."""
    tape = EventTape()
    for _ in range(3):
        tape.record_raw(_FakeTextDelta(delta="x"))
    tape.record_raw(_FakeCompletedEvent())

    counts = tape.summary_counts()
    assert counts == {"_FakeTextDelta": 3, "_FakeCompletedEvent": 1}


def test_ring_buffer_eviction() -> None:
    """Entries beyond capacity are evicted FIFO."""
    small_capacity = 5
    tape = EventTape(capacity=small_capacity)
    for i in range(10):
        tape.record_raw(_FakeTextDelta(delta=str(i)))

    entries = tape.entries
    # Only the last 5 should remain after 10 inserts.
    assert len(entries) == small_capacity, (
        f"Expected {small_capacity} entries after overflow, "
        f"got {len(entries)}. Ring buffer eviction is broken."
    )
    # The oldest entry should be the 6th event (index 5, delta="5").
    # This verifies FIFO eviction, not LIFO.
    assert entries[0].raw_event_type == "_FakeTextDelta"


# ── EventTape: reset_turn ─────────────────────────────────────────────


def test_reset_turn_zeros_counters_keeps_entries() -> None:
    """``reset_turn`` resets counters but preserves the tape buffer."""
    tape = EventTape()
    tape.record_raw(_FakeTextDelta(delta="a"))
    tape.record_raw(_FakeTextDelta(delta="b"))

    tape.reset_turn()

    # Counters should be zeroed for the new turn.
    assert tape.counters.raw == 0
    # But the tape buffer should still have entries from the previous turn.
    assert len(tape.entries) == 2, "reset_turn should preserve buffer entries, not clear them."


# ── _summarize_formatted_item ──────────────────────────────────────────


def test_summarize_streaming_text() -> None:
    """StreamingText-like objects show class name + char count."""
    item = _FakeStreamingText(text="Hello!")
    result = _summarize_formatted_item(item)
    assert result == "_FakeStreamingText(6 chars)"


def test_summarize_generic_object() -> None:
    """Objects without a .text attr show just the class name."""
    result = _summarize_formatted_item(42)
    assert result == "int"


# ── build_tape_targets ─────────────────────────────────────────────────


def test_build_tape_targets_empty() -> None:
    """Empty tape returns no sidebar targets."""
    tape = EventTape()
    targets = build_tape_targets(tape)
    assert targets == [], "An empty tape should produce zero sidebar targets."


def test_build_tape_targets_with_entries() -> None:
    """Tape with entries returns one target per entry with correct icons."""
    tape = EventTape()
    e1 = tape.record_raw(_FakeTextDelta(delta="hi"))
    tape.update_translation(e1, _FakeSDKTextDelta(delta="hi"))
    tape.mark_rendered(e1)

    e2 = tape.record_raw(_FakeCompletedEvent())
    tape.update_translation(e2, None)

    targets = build_tape_targets(tape)

    # One target per entry.
    assert len(targets) == 2, f"Expected 2 targets for 2 entries, got {len(targets)}."
    # First target: rendered → green icon.
    assert targets[0].key == "0"
    assert "_FakeTextDelta" in targets[0].label
    assert targets[0].icon == "🟢"
    # Second target: dropped → red icon.
    assert targets[1].key == "1"
    assert targets[1].icon == "🔴"


# ── build_tape_detail ──────────────────────────────────────────────────


def test_build_tape_detail_shows_pipeline_journey() -> None:
    """Detail panel shows translation, formatter, and rendered preview."""
    tape = EventTape()
    e1 = tape.record_raw(_FakeTextDelta(delta="hi"))
    tape.update_translation(e1, _FakeSDKTextDelta(delta="hi"))
    tape.update_format(e1, [_FakeStreamingText(text="hi")])
    tape.mark_rendered(e1)

    fmt = _FakeFmt()
    renderable = build_tape_detail(tape, "0", fmt)

    from rich.console import Console

    console = Console(width=120, no_color=True, file=None)
    with console.capture() as capture:
        console.print(renderable)
    text = capture.get()

    # Event type in the header.
    assert "_FakeTextDelta" in text
    # Translation result.
    assert "_FakeSDKTextDelta" in text
    # Formatter output summary.
    assert "_FakeStreamingText(2 chars)" in text
    # "Rendered As" section should show the actual text content
    # that host.output() received — "hi" from the StreamingText.
    assert "Rendered As" in text
    assert "hi" in text


def test_build_tape_detail_rich_renderable_preview() -> None:
    """Detail panel renders Rich renderables through Console capture."""
    from rich.text import Text as RichText

    tape = EventTape()
    e1 = tape.record_raw(_FakeTextDelta(delta="x"))
    tape.update_translation(e1, _FakeSDKTextDelta(delta="x"))
    # Simulate formatter producing a Rich Text object (e.g. from
    # format_response_start or format_error).
    rich_item = RichText("Agent response started")
    tape.update_format(e1, [rich_item])
    tape.mark_rendered(e1)

    fmt = _FakeFmt()
    renderable = build_tape_detail(tape, "0", fmt)

    from rich.console import Console

    console = Console(width=120, no_color=True, file=None)
    with console.capture() as capture:
        console.print(renderable)
    text = capture.get()

    # The "Rendered As" section should show the Rich Text content
    # as it would appear on the terminal.
    assert "Agent response started" in text


def test_build_tape_detail_dropped_event() -> None:
    """Detail panel shows dropped status for untranslated events."""
    tape = EventTape()
    e1 = tape.record_raw(_FakeDroppedEvent())
    tape.update_translation(e1, None)

    fmt = _FakeFmt()
    renderable = build_tape_detail(tape, "0", fmt)

    from rich.console import Console

    console = Console(width=120, no_color=True, file=None)
    with console.capture() as capture:
        console.print(renderable)
    text = capture.get()

    # Should show the dropped translation.
    assert "None (dropped)" in text
    # Should show the "not rendered" status.
    assert "not rendered" in text


def test_build_tape_detail_invalid_key() -> None:
    """Detail panel handles invalid target key gracefully."""
    tape = EventTape()
    fmt = _FakeFmt()
    renderable = build_tape_detail(tape, "999", fmt)

    from rich.console import Console

    console = Console(width=120, no_color=True, file=None)
    with console.capture() as capture:
        console.print(renderable)
    text = capture.get()
    assert "No event selected" in text


# ── open_event_log ─────────────────────────────────────────────────────


def test_open_event_log_creates_file(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``open_event_log`` creates the debug directory and returns a valid path."""
    # Redirect HOME so we don't pollute the real ~/.omnigent/debug/.
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    path = open_event_log("sess_abc123")

    assert path.parent.exists(), "Debug directory should be created."
    assert path.name == "events-sess_abc123.jsonl"
    assert str(tmp_path) in str(path)


def test_open_event_log_sanitizes_id(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``open_event_log`` replaces unsafe characters in the session id."""
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    path = open_event_log("sess/../../etc/passwd")

    # All slashes and dots should be replaced with underscores.
    assert "/" not in path.name.replace("events-", "").replace(".jsonl", "")
    assert ".." not in path.stem


# ── log_entry_jsonl ────────────────────────────────────────────────────


def test_log_entry_jsonl_writes_valid_json(tmp_path: pathlib.Path) -> None:
    """``log_entry_jsonl`` writes one JSON line with all expected fields."""
    log_file = tmp_path / "test.jsonl"

    entry = TapeEntry(
        ts=1715694123.456,
        delta_ms=3.2,
        raw_event_type="OutputTextDeltaEvent",
        sdk_translation="TextDelta",
        formatter_result="StreamingText(5 chars)",
        stage_reached=Stage.RENDERED,
        path="sessions",
    )

    with open(log_file, "a") as fh:
        log_entry_jsonl(fh, entry)

    lines = log_file.read_text().strip().split("\n")
    # Exactly one line should be written per call.
    assert len(lines) == 1, (
        f"Expected 1 JSONL line, got {len(lines)}. If 0, the write or flush failed."
    )

    record = json.loads(lines[0])
    # Verify all expected fields are present with correct values.
    assert record["ts"] == 1715694123.456
    assert record["delta_ms"] == 3.2
    assert record["raw_type"] == "OutputTextDeltaEvent"
    assert record["sdk_type"] == "TextDelta"
    assert record["fmt_items"] == "StreamingText(5 chars)"
    assert record["stage"] == "rendered"
    assert record["rendered"] is True
    assert record["path"] == "sessions"
    # Wall-clock ISO timestamp should be present.
    assert "wall" in record
    assert "2024" in record["wall"] or "2025" in record["wall"] or "2026" in record["wall"]


def test_log_entry_jsonl_dropped_event(tmp_path: pathlib.Path) -> None:
    """Dropped events have sdk_type=None and rendered=False."""
    log_file = tmp_path / "test.jsonl"

    entry = TapeEntry(
        ts=1715694123.456,
        delta_ms=0.0,
        raw_event_type="UnknownEvent",
        sdk_translation="None (dropped)",
        formatter_result=None,
        stage_reached=Stage.RAW,
        path="sessions",
    )

    with open(log_file, "a") as fh:
        log_entry_jsonl(fh, entry)

    record = json.loads(log_file.read_text().strip())
    # sdk_type is the raw translation string (not None for dropped).
    assert record["sdk_type"] == "None (dropped)"
    # fmt_items is None because no formatter ran on the dropped event.
    assert record["fmt_items"] is None
    assert record["rendered"] is False
    assert record["stage"] == "raw"


# ── Full pipeline walkthrough ──────────────────────────────────────────


def test_full_pipeline_walkthrough() -> None:
    """Exercise the complete record → translate → format → render pipeline.

    Verifies that counters increment correctly at each stage and that
    the tape entries reflect the full journey.
    """
    counters = PipelineCounters()
    tape = EventTape(counters=counters)

    # Event 1: fully rendered text delta.
    e1 = tape.record_raw(_FakeTextDelta(delta="Hello"))
    tape.update_translation(e1, _FakeSDKTextDelta(delta="Hello"))
    tape.update_format(e1, [_FakeStreamingText(text="Hello")])
    tape.mark_rendered(e1)

    # Event 2: translated but formatter produced nothing.
    e2 = tape.record_raw(_FakeCompletedEvent())
    tape.update_translation(e2, _FakeCompletedEvent())
    tape.update_format(e2, [])

    # Event 3: dropped at translation.
    e3 = tape.record_raw(_FakeDroppedEvent())
    tape.update_translation(e3, None)

    # Counter assertions:
    # raw=3 because three events were recorded.
    assert counters.raw == 3, f"Expected raw=3 (three events), got {counters.raw}."
    # translated=2 because event 3 was dropped (None).
    assert counters.translated == 2, (
        f"Expected translated=2 (event 3 dropped), got {counters.translated}."
    )
    # formatted=1 because only event 1 produced formatter items.
    assert counters.formatted == 1, (
        f"Expected formatted=1 (only event 1 had items), got {counters.formatted}."
    )
    # rendered=1 because only event 1 was rendered.
    assert counters.rendered == 1, f"Expected rendered=1 (only event 1), got {counters.rendered}."

    # Stage assertions:
    assert e1.stage_reached == Stage.RENDERED
    assert e2.stage_reached == Stage.TRANSLATED  # formatter produced nothing
    assert e3.stage_reached == Stage.RAW  # dropped at translation


# ── _snapshot_event ────────────────────────────────────────────────────


def test_snapshot_event_dataclass() -> None:
    """Dataclass events are captured via ``dataclasses.asdict``."""
    event = _FakeTextDelta(delta="hello world")
    result = _snapshot_event(event)
    assert result is not None
    # The snapshot must include the actual field values, not class
    # metadata. If delta is missing, the overlay would show empty JSON.
    assert result["delta"] == "hello world"


def test_snapshot_event_plain_object() -> None:
    """Plain objects with __dict__ are captured via ``vars()``."""

    class _PlainEvent:
        """Stub for a plain event object."""

        def __init__(self) -> None:
            self.status = "running"
            self.detail = "test"

    event = _PlainEvent()
    result = _snapshot_event(event)
    assert result is not None
    assert result["status"] == "running"
    assert result["detail"] == "test"


def test_snapshot_event_non_serializable_returns_none() -> None:
    """Primitive types without __dict__ return None."""
    # Integers don't have vars() — snapshot should return None.
    result = _snapshot_event(42)
    assert result is None


# ── _truncate_deep ─────────────────────────────────────────────────────


def test_truncate_deep_short_strings_unchanged() -> None:
    """Strings shorter than max_chars pass through unmodified."""
    result = _truncate_deep({"key": "short"}, max_chars=120)
    assert result == {"key": "short"}


def test_truncate_deep_long_strings_truncated() -> None:
    """Strings longer than max_chars are truncated with ellipsis."""
    long_str = "x" * 200
    result = _truncate_deep({"key": long_str}, max_chars=10)
    assert result["key"] == "x" * 10 + "…"


def test_truncate_deep_nested_structures() -> None:
    """Truncation applies recursively to dicts and lists."""
    data = {"outer": [{"inner": "a" * 50}]}
    result = _truncate_deep(data, max_chars=10)
    assert result["outer"][0]["inner"] == "a" * 10 + "…"


# ── _format_payload ────────────────────────────────────────────────────


def test_format_payload_simple_dict() -> None:
    """Simple dicts are formatted as indented JSON."""
    payload = {"delta": "hello", "type": "output_text.delta"}
    result = _format_payload(payload)
    # The output should be valid JSON (modulo truncation markers).
    parsed = json.loads(result)
    assert parsed["delta"] == "hello"
    assert parsed["type"] == "output_text.delta"


def test_format_payload_truncates_long_values() -> None:
    """Long string values in the payload are truncated."""
    payload = {"text": "x" * 300}
    result = _format_payload(payload)
    # The truncated value should contain the ellipsis (either as a
    # literal "…" or JSON-escaped "\u2026" depending on json.dumps).
    assert "…" in result or "\\u2026" in result
    # The full 300-char string should NOT appear.
    assert "x" * 300 not in result


def test_format_payload_caps_line_count() -> None:
    """Deeply nested payloads are capped at _PAYLOAD_MAX_LINES."""
    # Create a payload that would produce many lines.
    payload = {f"key_{i}": f"value_{i}" for i in range(100)}
    result = _format_payload(payload)
    lines = result.splitlines()
    # Should be capped (12 lines + 1 "... more lines" line = 13).
    assert len(lines) <= 14, f"Expected <= 14 lines after capping, got {len(lines)}."
    assert "more lines" in lines[-1]


# ── record_raw captures payload ────────────────────────────────────────


def test_record_raw_captures_payload() -> None:
    """``record_raw`` snapshots the event's fields into raw_payload."""
    tape = EventTape()
    event = _FakeTextDelta(delta="hello")

    entry = tape.record_raw(event)

    # The payload should contain the event's field values, enabling
    # JSON inspection in the overlay. If None, the overlay would
    # show no payload for this event type.
    assert entry.raw_payload is not None
    assert entry.raw_payload["delta"] == "hello"


# ── Overlay shows JSON payload ─────────────────────────────────────────


def test_build_tape_detail_shows_payload() -> None:
    """Detail panel includes JSON payload for the selected event."""
    tape = EventTape()
    e1 = tape.record_raw(_FakeTextDelta(delta="visible"))
    tape.update_translation(e1, _FakeSDKTextDelta(delta="visible"))
    tape.mark_rendered(e1)

    fmt = _FakeFmt()
    renderable = build_tape_detail(tape, "0", fmt)

    from rich.console import Console

    console = Console(width=120, no_color=True, file=None)
    with console.capture() as capture:
        console.print(renderable)
    text = capture.get()

    # The JSON payload should be visible in the detail panel.
    # If missing, users can't inspect what the server sent.
    assert "visible" in text, (
        f"Expected the event's JSON payload to appear in the detail panel. Got: {text[:300]}"
    )
    # The "Raw Event Payload" section header should be present.
    assert "Raw Event Payload" in text


# ── JSONL log includes payload ─────────────────────────────────────────


def test_log_entry_jsonl_includes_payload(
    tmp_path: pathlib.Path,
) -> None:
    """JSONL log entries include the raw_payload field."""
    log_file = tmp_path / "test.jsonl"

    entry = TapeEntry(
        ts=1715694123.456,
        delta_ms=0.0,
        raw_event_type="_FakeTextDelta",
        sdk_translation="TextDelta",
        stage_reached=Stage.RENDERED,
        path="sessions",
        raw_payload={"delta": "hello", "type": "output_text.delta"},
    )

    with open(log_file, "a") as fh:
        log_entry_jsonl(fh, entry)

    record = json.loads(log_file.read_text().strip())
    # The payload field must be present and contain the event data.
    # This enables `jq '.payload'` on the JSONL file for inspection.
    assert record["payload"] is not None
    assert record["payload"]["delta"] == "hello"
    assert record["payload"]["type"] == "output_text.delta"
