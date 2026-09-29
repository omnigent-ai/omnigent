"""Unit tests for :mod:`omnigent.tools.builtins.list_models`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from omnigent.spec.types import AgentSpec, ExecutorSpec, ProviderAuth
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
        "omnigent.models.model_catalog.catalog_for_spec",
        return_value=fake_catalog,
    ) as mock_catalog:
        tool = SysListModelsTool(spec=_make_spec())
        result = tool.invoke("{}", _ctx())

    mock_catalog.assert_called_once()
    parsed = json.loads(result)
    assert "self" in parsed
    assert parsed["self"]["models"][0]["id"] == "gpt-4o"


def test_invoke_lists_acp_curated_models_without_gateway_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real tool advertises only the ACP child's configured, dispatchable ids."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.delenv("ACP_TEST_CATALOG_API_KEY", raising=False)
    monkeypatch.setattr("omnigent.onboarding.detected.detect_providers", list)
    (tmp_path / "config.yaml").write_text(
        "providers:\n"
        "  gateway:\n"
        "    kind: gateway\n"
        "    openai:\n"
        "      base_url: https://gateway.example.com/v1\n"
        "      api_key: $ACP_TEST_CATALOG_API_KEY\n"
        "      models:\n"
        "        default: databricks-gpt-5-4\n"
        "        alternate: vendor/custom-b\n"
    )
    child = AgentSpec(
        spec_version=1,
        name="acp_worker",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "acp:custom"},
            auth=ProviderAuth(name="gateway"),
        ),
    )
    parent = AgentSpec(spec_version=1, sub_agents=[child])

    parsed = json.loads(SysListModelsTool(spec=parent).invoke("{}", _ctx()))

    assert [model["id"] for model in parsed["acp_worker"]["models"]] == [
        "databricks-gpt-5-4",
        "vendor/custom-b",
    ]
    assert parsed["acp_worker"]["source"] == "static"
    assert parsed["acp_worker"]["verified"] is False
