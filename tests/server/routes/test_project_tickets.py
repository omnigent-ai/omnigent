import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from omnigent.entities import MessageData
from omnigent.entities.project_ticket import CreateTicket, TicketActor
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import (
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostStatFrame,
    decode_host_frame,
)
from omnigent.native.native_coding_agents import NATIVE_CODING_AGENTS
from omnigent.native.permission_modes import PERMISSION_LAUNCH_ARGS
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runtime.project_ticket_instructions import PROJECT_TICKET_WORK_INSTRUCTION
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, RESERVED_USER_PUBLIC
from omnigent.server.managed_hosts import parse_sandbox_config
from omnigent.server.project_ticket_notifications import ProjectTicketNotifications
from omnigent.server.routes import project_tickets as ticket_routes
from omnigent.server.routes.sessions import routes_core
from omnigent.server.sharing_settings import DefaultPublicSessions
from omnigent.stores.conversation_store import (
    CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY,
    SessionActivity,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.stores.project_ticket_store import ProjectTicketStore
from tests._helpers.polling import poll_until_async
from tests._helpers.session import post_session_bundle
from tests.server.helpers import build_agent_bundle, register_test_runner
from tests.server.integration.test_sessions_attribution import _bind_runner
from tests.server.integration.test_sessions_permissions import _register_online_host
from tests.server.integration.test_sessions_web_send_stable_id import _stub_runner
from tests.server.routes.test_projects_crud import ALICE, BOB, PROJECT_AGENT_ID, _as_user
from tests.server.routes.test_projects_crud import multi_user_app as multi_user_app
from tests.server.routes.test_projects_crud import multi_user_client as multi_user_client
from tests.server.routes.test_projects_crud import project_agents as project_agents
from tests.server.routes.test_projects_crud import project_app as project_app
from tests.server.routes.test_projects_crud import project_client as project_client


async def test_project_creation_has_one_normal_main_session(project_client, db_uri):
    headers = {"Idempotency-Key": str(uuid4())}
    project = (
        await project_client.post("/v1/projects", json={"name": "Shipping"}, headers=headers)
    ).json()
    session_id = project["main_session_id"]
    session = (await project_client.get(f"/v1/sessions/{session_id}")).json()
    assert session["title"] == "Orchestrator"
    assert session["project_id"] == project["id"]
    assert session["project_main_session"] is True
    assert session["agent_name"] == "claude-native-ui"
    assert session["harness"] == "claude-native"
    assert session["project_orchestrator"] is True  # Compatibility alias through 0.25.0.
    assert project["main_session_state"] == "active"
    assert session["status"] == "idle"
    assert session.get("active_response_id") is None
    assert len(session["items"]) == 1
    greeting = session["items"][0]
    assert greeting["type"] == "message"
    assert greeting["status"] == "completed"
    assert greeting["data"]["role"] == "assistant"
    assert greeting["data"]["content"] == [
        {"type": "output_text", "text": "Hello, what would you like to work on?"}
    ]
    stored = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert stored.kind == "default"
    assert stored.terminal_launch_args == ["--permission-mode", "bypassPermissions"]
    again = await project_client.post("/v1/projects", json={"name": "Shipping"}, headers=headers)
    assert again.json()["main_session_id"] == session_id
    assert (
        await project_client.post(f"/v1/projects/{project['id']}/orchestrator")
    ).status_code == 404
    assert (await project_client.get(f"/v1/sessions/{session_id}")).json()["items"] == [greeting]
    info = (await project_client.get("/v1/info")).json()
    assert info["project_tickets"] is True
    assert info["project_ticket_lifecycle"] is True
    assert info["project_ticket_agent_involvement"] is True
    assert info["project_ticket_stages"] is True
    assert info["project_ticket_summary"] is True
    assert info["durable_initial_input"] is False

    worker = (
        await project_client.post(
            "/v1/sessions",
            json={
                "agent_id": session["agent_id"],
                "project_id": project["id"],
                "title": "Orchestrator",
            },
        )
    ).json()
    worker = (await project_client.get(f"/v1/sessions/{worker['id']}")).json()
    assert worker["project_id"] == project["id"]
    assert worker["project_main_session"] is False
    assert worker["items"] == []
    assert (
        SqlAlchemyConversationStore(db_uri).get_conversation(worker["id"]).terminal_launch_args
        is None
    )
    assert CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY not in worker["labels"]


async def test_ticket_response_summary_is_owner_private(multi_user_client, db_uri):
    project = SqlAlchemyProjectStore(db_uri).create(uuid4().hex, "Summary", ALICE)
    ProjectTicketStore(db_uri).create(
        project.id,
        CreateTicket(title="Needs a decision", labels=[{"key": "needs-human"}]),
        TicketActor(ALICE),
    )
    path = f"/v1/projects/{project.id}/tickets/summary"
    response = await multi_user_client.get(path, headers=_as_user(ALICE))
    assert response.status_code == 200
    assert response.json() == {"needs_response_count": 1, "is_complete": True}
    assert (await multi_user_client.get(path, headers=_as_user(BOB))).status_code == 404
    info = (await multi_user_client.get("/v1/info", headers=_as_user(ALICE))).json()
    assert info["project_ticket_summary"] is True


async def test_orchestrator_inherits_defaults_but_not_public_sharing(
    multi_user_client, multi_user_app, db_uri
):
    multi_user_app.state.default_public_sessions = lambda: DefaultPublicSessions.ALL
    response = await multi_user_client.post(
        "/v1/projects",
        json={
            "name": "Private board",
            "config": {"agent_id": PROJECT_AGENT_ID, "workspace": "/work/board", "effort": "high"},
        },
        headers=_as_user(ALICE),
    )
    assert response.status_code == 200, response.text
    project = response.json()
    session_id = project["main_session_id"]
    conversation = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert conversation.agent_id == PROJECT_AGENT_ID
    assert conversation.workspace == "/work/board"
    assert conversation.reasoning_effort == "high"
    permissions = SqlAlchemyPermissionStore(db_uri)
    assert permissions.get(ALICE, session_id).level == LEVEL_OWNER
    assert permissions.get(RESERVED_USER_PUBLIC, session_id) is None
    assert (
        await multi_user_client.get(f"/v1/sessions/{session_id}", headers=_as_user(BOB))
    ).status_code == 404


@pytest.mark.parametrize("unavailable", ["missing", "suppressed"])
async def test_unavailable_native_agents_requires_explicit_agent_and_is_retryable(
    project_client, project_app, project_agents, db_uri, unavailable
):
    if unavailable == "suppressed":
        project_app.state.suppressed_agent_names = {
            native.agent_name for native in NATIVE_CODING_AGENTS
        }
    else:
        for native in NATIVE_CODING_AGENTS:
            project_agents.delete(project_agents.get_by_name(native.agent_name).id)
    headers = {"Idempotency-Key": str(uuid4())}
    response = await project_client.post(
        "/v1/projects", json={"name": "Unconfigured"}, headers=headers
    )
    assert response.status_code == 400
    assert "available project agent" in response.text
    assert SqlAlchemyProjectStore(db_uri).list(user_id=None) == []
    assert SqlAlchemyConversationStore(db_uri).list_conversations().data == []
    retry = await project_client.post(
        "/v1/projects",
        json={
            "name": "Unconfigured",
            "config": {"agent_id": PROJECT_AGENT_ID},
        },
        headers=headers,
    )
    assert retry.status_code == 200, retry.text
    session = (await project_client.get(f"/v1/sessions/{retry.json()['main_session_id']}")).json()
    assert session["agent_name"] == "project-agent"


@pytest.mark.parametrize("bundled", [False, True])
@pytest.mark.parametrize(
    ("harness", "access", "expected_args", "bypass"),
    [
        ("claude-native", {}, ["--permission-mode", "bypassPermissions"], False),
        ("codex-native", {}, [], True),
        ("claude-native", {"terminal_launch_args": []}, [], False),
        ("codex-native", {"terminal_launch_args": []}, [], False),
        ("claude-native", {"terminal_launch_args": None}, None, False),
        ("codex-native", {"labels": {CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY: "0"}}, None, False),
        (
            "claude-native",
            {"terminal_launch_args": ["--permission-mode", "plan"]},
            ["--permission-mode", "plan"],
            False,
        ),
        (
            "codex-native",
            {"terminal_launch_args": PERMISSION_LAUNCH_ARGS["codex-native"]["read-only"]},
            PERMISSION_LAUNCH_ARGS["codex-native"]["read-only"],
            False,
        ),
    ],
)
async def test_existing_project_initializes_main_through_ordinary_sessions(
    project_client, project_agents, db_uri, bundled, harness, access, expected_args, bypass
):
    project = SqlAlchemyProjectStore(db_uri).create(uuid4().hex, "Existing", None)
    headers = {"Idempotency-Key": str(uuid4())}
    body = {
        "project_id": project.id,
        "project_main_session": True,
        "expected_main_session_id": None,
        **access,
    }
    before = (await project_client.get(f"/v1/projects/{project.id}")).json()
    assert before["main_session_id"] is None and before["main_session_state"] == "absent"

    async def submit():
        if bundled:
            return await post_session_bundle(
                project_client.post,
                "/v1/sessions",
                build_agent_bundle("custom-conductor", executor={"config": {"harness": harness}}),
                metadata=body,
                headers=headers,
            )
        agent = project_agents.get_by_name(f"{harness}-ui")
        return await project_client.post(
            "/v1/sessions", json={**body, "agent_id": agent.id}, headers=headers
        )

    first = await submit()
    second = await submit()
    assert first.status_code == second.status_code == 201
    id_key = "session_id" if bundled else "id"
    assert first.json()[id_key] == second.json()[id_key]
    session_id = first.json()[id_key]
    stored = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert stored.terminal_launch_args == expected_args
    assert (stored.labels.get(CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY) == "1") is bypass
    assert (await project_client.get(f"/v1/projects/{project.id}")).json()[
        "main_session_id"
    ] == session_id
    session = (await project_client.get(f"/v1/sessions/{session_id}")).json()
    assert session["title"] == "Orchestrator"
    assert len(session["items"]) == 1
    assert session["items"][0]["data"]["content"][0]["text"] == (
        "Hello, what would you like to work on?"
    )
    assert len(SqlAlchemyConversationStore(db_uri).list_conversations().data) == 1


async def test_failed_launch_retains_main_chat_and_idempotent_retry_does_not_relaunch(
    project_client, db_uri, monkeypatch
):
    projects = SqlAlchemyProjectStore(db_uri)
    project = projects.create(uuid4().hex, "Retry", None)
    headers = {"Idempotency-Key": str(uuid4())}
    body = {
        "project_id": project.id,
        "project_main_session": True,
        "expected_main_session_id": None,
    }
    with monkeypatch.context() as launch_failure:
        launch_failure.setattr(
            "omnigent.server.routes.sessions.routes_core._get_runner_client",
            AsyncMock(side_effect=OmnigentError("Launch failed", code=ErrorCode.INVALID_INPUT)),
        )
        response = await project_client.post("/v1/sessions", json=body, headers=headers)
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    assert projects.get(project.id, user_id=None).main_session_id == session_id
    assert response.json()["last_task_error"]["message"] == "Launch failed"
    assert response.json()["status"] == "failed"
    retry = await project_client.post("/v1/sessions", json=body, headers=headers)
    assert retry.status_code == 201
    assert retry.json()["id"] == session_id
    assert len(SqlAlchemyConversationStore(db_uri).list_conversations().data) == 1


@pytest.mark.parametrize("bundled", [False, True])
@pytest.mark.parametrize("enqueue_fails", [False, True])
async def test_main_multipart_binds_before_attachment_input_or_launch(
    multi_user_client, multi_user_app, db_uri, monkeypatch, bundled, enqueue_fails
):
    app = multi_user_app
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    project = projects.create(uuid4().hex, "Attachments", ALICE)
    app.state.host_store = HostStore(db_uri)
    app.state.sandbox_config = parse_sandbox_config(
        {
            "provider": "modal",
            "server_url": "https://managed.example.test",
            "modal": {"image": "docker.io/test/omnigent-host:latest"},
        }
    )
    startup = app.state.startup_inputs
    enqueue = startup.enqueue
    launches = []

    async def accept_input(session_id, event, **kwargs):
        assert projects.get(project.id, user_id=ALICE).main_session_id == session_id
        assert event.data["content"][0]["type"] == "input_file"
        if enqueue_fails:
            raise RuntimeError("input storage is unavailable")
        return await enqueue(session_id, event, **kwargs)

    async def capture_launch(**kwargs):
        session_id = kwargs["session_id"]
        assert projects.get(project.id, user_id=ALICE).main_session_id == session_id
        assert len(conversations.list_pending_inputs(session_id)) == 1
        launches.append(session_id)

    monkeypatch.setattr(startup, "enqueue", accept_input)
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.routes_core._run_managed_launch", capture_launch
    )
    metadata = {
        "project_id": project.id,
        "project_main_session": True,
        "expected_main_session_id": None,
        "host_type": "managed",
        "initial_input": {
            "type": "message",
            "data": {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Use the attachment"},
                ],
            },
        },
    }
    files = [("initial_files", ("context.txt", b"project context", "text/plain"))]
    if bundled:
        files.append(
            ("bundle", ("agent.tar.gz", build_agent_bundle("conductor"), "application/gzip"))
        )
    else:
        metadata.update(
            {
                "agent_id": PROJECT_AGENT_ID,
                "harness_override": "codex",
                "model_override": "gpt-5.4-mini",
                "reasoning_effort": "high",
            }
        )
    headers = {**_as_user(ALICE), "Idempotency-Key": str(uuid4())}
    try:
        response = await multi_user_client.post(
            "/v1/sessions", data={"metadata": json.dumps(metadata)}, files=files, headers=headers
        )
        assert response.status_code == 201, response.text
        session_id = response.json()["session_id" if bundled else "id"]
        assert projects.get(project.id, user_id=ALICE).main_session_id == session_id
        conversation = conversations.get_conversation(session_id)
        assert conversation is not None
        stored = SqlAlchemyFileStore(db_uri).list(session_id=session_id).data
        assert len(stored) == 1 and stored[0].filename == "context.txt"
        snapshot = (
            await multi_user_client.get(f"/v1/sessions/{session_id}", headers=headers)
        ).json()
        if enqueue_fails:
            assert snapshot["status"] == "failed" and snapshot["last_task_error"]
            assert launches == []
        else:
            await poll_until_async(lambda: bool(launches), timeout_s=5)
            assert launches == [session_id]
            assert len(conversations.list_pending_inputs(session_id)) == 1
        if not bundled:
            assert (
                conversation.harness_override,
                conversation.model_override,
                conversation.reasoning_effort,
            ) == ("codex", "gpt-5.4-mini", "high")
        retry = await multi_user_client.post(
            "/v1/sessions", data={"metadata": json.dumps(metadata)}, files=files, headers=headers
        )
        assert retry.status_code == 201, retry.text
        assert retry.json()["session_id" if bundled else "id"] == session_id
        assert len(SqlAlchemyFileStore(db_uri).list(session_id=session_id).data) == 1
        assert launches == ([] if enqueue_fails else [session_id])
    finally:
        await startup.shutdown()


