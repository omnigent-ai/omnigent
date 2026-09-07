"""Unit tests for session-title seeding from persisted items
(``_title_content_from_item``), including Skill slash-command titling (#851).

A Claude Code native session whose first action is a Skill / slash-command
arrives over the transcript bridge as a ``slash_command`` item, not a user
``message``. Without title seeding from that item the session stays untitled and
the sidebar falls back to the generic "Claude Code" label.
"""

from __future__ import annotations

from omnigent.entities.conversation import (
    MessageData,
    NewConversationItem,
    SlashCommandData,
    synthesize_conversation_title,
)
from omnigent.server.routes.sessions import _title_content_from_item


def _slash_command_item(name: str, arguments: str, *, kind: str = "skill") -> NewConversationItem:
    """Build a ``slash_command`` item as the native transcript bridge would."""
    return NewConversationItem(
        type="slash_command",
        response_id="turn_x",
        data=SlashCommandData(agent="claude-native-ui", kind=kind, name=name, arguments=arguments),
    )


def test_skill_slash_command_titles_from_typed_command() -> None:
    item = _slash_command_item("my-plugin:my-skill", "ARG-123")
    content = _title_content_from_item(item)
    assert content == [{"type": "input_text", "text": "/my-plugin:my-skill ARG-123"}]
    # And it synthesizes a real, descriptive title (not the "Claude Code" default).
    assert synthesize_conversation_title(content) == "/my-plugin:my-skill ARG-123"


def test_skill_slash_command_without_arguments_titles_from_command_only() -> None:
    item = _slash_command_item("dev-productivity:simplify", "")
    assert _title_content_from_item(item) == [
        {"type": "input_text", "text": "/dev-productivity:simplify"}
    ]


def test_skill_slash_command_strips_argument_whitespace() -> None:
    item = _slash_command_item("review", "  PR-9  ")
    assert _title_content_from_item(item) == [{"type": "input_text", "text": "/review PR-9"}]


def test_cli_builtin_command_does_not_title() -> None:
    # Surfaced CLI built-ins (kind == "command", e.g. /clear, /compact, /model)
    # are not meaningful session topics and must not seed the title.
    item = _slash_command_item("compact", "", kind="command")
    assert _title_content_from_item(item) == []
    assert synthesize_conversation_title(_title_content_from_item(item)) is None


def test_user_message_still_titles() -> None:
    # Regression guard: the pre-existing user-message path is unchanged.
    item = NewConversationItem(
        type="message",
        response_id="turn_x",
        data=MessageData(role="user", content=[{"type": "input_text", "text": "hello there"}]),
    )
    assert _title_content_from_item(item) == [{"type": "input_text", "text": "hello there"}]
