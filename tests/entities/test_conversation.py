"""Tests for conversation entity helpers."""

from __future__ import annotations

import pytest

from omnigent.entities.conversation import (
    ConversationItem,
    MessageData,
    synthesize_conversation_title,
)


def _message_item(created_by: str | None) -> ConversationItem:
    """Build a persisted user-message item with the given author."""
    return ConversationItem(
        id="msg_1",
        type="message",
        status="completed",
        response_id="resp_1",
        created_at=0,
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": "hi"}],
        ),
        created_by=created_by,
    )


def test_to_api_dict_exposes_created_by_when_set() -> None:
    """A human-authored item surfaces ``created_by`` in the API shape."""
    api = _message_item("alice@example.com").to_api_dict()
    assert api["created_by"] == "alice@example.com"


def test_to_api_dict_omits_created_by_when_none() -> None:
    """Agent/system items omit ``created_by`` so they stay distinguishable."""
    api = _message_item(None).to_api_dict()
    assert "created_by" not in api


def test_to_api_dict_exposes_interrupted_assistant_marker() -> None:
    """Interrupted assistant items surface the reload marker in API shape."""
    item = ConversationItem(
        id="msg_interrupted",
        type="message",
        status="completed",
        response_id="codex_turn_123",
        created_at=0,
        data=MessageData(
            role="assistant",
            agent="codex-native-ui",
            interrupted=True,
            content=[{"type": "output_text", "text": "partial answer"}],
        ),
    )

    api = item.to_api_dict()

    assert api["interrupted"] is True
    assert api["model"] == "codex-native-ui"


@pytest.mark.parametrize(
    "content,expected",
    [
        ([{"type": "input_text", "text": "Hello"}], "Hello"),
        ([{"type": "input_text", "text": "  hi   there  "}], "hi there"),
        ([{"type": "input_text", "text": "line one\nline two"}], "line one line two"),
        (
            [
                {"type": "input_text", "text": "first"},
                {"type": "input_text", "text": "second"},
            ],
            "first second",
        ),
        (
            [
                {"type": "input_file", "file_id": "file_123"},
                {"type": "input_text", "text": "real prompt"},
            ],
            "real prompt",
        ),
        ([], None),
        ([{"type": "input_file", "file_id": "file_123"}], None),
        ([{"type": "input_text", "text": "   \n  "}], None),
        ([{"type": "input_text", "text": "a" * 100}], "a" * 59 + "…"),
        # claude-native attachment marker (claude_native_executor
        # prepends "[Attached: <path>]\n\n<text>") — the marker line is
        # dropped so the title is the user's text, not a temp-file path.
        (
            [
                {
                    "type": "input_text",
                    "text": (
                        "[Attached: /tmp/omnigent/claude-native/0a1b/uploads/shot.png]"
                        "\n\nfix this layout bug"
                    ),
                }
            ],
            "fix this layout bug",
        ),
        # codex-native binary-file marker ("[Attached file: <path>]")
        # arrives as its own text block alongside the user's text.
        (
            [
                {"type": "input_text", "text": "[Attached file: /tmp/omnigent/u/report.bin]"},
                {"type": "input_text", "text": "summarize this"},
            ],
            "summarize this",
        ),
        # Image-only message: every line is a marker, so no title —
        # the next user message seeds it instead of a temp-file path.
        (
            [{"type": "input_text", "text": "[Attached: /tmp/omnigent/u/img.png]"}],
            None,
        ),
        # A marker mid-line is user prose, not an executor-emitted
        # attachment line — it must survive into the title.
        (
            [{"type": "input_text", "text": "why does [Attached: x.png] render twice?"}],
            "why does [Attached: x.png] render twice?",
        ),
    ],
)
def test_synthesize_conversation_title(
    content: list[dict[str, object]],
    expected: str | None,
) -> None:
    """Title synthesis collapses, joins, truncates, and drops attachment markers."""
    assert synthesize_conversation_title(content) == expected


def test_synthesize_conversation_title_respects_custom_limit() -> None:
    """Custom ``limit`` is honored."""
    content = [{"type": "input_text", "text": "a" * 50}]
    assert synthesize_conversation_title(content, limit=10) == "a" * 9 + "…"
