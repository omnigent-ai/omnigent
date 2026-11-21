import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from omnigent.entities import MessageData
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import (
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostStatFrame,
    decode_host_frame,
)
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, RESERVED_USER_PUBLIC
from omnigent.server.project_ticket_notifications import ProjectTicketNotifications
from omnigent.server.sharing_settings import DefaultPublicSessions
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from tests._helpers.polling import poll_until_async
from tests.server.helpers import register_test_runner
from tests.server.integration.test_sessions_attribution import _bind_runner
from tests.server.integration.test_sessions_permissions import _register_online_host
from tests.server.integration.test_sessions_web_send_stable_id import _stub_runner
from tests.server.routes.test_projects_crud import ALICE, BOB, PROJECT_AGENT_ID, _as_user
from tests.server.routes.test_projects_crud import multi_user_app as multi_user_app
from tests.server.routes.test_projects_crud import multi_user_client as multi_user_client
from tests.server.routes.test_projects_crud import project_agents as project_agents
from tests.server.routes.test_projects_crud import project_app as project_app
from tests.server.routes.test_projects_crud import project_client as project_client


async def test_project_creation_has_one_normal_orchestrator(project_client, db_uri):
    project = (await project_client.post("/v1/projects", json={"name": "Shipping"})).json()
    session_id = project["orchestrator_session_id"]
    session = (await project_client.get(f"/v1/sessions/{session_id}")).json()
    assert session["title"] == "Orchestrator"
    assert session["project_id"] == project["id"]
    assert session["project_orchestrator"] is True
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
    assert SqlAlchemyConversationStore(db_uri).get_conversation(session_id).kind == "default"
    again = await project_client.post(f"/v1/projects/{project['id']}/orchestrator")
    assert again.json()["orchestrator_session_id"] == session_id
    assert (await project_client.get(f"/v1/sessions/{session_id}")).json()["items"] == [greeting]
    info = (await project_client.get("/v1/info")).json()
    assert info["project_tickets"] is True
    assert info["project_ticket_lifecycle"] is True
    assert info["project_ticket_agent_involvement"] is True
    assert info["project_ticket_stages"] is True
    assert info["durable_initial_input"] is False

    worker = (
        await project_client.post(
            "/v1/sessions",
            json={
                "agent_id": PROJECT_AGENT_ID,
                "project_id": project["id"],
                "title": "Orchestrator",
            },
        )
    ).json()
    worker = (await project_client.get(f"/v1/sessions/{worker['id']}")).json()
    assert worker["project_id"] == project["id"]
    assert worker["project_orchestrator"] is False
    assert worker["items"] == []


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
    session_id = project["orchestrator_session_id"]
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


async def test_empty_agent_catalog_leaves_no_broken_project(
    project_client, project_agents, db_uri
):
    project_agents.delete(PROJECT_AGENT_ID)
    response = await project_client.post("/v1/projects", json={"name": "Unconfigured"})
    assert response.status_code == 400
    assert "available project agent" in response.text
    assert SqlAlchemyProjectStore(db_uri).list(user_id=None) == []
    assert SqlAlchemyConversationStore(db_uri).list_conversations().data == []


async def test_existing_project_can_initialize_its_orchestrator(project_client, db_uri):
    project = SqlAlchemyProjectStore(db_uri).create(uuid4().hex, "Existing", None)
    first = await project_client.post(f"/v1/projects/{project.id}/orchestrator")
    second = await project_client.post(f"/v1/projects/{project.id}/orchestrator")
    assert first.status_code == second.status_code == 200
    assert first.json()["orchestrator_session_id"] == second.json()["orchestrator_session_id"]
    session_id = first.json()["orchestrator_session_id"]
    session = (await project_client.get(f"/v1/sessions/{session_id}")).json()
    assert len(session["items"]) == 1
    assert session["items"][0]["data"]["content"][0]["text"] == (
        "Hello, what would you like to work on?"
    )
    assert len(SqlAlchemyConversationStore(db_uri).list_conversations().data) == 1


async def test_failed_launch_can_retry_without_a_dangling_project_role(
    project_client, db_uri, monkeypatch
):
    projects = SqlAlchemyProjectStore(db_uri)
    project = projects.create(uuid4().hex, "Retry", None)
    with monkeypatch.context() as launch_failure:
        launch_failure.setattr(
            "omnigent.server.routes.sessions.routes_core._get_runner_client",
            AsyncMock(side_effect=OmnigentError("Launch failed", code=ErrorCode.INVALID_INPUT)),
        )
        response = await project_client.post(f"/v1/projects/{project.id}/orchestrator")
    assert response.status_code == 400
    assert projects.get(project.id, user_id=None).orchestrator_session_id is None
    assert SqlAlchemyConversationStore(db_uri).list_conversations().data == []

    retry = await project_client.post(f"/v1/projects/{project.id}/orchestrator")
    assert retry.status_code == 200
    assert retry.json()["orchestrator_session_id"] is not None


