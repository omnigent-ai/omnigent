"""Unit tests for the ``tool_output=preview`` projection of session items."""

from __future__ import annotations

from omnigent.server.routes.sessions.routes_items import (
    TOOL_OUTPUT_PREVIEW_CHARS,
    project_tool_output_preview,
)


def test_project_tool_output_preview_shortens_only_long_outputs() -> None:
    """Only a ``function_call_output`` longer than the preview is rewritten."""
    long_output = "x" * (TOOL_OUTPUT_PREVIEW_CHARS + 1)
    stub = project_tool_output_preview({"type": "function_call_output", "output": long_output})
    assert stub == {
        "type": "function_call_output",
        "output": "x" * TOOL_OUTPUT_PREVIEW_CHARS,
        "output_length": TOOL_OUTPUT_PREVIEW_CHARS + 1,
        "output_truncated": True,
    }
    # The boundary case and other item types come back as the same object:
    # a client can treat anything without ``output_truncated`` as complete.
    fits = {"type": "function_call_output", "output": "y" * TOOL_OUTPUT_PREVIEW_CHARS}
    assert project_tool_output_preview(fits) is fits
    call = {"type": "function_call", "arguments": "z" * (TOOL_OUTPUT_PREVIEW_CHARS * 2)}
    assert project_tool_output_preview(call) is call
    assert project_tool_output_preview(
        {"type": "function_call_output", "output": "ab"}, preview_chars=1
    ) == {
        "type": "function_call_output",
        "output": "a",
        "output_length": 2,
        "output_truncated": True,
    }
