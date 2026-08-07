"""Tests for canonical system-instruction composition."""

import json
from types import SimpleNamespace
from typing import cast

import pytest

from omnigent.entities import (
    ComputerUsePresentation,
    ConversationItem,
    FunctionCallData,
    FunctionCallOutputAttachment,
    FunctionCallOutputData,
)
from omnigent.runtime.prompt import (
    append_framework_instructions,
    build_instructions,
    history_to_input_items,
)
from omnigent.spec import AgentSpec


def _output_item(output: str) -> ConversationItem:
    """Build a persisted ``function_call_output`` item for replay tests."""
    return ConversationItem(
        id="i1",
        status="completed",
        response_id="r1",
        created_at=1,
        type="function_call_output",
        data=FunctionCallOutputData(call_id="c1", output=output),
    )


def test_history_replay_strips_inline_base64_image() -> None:
    """A stored image tool result must not replay its base64 as prompt text.

    Older sessions persisted a ``Read`` of an image as a JSON list of
    ``{"type":"image","source":{"type":"base64",...}}`` blocks. Replaying that
    verbatim on resume overflows the context window and wedges compaction, so
    ``history_to_input_items`` strips the base64 to a placeholder.
    """
    huge_b64 = "iVBORw0KGgo" + "A" * 100_000
    stored = json.dumps(
        [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": huge_b64},
            }
        ],
        separators=(",", ":"),
    )

    result = history_to_input_items([_output_item(stored)])

    output = result[0]["output"]
    assert huge_b64 not in output, "base64 image data must not be replayed as text"
    assert "image/png image omitted from history" in output
    assert "re-run the tool call" in output
    assert len(output) < 300


def test_history_replay_strips_truncated_image_block() -> None:
    """Base64 clipped at the store byte cap (invalid JSON) is still stripped.

    Real wedged sessions stored the image output truncated at the
    conversation-store byte cap, leaving the base64 string unterminated — so it
    no longer parses as JSON. The strip must fall back to an in-place rewrite,
    or the exact payloads that wedge resume would slip through unchanged.
    """
    huge_b64 = "iVBORw0KGgo" + "A" * 100_000
    # Mimic the store cap: a valid prefix cut mid-base64, no closing quote/braces.
    truncated = (
        '[{"type":"image","source":{"type":"base64","data":"'
        + huge_b64
        + "…[truncated by conversation-store: item exceeded 245760B cap]"
    )
    # Precondition: this is genuinely not parseable JSON.
    with pytest.raises(ValueError):
        json.loads(truncated)

    result = history_to_input_items([_output_item(truncated)])

    output = result[0]["output"]
    assert huge_b64 not in output, "truncated base64 must not survive replay"
    assert "image omitted from history" in output
    assert len(output) < 300


def test_history_replay_leaves_plain_text_output_unchanged() -> None:
    """Plain-text tool outputs (the common case) pass through untouched."""
    result = history_to_input_items([_output_item("TODO contents")])
    assert result[0]["output"] == "TODO contents"


def test_history_replay_leaves_non_image_json_output_unchanged() -> None:
    """A JSON tool output with no image block is returned byte-for-byte."""
    stored = json.dumps([{"type": "text", "text": "hello"}], separators=(",", ":"))
    result = history_to_input_items([_output_item(stored)])
    assert result[0]["output"] == stored


def test_history_replay_ignores_computer_use_presentation_and_attachments() -> None:
    """Display metadata must never become model input on resume."""
    call = ConversationItem(
        id="i_call",
        status="completed",
        response_id="r1",
        created_at=1,
        type="function_call",
        data=FunctionCallData(
            agent="codex-native-ui",
            name="node_repl/js",
            arguments='{"code":"inspect"}',
            call_id="c1",
            presentation=ComputerUsePresentation(
                provider="codex",
                app_name="secret app label",
                action_label="secret action label",
            ),
        ),
    )
    output = ConversationItem(
        id="i_output",
        status="completed",
        response_id="r1",
        created_at=2,
        type="function_call_output",
        data=FunctionCallOutputData(
            call_id="c1",
            output="sanitized result",
            presentation=ComputerUsePresentation(
                provider="codex",
                app_id="secret.output.app",
            ),
            presentation_final=True,
            status="completed",
            attachments=[
                FunctionCallOutputAttachment(
                    kind="computer_frame",
                    file_id="secret_frame_id",
                    content_type="image/png",
                    width=1280,
                    height=800,
                )
            ],
        ),
    )

    assert history_to_input_items([call, output]) == [
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "node_repl/js",
            "arguments": '{"code":"inspect"}',
        },
        {
            "type": "function_call_output",
            "call_id": "c1",
            "output": "sanitized result",
        },
    ]


def test_framework_instructions_append_after_custom_prompts() -> None:
    spec = cast(AgentSpec, SimpleNamespace(instructions="Agent prompt", skills=[]))

    result = build_instructions(
        spec,
        "Request prompt",
        [],
        framework_instructions=("  Framework prompt  ",),
    )

    assert result == "Agent prompt\n\nRequest prompt\n\nFramework prompt"


def test_empty_framework_instructions_do_not_change_default() -> None:
    spec = cast(AgentSpec, SimpleNamespace(instructions=None, skills=[]))

    assert build_instructions(spec, None, [], framework_instructions=("", "   ")) == (
        "You are a helpful assistant."
    )


def test_framework_only_instructions_use_shared_composer() -> None:
    assert append_framework_instructions(None, ("Rename session",)) == "Rename session"