async def test_concurrent_initializer_uses_normal_session_teardown(
    project_client, db_uri, monkeypatch
):
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    project = projects.create(uuid4().hex, "Concurrent", None)
    winner = conversations.create_conversation(project_id=project.id, agent_id=PROJECT_AGENT_ID)
    attach = SqlAlchemyProjectStore.set_orchestrator

    def attach_winner(store, project_id, session_id, *, user_id):
        return attach(store, project_id, winner.id, user_id=user_id)

    monkeypatch.setattr(SqlAlchemyProjectStore, "set_orchestrator", attach_winner)
    response = await project_client.post(f"/v1/projects/{project.id}/orchestrator")
    assert response.status_code == 200, response.text
    assert response.json()["orchestrator_session_id"] == winner.id
    assert [session.id for session in conversations.list_conversations().data] == [winner.id]
    assert (await project_client.get(f"/v1/sessions/{winner.id}")).json()["items"] == []


@pytest.mark.parametrize("explicit_host", [False, True])
async def test_name_only_project_uses_owned_host_home_and_normal_launch(
    multi_user_client, multi_user_app, db_uri, monkeypatch, explicit_host
):
    app = multi_user_app
    app.state.host_store = HostStore(db_uri)
    foreign_host, own_host = uuid4().hex, uuid4().hex
    _register_online_host(app, foreign_host, BOB)
    _register_online_host(app, own_host, ALICE)
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
            assert project.orchestrator_session_id == frame.session_id
            items = SqlAlchemyConversationStore(db_uri).list_items(frame.session_id).data
            assert len(items) == 1 and items[0].type == "message"
            assert isinstance(items[0].data, MessageData)
            assert items[0].data.role == "assistant"
            launches.append(frame)
            connection.pending_launches[frame.request_id].set_result({"status": "launched"})
        else:
            pytest.fail(f"Unexpected host frame: {frame}")

    monkeypatch.setattr(app.state.host_registry, "send_text", answer_host)
    response = await multi_user_client.post(
        "/v1/projects",
        json={"name": "Home board", "config": {"host_id": own_host} if explicit_host else {}},
        headers=_as_user(ALICE),
    )
    assert response.status_code == 200, response.text
    session_id = response.json()["orchestrator_session_id"]
    conversation = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert conversation.host_id == own_host and conversation.workspace == "/home/owner"
    assert conversation.runner_id and len(launches) == 1


