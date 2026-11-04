"""Independent sessions an orchestrating session starts are filed in its project."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI

from omnigent.runner.tool_dispatch import execute_tool
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.managed_hosts import parse_sandbox_config
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store import CREATED_BY_SESSION_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from tests.server.integration.test_sessions_attribution import _bind_runner
from tests.server.integration.test_sessions_harness_override import _stub_late_runner_bind
from tests.server.integration.test_sessions_permissions import _create_session_as

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def orchestration_environment(monkeypatch):
    monkeypatch.delenv("OMNIGENT_FEATURES", raising=False)


@pytest.fixture
def auth_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """An auth-enabled app with projects and managed sandboxes."""
    from omnigent.server.auth import UnifiedAuthProvider

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        project_store=SqlAlchemyProjectStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
        host_store=HostStore(db_uri),
        sandbox_config=parse_sandbox_config(
            {
                "provider": "modal",
                "server_url": "https://managed-test.example.com",
                "modal": {"image": "docker.io/test/omnigent-host:latest"},
            }
        ),
    )


async def _start_worker(auth_client, auth_app, db_uri, monkeypatch, source) -> str:
    """Start a managed worker from ``source`` the way ``sys_session_create`` does."""
    from omnigent.server.routes.sessions import routes_core

    auth_client.headers.update(_bind_runner(db_uri, source["id"]))
    _stub_late_runner_bind(monkeypatch)
    launched = asyncio.Event()

    async def launch(**_kwargs):
        launched.set()

    monkeypatch.setattr(routes_core, "_run_managed_launch", launch)
    try:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps(
                    {"agent_id": source["agent_id"], "placement": "managed", "message": "Go"}
                ),
                server_client=auth_client,
                conversation_id=source["id"],
            )
        )
        assert "error" not in result, result
        await asyncio.wait_for(launched.wait(), timeout=2)
    finally:
        await auth_app.state.startup_inputs.shutdown()
    return result["conversation_id"]


@pytest.mark.parametrize("filed", [True, False])
async def test_worker_is_filed_in_the_project_of_the_session_that_started_it(
    auth_client, auth_app, db_uri, monkeypatch, filed
):
    auth_client.headers["X-Forwarded-Email"] = "alice"
    source = await _create_session_as(auth_client, "", "alice")
    project_id = None
    if filed:
        response = await auth_client.post("/v1/projects", json={"name": "Triage"})
        assert response.status_code == 200, response.text
        project_id = response.json()["id"]
        response = await auth_client.patch(
            f"/v1/sessions/{source['id']}", json={"project_id": project_id}
        )
        assert response.status_code == 200, response.text

    worker_id = await _start_worker(auth_client, auth_app, db_uri, monkeypatch, source)

    worker = SqlAlchemyConversationStore(db_uri).get_conversation(worker_id)
    assert worker.labels[CREATED_BY_SESSION_LABEL_KEY] == source["id"]
    assert worker.project_id == project_id


async def test_worker_is_unfiled_when_the_source_project_is_gone(
    auth_client, auth_app, db_uri, monkeypatch
):
    auth_client.headers["X-Forwarded-Email"] = "alice"
    source = await _create_session_as(auth_client, "", "alice")
    response = await auth_client.post("/v1/projects", json={"name": "Triage"})
    project_id = response.json()["id"]
    await auth_client.patch(f"/v1/sessions/{source['id']}", json={"project_id": project_id})
    # The source still points at a project the owner can no longer read.
    project_store = SqlAlchemyProjectStore(db_uri)
    monkeypatch.setattr(project_store.__class__, "get", lambda self, pid, *, user_id: None)

    worker_id = await _start_worker(auth_client, auth_app, db_uri, monkeypatch, source)

    assert SqlAlchemyConversationStore(db_uri).get_conversation(worker_id).project_id is None
