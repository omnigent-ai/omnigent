"""Tests for the Databricks supervisor harness spawn env."""

from __future__ import annotations

import json

from omnigent.runtime.workflow import _build_databricks_supervisor_spawn_env
from omnigent.spec.types import AgentSpec, DatabricksAuth, ExecutorSpec, LLMConfig


def _make_spec() -> AgentSpec:
    """Build a minimal Databricks supervisor spec."""
    model = "databricks-claude-sonnet-4-6"
    tools = [
        {
            "type": "uc_connection",
            "uc_connection": {
                "name": "system_ai_agent_google_drive",
                "description": "Search team Google Drive",
            },
        }
    ]
    return AgentSpec(
        spec_version=1,
        name="supervisor-test",
        instructions="Use the connector.",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "databricks_supervisor"},
            model=model,
            supervisor_tools=tools,
            auth=DatabricksAuth(profile="oss"),
        ),
        llm=LLMConfig(model=model),
    )


def test_supervisor_spawn_env_threads_model_profile_and_tools() -> None:
    """The supervisor harness gets every required ``HARNESS_SUPERVISOR_*`` key."""
    env = _build_databricks_supervisor_spawn_env(_make_spec())

    assert env["HARNESS_SUPERVISOR_MODEL"] == "databricks-claude-sonnet-4-6"
    assert env["HARNESS_SUPERVISOR_DATABRICKS_PROFILE"] == "oss"
    tools = json.loads(env["HARNESS_SUPERVISOR_TOOLS_JSON"])
    assert tools[0]["uc_connection"]["name"] == "system_ai_agent_google_drive"
