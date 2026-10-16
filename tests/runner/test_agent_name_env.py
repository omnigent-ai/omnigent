"""``OMNIGENT_AGENT_NAME`` reaches the shells behind ``sys_os_*`` tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.entities import DEFAULT_ENVIRONMENT_ID
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.runner.tool_dispatch import _execute_os_env_tool
from omnigent.spec.types import AgentSpec

_ECHO_NAME = 'echo "name=[${OMNIGENT_AGENT_NAME-unset}]"'


def _agent(workspace: Path) -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name="code-reviewer",
        os_env=OSEnvSpec(cwd=str(workspace), sandbox=OSEnvSandboxSpec(type="none")),
    )


@pytest.mark.asyncio
async def test_registry_primary_env_exports_agent_name(tmp_path: Path) -> None:
    registry = SessionResourceRegistry(runner_workspace=tmp_path)
    try:
        env = registry.resolve_environment("s", DEFAULT_ENVIRONMENT_ID, _agent(tmp_path))
        result = await env.shell(_ECHO_NAME)
    finally:
        await registry.cleanup_session("s")
    assert result["stdout"] == "name=[code-reviewer]\n"


@pytest.mark.asyncio
async def test_per_call_os_tool_env_exports_agent_name(tmp_path: Path) -> None:
    result = json.loads(
        await _execute_os_env_tool(
            "sys_os_shell", {"command": _ECHO_NAME}, agent_spec=_agent(tmp_path)
        )
    )
    assert result["stdout"] == "name=[code-reviewer]\n"
