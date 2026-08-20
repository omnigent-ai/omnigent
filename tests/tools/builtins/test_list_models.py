"""Unit tests for :mod:`omnigent.tools.builtins.list_models`."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from omnigent.spec.types import AgentSpec
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.list_models import SysListModelsTool


def _make_spec() -> AgentSpec:
    """Minimal AgentSpec for constructing the tool."""
    return AgentSpec(spec_version=1)


def _ctx() -> ToolContext:
    return ToolContext(task_id="task_test", agent_id="agent_test")


# ── Schema ───────────────────────────────────────────────


def test_schema_shape() -> None:
    """Schema is a function-type tool with no parameters."""
    tool = SysListModelsTool(spec=_make_spec())
    schema = tool.get_schema()
    assert schema["type"] == "function"
    func = schema["function"]
    assert func["name"] == "sys_list_models"
    assert func["parameters"]["properties"] == {}
    assert func["parameters"]["required"] == []


def test_name_and_description() -> None:
    """Class methods return stable name and non-empty description."""
    assert SysListModelsTool.name() == "sys_list_models"
    assert len(SysListModelsTool.description()) > 0


# ── Invoke ───────────────────────────────────────────────


def test_invoke_returns_catalog(
    monkeypatch: Any,
) -> None:
    """
    invoke() delegates to catalog_for_spec and returns its JSON output.
    """
    fake_catalog = {
        "self": {
            "source": "env",
            "verified": True,
            "models": [{"id": "gpt-4o", "family": "openai"}],
            "note": "",
        },
    }
    with patch(
        "omnigent.model_catalog.catalog_for_spec",
        return_value=fake_catalog,
    ) as mock_catalog:
        tool = SysListModelsTool(spec=_make_spec())
        result = tool.invoke("{}", _ctx())

    mock_catalog.assert_called_once()
    parsed = json.loads(result)
    assert "self" in parsed
    assert parsed["self"]["models"][0]["id"] == "gpt-4o"
