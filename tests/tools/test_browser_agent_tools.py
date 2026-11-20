"""Tests for the advanced browser agent tools switch."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from omnigent.tools.base import Tool
from omnigent.tools.browser_agent_tools import (
    ADVANCED_BROWSER_TOOL_NAMES,
    BROWSER_AGENT_TOOLS_HEADER,
    BROWSER_AGENT_TOOLS_LABEL,
    browser_agent_tools_creation_labels,
    browser_agent_tools_enabled,
    browser_agent_tools_mode,
    browser_agent_tools_turn_flag,
    filter_browser_agent_tool_schemas,
    filter_native_relay_browser_tools,
    filter_turn_browser_tools,
)
from omnigent.tools.builtins.browser import (
    BROWSER_TOOL_NAMES,
    BrowserClickTool,
    BrowserNavigateTool,
    BrowserScreenshotTool,
    BrowserSnapshotTool,
    BrowserTypeTool,
)
from omnigent.tools.builtins.browser_advanced import register_browser_agent_tools

BASIC = {
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_screenshot",
}


def _browser_tools() -> dict[str, Tool]:
    """The browser tools the manager registers: the standard five, then the advanced ones."""
    tools: dict[str, Tool] = {
        cls.name(): cls()
        for cls in (
            BrowserNavigateTool,
            BrowserSnapshotTool,
            BrowserClickTool,
            BrowserTypeTool,
            BrowserScreenshotTool,
        )
    }
    register_browser_agent_tools(tools)
    return tools


def _nested() -> list[dict[str, Any]]:
    """Every browser tool schema in the OpenAI shape, plus an unrelated tool."""
    return [tool.get_schema() for tool in _browser_tools().values()] + [
        {"type": "function", "function": {"name": "list_comments", "parameters": {}}}
    ]


def _flat() -> list[dict[str, Any]]:
    """The same schemas in the flat relay shape."""
    return [schema["function"] for schema in _nested()]


def _names(schemas: list[dict[str, Any]]) -> set[str]:
    return {schema.get("function", schema)["name"] for schema in schemas}


def _tool(schemas: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(s.get("function", s) for s in schemas if s.get("function", s)["name"] == name)


def _click(schemas: list[dict[str, Any]]) -> dict[str, Any]:
    return _tool(schemas, "browser_click")


def test_advanced_names_are_the_registered_browser_tools_beyond_the_standard_five() -> None:
    assert BROWSER_TOOL_NAMES == BASIC
    assert set(_browser_tools()) == BASIC | ADVANCED_BROWSER_TOOL_NAMES


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("on", {BROWSER_AGENT_TOOLS_LABEL: "on"}),
        (" ON ", {BROWSER_AGENT_TOOLS_LABEL: "on"}),
        ("trusted", {BROWSER_AGENT_TOOLS_LABEL: "trusted"}),
        ("off", {}),
        (None, {}),
    ],
)
def test_creation_labels_follow_the_header(value: str | None, expected: dict[str, str]) -> None:
    headers = {} if value is None else {BROWSER_AGENT_TOOLS_HEADER: value}
    assert browser_agent_tools_creation_labels(headers) == expected


@pytest.mark.parametrize(
    ("labels", "mode", "flags"),
    [
        (None, "standard", (False, False)),
        ({}, "standard", (False, False)),
        ({BROWSER_AGENT_TOOLS_LABEL: "off"}, "standard", (False, False)),
        ({BROWSER_AGENT_TOOLS_LABEL: "trusted"}, "trusted", (False, True)),
        ({BROWSER_AGENT_TOOLS_LABEL: "on"}, "on", (True, True)),
    ],
)
def test_mode_and_turn_flags_follow_the_label(
    labels: dict[str, str] | None, mode: str, flags: tuple[bool, bool]
) -> None:
    assert browser_agent_tools_mode(labels) == mode
    assert browser_agent_tools_enabled(labels) is (mode == "on")
    assert browser_agent_tools_turn_flag(labels) == {
        "browser_agent_tools": flags[0],
        "browser_trusted_input": flags[1],
    }


def _assert_no_hidden_tool_named(schemas: list[dict[str, Any]]) -> None:
    for schema in schemas:
        description = schema.get("function", schema).get("description", "")
        assert not any(hidden in description for hidden in ADVANCED_BROWSER_TOOL_NAMES), (
            description
        )


@pytest.mark.parametrize("shape", [_nested, _flat])
def test_trusted_hides_advanced_tools_and_click_options(shape: Any) -> None:
    schemas = shape()
    filtered = filter_browser_agent_tool_schemas(schemas, mode="trusted")
    assert _names(filtered) == BASIC | {"list_comments"}
    click = _click(filtered)
    assert set(click["parameters"]["properties"]) == {"ref", "snapshot_id", "selector"}
    assert "double_click" not in click["description"]
    assert click["description"].endswith("set them with browser_type.")
    typing = _tool(filtered, "browser_type")
    assert typing["description"].endswith("value or label is `text`.")
    _assert_no_hidden_tool_named(filtered)
    # The input list is not mutated.
    assert "button" in _click(schemas)["parameters"]["properties"]
    assert "browser_press_key" in _tool(schemas, "browser_type")["description"]


@pytest.mark.parametrize("shape", [_nested, _flat])
def test_standard_gives_click_and_type_their_standard_contract(shape: Any) -> None:
    filtered = filter_browser_agent_tool_schemas(shape(), mode="standard")
    assert _names(filtered) == BASIC | {"list_comments"}
    for cls in (BrowserClickTool, BrowserTypeTool):
        standard = cls().get_schema()["function"]
        kept = _tool(filtered, standard["name"])
        assert kept["description"] == standard["description"]
        assert kept["parameters"] == standard["parameters"]
    # No promise of rich-text editors or select labels an older desktop app can't keep.
    assert "rich-text" not in _tool(filtered, "browser_type")["description"]
    assert "label" not in _tool(filtered, "browser_type")["description"]
    _assert_no_hidden_tool_named(filtered)


def test_on_keeps_everything() -> None:
    schemas = _nested()
    assert filter_browser_agent_tool_schemas(schemas, mode="on") is schemas


def test_turn_filter_treats_missing_flags_as_standard() -> None:
    standard_type = BrowserTypeTool().get_schema()["function"]["description"]
    missing = filter_turn_browser_tools(_nested(), {})
    assert _names(missing) == BASIC | {"list_comments"}
    assert _tool(missing, "browser_type")["description"] == standard_type
    trusted = filter_turn_browser_tools(_nested(), {"browser_trusted_input": True})
    assert _tool(trusted, "browser_type")["description"].endswith("value or label is `text`.")
    assert _names(filter_turn_browser_tools(_nested(), {"browser_agent_tools": True})) == (
        BASIC | ADVANCED_BROWSER_TOOL_NAMES | {"list_comments"}
    )


async def test_native_relay_uses_given_labels_without_fetching() -> None:
    client = AsyncMock()
    filtered = await filter_native_relay_browser_tools(
        _flat(),
        session_id="conv_1",
        server_client=client,
        session_labels={BROWSER_AGENT_TOOLS_LABEL: "on"},
    )
    assert _names(filtered) == BASIC | ADVANCED_BROWSER_TOOL_NAMES | {"list_comments"}
    client.get.assert_not_called()


async def test_native_relay_fetches_labels_when_unknown() -> None:
    with patch(
        "omnigent.runner.native.orchestration._session_labels_for_runner_spawn",
        AsyncMock(return_value={BROWSER_AGENT_TOOLS_LABEL: "on"}),
    ) as fetch:
        filtered = await filter_native_relay_browser_tools(
            _flat(), session_id="conv_1", server_client=AsyncMock()
        )
    assert fetch.await_args.kwargs["session_id"] == "conv_1"
    assert _names(filtered) == BASIC | ADVANCED_BROWSER_TOOL_NAMES | {"list_comments"}


async def test_native_relay_without_labels_or_client_is_off() -> None:
    filtered = await filter_native_relay_browser_tools(
        _flat(), session_id="conv_1", server_client=None
    )
    assert _names(filtered) == BASIC | {"list_comments"}


def test_select_schema_states_the_executors_value_limits() -> None:
    from omnigent.tools.builtins.browser_advanced import BrowserSelectOptionTool

    schema = BrowserSelectOptionTool().get_schema()
    values = schema["function"]["parameters"]["properties"]["values"]
    assert (values["minItems"], values["maxItems"], values["items"]["maxLength"]) == (1, 100, 4096)


def test_snapshot_description_admits_older_apps_lack_viewport_metadata() -> None:
    assert "Older apps return the plain tree" in BrowserSnapshotTool().description()
