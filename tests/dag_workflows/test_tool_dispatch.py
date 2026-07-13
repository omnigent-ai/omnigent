from __future__ import annotations

import json

import pytest

from omnigent.dag_workflows.models import WorkflowRuntimeConfig
from omnigent.dag_workflows.runtime import remove_workflow_manager
from omnigent.runner.tool_dispatch import _execute_workflow_tool
from omnigent.spec.types import AgentSpec
from tests.runner.helpers import NullServerClient


@pytest.mark.asyncio
async def test_workflow_submit_and_get_use_runner_local_manager() -> None:
    session_id = "conv_workflow_tools"
    spec = AgentSpec(
        spec_version=1,
        workflows=WorkflowRuntimeConfig(enabled=True, max_concurrency=2),
    )
    definition = {
        "id": "wf",
        "name": "One node",
        "budget": {"max_concurrency": 1, "max_dispatches": 2},
        "nodes": [
            {
                "id": "a",
                "title": "A",
                "contract": "Return an object",
                "agent": "codex",
            }
        ],
    }
    kwargs = {
        "server_client": NullServerClient(),
        "conversation_id": session_id,
        "agent_spec": spec,
        "publish_event": None,
        "session_inbox": None,
        "session_async_tasks": None,
    }
    try:
        submitted = json.loads(
            await _execute_workflow_tool(
                "sys_workflow_submit", {"definition": definition}, **kwargs
            )
        )
        fetched = json.loads(
            await _execute_workflow_tool("sys_workflow_get", {"workflow_id": "wf"}, **kwargs)
        )
        assert submitted["status"] == "draft"
        assert fetched["definition_hash"] == submitted["definition_hash"]
        assert fetched["nodes"][0]["state"] == "pending"
    finally:
        await remove_workflow_manager(session_id)
