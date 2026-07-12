"""Tests for cursor-native model resolution from the agent spec.

``_cursor_native_model_from_spec`` is the seam that turns a session's
``executor.model`` (set via ``--model`` or a config.yaml ``model:`` key)
into the ``cursor-agent --model`` value the native TUI launches with.
A gateway-routed id is dropped so cursor-agent keeps its configured
default instead of erroring on an id it does not recognise.
"""

from __future__ import annotations

import logging

import pytest

from omnigent.runner.app import (
    _cursor_fork_history_preamble,
    _cursor_message_item_text,
    _cursor_native_model_from_spec,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec


def _spec(model: str | None) -> AgentSpec:
    """Build a minimal agent spec carrying *model* on its executor block."""
    return AgentSpec(spec_version=1, name="cursor", executor=ExecutorSpec(model=model))


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # A cursor-agent model id is passed through verbatim.
        ("sonnet-4-thinking", "sonnet-4-thinking"),
        ("gpt-5", "gpt-5"),
        # No pin → None (cursor-agent uses its configured default).
        (None, None),
        ("", None),
        # Gateway-routed ids are not cursor-agent model ids → dropped.
        ("databricks-claude-opus", None),
        ("databricks/claude-opus", None),
    ],
    ids=[
        "cursor-id-passthrough",
        "cursor-id-gpt5",
        "no-model",
        "empty-model",
        "databricks-dash-dropped",
        "databricks-slash-dropped",
    ],
)
def test_cursor_native_model_from_spec(model: str | None, expected: str | None) -> None:
    """A usable cursor model id is returned; non-cursor ids resolve to None."""
    assert _cursor_native_model_from_spec(_spec(model)) == expected


def test_cursor_native_model_from_spec_none_spec() -> None:
    """A missing spec yields no model (no ``--model`` injected)."""
    assert _cursor_native_model_from_spec(None) is None


def test_cursor_native_model_from_spec_warns_on_dropped_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dropping a non-cursor id warns so the silent fallback is visible."""
    with caplog.at_level(logging.WARNING):
        assert _cursor_native_model_from_spec(_spec("databricks-claude-opus")) is None
    assert any("databricks-claude-opus" in rec.getMessage() for rec in caplog.records)


def _msg(role: str, text: str) -> dict:
    """A message item as returned by GET /v1/sessions/{id}/items."""
    block = "input_text" if role == "user" else "output_text"
    return {"type": "message", "role": role, "content": [{"type": block, "text": text}]}


class TestCursorMessageItemText:
    def test_string_content(self) -> None:
        assert _cursor_message_item_text("hi") == "hi"

    def test_joins_text_blocks(self) -> None:
        content = [
            {"type": "input_text", "text": "one "},
            {"type": "text", "text": "two"},
            {"type": "input_image", "image_url": "data:..."},  # non-text -> skipped
        ]
        assert _cursor_message_item_text(content) == "one two"

    def test_non_text_is_empty(self) -> None:
        assert _cursor_message_item_text(None) == ""
        assert _cursor_message_item_text([{"type": "input_image"}]) == ""


class TestCursorForkHistoryPreamble:
    def test_renders_user_and_assistant_turns(self) -> None:
        items = [
            _msg("user", "add hello.txt"),
            _msg("assistant", "done"),
            _msg("user", "now delete it"),
        ]
        # Turns render as a speaker-labelled transcript, blank-line separated —
        # the closest single-block analog to claude/codex native bubbles.
        assert _cursor_fork_history_preamble(items) == (
            "You: add hello.txt\n\nAssistant: done\n\nYou: now delete it"
        )

    def test_skips_non_message_and_empty_items(self) -> None:
        items = [
            {"type": "function_call", "name": "sys_os_write"},  # tool call -> skipped
            _msg("assistant", ""),  # empty text -> skipped
            {"type": "message", "role": "system", "content": [{"text": "x"}]},  # system
            _msg("user", "real"),
        ]
        assert _cursor_fork_history_preamble(items) == "You: real"

    def test_no_replayable_text_yields_empty(self) -> None:
        assert _cursor_fork_history_preamble([]) == ""
        assert _cursor_fork_history_preamble([{"type": "function_call"}]) == ""
