"""Independent sessions an orchestrating session starts are filed in its project or a named one."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER, RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.runner.tool_dispatch import execute_tool
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ
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


@pytest.fixture(autouse=True, params=[False, True], ids=["standalone", "nested"])
def orchestration_environment(monkeypatch, request):
    monkeypatch.setenv(
        "OMNIGENT_FEATURES", "sibling_sessions" if request.param else "-sibling_sessions"
    )
    return request.param


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
# Nested agents stay in their source session's project, so project_name is for standalone mode.
_STANDALONE_ONLY = pytest.mark.parametrize(
    "orchestration_environment", [False], ids=["standalone"], indirect=True
)


@pytest.mark.parametrize("filed", [True, False])
@pytest.mark.parametrize("child_sender", [True, False])
async def test_worker_is_filed_in_the_project_of_the_session_that_started_it(
    auth_client, auth_app, db_uri, monkeypatch, filed, child_sender, orchestration_environment
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

    sender = source
    if child_sender:
        response = await auth_client.post(
            "/v1/sessions",
            json={"agent_id": source["agent_id"], "parent_session_id": source["id"]},
        )
        assert response.status_code == 201, response.text
        sender = response.json()
    worker_id = await _start_worker(auth_client, auth_app, db_uri, monkeypatch, sender)

    worker = SqlAlchemyConversationStore(db_uri).get_conversation(worker_id)
    assert worker.labels[CREATED_BY_SESSION_LABEL_KEY] == sender["id"]
    assert worker.family_id == (source["id"] if orchestration_environment else None)
    assert worker.parent_conversation_id is None
    assert worker.project_id == (
        project_id if orchestration_environment or not child_sender else None
    )
    # Only the filing carries over: the worker keeps its own agent and placement.
    assert worker.agent_id == source["agent_id"]
    assert worker.workspace != "/project-default"
    assert worker.git_branch is None


async def test_worker_handles_unavailable_source_project(
    auth_client, auth_app, db_uri, monkeypatch, orchestration_environment
):
    auth_client.headers["X-Forwarded-Email"] = "alice"
    source = await _create_session_as(auth_client, "", "alice")
    await _file(auth_client, db_uri, source["id"], await _project(auth_client, "Triage"))
    # The source still points at a project the owner can no longer read.
    monkeypatch.setattr(SqlAlchemyProjectStore, "get", lambda self, pid, *, user_id: None)

    if not orchestration_environment:
        worker_id = await _start_worker(auth_client, auth_app, db_uri, monkeypatch, source)
        worker = SqlAlchemyConversationStore(db_uri).get_conversation(worker_id)
        assert worker.project_id is None
        assert worker.family_id is None
        return

    auth_client.headers.update(_bind_runner(db_uri, source["id"]))
    response = await auth_client.post(
        "/v1/sessions/orchestrated",
        headers={ORCHESTRATION_SOURCE_HEADER: source["id"]},
        json={"agent_id": source["agent_id"], "host_type": "managed"},
    )
    assert response.status_code == 404
    assert SqlAlchemyConversationStore(db_uri).get_conversation(source["id"]).family_id is None


@pytest.mark.parametrize("requested", ["other", "none"])
async def test_worker_project_override_requires_standalone_mode(
    auth_client, auth_app, db_uri, monkeypatch, requested, orchestration_environment
):
    auth_client.headers["X-Forwarded-Email"] = "alice"
    source = await _create_session_as(auth_client, "", "alice")
    await _file(auth_client, db_uri, source["id"], await _project(auth_client, "Triage"))
    other = await _project(auth_client, "Elsewhere")
    project_id = other if requested == "other" else None

    if not orchestration_environment:
        worker_id = await _start_worker(
            auth_client, auth_app, db_uri, monkeypatch, source, body={"project_id": project_id}
        )
        worker = SqlAlchemyConversationStore(db_uri).get_conversation(worker_id)
        assert worker.project_id == project_id
        assert worker.family_id is None
        return

    auth_client.headers.update(_bind_runner(db_uri, source["id"]))
    response = await auth_client.post(
        "/v1/sessions/orchestrated",
        headers={ORCHESTRATION_SOURCE_HEADER: source["id"]},
        json={"agent_id": source["agent_id"], "host_type": "managed", "project_id": project_id},
    )
    assert response.status_code == 400
    assert "stay in their source session's project" in response.json()["error"]["message"]
    assert SqlAlchemyConversationStore(db_uri).get_conversation(source["id"]).family_id is None


@_STANDALONE_ONLY
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


@_STANDALONE_ONLY
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


@_STANDALONE_ONLY
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


@pytest.mark.parametrize("orchestration_environment", [True], ids=["nested"], indirect=True)
async def test_the_tool_refuses_project_name_for_nested_agents(auth_client, db_uri):
    auth_client.headers["X-Forwarded-Email"] = "alice"
    source = await _create_session_as(auth_client, "", "alice")
    auth_client.headers.update(_bind_runner(db_uri, source["id"]))

    result = json.loads(
        await execute_tool(
            tool_name="sys_session_create",
            arguments=json.dumps(
                {
                    "agent_id": source["agent_id"],
                    "placement": "managed",
                    "message": "Go",
                    "project_name": "New work",
                }
            ),
            server_client=auth_client,
            conversation_id=source["id"],
        )
    )

    assert "does not accept project_name" in result.get("error", ""), result
    # The refusal comes before the lookup, so no empty project is left behind.
    projects = SqlAlchemyProjectStore(db_uri).list(user_id="alice")
    assert "New work" not in [project.name for project in projects]


# The project tools do not depend on the sibling-sessions environment.
_ONE_ENVIRONMENT = pytest.mark.parametrize(
    "orchestration_environment", [True], indirect=True, ids=["nested"]
)


@pytest.fixture
async def peers(auth_client, db_uri, monkeypatch):
    source = await _create_session_as(auth_client, "", "alice")
    target = await _create_session_as(auth_client, "", "alice")
    auth_client.headers.update(
        {"X-Forwarded-Email": "alice", **_bind_runner(db_uri, source["id"])}
    )
    return source, target, _stub_late_runner_bind(monkeypatch)


async def _project_tool(client, source, tool_name, **arguments):
    return json.loads(
        await execute_tool(
            tool_name=tool_name,
            arguments=json.dumps(arguments),
            server_client=client,
            conversation_id=source["id"],
        )
    )


@_ONE_ENVIRONMENT
async def test_project_tools_list_and_file_running_sessions(auth_client, peers, db_uri):
    source, target, captured = peers
    store = SqlAlchemyConversationStore(db_uri)
    existing_id = await _project(auth_client, "Release prep")
    store.set_labels(target["id"], {"omni_project": "Legacy folder"})
    captured.clear()

    listed = await _project_tool(auth_client, source, "sys_project_list")
    assert {"name": "Release prep", "id": existing_id} in listed["projects"]
    rows = {row["session_id"]: row for row in listed["sessions"]}
    assert rows[source["id"]]["project"] is None and rows[source["id"]]["this_session"]
    assert rows[target["id"]]["project"] == "Legacy folder"

    # The calling session is mid-turn; filing it must not touch the turn.
    store.set_session_live_status(source["id"], "running")
    for session_id in (source["id"], target["id"]):
        updated = await _project_tool(
            auth_client,
            source,
            "sys_session_update",
            session_id=session_id,
            project="Release prep",
        )
        assert updated == {
            "session_id": session_id,
            "status": "updated",
            "project": "Release prep",
            "project_created": False,
        }
        saved = store.get_conversation(session_id)
        assert saved.project_id == existing_id
        assert not saved.labels.get("omni_project")
    assert store.get_conversation(source["id"]).live_status == "running"
    assert not captured.get("posts")

    created = await _project_tool(
        auth_client, source, "sys_session_update", session_id=target["id"], project="Follow-ups"
    )
    assert created["project_created"] is True and created["status"] == "updated"
    filtered = await _project_tool(auth_client, source, "sys_project_list", project="Follow-ups")
    assert [row["session_id"] for row in filtered["sessions"]] == [target["id"]]
    again = await _project_tool(
        auth_client, source, "sys_session_update", session_id=target["id"], project="Follow-ups"
    )
    assert again["status"] == "unchanged"
    unfiled = await _project_tool(
        auth_client, source, "sys_session_update", session_id=target["id"], project=""
    )
    assert unfiled["status"] == "updated" and unfiled["project"] is None
    assert store.get_conversation(target["id"]).project_id is None


@_ONE_ENVIRONMENT
async def test_project_update_refuses_sessions_it_cannot_refile(auth_client, peers, db_uri):
    source, target, _ = peers
    store = SqlAlchemyConversationStore(db_uri)
    others = await _create_session_as(auth_client, "", "bob")
    child = store.create_conversation(
        agent_id=source["agent_id"], parent_conversation_id=source["id"]
    )
    family_member = await _create_session_as(auth_client, "", "alice")
    store.add_new_sibling(target["id"], family_member["id"], user_id="alice")

    async def update(session_id: str, project: str) -> dict:
        return await _project_tool(
            auth_client, source, "sys_session_update", session_id=session_id, project=project
        )

    assert "not found" in (await update(others["id"], "Triage"))["error"]
    refused_child = await update(child.id, "Triage")
    assert (
        f"follow their parent's project; update {source['id']} instead" in refused_child["error"]
    )
    refused_member = await update(family_member["id"], "Triage")
    assert f"update {target['id']} instead" in refused_member["error"]
    assert (await update("not-a-session", "Triage"))["error"] == "invalid session ID"
    assert store.get_conversation(others["id"]).project_id is None
    assert store.get_conversation(family_member["id"]).project_id is None
    projects = (await auth_client.get("/v1/projects")).json()["data"]
    assert "Triage" not in {project["name"] for project in projects}

    read_shared = await _create_session_as(auth_client, "", "bob")
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("alice")
    permissions.grant("alice", read_shared["id"], LEVEL_READ)
    unfile = await update(read_shared["id"], "")
    assert "only the session's owner" in unfile["error"], unfile

    family = await update(target["id"], "Triage")
    assert family["status"] == "updated"
    assert store.get_conversation(family_member["id"]).project_id is not None


@_ONE_ENVIRONMENT
async def test_project_update_refuses_a_source_shared_with_edit_access(auth_client, peers, db_uri):
    source, target, _ = peers
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")
    permissions.grant("bob", source["id"], LEVEL_EDIT)

    listed = await _project_tool(auth_client, source, "sys_project_list")
    assert target["id"] in {row["session_id"] for row in listed["sessions"]}
    refused_create = await _project_tool(
        auth_client, source, "sys_session_update", session_id=target["id"], project="Shared"
    )
    assert "shared with edit access cannot" in refused_create["error"], refused_create
    projects = (await auth_client.get("/v1/projects")).json()["data"]
    assert "Shared" not in {project["name"] for project in projects}
    existing = await auth_client.post("/v1/projects", json={"name": "Existing"})
    assert existing.status_code == 200, existing.text
    refused = await _project_tool(
        auth_client, source, "sys_session_update", session_id=target["id"], project="Existing"
    )
    assert "shared with edit access cannot" in refused["error"], refused
    assert SqlAlchemyConversationStore(db_uri).get_conversation(target["id"]).project_id is None
    direct = await auth_client.patch(
        f"/v1/sessions/{target['id']}",
        headers={ORCHESTRATION_SOURCE_HEADER: target["id"], RUNNER_TUNNEL_TOKEN_HEADER: "other"},
        json={"project_id": ""},
    )
    assert direct.status_code == 403 and "bound runner" in direct.text
