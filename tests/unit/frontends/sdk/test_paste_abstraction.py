"""
Unit tests for the bracketed-paste abstraction in
:mod:`omnigent_ui_sdk.terminal._host`.

Covers the pure threshold/format helpers, the host-level registry
methods, the ``Keys.BracketedPaste`` key binding, and the
re-expansion path through :meth:`TerminalHost.run`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from omnigent_ui_sdk.terminal._host import (
    _PASTE_CHAR_THRESHOLD,
    _PASTE_LINE_THRESHOLD,
    TerminalHost,
    _format_paste_placeholder,
    _normalize_paste,
    _should_abstract_paste,
)
from prompt_toolkit.keys import Keys


class _RecordingBuffer:
    """Tiny prompt-toolkit ``Buffer`` stand-in that records insertions."""

    def __init__(self, text: str = "") -> None:
        """:param text: Initial buffer contents."""
        self.text = text

    def insert_text(self, text: str) -> None:
        """:param text: Appended to the recorded buffer."""
        self.text += text


def _fire_paste(host: TerminalHost, payload: str, prefix: str = "") -> _RecordingBuffer:
    """
    Drive the host's ``Keys.BracketedPaste`` handler with *payload*.

    :param host: A constructed :class:`TerminalHost`.
    :param payload: Bracketed-paste data, e.g. ``"a\\r\\nb"``.
    :param prefix: Initial buffer contents the user had typed.
    :returns: The recording buffer after the handler ran.
    """
    bindings = host._kb.get_bindings_for_keys((Keys.BracketedPaste,))
    # Exactly one binding — duplicates would chain inserts and
    # absent registration means prompt-toolkit's default dumps the
    # whole payload into the buffer.
    assert len(bindings) == 1, f"expected 1 BracketedPaste binding, got {len(bindings)}"
    buf = _RecordingBuffer(prefix)
    bindings[0].handler(SimpleNamespace(data=payload, current_buffer=buf))
    return buf


async def _drive_run(
    host: TerminalHost,
    inputs: list[str],
    expected_dispatches: int = 0,
) -> list[tuple[str, list[Any]]]:
    """
    Drive ``host.run`` once per *inputs* entry, then break the loop.

    :param host: Host whose ``_read_input`` is monkeypatched.
    :param inputs: Lines to feed; each becomes one prompt cycle.
    :param expected_dispatches: Wait until this many handler calls
        complete before returning. ``0`` skips the wait — use for
        tests that assert no dispatch happens.
    :returns: ``(text, files)`` recorded for every handler dispatch.
    """
    pending: list[str | BaseException] = [*inputs, KeyboardInterrupt()]
    received: list[tuple[str, list[Any]]] = []
    done = asyncio.Event()

    async def _fake_read_input() -> str:
        """Yield queued lines, then raise to break the loop."""
        item = pending.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def _handler(text: str, files: list[Any]) -> None:
        """Capture every handler dispatch made by ``host.run``."""
        received.append((text, files))
        if len(received) >= expected_dispatches:
            done.set()

    host._read_input = _fake_read_input  # type: ignore[method-assign]
    await host.run(_handler)
    if expected_dispatches:
        await asyncio.wait_for(done.wait(), timeout=1.0)
    return received


# ---------------------------------------------------------------------------
# Pure helpers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\r\nb", "a\nb"),
        ("a\rb", "a\nb"),
        ("a\nb", "a\nb"),
        ("a\r\nb\rc\nd", "a\nb\nc\nd"),
        ("", ""),
    ],
)
def test_normalize_paste_collapses_line_endings(raw: str, expected: str) -> None:
    """``\\r\\n`` and bare ``\\r`` collapse to ``\\n``; ``\\n`` stays."""
    assert _normalize_paste(raw) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Below both thresholds — stay inline.
        ("short", False),
        ("a\nb\nc", False),
        ("x" * (_PASTE_CHAR_THRESHOLD - 1), False),
        # At line threshold (3 newlines → 4 lines) — abstract.
        ("\n".join(["x"] * _PASTE_LINE_THRESHOLD), True),
        # At char threshold — abstract.
        ("x" * _PASTE_CHAR_THRESHOLD, True),
        # Past both — abstract.
        ("y" * (_PASTE_CHAR_THRESHOLD + 100), True),
    ],
)
def test_should_abstract_paste_threshold_boundaries(text: str, expected: bool) -> None:
    # The at-threshold rows pin >= semantics; off-by-one regressions flip them.
    assert _should_abstract_paste(text) is expected


@pytest.mark.parametrize(
    ("block_id", "text", "expected"),
    [
        (1, "\n".join(["row"] * 22), "[Pasted text #1 +22 lines]"),
        (2, "x" * 1230, "[Pasted text #2 +1230 chars]"),
    ],
)
def test_format_paste_placeholder_picks_metric_by_shape(
    block_id: int, text: str, expected: str
) -> None:
    """Multi-line pastes report ``+M lines``; single-line ones ``+M chars``."""
    assert _format_paste_placeholder(block_id=block_id, text=text) == expected


# ---------------------------------------------------------------------------
# Host registry methods


def test_handle_paste_text_returns_empty_for_empty_payload() -> None:
    """Empty pastes return ``""`` so the binding can skip the insert."""
    host = TerminalHost(model_name="test")
    assert host._handle_paste_text("") == ""
    assert host._pasted_blocks == []


def test_handle_paste_text_passes_short_paste_through_unchanged() -> None:
    """Short pastes return the normalized text without registering."""
    host = TerminalHost(model_name="test")
    assert host._handle_paste_text("a\r\nb") == "a\nb"
    # Short pastes stay editable inline; a placeholder would just get in the way.
    assert host._pasted_blocks == []


def test_handle_paste_text_abstracts_large_paste() -> None:
    """Multi-line pastes >= line threshold register a placeholder."""
    host = TerminalHost(model_name="test")
    paste = "\r\n".join(f"line-{i}" for i in range(_PASTE_LINE_THRESHOLD))
    out = host._handle_paste_text(paste)

    assert out == f"[Pasted text #1 +{_PASTE_LINE_THRESHOLD} lines]"
    # Registry must hold the normalized full content for re-splicing.
    assert len(host._pasted_blocks) == 1
    assert host._pasted_blocks[0].content == paste.replace("\r\n", "\n")
    assert host._pasted_blocks[0].placeholder == out


def test_handle_paste_text_skips_drag_and_drop_file_paths(tmp_path: Path) -> None:
    """A paste containing a real file path bypasses abstraction.

    Without this carve-out a dropped file would render as a placeholder
    and never reach the existing attachment-upload flow in ``run()``.
    """
    sample = tmp_path / "drop.txt"
    sample.write_text("hi")
    paste = str(sample)
    host = TerminalHost(model_name="test")

    assert host._handle_paste_text(paste) == paste
    assert host._pasted_blocks == []


def test_register_pasted_block_assigns_sequential_ids() -> None:
    """``#N`` ordinals start at 1 and increment per call."""
    host = TerminalHost(model_name="test")
    first = host._register_pasted_block("a" * _PASTE_CHAR_THRESHOLD)
    second = host._register_pasted_block("b" * _PASTE_CHAR_THRESHOLD)

    assert (first.block_id, second.block_id) == (1, 2)
    # Placeholder ordinal must match block_id — expansion looks up by string.
    assert first.placeholder.startswith("[Pasted text #1")
    assert second.placeholder.startswith("[Pasted text #2")


