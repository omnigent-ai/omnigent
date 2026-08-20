"""Unit tests for :mod:`omnigent.tools.base`.

Covers ``ToolContext`` construction, ``Tool`` ABC contract, and the
``is_valid_tool_name`` validator.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnigent.tools.base import Tool, ToolContext, is_valid_tool_name

# ── is_valid_tool_name ───────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "get_weather",
        "web-search",
        "a",
        "A1_b2-c3",
        "a" * 256,
    ],
    ids=["snake_case", "kebab-case", "single_char", "mixed", "max_length"],
)
def test_valid_names(name: str) -> None:
    assert is_valid_tool_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "",
        "has space",
        "has:colon",
        "has.dot",
        "a" * 257,
        "hello world",
    ],
    ids=["empty", "space", "colon", "dot", "too_long", "multi_word"],
)
def test_invalid_names(name: str) -> None:
    assert is_valid_tool_name(name) is False


# ── ToolContext ──────────────────────────────────────────


def test_tool_context_minimal() -> None:
    """ToolContext with only required fields."""
    ctx = ToolContext(task_id="t1", agent_id="a1")
    assert ctx.task_id == "t1"
    assert ctx.agent_id == "a1"
    assert ctx.workspace is None
    assert ctx.conversation_id is None


def test_tool_context_full() -> None:
    """ToolContext with all fields set."""
    ws = Path("/tmp/workspace")
    ctx = ToolContext(
        task_id="t1",
        agent_id="a1",
        workspace=ws,
        conversation_id="conv_123",
    )
    assert ctx.workspace == ws
    assert ctx.conversation_id == "conv_123"


def test_tool_context_is_frozen() -> None:
    """ToolContext is immutable (frozen dataclass)."""
    ctx = ToolContext(task_id="t", agent_id="a")
    with pytest.raises(AttributeError):
        ctx.task_id = "new"  # type: ignore[misc]


# ── Tool ABC ─────────────────────────────────────────────


def test_tool_abc_cannot_instantiate() -> None:
    """Tool cannot be instantiated directly."""
    with pytest.raises(TypeError):
        Tool()  # type: ignore[abstract]


def test_concrete_tool_subclass() -> None:
    """A concrete Tool subclass can be instantiated and used."""

    class DummyTool(Tool):
        @classmethod
        def name(cls) -> str:
            return "dummy"

        @classmethod
        def description(cls) -> str:
            return "A dummy tool."

        def get_schema(self) -> dict[str, Any]:
            return {
                "type": "function",
                "function": {
                    "name": "dummy",
                    "description": "A dummy tool.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }

        def invoke(self, arguments: str, ctx: ToolContext) -> str:
            return "ok"

    tool = DummyTool()
    assert tool.name() == "dummy"
    assert tool.description() == "A dummy tool."
    assert tool.get_schema()["function"]["name"] == "dummy"
    ctx = ToolContext(task_id="t", agent_id="a")
    assert tool.invoke("{}", ctx) == "ok"


def test_default_invoke_raises() -> None:
    """
    The base Tool.invoke raises NotImplementedError for runner-dispatched
    tools that don't override it.
    """

    class SchemaOnlyTool(Tool):
        @classmethod
        def name(cls) -> str:
            return "schema_only"

        @classmethod
        def description(cls) -> str:
            return "Schema only."

        def get_schema(self) -> dict[str, Any]:
            return {"type": "function", "function": {"name": "schema_only"}}

    tool = SchemaOnlyTool()
    ctx = ToolContext(task_id="t", agent_id="a")
    with pytest.raises(NotImplementedError, match="runner-dispatched"):
        tool.invoke("{}", ctx)


def test_default_is_async_false() -> None:
    """is_async() defaults to False."""

    class SyncTool(Tool):
        @classmethod
        def name(cls) -> str:
            return "sync"

        @classmethod
        def description(cls) -> str:
            return "Sync."

        def get_schema(self) -> dict[str, Any]:
            return {}

    tool = SyncTool()
    assert tool.is_async() is False
    assert tool.is_async(arguments='{"key": "value"}') is False


def test_cancel_is_noop() -> None:
    """cancel() is a no-op by default (does not raise)."""

    class NoCancelTool(Tool):
        @classmethod
        def name(cls) -> str:
            return "nc"

        @classmethod
        def description(cls) -> str:
            return "No cancel."

        def get_schema(self) -> dict[str, Any]:
            return {}

    tool = NoCancelTool()
    tool.cancel()  # should not raise
