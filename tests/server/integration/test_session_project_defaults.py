"""Sessions an agent starts in a project take the project's agent, model and effort."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI

from omnigent.native.permission_modes import PERMISSION_LAUNCH_ARGS
from omnigent.runner.tool_dispatch import execute_tool
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from tests.server.integration.test_session_orchestration import (
    _codex_creator,
    _upload_codex_worker,
)
from tests.server.integration.test_session_orchestration import (
    creator_client as creator_client,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def auth_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """The orchestration tests' app (managed sandboxes configured), with projects."""
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.server.managed_hosts import parse_sandbox_config
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
        host_store=HostStore(db_uri),
        project_store=SqlAlchemyProjectStore(db_uri),
        sandbox_config=parse_sandbox_config(
            {
                "provider": "modal",
                "server_url": "https://managed-test.example.com",
                "modal": {"image": "docker.io/test/omnigent-host:latest"},
            }
        ),
    )


@pytest.mark.parametrize(
    ("project_config", "arguments", "expected"),
    [
        # No agent, model or effort: the project's defaults apply.
        pytest.param(
            {"model": "project-model", "effort": "high"},
            {},
            ("worker", "project-model", "high"),
            id="inherits",
        ),
        pytest.param(
            {"model": "project-model", "effort": "high"},
            {"model": "chosen-model", "reasoning_effort": "low"},
            ("worker", "chosen-model", "low"),
            id="explicit-wins",
        ),
        # The project's model belongs to its own agent, not another one.
        pytest.param(
            {"model": "project-model", "effort": "high"},
            {"agent_id": "creator"},
            ("creator", None, None),
            id="other-agent",
        ),
        pytest.param(None, {"agent_id": "creator"}, ("creator", None, None), id="no-project"),
        # The project's agent sets a skip default; an agent-started session still asks.
        pytest.param(
            {"model": "project-model"},
            {},
            ("skipping-worker", "project-model", None),
            id="skip-default-withheld",
        ),
    ],
)
async def test_agent_started_sessions_take_their_projects_defaults(
    creator_client, auth_app, db_uri, monkeypatch, project_config, arguments, expected
):
    from omnigent.server.routes.sessions import routes_core

    creator = await _codex_creator(creator_client, auth_app, db_uri, monkeypatch, "approve-for-me")
    agents = {
        "creator": creator["agent_id"],
        "worker": await _upload_codex_worker(creator_client),
        "skipping-worker": await _upload_codex_worker(
            creator_client, permission_mode="full-access"
        ),
    }
    expected_agent, expected_model, expected_effort = expected
    if project_config is not None:
        project_agent = "skipping-worker" if expected_agent == "skipping-worker" else "worker"
        project = SqlAlchemyProjectStore(db_uri).create(
            uuid4().hex,
            "Defaults",
            "alice",
            config={"agent_id": agents[project_agent], **project_config},
        )
        SqlAlchemyConversationStore(db_uri).set_conversation_project(
            creator["session_id"], project.id
        )

    async def launch(**kwargs):
        pass

    monkeypatch.setattr(routes_core, "_run_managed_launch", launch)
    if "agent_id" in arguments:
        arguments = {**arguments, "agent_id": agents[arguments["agent_id"]]}
    result = json.loads(
        await execute_tool(
            tool_name="sys_session_create",
            arguments=json.dumps({"placement": "managed", **arguments}),
            server_client=creator_client,
            conversation_id=creator["session_id"],
        )
    )
    assert result.get("conversation_id"), result
    worker = SqlAlchemyConversationStore(db_uri).get_conversation(result["conversation_id"])
    assert worker.agent_id == agents[expected_agent]
    assert worker.model_override == expected_model
    assert worker.reasoning_effort == expected_effort
    assert worker.terminal_launch_args == PERMISSION_LAUNCH_ARGS["codex-native"]["approve-for-me"]


async def test_an_agent_without_a_project_agent_must_name_one(
    creator_client, auth_app, db_uri, monkeypatch
):
    creator = await _codex_creator(creator_client, auth_app, db_uri, monkeypatch, "approve-for-me")
    result = json.loads(
        await execute_tool(
            tool_name="sys_session_create",
            arguments=json.dumps({"placement": "managed"}),
            server_client=creator_client,
            conversation_id=creator["session_id"],
        )
    )
    assert "agent_id is required" in json.dumps(result), result


@pytest.mark.parametrize(
    ("config", "agent_id", "harness", "requested", "expected"),
    [
        ({"agent_id": "a1", "model": "m", "effort": "high"}, "a1", "codex-native", (), "both"),
        ({"agent_id": "a1", "model": "m", "effort": "high"}, "a2", "codex-native", (), "none"),
        # With no project agent, only the project's own harness takes its model.
        ({"harness": "codex", "model": "m", "effort": "high"}, "a2", "codex", (), "both"),
        ({"harness": "codex", "model": "m"}, "a2", "claude-native", (), "none"),
        ({"model": "m", "effort": "high"}, "a2", "codex-native", (), "none"),
        # A field the request set, even to null, keeps its value.
        (
            {"agent_id": "a1", "model": "m", "effort": "high"},
            "a1",
            None,
            ("model_override",),
            "effort",
        ),
        # Permission settings never ride along.
        ({"agent_id": "a1", "permission_mode": "full-access"}, "a1", None, (), "none"),
    ],
)
def test_project_model_defaults_match_the_projects_agent(
    config, agent_id, harness, requested, expected
):
    from omnigent.server.routes._session_create_validation import project_model_defaults

    updates = project_model_defaults(config, requested, agent_id=agent_id, harness=harness)
    assert (
        updates
        == {
            "both": {"model_override": "m", "reasoning_effort": "high"},
            "effort": {"reasoning_effort": "high"},
            "none": {},
        }[expected]
    )


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        ({"model_override": "persona-model", "reasoning_effort": "low"}, ("persona-model", "low")),
        ({"model_override": None, "reasoning_effort": None}, ("project-model", "high")),
    ],
)
async def test_a_persona_session_prefers_its_identitys_model_and_effort(identity, expected):
    """Team's "Project default" means the persona falls back to the project's values."""
    from types import SimpleNamespace

    from omnigent.entities import Project
    from omnigent.server.project_main_session import project_session_payload

    project = Project(
        id="p1",
        name="Defaults",
        user_id="alice",
        created_at=0,
        config={"host_id": "__sandbox__", "model": "project-model", "effort": "high"},
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(host_registry=None)))
    payload = await project_session_payload(request, project, {"project_id": "p1", **identity})
    assert (payload["model_override"], payload["reasoning_effort"]) == expected
