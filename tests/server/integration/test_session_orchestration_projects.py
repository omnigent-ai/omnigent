"""Independent sessions an orchestrating session starts are filed in its project or a named one."""

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


async def _start_worker(
    auth_client,
    auth_app,
    db_uri,
    monkeypatch,
    source,
    *,
    body: dict | None = None,
    tool_args: dict | None = None,
) -> str:
    """Start a managed worker from ``source``.

    With no ``body`` this runs the real ``sys_session_create`` tool, adding
    ``tool_args`` to its arguments; with one it posts that body to the
    orchestrated create route the tool uses, e.g. to send an explicit null.
    """
    from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
    from omnigent.server.routes.sessions import routes_core

    auth_client.headers.update(_bind_runner(db_uri, source["id"]))
    _stub_late_runner_bind(monkeypatch)
    launched = asyncio.Event()

    async def launch(**_kwargs):
        launched.set()

    monkeypatch.setattr(routes_core, "_run_managed_launch", launch)
    try:
        if body is None:
            result = json.loads(
                await execute_tool(
                    tool_name="sys_session_create",
                    arguments=json.dumps(
                        {
                            "agent_id": source["agent_id"],
                            "placement": "managed",
                            "message": "Go",
                            **(tool_args or {}),
                        }
                    ),
                    server_client=auth_client,
                    conversation_id=source["id"],
                )
            )
            assert "error" not in result, result
            worker_id = result["conversation_id"]
        else:
            response = await auth_client.post(
                "/v1/sessions/orchestrated",
                headers={ORCHESTRATION_SOURCE_HEADER: source["id"]},
                json={"agent_id": source["agent_id"], "host_type": "managed", **body},
            )
            assert response.status_code == 201, response.text
            worker_id = response.json()["id"]
        await asyncio.wait_for(launched.wait(), timeout=2)
    finally:
        await auth_app.state.startup_inputs.shutdown()
    return worker_id


async def _project(auth_client, name: str, config: dict | None = None) -> str:
    response = await auth_client.post("/v1/projects", json={"name": name, "config": config or {}})
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def _file(auth_client, db_uri, session_id: str, project_id: str) -> None:
    response = await auth_client.patch(
        f"/v1/sessions/{session_id}", json={"project_id": project_id}
    )
    assert response.status_code == 200, response.text
    assert SqlAlchemyConversationStore(db_uri).get_conversation(session_id).project_id == (
        project_id
    )


_DEFAULTS_THAT_WOULD_CHANGE_THE_WORKER = {"workspace": "/project-default", "git": {"base": "main"}}


@pytest.mark.parametrize("filed", [True, False])
async def test_worker_is_filed_in_the_project_of_the_session_that_started_it(
    auth_client, auth_app, db_uri, monkeypatch, filed
):
    auth_client.headers["X-Forwarded-Email"] = "alice"
    source = await _create_session_as(auth_client, "", "alice")
    project_id = None
    if filed:
        # Defaults that would change the worker if they were applied to it.
        other_agent = (await _create_session_as(auth_client, "", "alice"))["agent_id"]
        project_id = await _project(
            auth_client,
            "Triage",
            {"agent_id": other_agent, **_DEFAULTS_THAT_WOULD_CHANGE_THE_WORKER},
        )
        await _file(auth_client, db_uri, source["id"], project_id)

    worker_id = await _start_worker(auth_client, auth_app, db_uri, monkeypatch, source)

    worker = SqlAlchemyConversationStore(db_uri).get_conversation(worker_id)
    assert worker.labels[CREATED_BY_SESSION_LABEL_KEY] == source["id"]
    assert worker.project_id == project_id
    # Only the filing carries over: the worker keeps its own agent and placement.
    assert worker.agent_id == source["agent_id"]
    assert worker.workspace != "/project-default"
    assert worker.git_branch is None


async def test_worker_is_unfiled_when_the_source_project_is_gone(
    auth_client, auth_app, db_uri, monkeypatch
):
    auth_client.headers["X-Forwarded-Email"] = "alice"
    source = await _create_session_as(auth_client, "", "alice")
    await _file(auth_client, db_uri, source["id"], await _project(auth_client, "Triage"))
    # The source still points at a project the owner can no longer read.
    monkeypatch.setattr(SqlAlchemyProjectStore, "get", lambda self, pid, *, user_id: None)

    worker_id = await _start_worker(auth_client, auth_app, db_uri, monkeypatch, source)

    assert SqlAlchemyConversationStore(db_uri).get_conversation(worker_id).project_id is None


