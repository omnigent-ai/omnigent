"""Unit tests for the runner's per-turn context breakdown."""

from __future__ import annotations

import base64
import json

from omnigent.runtime.context_breakdown import (
    IMAGE_TOKEN_ESTIMATE,
    build_turn_context_breakdown,
    estimate_attachment_tokens,
)


def _data_uri(media_type: str, payload: bytes) -> str:
    return f"data:{media_type};base64,{base64.b64encode(payload).decode()}"


def test_turn_breakdown_splits_the_request_by_source() -> None:
    """Instructions, tools, history, attachments and the current message are sized apart."""
    tools = [{"name": "search", "parameters": {"type": "object"}}]
    breakdown = build_turn_context_breakdown(
        system_prompt="s" * 40,
        tools=tools,
        messages=[
            {"role": "system", "content": "x" * 8},
            {"role": "user", "content": "a" * 20},
            {"role": "assistant", "content": [{"type": "output_text", "text": "b" * 12}]},
            {"role": "tool_call", "content": {"tool": "search", "args": {}}},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "c" * 16},
                    {"type": "input_image", "image_url": _data_uri("image/png", b"\x89PNG")},
                    {
                        "type": "input_file",
                        "filename": "notes.txt",
                        "file_data": _data_uri("text/plain", b"n" * 400),
                    },
                    {
                        "type": "input_file",
                        "filename": "report.pdf",
                        "file_data": _data_uri("application/pdf", b"%PDF"),
                    },
                    # Bytes carried in the block are inlined whatever the name.
                    {
                        "type": "input_file",
                        "filename": "notes.docx",
                        "file_data": _data_uri("application/octet-stream", b"PK"),
                    },
                    # Delivered as a file, so only its path reaches the model.
                    {"type": "input_file", "filename": "trace.gz", "delivery": "filesystem"},
                ],
            },
        ],
    )

    assert breakdown.system_tokens == 10 + 2
    assert breakdown.tool_count == 1
    assert breakdown.tool_tokens == -(-len(json.dumps(tools).encode()) // 4)
    assert breakdown.history_item_count == 3
    assert breakdown.history_tokens == 5 + 3 + -(
        -len(json.dumps({"tool": "search", "args": {}})) // 4
    )
    assert breakdown.message_tokens == 4
    assert breakdown.attachment_count == 4
    assert breakdown.attachment_tokens == IMAGE_TOKEN_ESTIMATE + 100
    assert breakdown.unestimated_attachment_count == 2
    assert breakdown.total_tokens == (
        breakdown.system_tokens
        + breakdown.tool_tokens
        + breakdown.history_tokens
        + breakdown.attachment_tokens
        + breakdown.message_tokens
    )


def test_turn_breakdown_counts_a_trailing_reply_as_history() -> None:
    """Without a final user message there is no current message to size."""
    breakdown = build_turn_context_breakdown(
        system_prompt="",
        tools=[],
        messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}],
    )

    assert breakdown.message_tokens == 0
    assert breakdown.history_item_count == 2
    assert breakdown.tool_tokens == 0


def test_attachment_estimates_follow_the_type() -> None:
    """Images get a fixed estimate, text its size, and other binaries none."""
    assert (
        estimate_attachment_tokens(is_image=True, content_type=None, filename=None, size=None)
        == IMAGE_TOKEN_ESTIMATE
    )
    assert (
        estimate_attachment_tokens(
            is_image=False, content_type="text/csv", filename="a.csv", size=9
        )
        == 3
    )
    assert (
        estimate_attachment_tokens(
            is_image=False, content_type="application/pdf", filename="a.pdf", size=9
        )
        is None
    )
    # A text file's estimate needs its size.
    assert (
        estimate_attachment_tokens(
            is_image=False, content_type="text/plain", filename="a.txt", size=None
        )
        is None
    )
