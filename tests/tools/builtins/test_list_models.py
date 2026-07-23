"""Unit tests for :mod:`omnigent.tools.builtins.list_models`."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from omnigent.spec.types import AgentSpec
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.list_models import SysListModelsTool, filter_model_catalog


def _make_spec() -> AgentSpec:
    """Minimal AgentSpec for constructing the tool."""
    return AgentSpec(spec_version=1)


def _ctx() -> ToolContext:
    return ToolContext(task_id="task_test", agent_id="agent_test")


# ── Schema ───────────────────────────────────────────────


def test_schema_shape() -> None:
    """Schema exposes both optional filters."""
    tool = SysListModelsTool(spec=_make_spec())
    schema = tool.get_schema()
    assert schema["type"] == "function"
    func = schema["function"]
    assert func["name"] == "sys_list_models"
    properties = func["parameters"]["properties"]
    assert set(properties) == {"workers", "model_ids"}
    assert properties["workers"]["items"] == {"type": "string"}
    assert properties["workers"]["uniqueItems"] is True
    assert properties["model_ids"]["items"] == {"type": "string"}
    assert properties["model_ids"]["uniqueItems"] is True
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


def test_invoke_applies_combined_filters() -> None:
    """invoke() applies the same worker and model filters as the runner."""
    fake_catalog = {
        "worker": {
            "models": [
                {"id": "claude-opus-4-8", "family": "anthropic"},
                {"id": "claude-sonnet-5", "family": "anthropic"},
            ],
        },
        "self": {"models": [{"id": "gpt-5.6-sol", "family": "openai"}]},
    }
    with patch(
        "omnigent.model_catalog.catalog_for_spec",
        return_value=fake_catalog,
    ):
        tool = SysListModelsTool(spec=_make_spec())
        result = tool.invoke(
            json.dumps(
                {
                    "workers": ["worker"],
                    "model_ids": ["claude-sonnet-5"],
                }
            ),
            _ctx(),
        )

    assert json.loads(result) == {
        "worker": {
            "models": [{"id": "claude-sonnet-5", "family": "anthropic"}],
        }
    }


def test_invoke_rejects_non_object_arguments() -> None:
    """Direct callers receive the standard JSON-object validation error."""
    tool = SysListModelsTool(spec=_make_spec())
    assert json.loads(tool.invoke("[]", _ctx())) == {"error": "arguments must be a JSON object"}


def test_filter_model_catalog_filters_independently_and_preserves_rows() -> None:
    """Each optional filter works alone and filtering does not mutate input."""
    catalog = {
        "worker": {
            "source": "static",
            "models": [
                {"id": "claude-opus-4-8", "family": "anthropic"},
                {"id": "claude-sonnet-5", "family": "anthropic"},
            ],
        },
        "self": {"source": "none", "models": [], "note": "unavailable"},
    }

    assert set(filter_model_catalog(catalog, {"workers": ["self"]})) == {"self"}
    assert filter_model_catalog(catalog, {"model_ids": ["claude-sonnet-5"]}) == {
        "worker": {
            "source": "static",
            "models": [{"id": "claude-sonnet-5", "family": "anthropic"}],
        },
        "self": {"source": "none", "models": [], "note": "unavailable"},
    }
    assert len(catalog["worker"]["models"]) == 2