@pytest.mark.parametrize("requested", ["other", "none"])
async def test_a_project_named_in_the_request_wins_over_the_source_project(
    auth_client, auth_app, db_uri, monkeypatch, requested
):
    auth_client.headers["X-Forwarded-Email"] = "alice"
    source = await _create_session_as(auth_client, "", "alice")
    await _file(auth_client, db_uri, source["id"], await _project(auth_client, "Triage"))
    other = await _project(auth_client, "Elsewhere")
    project_id = other if requested == "other" else None

    worker_id = await _start_worker(
        auth_client, auth_app, db_uri, monkeypatch, source, body={"project_id": project_id}
    )

    assert SqlAlchemyConversationStore(db_uri).get_conversation(worker_id).project_id == (
        project_id
    )


async def test_the_tool_files_a_worker_in_a_named_project_without_its_defaults(
    auth_client, auth_app, db_uri, monkeypatch
):
    auth_client.headers["X-Forwarded-Email"] = "alice"
    source = await _create_session_as(auth_client, "", "alice")
    await _file(auth_client, db_uri, source["id"], await _project(auth_client, "Triage"))
    other_agent = (await _create_session_as(auth_client, "", "alice"))["agent_id"]
    named = await _project(
        auth_client,
        "Elsewhere",
        {"agent_id": other_agent, **_DEFAULTS_THAT_WOULD_CHANGE_THE_WORKER},
    )

    worker_id = await _start_worker(
        auth_client, auth_app, db_uri, monkeypatch, source, tool_args={"project_name": "Elsewhere"}
    )

    worker = SqlAlchemyConversationStore(db_uri).get_conversation(worker_id)
    assert worker.project_id == named
    assert worker.agent_id == source["agent_id"]
    assert worker.workspace != "/project-default"
    assert worker.git_branch is None
    info = json.loads(
        await execute_tool(
            tool_name="sys_session_get_info",
            arguments=json.dumps({"session_id": worker_id}),
            server_client=auth_client,
            conversation_id=source["id"],
        )
    )
    assert info["project_id"] == named
    assert info["project_name"] == "Elsewhere"


async def test_a_name_only_another_user_has_gets_a_new_project(
    auth_client, auth_app, db_uri, monkeypatch
):
    auth_client.headers["X-Forwarded-Email"] = "bob"
    await _create_session_as(auth_client, "", "bob")
    bobs_project = await _project(auth_client, "Bob's work")
    auth_client.headers["X-Forwarded-Email"] = "alice"
    source = await _create_session_as(auth_client, "", "alice")

    worker_id = await _start_worker(
        auth_client,
        auth_app,
        db_uri,
        monkeypatch,
        source,
        tool_args={"project_name": "Bob's work"},
    )

    worker = SqlAlchemyConversationStore(db_uri).get_conversation(worker_id)
    assert worker.project_id not in (None, bobs_project)
    alices = SqlAlchemyProjectStore(db_uri).get(worker.project_id, user_id="alice")
    assert alices is not None and alices.name == "Bob's work"


async def test_same_host_worker_in_a_named_project_starts_no_worktree(
    auth_client, auth_app, db_uri, monkeypatch
):
    from omnigent.host.frames import HostLaunchRunnerFrame, HostStatFrame, decode_host_frame
    from tests.server.integration.test_sessions_permissions import _register_online_host

    auth_client.headers["X-Forwarded-Email"] = "alice"
    source = await _create_session_as(auth_client, "", "alice")
    host_id = "a" * 32
    _register_online_host(auth_app, host_id, "alice")
    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(source["id"], host_id, workspace="/original")
    named = await _project(auth_client, "Elsewhere", _DEFAULTS_THAT_WOULD_CHANGE_THE_WORKER)

    def answer_host(connection, payload):
        frame = decode_host_frame(payload)
        if isinstance(frame, HostStatFrame):
            connection.pending_stats[frame.request_id].set_result(
                {"status": "ok", "exists": True, "type": "directory", "canonical_path": frame.path}
            )
        elif isinstance(frame, HostLaunchRunnerFrame):
            connection.pending_launches[frame.request_id].set_result({"status": "launched"})
        else:
            pytest.fail(f"Unexpected host frame: {frame}")

    monkeypatch.setattr(auth_app.state.host_registry, "send_text", answer_host)
    auth_client.headers.update(_bind_runner(db_uri, source["id"]))
    _stub_late_runner_bind(monkeypatch)
    result = json.loads(
        await execute_tool(
            tool_name="sys_session_create",
            arguments=json.dumps(
                {
                    "agent_id": source["agent_id"],
                    "placement": "same_host",
                    "project_name": "Elsewhere",
                }
            ),
            server_client=auth_client,
            conversation_id=source["id"],
        )
    )

    assert "error" not in result, result
    worker = store.get_conversation(result["conversation_id"])
    assert worker.project_id == named
    assert worker.workspace == "/original"
    assert worker.git_branch is None
