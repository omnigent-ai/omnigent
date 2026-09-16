"""Regression tests for local-tool bundle and session workspace separation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from omnigent.runner.tool_dispatch import execute_tool
from omnigent.spec.types import AgentSpec, LocalToolInfo


def _probe_spec(bundle: Path, *, agent_name: str = "probe") -> AgentSpec:
    """Create a bundle tool that reports its code root and execution workspace."""
    tool_path = bundle / "tools" / "python" / "workspace_probe.py"
    tool_path.parent.mkdir(parents=True)
    tool_path.write_text(
        "from pathlib import Path\n"
        "import os\n"
        "from omnigent_client import tool\n\n"
        "@tool\n"
        "def workspace_probe(value: str) -> str:\n"
        "    return f'{os.environ.get(\"_AP_WORKSPACE\")}|{Path(__file__).resolve()}'\n"
    )
    return AgentSpec(
        spec_version=1,
        name=agent_name,
        local_tools=[
            LocalToolInfo(
                name="workspace_probe",
                path="tools/python/workspace_probe.py",
                language="python",
            )
        ],
    )


async def _run_probe(spec: AgentSpec, bundle: Path, workspace: Path, session_id: str) -> str:
    return await execute_tool(
        tool_name="workspace_probe",
        arguments=json.dumps({"value": "ignored"}),
        agent_spec=spec,
        conversation_id=session_id,
        agent_id="ag_probe",
        runner_workspace=workspace,
        local_tool_workdir=bundle,
    )


@pytest.mark.asyncio
async def test_local_tool_import_root_is_separate_from_exact_session_workspace(
    tmp_path: Path,
) -> None:
    """Tool code loads from the bundle while ``_AP_WORKSPACE`` is the worktree."""
    bundle = tmp_path / "bundle"
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    spec = _probe_spec(bundle)

    result = await _run_probe(spec, bundle, workspace, "child-session")

    assert result == f"{workspace}|{(bundle / 'tools/python/workspace_probe.py').resolve()}"
    assert not (workspace / "child-session").exists()
    assert not (workspace / ".tool_state").exists()


@pytest.mark.asyncio
async def test_concurrent_sessions_do_not_cross_bind_execution_workspaces(tmp_path: Path) -> None:
    """Concurrent children keep their own `_AP_WORKSPACE` values."""
    bundle = tmp_path / "bundle"
    first_workspace = tmp_path / "first-worktree"
    second_workspace = tmp_path / "second-worktree"
    first_workspace.mkdir()
    second_workspace.mkdir()
    spec = _probe_spec(bundle)

    first, second = await asyncio.gather(
        _run_probe(spec, bundle, first_workspace, "child-a"),
        _run_probe(spec, bundle, second_workspace, "child-b"),
    )

    module_path = (bundle / "tools/python/workspace_probe.py").resolve()
    assert first == f"{first_workspace}|{module_path}"
    assert second == f"{second_workspace}|{module_path}"


@pytest.mark.asyncio
async def test_sibling_bundle_roots_remain_isolated_from_shared_workspace(tmp_path: Path) -> None:
    """Siblings may share a runner root without sharing bundle imports."""
    workspace = tmp_path / "shared-worktree"
    first_bundle = tmp_path / "first-bundle"
    second_bundle = tmp_path / "second-bundle"
    workspace.mkdir()
    first_spec = _probe_spec(first_bundle, agent_name="first")
    second_spec = _probe_spec(second_bundle, agent_name="second")

    first, second = await asyncio.gather(
        _run_probe(first_spec, first_bundle, workspace, "first-child"),
        _run_probe(second_spec, second_bundle, workspace, "second-child"),
    )

    assert first == f"{workspace}|{(first_bundle / 'tools/python/workspace_probe.py').resolve()}"
    assert second == f"{workspace}|{(second_bundle / 'tools/python/workspace_probe.py').resolve()}"


@pytest.mark.asyncio
async def test_async_custom_tool_keeps_bundle_root_and_session_workspace(tmp_path: Path) -> None:
    """A builtin ``sys_call_async`` still threads both roots to its custom target."""
    bundle = tmp_path / "bundle"
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    spec = _probe_spec(bundle)
    inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}

    raw_handle = await execute_tool(
        tool_name="sys_call_async",
        arguments=json.dumps(
            {"tool": "workspace_probe", "args": json.dumps({"value": "ignored"})}
        ),
        agent_spec=spec,
        conversation_id="async-child",
        agent_id="ag_probe",
        runner_workspace=workspace,
        local_tool_workdir=bundle,
        session_inbox=inbox,
        session_async_tasks=tasks,
    )
    handle_id = json.loads(raw_handle)["handle_id"]
    await tasks[handle_id][0]
    completion = await inbox.get()

    expected = f"{workspace}|{(bundle / 'tools/python/workspace_probe.py').resolve()}"
    assert completion["status"] == "completed"
    assert completion["output"] == expected


@pytest.mark.asyncio
async def test_explicit_missing_bundle_never_falls_back_to_session_workspace(
    tmp_path: Path,
) -> None:
    """A resolved no-bundle verdict cannot load tools from the worktree."""
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    spec = _probe_spec(workspace)

    result = await execute_tool(
        tool_name="workspace_probe",
        arguments=json.dumps({"value": "ignored"}),
        agent_spec=spec,
        conversation_id="no-bundle-child",
        runner_workspace=workspace,
        local_tool_workdir=None,
    )

    assert result.startswith("Error:")
    assert str(workspace / "tools/python/workspace_probe.py") not in result