def test_expand_pasted_blocks_round_trips_full_content() -> None:
    """Placeholders expand back to the original content."""
    host = TerminalHost(model_name="test")
    block = host._register_pasted_block("\n".join(["row"] * 22))
    assert (
        host._expand_pasted_blocks(f"summarize {block.placeholder} please")
        == f"summarize {block.content} please"
    )


def test_expand_pasted_blocks_clears_registry() -> None:
    """Registry empties after expansion so the next message starts at #1."""
    host = TerminalHost(model_name="test")
    block = host._register_pasted_block("\n".join(["x"] * 10))
    host._expand_pasted_blocks(block.placeholder)
    assert host._pasted_blocks == []


def test_expand_pasted_blocks_drops_edited_placeholder() -> None:
    """A placeholder edited in the buffer is not expanded."""
    host = TerminalHost(model_name="test")
    block = host._register_pasted_block("\n".join(["x"] * 10))
    # Drop the trailing ']' to simulate the user backspacing into the marker.
    edited = block.placeholder[:-1]

    out = host._expand_pasted_blocks(edited)

    # Partial marker passes through; full content is dropped (safer than guessing).
    assert out == edited
    assert block.content not in out


def test_expand_pasted_blocks_handles_multiple_blocks() -> None:
    """Each registered block expands at its own placeholder."""
    host = TerminalHost(model_name="test")
    a = host._register_pasted_block("\n".join(["a"] * 10))
    b = host._register_pasted_block("b" * _PASTE_CHAR_THRESHOLD)
    assert (
        host._expand_pasted_blocks(f"first {a.placeholder} then {b.placeholder}")
        == f"first {a.content} then {b.content}"
    )


