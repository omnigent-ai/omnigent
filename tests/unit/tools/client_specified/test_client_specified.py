"""Tests for omnigent.tools.client_specified."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.tools.base import ToolContext
from omnigent.tools.client_specified import (
    ClientSideTool,
    ClientSideToolSpec,
    parse_client_side_tool_spec,
    parse_client_side_tool_specs,
)

# ── Fixtures ──────────────────────────────────────────────


@pytest.fixture()
def minimal_raw_tool() -> dict[str, Any]:
    """
    The minimum valid raw tool dict: a standard OpenAI function schema.

    :returns: A dict in OpenAI function tool format.
    """
    return {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }


@pytest.fixture()
def search_raw_tool() -> dict[str, Any]:
    """
    A raw tool dict for a search tool.

    :returns: A dict in OpenAI function tool format.
    """
    return {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search for documents.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }


@pytest.fixture()
def weather_spec() -> ClientSideToolSpec:
    """
    A pre-built ClientSideToolSpec for the get_weather tool.

    :returns: A :class:`ClientSideToolSpec` with name and schema.
    """
    return ClientSideToolSpec(
        name="get_weather",
        schema={
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get current weather for a city.",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        },
    )


# ── parse_client_side_tool_spec ───────────────────────────


def test_parse_minimal_tool(minimal_raw_tool: dict[str, Any]) -> None:
    """
    parse_client_side_tool_spec returns a correctly populated
    ClientSideToolSpec from a minimal valid raw dict.
    """
    spec = parse_client_side_tool_spec(minimal_raw_tool)

    # Name extracted from function.name
    assert spec.name == "get_weather"
    # Schema stored as-is — no keys stripped
    assert spec.schema == minimal_raw_tool


def test_parse_schema_stored_verbatim(minimal_raw_tool: dict[str, Any]) -> None:
    """
    parse_client_side_tool_spec stores the schema dict verbatim.

    There is no ``omnigent`` extension key to strip — the raw dict
    IS the schema that gets stored and later returned to the LLM.
    """
    spec = parse_client_side_tool_spec(minimal_raw_tool)

    assert spec.schema["type"] == "function"
    assert spec.schema["function"]["name"] == "get_weather"


@pytest.mark.parametrize(
    "bad_tool,expected_fragment",
    [
        # Wrong type field
        (
            {
                "type": "not_function",
                "function": {"name": "x"},
            },
            "type 'function'",
        ),
        # Missing function object entirely
        (
            {"type": "function"},
            "missing 'function'",
        ),
        # Missing function.name
        (
            {
                "type": "function",
                "function": {"description": "no name here"},
            },
            "missing function.name",
        ),
    ],
)
def test_parse_raises_on_malformed(
    bad_tool: dict[str, Any],
    expected_fragment: str,
) -> None:
    """
    parse_client_side_tool_spec raises ValueError with a descriptive
    message for each class of malformed input.

    A failure (no exception raised, or wrong exception type) would
    mean malformed client tools are silently accepted, leading to
    runtime errors deep inside the agent loop.
    """
    with pytest.raises(ValueError, match=expected_fragment):
        parse_client_side_tool_spec(bad_tool)


def test_parse_client_side_tool_specs_empty() -> None:
    """
    parse_client_side_tool_specs returns an empty list for empty input.
    """
    assert parse_client_side_tool_specs([]) == []


def test_parse_client_side_tool_specs_multiple(
    minimal_raw_tool: dict[str, Any],
    search_raw_tool: dict[str, Any],
) -> None:
    """
    parse_client_side_tool_specs parses every tool in the list and
    returns them in order.
    """
    specs = parse_client_side_tool_specs([minimal_raw_tool, search_raw_tool])

    # Two tools parsed in order
    assert len(specs) == 2, (
        f"Expected 2 specs (one per raw tool), got {len(specs)}. "
        "If 0 or 1, parse_client_side_tool_specs short-circuited."
    )
    assert specs[0].name == "get_weather"
    assert specs[1].name == "search"


# ── ClientSideTool.get_schema ─────────────────────────────


def test_get_schema_returns_spec_schema(weather_spec: ClientSideToolSpec) -> None:
    """
    ClientSideTool.get_schema returns exactly the schema stored in the
    spec — the LLM sees a standard OpenAI function schema.
    """
    tool = ClientSideTool(weather_spec)

    schema = tool.get_schema()

    assert schema is weather_spec.schema
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "get_weather"


def test_name_property(weather_spec: ClientSideToolSpec) -> None:
    """
    ClientSideTool.name() returns the tool name from the spec.
    """
    tool = ClientSideTool(weather_spec)

    assert tool.name() == "get_weather"


# ── ClientSideTool.invoke ─────────────────────────────────


def test_invoke_raises_runtime_error(
    weather_spec: ClientSideToolSpec, tool_ctx: ToolContext
) -> None:
    """
    ClientSideTool.invoke raises RuntimeError — client-side tools
    must never be executed server-side.

    The agent loop uses ToolManager.is_client_side_tool() to detect
    these tools BEFORE calling invoke. If invoke is reached, that is
    a workflow bug that must surface loudly.

    A failure here (no exception raised) would mean client-side tools
    are silently executed server-side, violating the contract that
    function_call items should be returned to the caller.
    """
    tool = ClientSideTool(weather_spec)

    with pytest.raises(RuntimeError, match="must not be invoked server-side"):
        tool.invoke('{"city": "San Francisco"}', tool_ctx)


# ── Tool name validation ─────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "tool with spaces",
        "tool:colon",
        "tool.dot",
        "a" * 257,
        "ns::method",
    ],
    ids=[
        "spaces",
        "colon",
        "dot",
        "too_long",
        "double_colon",
    ],
)
def test_parse_rejects_invalid_tool_name(name: str) -> None:
    """
    ``parse_client_side_tool_spec`` raises ``ValueError`` when the
    tool name violates the OpenAI constraint
    (``[a-zA-Z0-9_-]{1,64}``).
    """
    raw = {
        "type": "function",
        "function": {
            "name": name,
            "description": "A tool.",
            "parameters": {},
        },
    }
    with pytest.raises(ValueError, match="Invalid tool name"):
        parse_client_side_tool_spec(raw)


@pytest.mark.parametrize(
    "name",
    [
        "simple",
        "with_underscore",
        "with-hyphen",
        "CamelCase",
        "a" * 64,
    ],
    ids=[
        "simple",
        "underscore",
        "hyphen",
        "camel_case",
        "max_length",
    ],
)
def test_parse_accepts_valid_tool_name(name: str) -> None:
    """
    ``parse_client_side_tool_spec`` accepts names that match the
    OpenAI constraint (``[a-zA-Z0-9_-]{1,64}``).
    """
    raw = {
        "type": "function",
        "function": {
            "name": name,
            "description": "A tool.",
            "parameters": {},
        },
    }
    spec = parse_client_side_tool_spec(raw)
    assert spec.name == name