async def test_board_api_lifecycle_and_no_deletion(project_client):
    project = (await project_client.post("/v1/projects", json={"name": "Shipping"})).json()
    base = f"/v1/projects/{project['id']}/tickets"
    created = await project_client.post(base, json={"title": "Ship"})
    assert created.status_code == 201
    ticket = created.json()
    assert ticket["stage_id"] == "open" and ticket["description"] == ""
    path = base + f"/{ticket['id']}"
    response = await project_client.patch(
        path, json={"tags": [{"key": "needs-human", "value": "review"}], "stage_id": "in_progress"}
    )
    assert response.status_code == 200
    assert (await project_client.get(base, params={"stage_id": "in_progress", "limit": 1})).json()[
        "data"
    ][0]["id"] == ticket["id"]
    assert (await project_client.patch(path, json={"stage_id": "done"})).status_code == 400
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
        path, json={"stage_id": "done", "completion_summary": "Tests passed"}
    )
    assert completed.json()["completion_summary"] == "Tests passed"
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
    stages = await project_client.get(base + "/stages")
    assert stages.status_code == 200 and stages.json()["initial_stage_id"] == "open"
    ticket = (await project_client.post(base, json={"title": "Investigate"})).json()
    configuration = {
        "stages": [
            {"id": "triage", "name": "New"},
            {"id": "verifying", "name": "Verifying", "instructions": "Attach recovery evidence"},
            {"id": "resolved", "name": "Resolved"},
        ],
        "moves": {"open": "triage"},
    }
    response = await project_client.put(base + "/stages", json=configuration)
    assert response.status_code == 200, response.text
    assert response.json()["initial_stage_id"] == "triage"
    assert [stage["is_complete"] for stage in response.json()["stages"]] == [False, False, True]
    assert (await project_client.get(base + "/stages", params={"include_usage": True})).json()[
        "occupied_stage_ids"
    ] == ["triage"]
    assert (await project_client.get(f"{base}/{ticket['id']}")).json()["stage_id"] == "triage"
    assert (await project_client.post(base, json={"title": "Next"})).json()["stage_id"] == "triage"
    source = project["orchestrator_session_id"]
    listed = json.loads(await execute_ticket_tool("sys_ticket_list", {}, project_client, source))
    assert listed["stages"] == response.json()["stages"]
    detail = json.loads(
        await execute_ticket_tool(
            "sys_ticket_get", {"ticket_id": ticket["id"]}, project_client, source
        )
    )
    assert detail["stages"][1]["instructions"] == "Attach recovery evidence"
    moved = json.loads(
        await execute_ticket_tool(
            "sys_ticket_update",
            {
                "ticket_id": ticket["id"],
                "stage_id": "verifying",
                "review_summary": "Recovery looks good",
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
    assert completed_without_summary["completion_summary"] is None
    completed = json.loads(
        await execute_ticket_tool(
            "sys_ticket_update",
            {
                "ticket_id": ticket["id"],
                "stage_id": "resolved",
                "completion_summary": "Evidence attached",
            },
            project_client,
            source,
        )
    )
    assert completed["stage_id"] == "resolved"
    assert completed["review_summary"] == "Recovery looks good"
    for invalid in (
        {**configuration, "stages": [configuration["stages"][0]] * 2},
        {**configuration, "stages": []},
    ):
        assert (await project_client.put(base + "/stages", json=invalid)).status_code == 422
    assert (
        await project_client.patch(f"{base}/{ticket['id']}", json={"stage_id": "foreign"})
    ).status_code == 400
    assert (await project_client.get(base, params={"stage_id": "foreign"})).status_code == 400


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


async def test_lifecycle_related_links_and_lead_api(project_client, db_uri):
    project = (await project_client.post("/v1/projects", json={"name": "Lifecycle"})).json()
    base = f"/v1/projects/{project['id']}/tickets"
    ticket = (await project_client.post(base, json={"title": "Feature"})).json()
    related = (await project_client.post(base, json={"title": "Related work"})).json()
    path, other_path = f"{base}/{ticket['id']}", f"{base}/{related['id']}"
    planned = await project_client.patch(
        path, json={"stage_id": "planning", "plan": "## Plan\n\n- Acceptance"}
    )
    assert planned.status_code == 200 and planned.json()["plan"].startswith("## Plan")
    assert (await project_client.patch(path, json={"stage_id": "review"})).status_code == 200
    review = await project_client.patch(
        path, json={"stage_id": "review", "review_summary": "Verify the demo"}
    )
    assert review.status_code == 200
    changed = await project_client.patch(
        path, json={"stage_id": "in_progress", "comment": "Adjust the layout"}
    )
    assert (
        changed.json()["review_summary"] == "Verify the demo"
        and changed.json()["comment_count"] == 1
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
        await project_client.patch(path, json={"lead_session_id": session.id})
    ).status_code == 400
    await project_client.post(path + "/sessions", json={"session_id": session.id})
    assert (await project_client.patch(path, json={"lead_session_id": session.id})).json()[
        "lead_session_id"
    ] == session.id
    assert (await project_client.patch(path, json={"lead_session_id": None})).json()[
        "lead_session_id"
    ] is None
    SqlAlchemyConversationStore(db_uri).update_conversation(session.id, archived=True)
    assert (
        await project_client.patch(path, json={"lead_session_id": session.id})
    ).status_code == 400
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


async def test_related_and_lead_changes_respect_user_and_project_permissions(
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
    response = await client.patch(
        path, json={"lead_session_id": foreign.id}, headers=_as_user(ALICE)
    )
    assert response.status_code in (403, 404)
    outside = store.create_conversation()
    SqlAlchemyPermissionStore(db_uri).grant(ALICE, outside.id, LEVEL_OWNER)
    assert (
        await client.patch(path, json={"lead_session_id": outside.id}, headers=_as_user(ALICE))
    ).status_code == 400
    assert (await client.get(path, headers=_as_user(ALICE))).json()["lead_session_id"] is None


async def test_notification_enqueue_survives_crash_without_duplicate_message(
    project_client, project_app, db_uri, monkeypatch
):
    project = (await project_client.post("/v1/projects", json={"name": "Outbox"})).json()
    session_id = project["orchestrator_session_id"]
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
    session_id = project["orchestrator_session_id"]
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
