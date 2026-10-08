"""
Tests for :func:`omnigent.runtime.tool_output.cap_tool_output` — the canonical
size cap applied to every ``function_call_output`` producer's ``output`` field.
Each assertion is chosen so the corresponding production breakage turns it red.
"""

from __future__ import annotations

import json

import pytest

from omnigent.runtime.mcp_tool_result import encode_mcp_image_result
from omnigent.runtime.tool_output import MAX_TOOL_OUTPUT_BYTES, cap_tool_output
from omnigent.runtime.tool_result_replay import tool_result_content_blocks
from tests._image_fixtures import _TINY_PNG_BASE64

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


@pytest.mark.parametrize("is_error", [False, True])
def test_cap_image_history_preserves_text_after_oversized_image(is_error: bool) -> None:
    output = encode_mcp_image_result(
        [
            {"type": "text", "text": "before"},
            {"type": "image", "mimeType": "image/png", "data": "A" * MAX_TOOL_OUTPUT_BYTES},
            {"type": "text", "text": "Required trailing instruction: choose blue."},
        ],
        is_error=is_error,
    )
    capped = cap_tool_output(output)
    assert len(capped.encode("utf-8")) <= MAX_TOOL_OUTPUT_BYTES
    blocks = tool_result_content_blocks(capped).blocks
    assert blocks is not None
    if is_error:
        assert blocks.pop(0) == {"type": "text", "text": "Error:"}
    assert blocks[0] == {"type": "text", "text": "before"}
    assert "omitted from history" in blocks[1]["text"]
    assert blocks[2] == {"type": "text", "text": "Required trailing instruction: choose blue."}


def test_cap_image_history_retains_images_that_fit_in_order() -> None:
    output = encode_mcp_image_result(
        [
            {"type": "image", "mimeType": "image/png", "data": _TINY_PNG_BASE64},
            {"type": "text", "text": "between"},
            {"type": "image", "mimeType": "image/png", "data": "A" * MAX_TOOL_OUTPUT_BYTES},
            {"type": "text", "text": "after"},
        ],
        is_error=True,
    )
    capped = cap_tool_output(output)
    assert len(capped.encode("utf-8")) <= MAX_TOOL_OUTPUT_BYTES
    assert json.loads(capped)["isError"] is True
    blocks = tool_result_content_blocks(capped).blocks
    assert blocks is not None
    assert blocks[0] == {"type": "text", "text": "Error:"}
    assert blocks[1]["source"]["data"] == _TINY_PNG_BASE64
    assert blocks[2] == {"type": "text", "text": "between"}
    assert "omitted from history" in blocks[3]["text"]
    assert blocks[4] == {"type": "text", "text": "after"}
    assert cap_tool_output(capped) == capped


def test_cap_ordinary_json_with_image_field_uses_existing_byte_cap() -> None:
    output = json.dumps({"content": "A" * MAX_TOOL_OUTPUT_BYTES, "image": "ordinary data"})
    assert cap_tool_output(output).startswith(output[:MAX_TOOL_OUTPUT_BYTES])
    assert _TRUNCATION_MARKER in cap_tool_output(output)
