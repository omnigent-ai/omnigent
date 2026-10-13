"""Native relay surfaces spec-local Python tools from the verified agent bundle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.runner.app import ResolvedSpec
from omnigent.runner.native.orchestration import _is_spec_local_native_python_tool
from omnigent.runner.tool_dispatch import (
    _NATIVE_RELAY_BUILTIN_TOOLS,
    build_native_relay_tool_schemas,
    execute_tool,
)
from omnigent.spec.types import AgentSpec, LocalToolInfo


def _write_echo_tool(bundle: Path, name: str = "relay_echo") -> None:
    tool_path = bundle / "tools" / "python" / f"{name}.py"
    tool_path.parent.mkdir(parents=True, exist_ok=True)
    tool_path.write_text(
        "from omnigent_client import tool\n\n"
        f"@tool\n"
        f"def {name}(message: str) -> str:\n"
        '    return "echo:" + message\n'
    )


_MOTION_TOOL_NAMES = (
    "motion_task_submit",
    "motion_task_status",
    "motion_task_result",
    "motion_task_cancel",
)


def _write_motion_style_tools(bundle: Path) -> None:
    """One module stem (parser name) exporting four distinct @tool functions."""
    tool_path = bundle / "tools" / "python" / "motion_task_tools.py"
    tool_path.parent.mkdir(parents=True, exist_ok=True)
    bodies = []
    for fn in _MOTION_TOOL_NAMES:
        bodies.append(f'@tool\ndef {fn}(task_id: str) -> str:\n    return "{fn}:" + task_id\n')
    tool_path.write_text("from omnigent_client import tool\n\n" + "\n".join(bodies) + "\n")


def _spec_with_local(name: str, path: str, *, agent_name: str = "relay-agent") -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name=agent_name,
        local_tools=[
            LocalToolInfo(
                name=name,
                path=path,
                language="python",
            )
        ],
    )


def test_multi_tool_module_relayed_when_parser_name_is_file_stem(tmp_path: Path) -> None:
    """``LocalToolInfo.name`` is the file stem; relay exposes each ``@tool`` fn."""
    bundle = tmp_path / "bundle"
    _write_motion_style_tools(bundle)
    spec = _spec_with_local(
        "motion_task_tools",
        "tools/python/motion_task_tools.py",
        agent_name="tolowa-orchestrator",
    )

    names = {s["name"] for s in build_native_relay_tool_schemas(spec, local_tool_workdir=bundle)}

    assert set(_MOTION_TOOL_NAMES) <= names
    assert "motion_task_tools" not in names


def test_undeclared_local_module_tools_do_not_relay(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    _write_motion_style_tools(bundle)
    _write_echo_tool(bundle, name="extra_secret_tool")
    spec = _spec_with_local("motion_task_tools", "tools/python/motion_task_tools.py")

    names = {s["name"] for s in build_native_relay_tool_schemas(spec, local_tool_workdir=bundle)}

    assert set(_MOTION_TOOL_NAMES) <= names
    assert "extra_secret_tool" not in names


def test_local_tool_omitted_without_bundle_workdir(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    _write_echo_tool(bundle)
    spec = _spec_with_local("relay_echo", "tools/python/relay_echo.py")

    names = {s["name"] for s in build_native_relay_tool_schemas(spec, local_tool_workdir=None)}

    assert "relay_echo" not in names


def test_foreign_bundle_does_not_leak_unrelated_local_tools(tmp_path: Path) -> None:
    agent_a_bundle = tmp_path / "agent-a"
    agent_b_bundle = tmp_path / "agent-b"
    _write_echo_tool(agent_a_bundle, name="agent_a_echo")
    _write_echo_tool(agent_b_bundle, name="agent_b_echo")
    spec_a = _spec_with_local("agent_a_echo", "tools/python/agent_a_echo.py", agent_name="agent-a")

    names = {
        s["name"]
        for s in build_native_relay_tool_schemas(spec_a, local_tool_workdir=agent_b_bundle)
    }

    assert "agent_a_echo" not in names
    assert "agent_b_echo" not in names


def test_builtins_remain_when_local_tools_are_relayed(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    _write_echo_tool(bundle)
    spec = _spec_with_local("relay_echo", "tools/python/relay_echo.py")

    names = {s["name"] for s in build_native_relay_tool_schemas(spec, local_tool_workdir=bundle)}

    assert "load_skill" in names
    assert names & _NATIVE_RELAY_BUILTIN_TOOLS


@pytest.mark.asyncio
async def test_local_tool_executes_through_central_dispatch(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_motion_style_tools(bundle)
    spec = _spec_with_local("motion_task_tools", "tools/python/motion_task_tools.py")
    entry = ResolvedSpec(spec=spec, workdir=bundle)

    assert _is_spec_local_native_python_tool(entry, "motion_task_status")

    result = await execute_tool(
        tool_name="motion_task_status",
        arguments=json.dumps({"task_id": "task-1"}),
        agent_spec=spec,
        conversation_id="conv-native-relay",
        agent_id="ag_relay",
        runner_workspace=workspace,
        local_tool_workdir=bundle,
    )

    assert result == "motion_task_status:task-1"