def test_expand_pasted_blocks_no_op_on_empty_registry() -> None:
    """Expanding with no registered blocks returns the text unchanged."""
    host = TerminalHost(model_name="test")
    assert host._expand_pasted_blocks("hello") == "hello"


# ---------------------------------------------------------------------------
# Key binding integration


def test_bracketed_paste_binding_inserts_placeholder_for_large_paste() -> None:
    """The registered binding writes a placeholder, not the raw payload."""
    host = TerminalHost(model_name="test")
    payload = "\r\n".join(f"line-{i}" for i in range(_PASTE_LINE_THRESHOLD))
    buf = _fire_paste(host, payload, prefix="ask: ")

    expected = f"[Pasted text #1 +{_PASTE_LINE_THRESHOLD} lines]"
    assert buf.text == f"ask: {expected}"
    # Critical: raw payload must NOT appear, or the abstraction is defeated.
    assert payload.replace("\r\n", "\n") not in buf.text


def test_bracketed_paste_binding_inserts_short_paste_inline() -> None:
    """Short pastes go inline (CRLF-normalized); no placeholder is registered."""
    host = TerminalHost(model_name="test")
    buf = _fire_paste(host, "first\r\nsecond")
    assert buf.text == "first\nsecond"
    assert host._pasted_blocks == []


def test_bracketed_paste_binding_skips_empty_payload() -> None:
    """Empty pastes do not insert anything."""
    host = TerminalHost(model_name="test")
    buf = _fire_paste(host, "", prefix="untouched")
    assert buf.text == "untouched"
    assert host._pasted_blocks == []


# ---------------------------------------------------------------------------
# End-to-end through TerminalHost.run


@pytest.mark.asyncio
async def test_run_dispatches_full_paste_content_to_handler() -> None:
    """The handler receives the full paste content, not the placeholder.

    Without ``_expand_pasted_blocks`` in the run loop the agent would
    only see ``[Pasted text #1 ...]`` and never the actual paste.
    """
    host = TerminalHost(model_name="test")
    block = host._register_pasted_block("x" * _PASTE_CHAR_THRESHOLD)

    received = await _drive_run(host, [f"explain {block.placeholder}"], expected_dispatches=1)

    # Exactly one dispatch — the second iteration raises before reaching it.
    assert len(received) == 1
    text, files = received[0]
    # The decisive assertion: placeholder was re-spliced to original content.
    assert text == f"explain {'x' * _PASTE_CHAR_THRESHOLD}"
    assert files == []
    # Registry was drained at expansion time.
    assert host._pasted_blocks == []


@pytest.mark.asyncio
async def test_run_resets_registry_on_empty_submit() -> None:
    """Empty submit clears the registry so ``#N`` restarts at 1.

    Reproduces backspace-then-Enter: a paste was registered, the user
    deleted the placeholder and submitted nothing. Without the reset,
    the next paste would be ``#2``.
    """
    host = TerminalHost(model_name="test")
    host._register_pasted_block("x" * _PASTE_CHAR_THRESHOLD)

    received = await _drive_run(host, [""])

    # Empty submit + no attachments → handler must not be reached.
    assert received == []
    assert host._pasted_blocks == []
