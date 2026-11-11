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
from omnigent.server.routes import sessions as sessions_module
from omnigent.server.routes._sessions import common as sessions_common
from omnigent.server.routes._sessions import orchestration
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
_NESTED_ONLY = pytest.mark.parametrize(
    "orchestration_environment", [True], ids=["nested"], indirect=True
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


@pytest.mark.parametrize("requested", ["other", "none", "source"])
async def test_a_project_named_in_the_request_wins_over_the_source_project(
    auth_client, auth_app, db_uri, monkeypatch, requested, orchestration_environment
):
    auth_client.headers["X-Forwarded-Email"] = "alice"
    source = await _create_session_as(auth_client, "", "alice")
    triage = await _project(auth_client, "Triage")
    await _file(auth_client, db_uri, source["id"], triage)
    other = await _project(auth_client, "Elsewhere")
    project_id = {"other": other, "none": None, "source": triage}[requested]

    worker_id = await _start_worker(
        auth_client, auth_app, db_uri, monkeypatch, source, body={"project_id": project_id}
    )

    worker = SqlAlchemyConversationStore(db_uri).get_conversation(worker_id)
    assert worker.project_id == project_id
    # Only the source's own project keeps the worker in the source's family.
    nested = orchestration_environment and requested == "source"
    assert worker.family_id == (source["id"] if nested else None)


@_NESTED_ONLY
async def test_a_client_sibling_keeps_its_source_project(auth_client, db_uri):
    auth_client.headers["X-Forwarded-Email"] = "alice"
    source = await _create_session_as(auth_client, "", "alice")
    await _file(auth_client, db_uri, source["id"], await _project(auth_client, "Triage"))
    other = await _project(auth_client, "Elsewhere")

    response = await auth_client.post(
        "/v1/sessions",
        json={
            "agent_id": source["agent_id"],
            "sibling_source_id": source["id"],
            "project_id": other,
        },
    )

    assert response.status_code == 400, response.text
    assert "stay in their source session's project" in response.json()["error"]["message"]
    assert SqlAlchemyConversationStore(db_uri).get_conversation(source["id"]).family_id is None


@_NESTED_ONLY
async def test_another_project_keeps_the_destination_policy(auth_client, auth_app, db_uri):
    from omnigent.server.sharing_settings import SiblingSessionDestinations

    auth_client.headers["X-Forwarded-Email"] = "alice"
    source = await _create_session_as(auth_client, "", "alice")
    other = await _project(auth_client, "Elsewhere")
    auth_app.state.sibling_session_destinations = lambda: SiblingSessionDestinations.CURRENT_HOST
    auth_client.headers.update(_bind_runner(db_uri, source["id"]))
    store = SqlAlchemyConversationStore(db_uri)
    before = {session.id for session in store.list_conversations().data}

    response = await auth_client.post(
        "/v1/sessions/orchestrated",
        headers={ORCHESTRATION_SOURCE_HEADER: source["id"]},
        json={"agent_id": source["agent_id"], "host_type": "managed", "project_id": other},
    )

    assert response.status_code == 403, response.text
    assert "limited to the current host" in response.json()["error"]["message"]
    assert {session.id for session in store.list_conversations().data} == before


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
    assert worker.family_id is None
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


@_NESTED_ONLY
async def test_the_tool_nests_a_worker_named_into_its_source_project(
    auth_client, auth_app, db_uri, monkeypatch
):
    auth_client.headers["X-Forwarded-Email"] = "alice"
    source = await _create_session_as(auth_client, "", "alice")
    triage = await _project(auth_client, "Triage")
    await _file(auth_client, db_uri, source["id"], triage)

    worker_id = await _start_worker(
        auth_client, auth_app, db_uri, monkeypatch, source, tool_args={"project_name": "Triage"}
    )

    worker = SqlAlchemyConversationStore(db_uri).get_conversation(worker_id)
    assert worker.project_id == triage
    assert worker.family_id == source["id"]


# The project tools do not depend on the sibling-sessions environment.
_ONE_ENVIRONMENT = _NESTED_ONLY


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
    refused_session = await _project_tool(
        auth_client,
        source,
        "sys_session_create",
        agent_id=source["agent_id"],
        placement="managed",
        project_name="Spawned",
    )
    assert "shared with edit access cannot" in refused_session["error"], refused_session
    projects = (await auth_client.get("/v1/projects")).json()["data"]
    assert not {"Shared", "Spawned"} & {project["name"] for project in projects}
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


@_ONE_ENVIRONMENT
async def test_organizing_tools_rename_pin_archive_and_manage_projects(
    auth_client, peers, db_uri, monkeypatch
):
    source, target, _ = peers
    store = SqlAlchemyConversationStore(db_uri)

    async def tool(tool_name: str, /, **arguments) -> dict:
        result = await _project_tool(auth_client, source, tool_name, **arguments)
        assert "error" not in result, result
        return result

    await tool(
        "sys_session_update", session_id=target["id"], title="Quarterly report", pinned=True
    )
    assert store.get_conversation(target["id"]).title == "Quarterly report"
    pinned = await tool("sys_project_list", pinned=True)
    assert [(row["session_id"], row.get("pinned")) for row in pinned["sessions"]] == [
        (target["id"], True)
    ]
    found = await tool("sys_project_list", search="quarterly")
    assert [row["session_id"] for row in found["sessions"]] == [target["id"]]

    await tool("sys_session_update", session_id=target["id"], project="Drafts")
    renamed = await tool("sys_project_update", project="Drafts", name="Reports", icon="📊")
    assert renamed == {
        "project": "Reports",
        "status": "updated",
        "previous_name": "Drafts",
        "icon": "📊",
    }
    (project,) = (await auth_client.get("/v1/projects")).json()["data"]
    assert project["name"] == "Reports" and project["config"] == {"icon": "📊"}

    # The calling session is mid-turn; archiving another session must not touch it.
    # (Listing reconciles live status against the stubbed runner, so set it here.)
    store.set_session_live_status(source["id"], "running")
    target_child = store.create_conversation(
        agent_id=source["agent_id"], parent_conversation_id=target["id"]
    )
    source_child = store.create_conversation(
        agent_id=source["agent_id"], parent_conversation_id=source["id"]
    )
    running = (source["id"], source_child.id, target["id"], target_child.id)
    stopped: list[str] = []

    async def record_stop(session_id: str, _runner_router) -> bool:
        stopped.append(session_id)
        return True

    monkeypatch.setattr(sessions_module, "_ARCHIVE_STOP_UNDO_GRACE_S", 0.0)
    monkeypatch.setattr(orchestration, "_stop_session_via_runner", record_stop)
    for session_id in running:
        monkeypatch.setitem(sessions_common._session_status_cache, session_id, "running")
    archived = await tool("sys_session_update", session_id=target["id"], archived=True)
    assert archived["status"] == "updated"
    await asyncio.gather(*list(orchestration._detached_stop_tasks))
    # Teardown reaches the archived session and its sub-agent, never the caller's tree.
    assert sorted(stopped) == sorted([target["id"], target_child.id])
    saved = store.get_conversation(target["id"])
    assert saved.archived is True and saved.project_id == project["id"]
    assert store.get_conversation(source["id"]).live_status == "running"
    listed = await tool("sys_project_list", include_archived=True)
    rows = {row["session_id"]: row for row in listed["sessions"]}
    assert rows[target["id"]].get("archived") is True and "pinned" not in rows[target["id"]]


@_ONE_ENVIRONMENT
async def test_organizing_tools_refuse_a_source_shared_with_edit_access(
    auth_client, peers, db_uri
):
    source, target, _ = peers
    project_id = await _project(auth_client, "Shared folder")
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")
    permissions.grant("bob", source["id"], LEVEL_EDIT)

    for name, arguments in (
        ("sys_session_update", {"session_id": target["id"], "title": "Hijacked"}),
        ("sys_session_update", {"session_id": target["id"], "pinned": True}),
        ("sys_session_update", {"session_id": target["id"], "archived": True}),
        ("sys_project_update", {"project": "Shared folder", "name": "Hijacked"}),
    ):
        refused = await _project_tool(auth_client, source, name, **arguments)
        assert "shared with edit access cannot" in refused["error"], (name, refused)
    # The routes themselves refuse the shared source, not only the tools.
    source_headers = {ORCHESTRATION_SOURCE_HEADER: source["id"]}
    direct = [
        await auth_client.patch(
            f"/v1/projects/{project_id}",
            headers=source_headers,
            json={"config": {"icon": "🔥"}},
        ),
        await auth_client.delete(f"/v1/projects/{project_id}", headers=source_headers),
    ]
    assert [response.status_code for response in direct] == [403, 403], [r.text for r in direct]
    saved = SqlAlchemyConversationStore(db_uri).get_conversation(target["id"])
    assert saved.title != "Hijacked" and saved.archived is not True
    assert not any(key.startswith("omnigent.pinned") for key in saved.labels)
    (project,) = (await auth_client.get("/v1/projects")).json()["data"]
    assert project == {**project, "id": project_id, "name": "Shared folder", "config": {}}


@_ONE_ENVIRONMENT
async def test_a_child_cannot_archive_its_rebound_parent(auth_client, peers, db_uri):
    source, _, _ = peers
    store = SqlAlchemyConversationStore(db_uri)
    child = store.create_conversation(
        agent_id=source["agent_id"], parent_conversation_id=source["id"]
    )
    # The parent moved to another runner while the child stayed on the original.
    auth_client.headers.update(_bind_runner(db_uri, child.id))
    store.clear_runner_id(source["id"])
    assert store.set_runner_id(source["id"], "rebound-runner")

    existing_id = await _project(auth_client, "Existing")

    async def archive_parent(**fields) -> dict:
        return json.loads(
            await execute_tool(
                tool_name="sys_session_update",
                arguments=json.dumps({"session_id": source["id"], "archived": True, **fields}),
                server_client=auth_client,
                conversation_id=child.id,
            )
        )

    result = await archive_parent()
    assert "cannot archive its own session or a session it runs under" in result["error"], result
    into_existing = await archive_parent(project="Existing")
    assert "a session it runs under" in into_existing["error"], into_existing
    into_new = await archive_parent(project="Brand new")
    assert "in separate calls" in into_new["error"], into_new
    parent = store.get_conversation(source["id"])
    assert parent.archived is not True and parent.project_id is None
    projects = (await auth_client.get("/v1/projects")).json()["data"]
    assert [project["id"] for project in projects] == [existing_id]


@_ONE_ENVIRONMENT
async def test_project_update_promotes_a_label_only_folder(auth_client, peers, db_uri):
    source, target, _ = peers
    store = SqlAlchemyConversationStore(db_uri)
    legacy = {"omni_project": "Legacy folder"}
    archived_member = await _create_session_as(auth_client, "", "alice")
    family_member = await _create_session_as(auth_client, "", "alice")
    store.add_new_sibling(target["id"], family_member["id"], user_id="alice")
    filed_elsewhere = await _create_session_as(auth_client, "", "alice")
    others = await _create_session_as(auth_client, "", "bob")
    other_project_id = await _project(auth_client, "Elsewhere")
    await _file(auth_client, db_uri, filed_elsewhere["id"], other_project_id)
    for session_id in (target["id"], archived_member["id"], filed_elsewhere["id"], others["id"]):
        store.set_labels(session_id, legacy)
    store.update_conversation(archived_member["id"], archived=True)

    promoted = await _project_tool(
        auth_client,
        source,
        "sys_project_update",
        project="Legacy folder",
        name="Field notes",
        icon="📁",
    )
    assert promoted == {
        "project": "Field notes",
        "status": "updated",
        "previous_name": "Legacy folder",
        "icon": "📁",
    }
    projects = {p["name"]: p for p in (await auth_client.get("/v1/projects")).json()["data"]}
    assert set(projects) == {"Elsewhere", "Field notes"}
    new_id = projects["Field notes"]["id"]
    assert projects["Field notes"]["config"] == {"icon": "📁"}
    # Archived label members move too, and the family follows its representative.
    for session_id in (target["id"], archived_member["id"], family_member["id"]):
        saved = store.get_conversation(session_id)
        assert saved.project_id == new_id, session_id
        assert not saved.labels.get("omni_project"), session_id
    # A session already filed in another project keeps it, and so does another user's.
    elsewhere = store.get_conversation(filed_elsewhere["id"])
    assert elsewhere.project_id == other_project_id
    assert elsewhere.labels.get("omni_project") == "Legacy folder"
    foreign = store.get_conversation(others["id"])
    assert foreign.project_id is None and foreign.labels.get("omni_project") == "Legacy folder"

    # Without a rename the folder keeps its name; only the icon is set.
    inbox_member = await _create_session_as(auth_client, "", "alice")
    store.set_labels(inbox_member["id"], {"omni_project": "Inbox"})
    iconed = await _project_tool(
        auth_client, source, "sys_project_update", project="Inbox", icon="📥"
    )
    assert iconed == {"project": "Inbox", "status": "updated", "icon": "📥"}
    projects = {p["name"]: p for p in (await auth_client.get("/v1/projects")).json()["data"]}
    assert projects["Inbox"]["config"] == {"icon": "📥"}
    inbox = store.get_conversation(inbox_member["id"])
    assert inbox.project_id == projects["Inbox"]["id"] and not inbox.labels.get("omni_project")

    missing = await _project_tool(
        auth_client, source, "sys_project_update", project="Nowhere", name="Somewhere"
    )
    assert missing["error"].startswith("no project named 'Nowhere'"), missing
    assert "Somewhere" not in {
        p["name"] for p in (await auth_client.get("/v1/projects")).json()["data"]
    }
