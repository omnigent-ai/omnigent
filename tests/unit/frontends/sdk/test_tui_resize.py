"""Tests that the TUI adapts to terminal size changes.

Verifies that ``_term_width``/``_term_height`` monkeypatching affects
Rich rendering width, streaming text wrapping, viewport ceiling gating,
prompt bar width, and live region capping.
"""

from __future__ import annotations

import sys

import pytest
from omnigent_ui_sdk.terminal._formatter import StreamingText, StreamLive
from omnigent_ui_sdk.terminal._host import (
    _BOTTOM_RESERVED_ROWS,
    TerminalHost,
)
from rich.text import Text

# ── Helpers ─────────────────────────────────────────────────────


def _noop_stdout(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Redirect stdout.write to a capture list and noop flush.

    Returns the capture list so callers can inspect what was written.
    """
    writes: list[str] = []
    monkeypatch.setattr(sys.stdout, "write", lambda d: (writes.append(d), len(d))[1])
    monkeypatch.setattr(sys.stdout, "flush", lambda: None)
    return writes


# ── Width → Rich render width ──────────────────────────────────


def test_resize_width_affects_rich_render_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Monkeypatching ``_term_width`` to 40 → Rich Console renders at width 40.

    The host creates a ``Console(width=_term_width())`` for non-streaming
    items. Narrower terminals produce more wrapped lines for the same
    content. Wider terminals produce fewer.
    """
    host = TerminalHost(model_name="test")
    writes = _noop_stdout(monkeypatch)

    # Wide terminal: 200 columns.
    monkeypatch.setattr("omnigent_ui_sdk.terminal._host._term_width", lambda: 200)
    host.output(Text("A" * 100))
    wide_output = "".join(writes)
    wide_lines = wide_output.strip().split("\n")

    writes.clear()

    # Narrow terminal: 40 columns.
    monkeypatch.setattr("omnigent_ui_sdk.terminal._host._term_width", lambda: 40)
    host.output(Text("A" * 100))
    narrow_output = "".join(writes)
    narrow_lines = narrow_output.strip().split("\n")

    # Narrow terminal should produce MORE lines than wide for the same
    # 100-char text.
    assert len(narrow_lines) > len(wide_lines), (
        f"Expected narrow (40 cols) to produce more lines than wide (200 cols) "
        f"for 100-char text. narrow={len(narrow_lines)}, wide={len(wide_lines)}. "
        f"If equal, the Console width is not being set from _term_width."
    )


# ── Width → StreamingText wrap ──────────────────────────────────


def test_resize_width_affects_streaming_text_wrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Narrow width → more lines from same text. Wide width → fewer lines.

    The host wraps ``StreamingText`` at ``_term_width() - indent_width``
    characters. A 30-column terminal wraps sooner than a 200-column one.
    """
    # Build a long text with a trailing newline to flush everything.
    long_text = ("word " * 40).strip() + "\n"

    # Wide terminal: 200 columns → text fits in few lines.
    host_wide = TerminalHost(model_name="test")
    monkeypatch.setattr("omnigent_ui_sdk.terminal._host._term_width", lambda: 200)
    host_wide.output(StreamingText(text=long_text))
    wide_count = host_wide._streamed_line_count

    # Narrow terminal: 30 columns → text wraps into many lines.
    host_narrow = TerminalHost(model_name="test")
    monkeypatch.setattr("omnigent_ui_sdk.terminal._host._term_width", lambda: 30)
    host_narrow.output(StreamingText(text=long_text))
    narrow_count = host_narrow._streamed_line_count

    # Narrow should produce more lines.
    assert narrow_count > wide_count, (
        f"Expected narrow (30 cols) to produce more streamed lines than "
        f"wide (200 cols). narrow={narrow_count}, wide={wide_count}. "
        f"If equal, the wrapping logic in output() is not consulting _term_width."
    )


# ── Height → viewport ceiling ──────────────────────────────────


def test_resize_height_affects_viewport_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Height 10 (ceiling 5) → ``_should_stream_more()`` gates after 5 lines.

    The ceiling is ``_term_height() - _BOTTOM_RESERVED_ROWS``. When
    ``_streamed_line_count >= ceiling``, no more lines are printed.
    """
    host = TerminalHost(model_name="test")
    _noop_stdout(monkeypatch)

    # Small terminal: 10 rows → ceiling = 10 - 5 = 5.
    monkeypatch.setattr("omnigent_ui_sdk.terminal._host._term_height", lambda: 10)
    ceiling_small = 10 - _BOTTOM_RESERVED_ROWS

    # Stream many lines.
    for i in range(20):
        host.output(StreamingText(text=f"line {i}\n"))

    # _streamed_line_count should not exceed the ceiling.
    assert host._streamed_line_count <= ceiling_small, (
        f"Expected _streamed_line_count <= {ceiling_small} (height 10 ceiling), "
        f"got {host._streamed_line_count}. If higher, _should_stream_more is "
        f"not gating correctly for the terminal height."
    )


def test_resize_height_large_allows_more_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Height 50 (ceiling 45) → allows 20+ lines without gating."""
    host = TerminalHost(model_name="test")
    _noop_stdout(monkeypatch)

    monkeypatch.setattr("omnigent_ui_sdk.terminal._host._term_height", lambda: 50)
    ceiling_large = 50 - _BOTTOM_RESERVED_ROWS

    for i in range(20):
        host.output(StreamingText(text=f"line {i}\n"))

    # With a ceiling of 45, all 20 lines should have been streamed.
    assert host._streamed_line_count == 20, (
        f"Expected all 20 lines to be streamed with ceiling {ceiling_large}, "
        f"got {host._streamed_line_count}. If < 20, the gating is too "
        f"aggressive for this terminal height."
    )


# ── Mid-stream resize ──────────────────────────────────────────


def test_resize_mid_stream_changes_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start height 20, stream 10 lines, shrink to 8 → gating kicks in.

    ``_should_stream_more()`` evaluates ``_term_height()`` live each call,
    so a mid-stream terminal resize takes effect immediately.
    """
    host = TerminalHost(model_name="test")
    _noop_stdout(monkeypatch)

    # Start with a tall terminal: ceiling = 20 - 5 = 15.
    height = 20
    monkeypatch.setattr("omnigent_ui_sdk.terminal._host._term_height", lambda: height)
    for i in range(10):
        host.output(StreamingText(text=f"line {i}\n"))

    # 10 lines streamed — still under the ceiling of 15.
    assert host._streamed_line_count == 10

    # Shrink terminal mid-stream to 8 rows → ceiling = 8 - 5 = 3.
    # The streamed count is already 10, which is way above the new
    # ceiling of 3, so no further lines should be printed.
    monkeypatch.setattr("omnigent_ui_sdk.terminal._host._term_height", lambda: 8)
    count_before = host._streamed_line_count
    for i in range(10, 20):
        host.output(StreamingText(text=f"line {i}\n"))

    # No additional lines should have been printed after the resize.
    assert host._streamed_line_count == count_before, (
        f"Expected no additional lines after resize to height 8 "
        f"(ceiling 3, already at {count_before}), but _streamed_line_count "
        f"grew to {host._streamed_line_count}."
    )


# ── build_prompt bar width ──────────────────────────────────────


def test_resize_build_prompt_bar_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prompt bar width tracks ``_term_width()``: 50 → 50 chars, 100 → 100 chars."""
    host = TerminalHost(model_name="test")

    monkeypatch.setattr("omnigent_ui_sdk.terminal._host._term_width", lambda: 50)
    prompt_50 = host.build_prompt()
    # The prompt contains a bar of "─" * width. Extract the bar segment.
    bar_50 = _extract_bar_from_prompt(prompt_50)

    monkeypatch.setattr("omnigent_ui_sdk.terminal._host._term_width", lambda: 100)
    prompt_100 = host.build_prompt()
    bar_100 = _extract_bar_from_prompt(prompt_100)

    assert len(bar_50) == 50, f"Expected bar of 50 '─' chars for width 50, got {len(bar_50)}."
    assert len(bar_100) == 100, f"Expected bar of 100 '─' chars for width 100, got {len(bar_100)}."


def _extract_bar_from_prompt(prompt: object) -> str:
    """Extract the ``─``-bar string from a ``FormattedText`` prompt.

    The prompt's ``("class:bar", bar + "\\n")`` tuple contains the
    separator bar. We find the tuple whose style is ``"class:bar"``
    and whose value consists of ``─`` characters (plus a trailing ``\\n``).
    """
    for style, text in prompt:
        if style == "class:bar" and "─" in text:
            return text.rstrip("\n")
    return ""


# ── build_toolbar right padding ─────────────────────────────────


def test_resize_build_toolbar_right_padding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wider terminal → longer right-padding bar segment in toolbar."""
    host = TerminalHost(model_name="test")

    monkeypatch.setattr("omnigent_ui_sdk.terminal._host._term_width", lambda: 60)
    toolbar_60 = host.build_toolbar()
    bar_60 = _extract_toolbar_bar(toolbar_60)

    monkeypatch.setattr("omnigent_ui_sdk.terminal._host._term_width", lambda: 120)
    toolbar_120 = host.build_toolbar()
    bar_120 = _extract_toolbar_bar(toolbar_120)

    # The right-padding bar segment should be longer at 120 cols than
    # at 60 cols. The padding formula is:
    # max(0, width - 2 - len(parts) - len(hints) - len(state_segment))
    assert len(bar_120) > len(bar_60), (
        f"Expected wider terminal (120) to have longer toolbar padding "
        f"than narrow (60). bar_120={len(bar_120)}, bar_60={len(bar_60)}."
    )


def _extract_toolbar_bar(toolbar: object) -> str:
    """Extract the right-padding bar from a toolbar FormattedText.

    The toolbar layout is: ``("class:bar", "──")``, model, hints,
    ``("class:bar", "─" * bar_right)``, state. The right-padding
    segment is the last ``class:bar`` entry.
    """
    bars = [(s, t) for s, t in toolbar if s == "class:bar"]
    if len(bars) >= 2:
        # Second bar entry is the right-padding segment.
        return bars[1][1]
    return ""


# ── Live region cap vs. height ──────────────────────────────────


def test_resize_live_region_cap_changes_with_height(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Height 15 (ceiling 10) → ``_live_line_count`` capped at 10.
    Height 40 (ceiling 35) → 30-line content fits uncapped.
    """
    # Generate 30-line content.
    long_text = "\n".join(f"line {i}" for i in range(30))
    writes = _noop_stdout(monkeypatch)

    # Small terminal: ceiling = 15 - 5 = 10.
    host_small = TerminalHost(model_name="test")
    monkeypatch.setattr("omnigent_ui_sdk.terminal._host._term_height", lambda: 15)
    host_small.output(StreamLive(renderable=Text(long_text)))
    ceiling_small = 15 - _BOTTOM_RESERVED_ROWS

    assert host_small._live_line_count <= ceiling_small, (
        f"Expected _live_line_count <= {ceiling_small} at height 15, "
        f"got {host_small._live_line_count}. Uncapped live regions cause "
        f"scrollback duplication."
    )

    writes.clear()

    # Tall terminal: ceiling = 40 - 5 = 35.
    host_tall = TerminalHost(model_name="test")
    monkeypatch.setattr("omnigent_ui_sdk.terminal._host._term_height", lambda: 40)
    host_tall.output(StreamLive(renderable=Text(long_text)))

    # 30 lines fits within ceiling 35 — should be uncapped.
    assert host_tall._live_line_count == 30, (
        f"Expected _live_line_count == 30 (all lines fit within ceiling 35), "
        f"got {host_tall._live_line_count}."
    )
