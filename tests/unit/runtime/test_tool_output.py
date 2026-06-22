"""
Tests for :func:`omnigent.runtime.tool_output.cap_tool_output` — the canonical
size cap applied to every ``function_call_output`` producer's ``output`` field.
Each assertion is chosen so the corresponding production breakage turns it red.
"""

from __future__ import annotations

from omnigent.runtime.tool_output import MAX_TOOL_OUTPUT_BYTES, cap_tool_output

_TRUNCATION_MARKER = "[output truncated by omnigent:"


def test_cap_tool_output_passes_through_when_within_cap() -> None:
    """A tool result at or under the cap is returned unchanged (same object)."""
    small = "hello world"
    # `is` (not just ==) proves no re-encode/copy on the common path.
    assert cap_tool_output(small) is small
    # Exactly at the cap is still within bounds — the check is `<=`, so a
    # full-size-but-not-over result must hit the same no-copy passthrough
    # (`is`), not fall into the truncation branch (which builds a new string).
    at_cap = "x" * MAX_TOOL_OUTPUT_BYTES
    assert cap_tool_output(at_cap) is at_cap
    assert _TRUNCATION_MARKER not in cap_tool_output(at_cap)


def test_cap_tool_output_truncates_when_over_cap() -> None:
    """An over-cap result is truncated to the cap plus a byte-accurate notice."""
    over = 50_000
    big = "x" * (MAX_TOOL_OUTPUT_BYTES + over)
    capped = cap_tool_output(big)
    # The kept prefix is exactly the first MAX_TOOL_OUTPUT_BYTES bytes...
    assert capped.startswith("x" * MAX_TOOL_OUTPUT_BYTES)
    # ...followed by the notice naming how many bytes were dropped. If `over`
    # is reported wrong, the byte arithmetic in cap_tool_output regressed.
    assert _TRUNCATION_MARKER in capped
    assert f"{over} of {MAX_TOOL_OUTPUT_BYTES + over} bytes omitted" in capped
    # The whole capped string stays bounded: kept bytes + the short notice,
    # never the multi-MB original. A failure here means truncation didn't fire.
    assert len(capped.encode("utf-8")) < MAX_TOOL_OUTPUT_BYTES + over


def test_cap_tool_output_truncates_on_a_character_boundary() -> None:
    """Truncation never splits a multibyte UTF-8 char (no decode error / U+FFFD)."""
    # "€" is 3 UTF-8 bytes. Sizing the string so the byte cap lands mid-char
    # forces the boundary logic: cap+1 chars => 3*(cap+1) bytes, well over cap,
    # and MAX_TOOL_OUTPUT_BYTES is not a multiple of 3, so the cap splits a char.
    euros = "€" * (MAX_TOOL_OUTPUT_BYTES + 1)
    capped = cap_tool_output(euros)
    # decode("utf-8") on the kept prefix would have raised / inserted U+FFFD if
    # a partial char survived; assert clean euros only before the notice.
    kept = capped.split("\n\n" + _TRUNCATION_MARKER)[0]
    assert "�" not in kept
    assert set(kept) == {"€"}
    # The kept prefix is the largest whole-char run within the byte cap.
    assert len(kept.encode("utf-8")) <= MAX_TOOL_OUTPUT_BYTES
    assert len(kept.encode("utf-8")) > MAX_TOOL_OUTPUT_BYTES - 3
