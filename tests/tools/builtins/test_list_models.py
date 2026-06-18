"""Unit tests for :mod:`omnigent.tools.builtins.list_models`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from omnigent.spec.types import AgentSpec, ApiKeyAuth, ExecutorSpec
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.list_models import SysListModelsTool


def _make_spec() -> AgentSpec:
    """Minimal AgentSpec for constructing the tool."""
    return AgentSpec(spec_version=1)


def _ctx() -> ToolContext:
    return ToolContext(task_id="task_test", agent_id="agent_test")


def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the provider-config layer at an empty isolated config.

    So ``catalog_for_spec`` resolves against a known-empty config (no
    ``providers:`` default, no keyring) instead of the developer's real one.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir for the config file.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    (tmp_path / "config.yaml").write_text("")


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


# ── End-to-end: an antigravity worker's row in the real tool payload ──


def test_invoke_antigravity_worker_reports_runnable_not_dead_or_openai(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A configured antigravity sub-agent surfaces as runnable-non-enumerable.

    End-to-end through the REAL ``catalog_for_spec`` (no mock): the JSON an
    orchestrator actually receives from ``sys_list_models`` for a Gemini-native
    antigravity worker holding an api-key must say the worker CAN run
    (``source="runnable"``, empty models, a "can run" note) — NOT the dead-worker
    ``source="none"`` "cannot run here" signal, and NOT a fabricated OpenAI-family
    model list. This is the surface the dispatch-preflight reads, so the row's
    semantics (runnable vs dead) are load-bearing.
    """
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_API_KEY", raising=False)
    _isolate_config(monkeypatch, tmp_path)
    parent = AgentSpec(
        spec_version=1,
        name="orchestrator",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        sub_agents=[
            AgentSpec(
                spec_version=1,
                name="gemini_worker",
                executor=ExecutorSpec(
                    type="omnigent",
                    config={"harness": "antigravity"},
                    auth=ApiKeyAuth(api_key="AIza-test"),
                ),
            ),
        ],
    )
    payload = json.loads(SysListModelsTool(spec=parent).invoke("{}", _ctx()))
    row = payload["gemini_worker"]
    assert row["source"] == "runnable"
    assert row["models"] == []
    assert "can run" in row["note"]
    assert "cannot run here" not in row["note"]


def test_invoke_antigravity_unconfigured_worker_reads_dead(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A keyless antigravity sub-agent keeps the dead-worker preflight signal.

    The complement to the runnable case: with no Gemini credential anywhere, the
    worker genuinely cannot run, so its ``sys_list_models`` row must read
    ``source="none"`` with the "cannot run here" note.
    """
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_API_KEY", raising=False)
    _isolate_config(monkeypatch, tmp_path)
    parent = AgentSpec(
        spec_version=1,
        name="orchestrator",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        sub_agents=[
            AgentSpec(
                spec_version=1,
                name="gemini_worker",
                executor=ExecutorSpec(type="omnigent", config={"harness": "antigravity"}),
            ),
        ],
    )
    payload = json.loads(SysListModelsTool(spec=parent).invoke("{}", _ctx()))
    row = payload["gemini_worker"]
    assert row["source"] == "none"
    assert row["models"] == []
    assert "cannot run here" in row["note"]
