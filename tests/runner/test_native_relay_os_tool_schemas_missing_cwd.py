"""Native relay schema building must survive a removed process cwd."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omnigent.runner.tool_dispatch import build_native_relay_tool_schemas
from omnigent.spec.types import AgentSpec


def _remove_process_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``os.getcwd()`` fail like a deleted launch directory."""

    def missing_cwd() -> str:
        raise FileNotFoundError("process cwd was removed")

    monkeypatch.setattr(os, "getcwd", missing_cwd)


def test_relay_schemas_use_workspace_when_process_cwd_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Build OS tool schemas from runner workspace after process cwd is removed."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(workspace))
    _remove_process_cwd(monkeypatch)
    schemas = build_native_relay_tool_schemas(AgentSpec(spec_version=1))
    names = {str(schema["name"]) for schema in schemas}
    assert any(name.startswith("sys_os_") for name in names)


def test_relay_schemas_degrade_without_workspace_when_cwd_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omit OS schemas instead of raising when no workspace remains."""
    monkeypatch.delenv("OMNIGENT_RUNNER_WORKSPACE", raising=False)
    _remove_process_cwd(monkeypatch)
    schemas = build_native_relay_tool_schemas(AgentSpec(spec_version=1))
    names = {str(schema["name"]) for schema in schemas}
    assert names, "non-OS relay schemas must survive a removed process cwd"
    assert not any(name.startswith("sys_os_") for name in names)
