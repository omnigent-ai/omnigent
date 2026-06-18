"""Unit tests for :mod:`omnigent.tools.builtins.agents`.

These tools are runner-dispatched (schema-only); tests verify schema
shapes, name/description class methods, and that the base-class
``invoke`` raises ``NotImplementedError``.
"""

from __future__ import annotations

import pytest

from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.agents import (
    SysAgentDownloadTool,
    SysAgentGetTool,
    SysAgentListTool,
)

_CTX = ToolContext(task_id="task_test", agent_id="agent_test")


# ── SysAgentGetTool ──────────────────────────────────────


class TestSysAgentGetTool:
    def test_name(self) -> None:
        assert SysAgentGetTool.name() == "sys_agent_get"

    def test_description_non_empty(self) -> None:
        assert len(SysAgentGetTool.description()) > 0

    def test_schema_shape(self) -> None:
        tool = SysAgentGetTool()
        schema = tool.get_schema()
        assert schema["type"] == "function"
        func = schema["function"]
        assert func["name"] == "sys_agent_get"
        params = func["parameters"]
        assert "session_id" in params["required"]
        assert params["properties"]["session_id"]["type"] == "string"
        assert params.get("additionalProperties") is False

    def test_invoke_raises(self) -> None:
        """Runner-dispatched; invoke raises NotImplementedError."""
        tool = SysAgentGetTool()
        with pytest.raises(NotImplementedError):
            tool.invoke("{}", _CTX)


# ── SysAgentDownloadTool ─────────────────────────────────


class TestSysAgentDownloadTool:
    def test_name(self) -> None:
        assert SysAgentDownloadTool.name() == "sys_agent_download"

    def test_description_non_empty(self) -> None:
        assert len(SysAgentDownloadTool.description()) > 0

    def test_schema_shape(self) -> None:
        tool = SysAgentDownloadTool()
        schema = tool.get_schema()
        assert schema["type"] == "function"
        func = schema["function"]
        assert func["name"] == "sys_agent_download"
        params = func["parameters"]
        assert "session_id" in params["required"]
        props = params["properties"]
        assert props["session_id"]["type"] == "string"
        assert "dest_filename" in props
        assert props["dest_filename"]["type"] == "string"
        assert params.get("additionalProperties") is False

    def test_invoke_raises(self) -> None:
        tool = SysAgentDownloadTool()
        with pytest.raises(NotImplementedError):
            tool.invoke("{}", _CTX)


# ── SysAgentListTool ─────────────────────────────────────


class TestSysAgentListTool:
    def test_name(self) -> None:
        assert SysAgentListTool.name() == "sys_agent_list"

    def test_description_non_empty(self) -> None:
        assert len(SysAgentListTool.description()) > 0

    def test_schema_shape(self) -> None:
        tool = SysAgentListTool()
        schema = tool.get_schema()
        assert schema["type"] == "function"
        func = schema["function"]
        assert func["name"] == "sys_agent_list"
        params = func["parameters"]
        assert params["properties"] == {}
        assert params.get("additionalProperties") is False

    def test_invoke_raises(self) -> None:
        tool = SysAgentListTool()
        with pytest.raises(NotImplementedError):
            tool.invoke("{}", _CTX)