async def test_concurrent_replacement_cannot_overwrite_winner_or_launch_loser(
    project_client, db_uri, monkeypatch
):
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    project = projects.create(uuid4().hex, "Concurrent", None)
    winner = conversations.create_conversation(project_id=project.id, agent_id=PROJECT_AGENT_ID)
    attach = SqlAlchemyProjectStore.set_main_session

    def attach_winner(store, project_id, session_id, *, user_id, expected_main_session_id=None):
        attach(store, project_id, winner.id, user_id=user_id)
        return attach(
            store,
            project_id,
            session_id,
            user_id=user_id,
            expected_main_session_id=expected_main_session_id,
        )

    monkeypatch.setattr(SqlAlchemyProjectStore, "set_main_session", attach_winner)
    launch = AsyncMock()
    monkeypatch.setattr("omnigent.server.routes.sessions.routes_core._get_runner_client", launch)
    response = await project_client.post(
        "/v1/sessions",
        json={
            "project_id": project.id,
            "project_main_session": True,
            "expected_main_session_id": None,
        },
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 409, response.text
    assert projects.get(project.id, user_id=None).main_session_id == winner.id
    launch.assert_not_called()
    assert [session.id for session in conversations.list_conversations().data] == [winner.id]
    assert (await project_client.get(f"/v1/sessions/{winner.id}")).json()["items"] == []


@pytest.mark.parametrize("new_project", [False, True])
async def test_same_key_concurrent_creations_keep_one_session(
    project_client, db_uri, monkeypatch, new_project
):
    prepared = 0
    both_prepared = asyncio.Event()
    prepare = routes_core.prepare_main_creation

    async def prepare_together(*args, **kwargs):
        nonlocal prepared
        result = await prepare(*args, **kwargs)
        prepared += 1
        if prepared >= 2:
            both_prepared.set()
        await asyncio.wait_for(both_prepared.wait(), timeout=5)
        return result

    monkeypatch.setattr(routes_core, "prepare_main_creation", prepare_together)
    headers = {"Idempotency-Key": str(uuid4())}
    if new_project:
        path, body, success = "/v1/projects", {"name": "One operation"}, 200
    else:
        project = SqlAlchemyProjectStore(db_uri).create(uuid4().hex, "One operation", None)
        path, body, success = (
            "/v1/sessions",
            {
                "project_id": project.id,
                "project_main_session": True,
                "expected_main_session_id": None,
            },
            201,
        )
    responses = await asyncio.gather(
        *[project_client.post(path, json=body, headers=headers) for _ in range(2)]
    )
    assert any(response.status_code == success for response in responses)
    assert all(response.status_code in (success, 409) for response in responses), [
        r.text for r in responses
    ]
    conversations = SqlAlchemyConversationStore(db_uri).list_conversations().data
    assert len(conversations) == 1
    retry = await project_client.post(path, json=body, headers=headers)
    assert retry.status_code == success, retry.text
    session_id = retry.json()["main_session_id" if new_project else "id"]
    assert session_id == conversations[0].id
    assert len(SqlAlchemyConversationStore(db_uri).list_items(session_id).data) == 1


@pytest.mark.parametrize(
    "state", ["absent", "missing", "archived", "active", "inaccessible", "foreign"]
)
@pytest.mark.parametrize("bundled", [False, True])
async def test_main_lifecycle_read_is_nonmutating_and_only_recoverable_states_replace(
    multi_user_client, multi_user_app, db_uri, state, bundled
):
    client = multi_user_client
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    project = projects.create(uuid4().hex, "Lifecycle", ALICE)
    old_id = None
    if state != "absent":
        old = conversations.create_conversation(project_id=project.id, agent_id=PROJECT_AGENT_ID)
        old_id = old.id
        permissions.grant(ALICE if state != "inaccessible" else BOB, old.id, LEVEL_OWNER)
        projects.set_main_session(project.id, old.id, user_id=ALICE)
        if state == "archived":
            archived = await client.patch(
                f"/v1/sessions/{old.id}", json={"archived": True}, headers=_as_user(ALICE)
            )
            assert archived.status_code == 200, archived.text
        elif state == "missing":
            assert (
                await client.delete(f"/v1/sessions/{old.id}", headers=_as_user(ALICE))
            ).status_code == 200
        elif state == "foreign":
            other = projects.create(uuid4().hex, "Other", ALICE)
            conversations.set_conversation_project(old.id, other.id)
    before_count = len(conversations.list_conversations().data)
    for _ in range(2):
        snapshot = (await client.get(f"/v1/projects/{project.id}", headers=_as_user(ALICE))).json()
        assert snapshot["main_session_id"] == old_id
        assert snapshot["main_session_state"] == (
            "unavailable" if state in ("inaccessible", "foreign") else state
        )
    assert len(conversations.list_conversations().data) == before_count
    multi_user_app.state.default_public_sessions = lambda: DefaultPublicSessions.ALL
    headers = {**_as_user(ALICE), "Idempotency-Key": str(uuid4())}
    metadata = {
        "project_id": project.id,
        "project_main_session": True,
        "expected_main_session_id": old_id,
    }

    async def submit():
        if bundled:
            return await post_session_bundle(
                client.post,
                "/v1/sessions",
                build_agent_bundle("custom-conductor"),
                metadata=metadata,
                headers=headers,
            )
        return await client.post(
            "/v1/sessions", json={**metadata, "agent_id": PROJECT_AGENT_ID}, headers=headers
        )

    result = await submit()
    if state in ("active", "inaccessible", "foreign"):
        assert result.status_code == 409, result.text
        assert projects.get(project.id, user_id=ALICE).main_session_id == old_id
        assert len(conversations.list_conversations().data) == before_count
        return
    assert result.status_code == 201, result.text
    new_id = result.json()["session_id" if bundled else "id"]
    assert new_id != old_id
    assert projects.get(project.id, user_id=ALICE).main_session_id == new_id
    assert permissions.get(RESERVED_USER_PUBLIC, new_id) is None
    retry = await submit()
    assert retry.status_code == 201, retry.text
    assert retry.json()["session_id" if bundled else "id"] == new_id
    assert len(conversations.list_conversations().data) == before_count + 1
    new = (await client.get(f"/v1/sessions/{new_id}", headers=_as_user(ALICE))).json()
    assert new["project_main_session"] is True
    assert new["agent_name"] == ("custom-conductor" if bundled else "project-agent")
    assert new["items"][0]["data"]["model"] == new["agent_name"]
    if state == "archived":
        restored = await client.patch(
            f"/v1/sessions/{old_id}", json={"archived": False}, headers=_as_user(ALICE)
        )
        assert restored.status_code == 200
        assert projects.get(project.id, user_id=ALICE).main_session_id == new_id
        assert (await client.get(f"/v1/sessions/{old_id}", headers=_as_user(ALICE))).json()[
            "project_main_session"
        ] is False


async def test_main_creation_requires_owner_idempotency_and_expected_pointer(
    multi_user_client, db_uri
):
    projects = SqlAlchemyProjectStore(db_uri)
    project = projects.create(uuid4().hex, "Validation", ALICE)
    body = {
        "project_id": project.id,
        "project_main_session": True,
        "expected_main_session_id": None,
    }
    for patch, headers, expected in (
        ({}, _as_user(ALICE), 400),
        ({}, {**_as_user(ALICE), "Idempotency-Key": "invalid"}, 400),
        (
            {"expected_main_session_id": uuid4().hex},
            {**_as_user(ALICE), "Idempotency-Key": str(uuid4())},
            409,
        ),
        ({}, {**_as_user(BOB), "Idempotency-Key": str(uuid4())}, 404),
        (
            {"parent_session_id": uuid4().hex},
            {**_as_user(ALICE), "Idempotency-Key": str(uuid4())},
            422,
        ),
    ):
        response = await multi_user_client.post(
            "/v1/sessions", json={**body, **patch}, headers=headers
        )
        assert response.status_code == expected, response.text
    assert projects.get(project.id, user_id=ALICE).main_session_id is None
    assert SqlAlchemyConversationStore(db_uri).list_conversations().data == []


async def test_project_main_assignment_validates_target_and_cas(multi_user_client, db_uri):
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    project = projects.create(uuid4().hex, "Assignment", ALICE)
    other = projects.create(uuid4().hex, "Other", ALICE)
    valid = conversations.create_conversation(project_id=project.id, agent_id=PROJECT_AGENT_ID)
    permissions.grant(ALICE, valid.id, LEVEL_OWNER)
    path = f"/v1/projects/{project.id}"
    for kind in ("missing", "archived", "foreign", "inaccessible", "child"):
        target = conversations.create_conversation(
            project_id=other.id if kind == "foreign" else project.id,
            parent_conversation_id=valid.id if kind == "child" else None,
        )
        permissions.grant(BOB if kind == "inaccessible" else ALICE, target.id, LEVEL_OWNER)
        if kind == "missing":
            await conversations.delete_conversation(target.id)
        elif kind == "archived":
            conversations.update_conversation(target.id, archived=True)
        rejected = await multi_user_client.patch(
            path,
            json={"main_session_id": target.id, "expected_main_session_id": None},
            headers=_as_user(ALICE),
        )
        assert rejected.status_code in (400, 403, 404), rejected.text
        assert projects.get(project.id, user_id=ALICE).main_session_id is None
    assigned = await multi_user_client.patch(
        path,
        json={"main_session_id": valid.id, "expected_main_session_id": None},
        headers=_as_user(ALICE),
    )
    assert assigned.status_code == 200 and assigned.json()["main_session_id"] == valid.id
    next_session = conversations.create_conversation(project_id=project.id)
    permissions.grant(ALICE, next_session.id, LEVEL_OWNER)
    stale = await multi_user_client.patch(
        path,
        json={"main_session_id": next_session.id, "expected_main_session_id": None},
        headers=_as_user(ALICE),
    )
    assert stale.status_code == 409
    assert projects.get(project.id, user_id=ALICE).main_session_id == valid.id


async def test_orchestrator_session_choices_are_independent_and_do_not_change_agent(
    project_client, project_agents, db_uri
):
    choices = [
        ("project-agent", "codex", "gpt-5.4-mini", "high"),
        ("project-agent", "claude-sdk", "claude-sonnet-4-6", "medium"),
        ("claude-native-ui", "claude-native", "claude-sonnet-4-6", "high"),
        ("codex-native-ui", "codex-native", "gpt-5.4-mini", "high"),
    ]
    for agent_name, harness, model, effort in choices:
        agent = project_agents.get_by_name(agent_name)
        override = harness if agent_name == "project-agent" else None
        response = await project_client.post(
            "/v1/projects",
            json={
                "name": harness,
                "config": {
                    "agent_id": agent.id,
                    **({"harness": override} if override else {}),
                    "model": model,
                    "effort": effort,
                    "workspace": "/work/project",
                },
            },
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 200, response.text
        session_id = response.json()["main_session_id"]
        stored = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
        assert (
            stored.agent_id,
            stored.harness_override,
            stored.model_override,
            stored.reasoning_effort,
        ) == (agent.id, override, model, effort)
        expected_args = (
            ["--permission-mode", "bypassPermissions"]
            if harness == "claude-native"
            else []
            if harness == "codex-native"
            else None
        )
        assert stored.terminal_launch_args == expected_args
        assert (stored.labels.get(CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY) == "1") is (
            harness == "codex-native"
        )
        snapshot = (await project_client.get(f"/v1/sessions/{session_id}")).json()
        assert snapshot["harness"] == harness and snapshot["agent_name"] == agent_name
        await project_client.patch(
            f"/v1/projects/{response.json()['id']}",
            json={"config": {"harness": "pi", "effort": "low"}},
        )
        unchanged = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
        assert (
            unchanged.harness_override,
            unchanged.model_override,
            unchanged.reasoning_effort,
        ) == (override, model, effort)
        assert unchanged.terminal_launch_args == stored.terminal_launch_args
        assert unchanged.labels == stored.labels
        assert project_agents.get(agent.id).bundle_location == agent.bundle_location


@pytest.mark.parametrize("explicit_host", [False, True])
async def test_name_only_project_uses_owned_host_home_and_normal_launch(
    multi_user_client, multi_user_app, project_agents, db_uri, monkeypatch, explicit_host
):
    app = multi_user_app
    app.state.host_store = HostStore(db_uri)
    foreign_host, own_host = uuid4().hex, uuid4().hex
    _register_online_host(app, foreign_host, BOB)
    _register_online_host(app, own_host, ALICE)
    app.state.host_registry.get(own_host).configured_harnesses = {
        "claude-native": explicit_host,
        "codex-native": True,
    }
    agent = project_agents.get_by_name("codex-native-ui")
    launches = []

    def answer_host(connection, payload):
        assert connection.host_id == own_host
        frame = decode_host_frame(payload)
        if isinstance(frame, HostStatFrame):
            connection.pending_stats[frame.request_id].set_result(
                {
                    "status": "ok",
                    "exists": True,
                    "type": "directory",
                    "canonical_path": "/home/owner",
                }
            )
        elif isinstance(frame, HostLaunchRunnerFrame):
            conversation = SqlAlchemyConversationStore(db_uri).get_conversation(frame.session_id)
            project = SqlAlchemyProjectStore(db_uri).get(conversation.project_id, user_id=ALICE)
            assert project.main_session_id == frame.session_id
            items = SqlAlchemyConversationStore(db_uri).list_items(frame.session_id).data
            assert len(items) == 1 and items[0].type == "message"
            assert isinstance(items[0].data, MessageData)
            assert items[0].data.role == "assistant"
            assert frame.harness == "codex-native"
            if explicit_host:
                assert (
                    conversation.harness_override,
                    conversation.model_override,
                    conversation.reasoning_effort,
                ) == (None, "gpt-5.4-mini", "high")
            launches.append(frame)
            connection.pending_launches[frame.request_id].set_result({"status": "launched"})
        else:
            pytest.fail(f"Unexpected host frame: {frame}")

    monkeypatch.setattr(app.state.host_registry, "send_text", answer_host)
    response = await multi_user_client.post(
        "/v1/projects",
        json={
            "name": "Home board",
            "config": {
                "host_id": own_host,
                "agent_id": agent.id,
                "model": "gpt-5.4-mini",
                "effort": "high",
            }
            if explicit_host
            else {},
        },
        headers=_as_user(ALICE),
    )
    assert response.status_code == 200, response.text
    session_id = response.json()["main_session_id"]
    conversation = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert conversation.host_id == own_host and conversation.workspace == "/home/owner"
    assert conversation.runner_id and len(launches) == 1


async def test_board_api_lifecycle_and_no_deletion(project_client):
    project = (await project_client.post("/v1/projects", json={"name": "Shipping"})).json()
    base = f"/v1/projects/{project['id']}/tickets"
    labels = [{"key": "source", "value": "report"}]
    created = await project_client.post(base, json={"title": "Ship", "labels": labels})
    assert created.status_code == 201
    ticket = created.json()
    assert ticket["stage_id"] == "open" and ticket["description"] == ""
    assert ticket["labels"] == labels and not {"tags", "lead_session_id"} & ticket.keys()
    path = base + f"/{ticket['id']}"
    response = await project_client.patch(
        path,
        json={"labels": [{"key": "needs-human", "value": "review"}], "stage_id": "in_progress"},
    )
    assert response.status_code == 200
    assert (await project_client.get(base, params={"stage_id": "in_progress", "limit": 1})).json()[
        "data"
    ][0]["id"] == ticket["id"]
    assert (await project_client.patch(path, json={"stage_id": "done"})).status_code == 200
    comment = await project_client.post(
        path + "/comments",
        json={
            "text": "Review the PR",
            "outputs": [
                {"type": "pull_request", "url": "https://git.example.com/org/repo/pull/42/files"}
            ],
        },
    )
    assert comment.status_code == 201
    outputs = (await project_client.get(path + "/events", params={"kind": "output"})).json()[
        "data"
    ]
    assert outputs[0]["payload"] == {
        "type": "pull_request",
        "url": "https://git.example.com/org/repo/pull/42",
    }
    completed = await project_client.patch(
        path, json={"stage_id": "done", "comment": "Tests passed"}
    )
    assert completed.status_code == 200
    assert completed.json()["comment_count"] == 2
    assert completed.json()["labels"] == [{"key": "needs-human", "value": "review"}]
    assert {"plan", "review_summary", "completion_summary"}.isdisjoint(completed.json())
    events_before = (await project_client.get(path + "/events")).json()
    rejected = await project_client.patch(
        path, json={"description": "Must not be applied", "review_action": "approve"}
    )
    assert rejected.status_code == 422
    assert (await project_client.get(path)).json()["description"] == ""
    assert (await project_client.get(path + "/events")).json() == events_before
    for method, url, body, field in (
        ("POST", base, {"title": "Rejected", "tags": labels}, "tags"),
        ("PATCH", path, {"tags": [], "description": "Must not be applied"}, "tags"),
        ("POST", base, {"title": "Rejected", "lead_session_id": None}, "lead_session_id"),
        ("PATCH", path, {"lead_session_id": uuid4().hex}, "lead_session_id"),
        (
            "PATCH",
            path,
            {"lead_session_id": None, "description": "Must not be applied"},
            "lead_session_id",
        ),
    ):
        rejected = await project_client.request(method, url, json=body)
        assert rejected.status_code == 422
        assert any(error["loc"] == ["body", field] for error in rejected.json()["detail"])
    assert (await project_client.get(path)).json() == {
        **completed.json(),
        "agents": [],
        "agents_has_more": False,
        "has_running_agents": False,
    }
    assert (await project_client.get(path + "/events")).json() == events_before
    assert len((await project_client.get(base)).json()["data"]) == 1
    schemas = (await project_client.get("/openapi.json")).json()["components"]["schemas"]
    for name in ("CreateTicket", "UpdateTicket"):
        assert "labels" in schemas[name]["properties"]
        assert not {"tags", "lead_session_id"} & schemas[name]["properties"].keys()
    assert "TicketLabel" in schemas and "TicketTag" not in schemas
    assert (await project_client.delete(path)).status_code in (404, 405)
    assert (await project_client.delete(f"/v1/projects/{project['id']}")).status_code == 409
    assert (await project_client.get(path)).status_code == 200
    assert (await project_client.get(base, params={"limit": 101})).status_code == 422
    assert (
        await project_client.post(
            path + "/comments", json={"text": "Hi", "notify_orchestrator": False}
        )
    ).status_code == 422


async def test_custom_stage_api_and_ticket_tools_share_real_project_definitions(project_client):
    from omnigent.runner.project_tickets import execute_ticket_tool

    project = (await project_client.post("/v1/projects", json={"name": "Response"})).json()
    base = f"/v1/projects/{project['id']}/tickets"
    source = project["main_session_id"]
    empty = json.loads(await execute_ticket_tool("sys_ticket_list", {}, project_client, source))
    assert empty["data"] == []
    assert empty["agent_guidance"] == PROJECT_TICKET_WORK_INSTRUCTION
    assert "agent_guidance" not in (await project_client.get(base)).json()
    stages = await project_client.get(base + "/stages")
    assert stages.status_code == 200 and stages.json()["initial_stage_id"] == "open"
    ticket = (
        await project_client.post(
            base,
            json={
                "title": "Investigate",
                "description": "Keep context",
                "labels": [{"key": "needs-human"}],
            },
        )
    ).json()
    configuration = {
        "stages": [
            {"id": "triage", "name": "New"},
            {
                "id": "verifying",
                "name": "Verifying",
                "instructions": "Attach recovery evidence",
                "color": "purple",
                "icon": "search",
            },
            {"id": "resolved", "name": "Resolved"},
        ],
        "moves": {"open": "triage"},
    }
    events_before = (await project_client.get(f"{base}/{ticket['id']}/events")).json()
    rejected = json.loads(
        await execute_ticket_tool(
            "sys_ticket_stages_update",
            {"stages": configuration["stages"]},
            project_client,
            source,
        )
    )
    assert "HTTP 400" in rejected["error"]
    assert "contains tickets" in rejected["detail"]
    assert (await project_client.get(base + "/stages")).json() == stages.json()
    assert (await project_client.get(f"{base}/{ticket['id']}")).json() == {
        **ticket,
        "agents": [],
        "agents_has_more": False,
        "has_running_agents": False,
    }
    assert (await project_client.get(f"{base}/{ticket['id']}/events")).json() == events_before
    configured = json.loads(
        await execute_ticket_tool(
            "sys_ticket_stages_update", configuration, project_client, source
        )
    )
    assert configured["initial_stage_id"] == "triage"
    assert [stage["is_complete"] for stage in configured["stages"]] == [False, False, True]
    assert (await project_client.get(base + "/stages")).json() == configured
    assert (await project_client.get(base + "/stages", params={"include_usage": True})).json()[
        "occupied_stage_ids"
    ] == ["triage"]
    human_ticket = (await project_client.get(f"{base}/{ticket['id']}")).json()
    assert human_ticket["stage_id"] == "triage"
    assert human_ticket["description"] == "Keep context"
    assert human_ticket["labels"] == ticket["labels"]
    assert "agent_guidance" not in human_ticket
    events = (await project_client.get(f"{base}/{ticket['id']}/events")).json()["data"]
    assert events[:-1] == events_before["data"]
    assert events[-1]["session_id"] == source
    assert events[-1]["payload"]["reason"] == "stage_removed"
    assert (await project_client.post(base, json={"title": "Next"})).json()["stage_id"] == "triage"
    listed = json.loads(await execute_ticket_tool("sys_ticket_list", {}, project_client, source))
    assert listed["stages"] == configured["stages"]
    assert listed["agent_guidance"] == PROJECT_TICKET_WORK_INSTRUCTION
    detail = json.loads(
        await execute_ticket_tool(
            "sys_ticket_get", {"ticket_id": ticket["id"]}, project_client, source
        )
    )
    assert detail["stages"][1]["instructions"] == "Attach recovery evidence"
    assert detail["stages"][1]["color"] == "purple"
    assert detail["stages"][1]["icon"] == "search"
    assert detail["ticket"]["agent_guidance"] == PROJECT_TICKET_WORK_INSTRUCTION
    moved = json.loads(
        await execute_ticket_tool(
            "sys_ticket_update",
            {
                "ticket_id": ticket["id"],
                "stage_id": "verifying",
                "description": "Recovery looks good",
            },
            project_client,
            source,
        )
    )
    assert moved["stage_id"] == "verifying"
    completed_without_summary = json.loads(
        await execute_ticket_tool(
            "sys_ticket_update",
            {"ticket_id": ticket["id"], "stage_id": "resolved"},
            project_client,
            source,
        )
    )
    assert completed_without_summary["stage_id"] == "resolved"
    assert "completion_summary" not in completed_without_summary
    completed = json.loads(
        await execute_ticket_tool(
            "sys_ticket_update",
            {
                "ticket_id": ticket["id"],
                "stage_id": "resolved",
                "comment": "Evidence attached",
            },
            project_client,
            source,
        )
    )
    assert completed["stage_id"] == "resolved"
    assert completed["description"] == "Recovery looks good"
    for invalid in (
        {**configuration, "stages": [configuration["stages"][0]] * 2},
        {**configuration, "stages": []},
        {"stages": [{"id": "triage", "name": "New", "color": "#ff0000"}]},
        {"stages": [{"id": "triage", "name": "New", "icon": "<svg />"}]},
    ):
        assert (await project_client.put(base + "/stages", json=invalid)).status_code == 422
    assert (
        await project_client.patch(f"{base}/{ticket['id']}", json={"stage_id": "foreign"})
    ).status_code == 400
    assert (await project_client.get(base, params={"stage_id": "foreign"})).status_code == 400
    reordered = json.loads(
        await execute_ticket_tool(
            "sys_ticket_stages_update",
            {
                "stages": [
                    configuration["stages"][1],
                    {"id": "triage", "name": "Queued"},
                    configuration["stages"][2],
                ]
            },
            project_client,
            source,
        )
    )
    assert reordered["initial_stage_id"] == "verifying"
    assert [stage["id"] for stage in reordered["stages"]] == ["verifying", "triage", "resolved"]
    assert reordered["stages"][1]["name"] == "Queued"
    assert reordered["stages"][0]["color"] == "purple"
    assert reordered["stages"][0]["icon"] == "search"
    assert (await project_client.get(f"{base}/{ticket['id']}")).json()["stage_id"] == "resolved"


async def test_agent_source_is_project_scoped_owned_bound_and_private(multi_user_client, db_uri):
    client = multi_user_client
    project = (
        await client.post("/v1/projects", json={"name": "Private"}, headers=_as_user(ALICE))
    ).json()
    path = f"/v1/projects/{project['id']}/tickets"
    store = SqlAlchemyConversationStore(db_uri)
    source = store.create_conversation(project_id=project["id"])
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.grant(ALICE, source.id, LEVEL_OWNER)
    headers = {
        **_as_user(ALICE),
        **_bind_runner(db_uri, source.id),
        ORCHESTRATION_SOURCE_HEADER: source.id,
    }
    response = await client.post(path, json={"title": "Agent ticket"}, headers=headers)
    assert response.status_code == 201, response.text
    ticket_id = response.json()["id"]
    assert (await client.get(path, headers=headers)).json()["agent_guidance"] == (
        PROJECT_TICKET_WORK_INSTRUCTION
    )
    assert (await client.get(f"{path}/{ticket_id}", headers=headers)).json()["agent_guidance"] == (
        PROJECT_TICKET_WORK_INSTRUCTION
    )
    events = (await client.get(f"{path}/{ticket_id}/events", headers=headers)).json()["data"]
    assert events[0]["session_id"] == source.id and events[0]["user_id"] == ALICE
    stages = (await client.get(path + "/stages", headers=headers)).json()
    assert (await client.put(path + "/stages", json=stages, headers=headers)).status_code == 200
    assert (await client.get(path, headers=_as_user(BOB))).status_code == 404
    assert (await client.get(path + "/stages", headers=_as_user(BOB))).status_code == 404
    assert (
        await client.put(path + "/stages", json=stages, headers=_as_user(BOB))
    ).status_code == 404
    assert (
        await client.get(path, headers={**_as_user(ALICE), ORCHESTRATION_SOURCE_HEADER: source.id})
    ).status_code == 403
    assert (
        await client.put(
            path + "/stages",
            json=stages,
            headers={**_as_user(ALICE), ORCHESTRATION_SOURCE_HEADER: source.id},
        )
    ).status_code == 403
    other = (
        await client.post("/v1/projects", json={"name": "Other"}, headers=_as_user(ALICE))
    ).json()
    assert (
        await client.get(f"/v1/projects/{other['id']}/tickets", headers=headers)
    ).status_code == 403
    assert (
        await client.put(
            f"/v1/projects/{other['id']}/tickets/stages", json=stages, headers=headers
        )
    ).status_code == 403
    permissions.ensure_user(BOB)
    permissions.grant(BOB, source.id, LEVEL_EDIT)
    assert (await client.get(path, headers=headers)).status_code == 403
    assert (
        await client.post(path, json={"title": "Shared writer"}, headers=headers)
    ).status_code == 403
    assert (await client.get(path + "/stages", headers=headers)).status_code == 403
    assert (await client.put(path + "/stages", json=stages, headers=headers)).status_code == 403


async def test_links_require_same_project_and_preserve_history(project_client, db_uri):
    project = (await project_client.post("/v1/projects", json={"name": "Links"})).json()
    base = f"/v1/projects/{project['id']}/tickets"
    ticket = (await project_client.post(base, json={"title": "Keep history"})).json()
    path = f"{base}/{ticket['id']}"
    store = SqlAlchemyConversationStore(db_uri)
    source = store.create_conversation(project_id=project["id"])
    foreign = store.create_conversation()
    assert (
        await project_client.post(
            path + "/sessions", json={"session_id": foreign.id, "involvement": "Not this project"}
        )
    ).status_code == 400
    assert (
        await project_client.post(
            path + "/sessions", json={"session_id": source.id, "involvement": " Plan the work "}
        )
    ).status_code == 200
    assert (await project_client.get(path + "/sessions")).json()["data"][0][
        "session_id"
    ] == source.id
    assert (await project_client.get(path + "/sessions")).json()["data"][0][
        "involvement"
    ] == "Plan the work"
    assert (
        await project_client.post(
            path + "/sessions", json={"session_id": source.id, "involvement": "Build the plan"}
        )
    ).status_code == 200
    assert (await project_client.get(path + "/sessions")).json()["data"][0][
        "involvement"
    ] == "Build the plan"
    await store.delete_conversation(source.id)
    assert (await project_client.get(path)).json()["stage_id"] == "open"
    assert len((await project_client.get(path + "/events")).json()["data"]) == 3


@pytest.mark.parametrize(
    ("persisted", "cached", "archived", "expected"),
    [
        ("running", None, False, True),
        ("running", "idle", False, False),
        ("idle", "running", False, True),
        ("waiting", None, False, False),
        ("failed", None, False, False),
        ("running", None, True, False),
        (None, None, False, None),
        ("running", None, None, None),
        ("idle", "unknown", False, None),
    ],
)
def test_ticket_running_projection_uses_only_known_live_activity(
    monkeypatch, persisted, cached, archived, expected
):
    session_id = uuid4().hex
    monkeypatch.setattr(
        ticket_routes, "_session_status_cache", {session_id: cached} if cached else {}
    )
    sessions = {
        session_id: SessionActivity(updated_at=0, live_status=persisted, archived=archived)
    }
    assert ticket_routes._has_running_agents([session_id], sessions, has_more=False) is expected
    assert ticket_routes._has_running_agents([session_id], sessions, has_more=True) is (
        True if expected is True else None
    )
    assert ticket_routes._has_running_agents([session_id], {}, has_more=False) is None


async def test_ticket_running_projection_batches_links_and_respects_access(
    multi_user_client, db_uri, monkeypatch
):
    client = multi_user_client
    headers = _as_user(ALICE)
    project = (
        await client.post("/v1/projects", json={"name": "Activity"}, headers=headers)
    ).json()
    base = f"/v1/projects/{project['id']}/tickets"
    active = (await client.post(base, json={"title": "Working"}, headers=headers)).json()
    quiet = (await client.post(base, json={"title": "Unassigned"}, headers=headers)).json()
    conversations = SqlAlchemyConversationStore(db_uri)
    worker = conversations.create_conversation(
        project_id=project["id"], agent_id=PROJECT_AGENT_ID, harness_override="codex"
    )
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.grant(ALICE, worker.id, LEVEL_OWNER)
    assert (
        await client.post(
            f"{base}/{active['id']}/sessions", json={"session_id": worker.id}, headers=headers
        )
    ).status_code == 200
    conversations.set_session_live_status(worker.id, "running")
    monkeypatch.setattr(
        ticket_routes, "_session_status_cache", {project["main_session_id"]: "running"}
    )
    original = SqlAlchemyConversationStore.get_session_activity
    original_grants = SqlAlchemyPermissionStore.list_for_sessions
    batches = []
    grant_batches = []

    def capture(store, session_ids):
        batches.append(session_ids)
        return original(store, session_ids)

    def capture_grants(store, session_ids, *, user_id=None):
        assert user_id in (ALICE, RESERVED_USER_PUBLIC)
        grant_batches.append((session_ids, user_id))
        return original_grants(store, session_ids, user_id=user_id)

    monkeypatch.setattr(SqlAlchemyConversationStore, "get_session_activity", capture)
    monkeypatch.setattr(SqlAlchemyPermissionStore, "list_for_sessions", capture_grants)
    page = (await client.get(base, headers=headers)).json()["data"]
    assert {row["id"]: row["has_running_agents"] for row in page} == {
        active["id"]: True,
        quiet["id"]: False,
    }
    assert batches == [[worker.id]]
    assert grant_batches == [([worker.id], ALICE), ([worker.id], RESERVED_USER_PUBLIC)]
    active_row = next(row for row in page if row["id"] == active["id"])
    identity = {"agent_name": "project-agent", "harness": "codex"}
    assert active_row["agents"] == [identity] and active_row["agents_has_more"] is False
    assert next(row for row in page if row["id"] == quiet["id"])["agents"] == []
    ticket_routes._session_status_cache[worker.id] = "idle"
    page = (await client.get(base, headers=headers)).json()["data"]
    assert all(row["has_running_agents"] is False for row in page)
    ticket_routes._session_status_cache[worker.id] = "running"
    permissions.revoke(ALICE, worker.id)
    page = (await client.get(base, headers=headers)).json()["data"]
    assert next(row for row in page if row["id"] == active["id"])["has_running_agents"] is None
    assert next(row for row in page if row["id"] == active["id"])["agents"] == []
    permissions.grant(RESERVED_USER_PUBLIC, worker.id, LEVEL_EDIT)
    page = (await client.get(base, headers=headers)).json()["data"]
    assert next(row for row in page if row["id"] == active["id"])["has_running_agents"] is True
    # Several linked conversations on one agent/harness render one identity.
    duplicate = conversations.create_conversation(
        project_id=project["id"], agent_id=PROJECT_AGENT_ID, harness_override="codex"
    )
    permissions.grant(ALICE, duplicate.id, LEVEL_OWNER)
    assert (
        await client.post(
            f"{base}/{active['id']}/sessions", json={"session_id": duplicate.id}, headers=headers
        )
    ).status_code == 200
    detail = (await client.get(f"{base}/{active['id']}", headers=headers)).json()
    assert detail["agents"] == [identity] and detail["agents_has_more"] is False
    assert (await client.get(base, headers=_as_user(BOB))).status_code == 404


async def test_lifecycle_related_and_session_links_api(project_client, db_uri):
    project = (await project_client.post("/v1/projects", json={"name": "Lifecycle"})).json()
    base = f"/v1/projects/{project['id']}/tickets"
    ticket = (await project_client.post(base, json={"title": "Feature"})).json()
    related = (await project_client.post(base, json={"title": "Related work"})).json()
    path, other_path = f"{base}/{ticket['id']}", f"{base}/{related['id']}"
    planned = await project_client.patch(
        path, json={"stage_id": "planning", "description": "## Approach\n\n- Acceptance"}
    )
    assert planned.status_code == 200 and planned.json()["description"].startswith("## Approach")
    assert (await project_client.patch(path, json={"stage_id": "review"})).status_code == 200
    review = await project_client.patch(
        path, json={"stage_id": "review", "comment": "Verify the demo"}
    )
    assert review.status_code == 200
    changed = await project_client.patch(
        path, json={"stage_id": "in_progress", "comment": "Adjust the layout"}
    )
    assert (
        changed.json()["description"] == "## Approach\n\n- Acceptance"
        and changed.json()["comment_count"] == 2
    )
    for source, target in ((path, related["id"]), (other_path, ticket["id"])):
        assert (
            await project_client.post(source + "/related", json={"related_ticket_id": target})
        ).status_code == 200
        assert (await project_client.get(source + "/related")).json()["data"][0]["id"] == target
    assert (
        await project_client.post(path + "/related", json={"related_ticket_id": ticket["id"]})
    ).status_code == 400
    foreign = (await project_client.post("/v1/projects", json={"name": "Elsewhere"})).json()
    foreign_ticket = (
        await project_client.post(f"/v1/projects/{foreign['id']}/tickets", json={"title": "Other"})
    ).json()
    assert (
        await project_client.post(
            path + "/related", json={"related_ticket_id": foreign_ticket["id"]}
        )
    ).status_code == 404
    session = SqlAlchemyConversationStore(db_uri).create_conversation(project_id=project["id"])
    assert (
        await project_client.post(path + "/sessions", json={"session_id": session.id})
    ).status_code == 200
    assert (await project_client.get(path + "/sessions")).json()["data"][0][
        "session_id"
    ] == session.id
    SqlAlchemyConversationStore(db_uri).update_conversation(session.id, archived=True)
    assert (
        await project_client.post(path + "/sessions", json={"session_id": session.id})
    ).status_code == 200
    assert (
        await project_client.post(
            path + "/sessions", json={"session_id": session.id, "action": "remove"}
        )
    ).status_code == 200
    assert (
        await project_client.post(
            other_path + "/related", json={"related_ticket_id": ticket["id"], "action": "remove"}
        )
    ).status_code == 200
    assert (await project_client.get(path + "/related")).json()["data"] == []
    assert len((await project_client.get(other_path + "/events")).json()["data"]) == 3


async def test_related_and_session_links_respect_user_and_project_permissions(
    multi_user_client, db_uri
):
    client = multi_user_client
    project = (
        await client.post("/v1/projects", json={"name": "Private"}, headers=_as_user(ALICE))
    ).json()
    base = f"/v1/projects/{project['id']}/tickets"
    ticket = (await client.post(base, json={"title": "Owned"}, headers=_as_user(ALICE))).json()
    related = (await client.post(base, json={"title": "Peer"}, headers=_as_user(ALICE))).json()
    path = f"{base}/{ticket['id']}"
    assert (
        await client.post(
            path + "/related", json={"related_ticket_id": related["id"]}, headers=_as_user(BOB)
        )
    ).status_code == 404
    assert (await client.get(path + "/related", headers=_as_user(BOB))).status_code == 404
    store = SqlAlchemyConversationStore(db_uri)
    foreign = store.create_conversation(project_id=project["id"])
    SqlAlchemyPermissionStore(db_uri).grant(BOB, foreign.id, LEVEL_OWNER)
    response = await client.post(
        path + "/sessions", json={"session_id": foreign.id}, headers=_as_user(ALICE)
    )
    assert response.status_code in (403, 404)
    outside = store.create_conversation()
    SqlAlchemyPermissionStore(db_uri).grant(ALICE, outside.id, LEVEL_OWNER)
    assert (
        await client.post(
            path + "/sessions", json={"session_id": outside.id}, headers=_as_user(ALICE)
        )
    ).status_code == 400
    assert (await client.get(path + "/sessions", headers=_as_user(ALICE))).json()["data"] == []


async def test_notification_enqueue_survives_crash_without_duplicate_message(
    project_client, project_app, db_uri, monkeypatch
):
    project = (await project_client.post("/v1/projects", json={"name": "Outbox"})).json()
    session_id = project["main_session_id"]
    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(session_id, uuid4().hex, "/tmp/project")
    service = project_app.state.project_ticket_notifications
    base = f"/v1/projects/{project['id']}/tickets"
    ticket = (await project_client.post(base, json={"title": "Durable update"})).json()
    mark = service.tickets.mark_notified

    def crash(*args):
        raise RuntimeError("crash after durable enqueue")

    monkeypatch.setattr(service.tickets, "mark_notified", crash)
    await service.flush_page(project_app)
    queued = store.list_pending_inputs(session_id)
    assert len(queued) == 1
    assert "Durable update" in queued[0].data.content[0]["text"]
    assert service.tickets.pending_notifications()
    monkeypatch.setattr(service.tickets, "mark_notified", mark)
    restarted = ProjectTicketNotifications(service.tickets, service.projects, service.inputs)
    await restarted.flush_page(project_app)
    assert len(store.list_pending_inputs(session_id)) == 1
    assert service.tickets.pending_notifications() == []
    await project_client.post(
        f"{base}/{ticket['id']}/comments",
        json={"text": "Own update"},
        headers={ORCHESTRATION_SOURCE_HEADER: session_id},
    )
    assert service.tickets.pending_notifications() == []


@pytest.mark.parametrize("external_host", [False, True])
async def test_notifications_drain_through_normal_dispatch_after_reconnect(
    project_client, project_app, db_uri, monkeypatch, external_host
):
    project = (await project_client.post("/v1/projects", json={"name": "Reconnect"})).json()
    session_id = project["main_session_id"]
    store = SqlAlchemyConversationStore(db_uri)
    host_id, runner_id = uuid4().hex, uuid4().hex
    if external_host:
        store.set_host_id(session_id, host_id, "/work/board")
    store.set_runner_id(session_id, runner_id)
    base = f"/v1/projects/{project['id']}/tickets"
    ticket = (await project_client.post(base, json={"title": "Persisted task"})).json()
    await project_client.post(f"{base}/{ticket['id']}/comments", json={"text": "First update"})
    service = project_app.state.project_ticket_notifications
    await service.flush_page(project_app)
    assert len(store.list_pending_inputs(session_id)) == 2
    assert len(service.tickets.pending_notifications()) == (0 if external_host else 2)

    forwarded = []
    runner = _stub_runner(monkeypatch, forwarded)
    monkeypatch.setattr("omnigent.server.routes.sessions._ensure_runner_relay_ready", AsyncMock())
    inputs = project_app.state.startup_inputs
    finish = inputs._finish
    late_update = False

    async def finish_with_update(current_session_id):
        nonlocal late_update
        await finish(current_session_id)
        if not late_update:
            late_update = True
            response = await project_client.post(
                f"{base}/{ticket['id']}/comments", json={"text": "Arrived while draining"}
            )
            assert response.status_code == 201
            await service.flush_page(project_app)

    monkeypatch.setattr(inputs, "_finish", finish_with_update)
    register_test_runner(project_app, runner_id)
    try:
        if external_host:
            project_app.state.host_registry.register(
                host_id,
                AsyncMock(),
                HostHelloFrame(version="test", frame_protocol_version=1, name="Ticket host"),
                owner=None,
                registered_with_managed_token=False,
            )
            inputs.host_changed(project_app, host_id)
        else:
            await service.flush_page(project_app)
        await poll_until_async(
            lambda: (
                late_update
                and sum(event.get("type") == "message" for event in forwarded) >= 3
                and not store.list_pending_inputs(session_id)
            ),
            timeout_s=5,
        )
        turns = [event for event in forwarded if event.get("type") == "message"]
        assert len(turns) == 3
        assert len({turn["persisted_item_id"] for turn in turns}) == 3
        assert "Persisted task" in json.dumps(turns[0])
        assert "First update" in json.dumps(turns[1])
        assert "Arrived while draining" in json.dumps(turns[2])
        assert service.tickets.pending_notifications() == []
    finally:
        await inputs.shutdown()
        await runner.aclose()
