"""Human approvals and runtime principal resolution through the real server middleware."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.session_authority import (
    INPUT_AUTHORITY_FIELD,
    REQUEST_AUTHORITY_HEADER,
    SOURCE_SESSION_HEADER,
    RequestAuthority,
)
from tests.server.integration.test_sessions_attribution import _noop_relay_ready
from tests.server.integration.test_sessions_harness_override import _stub_runner_client
from tests.server.integration.test_sessions_permissions import (
    _create_session_as,
)
from tests.server.integration.test_sessions_permissions import (
    auth_app as auth_app,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _enable_links(monkeypatch, request):
    monkeypatch.setenv("OMNIGENT_FEATURES", getattr(request, "param", "session_links"))


@pytest.mark.parametrize("_enable_links", ["", "session_links"], indirect=True)
async def test_global_authority_dependency_preserves_websocket_startup(auth_app):
    from fastapi import WebSocket
    from fastapi.testclient import TestClient

    @auth_app.websocket("/test-startup")
    async def startup_socket(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"connected": True})
        await websocket.close()

    auth_app.router.routes.insert(0, auth_app.router.routes.pop())
    with TestClient(auth_app).websocket_connect(
        "/test-startup", headers={"X-Forwarded-Email": "alice"}
    ) as websocket:
        assert websocket.receive_json() == {"connected": True}


@pytest.mark.parametrize("_enable_links", [""], indirect=True)
async def test_disabled_links_are_unavailable_even_to_session_owners(auth_client):
    manager = (await _create_session_as(auth_client, "", "alice"))["id"]
    worker = (await _create_session_as(auth_client, "", "alice"))["id"]
    headers = {"X-Forwarded-Email": "alice"}
    for method, path, body in (
        ("GET", f"/v1/sessions/{manager}/links", None),
        ("POST", f"/v1/sessions/{manager}/links", {"target_id": worker}),
        ("DELETE", f"/v1/sessions/{manager}/links/{worker}", None),
        (
            "POST",
            f"/v1/sessions/{manager}/linked-requests",
            {"target_id": worker, "message": "work", "operation_id": "test"},
        ),
        ("GET", f"/v1/sessions/{manager}/linked-requests/{uuid4().hex}", None),
    ):
        response = await auth_client.request(method, path, headers=headers, json=body)
        assert response.status_code == 404, response.text


async def test_human_approvals_require_both_owners_and_directions_are_independent(
    auth_app, auth_client
):
    manager = (await _create_session_as(auth_client, "", "alice", title="Manager"))["id"]
    worker = (await _create_session_as(auth_client, "", "alice", title="Worker"))["id"]
    service = auth_app.state.session_links
    service.permissions.ensure_user("bob")
    for session_id in (manager, worker):
        service.permissions.grant("bob", session_id, 3)
    endpoint = f"/v1/sessions/{manager}/links"
    denied = await auth_client.post(
        endpoint, headers={"X-Forwarded-Email": "bob"}, json={"target_id": worker}
    )
    assert denied.status_code == 403, denied.text
    for source, target in ((manager, worker), (worker, manager)):
        approved = await auth_client.post(
            f"/v1/sessions/{source}/links",
            headers={"X-Forwarded-Email": "alice"},
            json={"target_id": target},
        )
        assert approved.status_code == 201, approved.text
    listed = await auth_client.get(endpoint, headers={"X-Forwarded-Email": "bob"})
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"][0]["can_disconnect"] is False
    disconnected = await auth_client.delete(
        f"{endpoint}/{worker}", headers={"X-Forwarded-Email": "alice"}
    )
    assert disconnected.status_code == 204, disconnected.text
    assert (await auth_client.get(endpoint, headers={"X-Forwarded-Email": "alice"})).json()[
        "data"
    ] == []
    reverse = await auth_client.get(
        f"/v1/sessions/{worker}/links", headers={"X-Forwarded-Email": "alice"}
    )
    assert reverse.json()["data"][0]["target_id"] == manager


async def test_runtime_reads_never_fall_back_to_owner_and_inputs_cannot_be_forged(
    auth_app, auth_client
):
    manager = (await _create_session_as(auth_client, "", "alice", title="Manager"))["id"]
    worker = (await _create_session_as(auth_client, "", "alice", title="Worker"))["id"]
    service = auth_app.state.session_links
    runner_token = "manager-test-runner"
    service.conversations.set_runner_id(manager, token_bound_runner_id(runner_token))
    service.permissions.ensure_user("bob")
    service.permissions.grant("bob", manager, 2)
    _, endpoint = service.endpoint(manager)
    bob_input = service.store.capture_input(endpoint, "bob", None)
    headers = {
        "X-Forwarded-Email": "alice",
        RUNNER_TUNNEL_TOKEN_HEADER: runner_token,
        SOURCE_SESSION_HEADER: manager,
        REQUEST_AUTHORITY_HEADER: json.dumps(RequestAuthority((bob_input,)).to_wire()),
    }
    private_read = await auth_client.get(f"/v1/sessions/{worker}", headers=headers)
    assert private_read.status_code in (403, 404), private_read.text
    approved = await auth_client.post(
        f"/v1/sessions/{manager}/links",
        headers={"X-Forwarded-Email": "alice"},
        json={"target_id": worker},
    )
    assert approved.status_code == 201, approved.text
    for mutation in (
        {SOURCE_SESSION_HEADER: worker},
        {RUNNER_TUNNEL_TOKEN_HEADER: "another-runner"},
        {REQUEST_AUTHORITY_HEADER: json.dumps(RequestAuthority((uuid4().hex,)).to_wire())},
    ):
        response = await auth_client.get(
            f"/v1/sessions/{manager}/links", headers={**headers, **mutation}
        )
        assert response.status_code in (403, 404), response.text
    agent_approval = await auth_client.post(
        f"/v1/sessions/{manager}/links", headers=headers, json={"target_id": worker}
    )
    assert agent_approval.status_code == 403
    service.permissions.grant("bob", worker, 2)
    permitted = await auth_client.get(f"/v1/sessions/{worker}", headers=headers)
    assert permitted.status_code == 200, permitted.text
    service.permissions.revoke("bob", worker)
    assert (await auth_client.get(f"/v1/sessions/{worker}", headers=headers)).status_code in (
        403,
        404,
    )
    owner_input = service.store.capture_input(endpoint, "alice", None)
    mixed = {
        **headers,
        REQUEST_AUTHORITY_HEADER: json.dumps(RequestAuthority((bob_input, owner_input)).to_wire()),
    }
    assert (
        await auth_client.get(f"/v1/sessions/{manager}/links", headers=mixed)
    ).status_code == 403


@pytest.mark.parametrize("caller", ["alice", "bob"])
async def test_owner_can_remove_stale_link_without_reading_other_endpoint(
    auth_app, auth_client, caller
):
    manager = (await _create_session_as(auth_client, "", "alice", title="Private manager"))["id"]
    worker = (await _create_session_as(auth_client, "", "alice", title="Private worker"))["id"]
    for source, target in ((manager, worker), (worker, manager)):
        approved = await auth_client.post(
            f"/v1/sessions/{source}/links",
            headers={"X-Forwarded-Email": "alice"},
            json={"target_id": target},
        )
        assert approved.status_code == 201, approved.text
    permissions = auth_app.state.session_links.permissions
    permissions.ensure_user("bob")
    permissions.grant("bob", worker, 4)
    permissions.revoke("alice", worker)
    permissions.ensure_user("reader")
    permissions.grant("reader", manager, 1)
    invisible = await auth_client.get(
        f"/v1/sessions/{manager}/links", headers={"X-Forwarded-Email": "reader"}
    )
    assert invisible.json()["data"] == []
    own_session, other_session = (manager, worker) if caller == "alice" else (worker, manager)
    headers = {"X-Forwarded-Email": caller}
    listed = await auth_client.get(
        f"/v1/sessions/{own_session}/links", headers=headers, params={"incoming": caller == "bob"}
    )
    assert listed.status_code == 200, listed.text
    (link,) = listed.json()["data"]
    hidden_title = "target_title" if caller == "alice" else "source_title"
    assert link[hidden_title] == "Restricted session"
    assert link["can_disconnect"] is True
    assert link["active"] is False
    assert (
        await auth_client.get(f"/v1/sessions/{other_session}", headers=headers)
    ).status_code in (403, 404)
    disconnected = await auth_client.delete(
        f"/v1/sessions/{manager}/links/{worker}", headers=headers
    )
    assert disconnected.status_code == 204, disconnected.text
    assert (
        await auth_client.get(f"/v1/sessions/{other_session}", headers=headers)
    ).status_code in (403, 404)
    remaining = await auth_client.get(
        f"/v1/sessions/{own_session}/links",
        headers=headers,
        params={"incoming": caller == "alice"},
    )
    assert remaining.json()["data"][0]["source_id"] == worker
    denied = await auth_client.post(
        f"/v1/sessions/{manager}/links", headers=headers, json={"target_id": worker}
    )
    assert denied.status_code == 403, denied.text


@pytest.mark.parametrize("delegated", [False, True], ids=["human", "parent-tool"])
async def test_child_kickoff_retains_the_consumed_requester(
    auth_app, auth_client, monkeypatch, delegated
):
    from omnigent.server.routes import sessions as sessions_mod

    parent = await _create_session_as(auth_client, "", "alice")
    service = auth_app.state.session_links
    runner_token = "child-kickoff-runner"
    service.conversations.set_runner_id(parent["id"], token_bound_runner_id(runner_token))
    service.permissions.ensure_user("bob")
    service.permissions.grant("bob", parent["id"], 2)
    captured = _stub_runner_client(monkeypatch)
    monkeypatch.setattr(sessions_mod, "_ensure_runner_relay_ready", _noop_relay_ready)
    message = {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": "start a child"}]},
    }
    headers = {"X-Forwarded-Email": "bob"}
    sent = await auth_client.post(
        f"/v1/sessions/{parent['id']}/events", headers=headers, json=message
    )
    assert sent.status_code == 202, sent.text
    parent_body = next(body for path, body in captured["posts"] if path.endswith("/events"))
    if delegated:
        headers = {
            "X-Forwarded-Email": "alice",
            RUNNER_TUNNEL_TOKEN_HEADER: runner_token,
            SOURCE_SESSION_HEADER: parent["id"],
            REQUEST_AUTHORITY_HEADER: json.dumps(
                RequestAuthority.from_input(parent_body).to_wire()
            ),
        }
    captured["posts"].clear()
    created = await auth_client.post(
        "/v1/sessions",
        headers=headers,
        json={
            "agent_id": parent["agent_id"],
            "parent_session_id": parent["id"],
            "initial_items": [message],
        },
    )
    assert created.status_code == 201, created.text
    child_id = created.json()["id"]
    kickoff = next(
        body for path, body in captured["posts"] if path == f"/v1/sessions/{child_id}/events"
    )
    assert kickoff[INPUT_AUTHORITY_FIELD] != parent_body[INPUT_AUTHORITY_FIELD]
    _, child = service.endpoint(child_id)
    assert (
        service.store.resolve_inputs(child, RequestAuthority.from_input(kickoff)).requester
        == "bob"
    )
    snapshot = next(body for path, body in captured["posts"] if path == "/v1/sessions")
    assert snapshot["session_init"]["snapshot"]["request_authority_required"] is True


@pytest.mark.parametrize("_enable_links", ["", "session_links"], indirect=True)
@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("fork", [False, True], ids=["original", "fork"])
async def test_linked_delivery_receipt_controls_recovery(auth_client, db_uri, accepted, fork):
    from omnigent.errors import OmnigentError
    from omnigent.harnesses.claude_native.main import _fetch_all_session_items_for_claude_resume
    from omnigent.harnesses.codex_native.main import _fetch_all_session_items_for_codex_resume
    from omnigent.harnesses.pi_native.resume import fetch_all_session_items_for_pi_resume
    from omnigent.runner.native.orchestration import _render_opencode_transcript_text
    from omnigent.server.routes._sessions.orchestration import _forward_event_to_runner
    from omnigent.server.schemas import SessionEventInput
    from omnigent.session_authority import LINKED_REQUEST_FIELD
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.session_link_store import Endpoint, SessionLinkStore, payload_digest
    from tests.runner.conftest import _runner_client
    from tests.runner.test_app_sessions_native_workflow_messages import _build_recovery_app

    manager = (await _create_session_as(auth_client, "", "alice"))["id"]
    worker = (await _create_session_as(auth_client, "", "alice"))["id"]
    conversations = SqlAlchemyConversationStore(db_uri)
    links = SessionLinkStore(db_uri)
    endpoints = []
    for session_id in (manager, worker):
        conversations.set_runner_id(session_id, "test-runner")
        endpoints.append(Endpoint(session_id, session_id, "test-runner"))
    source, target = endpoints
    links.connect(source, target, "alice", None)
    input_id = links.capture_input(source, "alice", None)
    identity = links.resolve_inputs(source, RequestAuthority((input_id,)))
    receipt, _ = links.create_request(source, target, identity, "request", payload_digest("work"))
    assert links.begin_handoff(receipt["id"])
    assert links.accept_request(receipt["id"], source, target)
    event = SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": "work"}],
            "stable_id": receipt["id"],
        },
    )
    event._trusted_transport[LINKED_REQUEST_FIELD] = receipt["id"]
    async with httpx.AsyncClient(
        base_url="http://runner",
        transport=httpx.MockTransport(lambda _: httpx.Response(202 if accepted else 409, json={})),
    ) as runner:
        if accepted:
            await _forward_event_to_runner(
                worker, conversations.get_conversation(worker), event, conversations, runner
            )
        else:
            with pytest.raises(OmnigentError, match="Runner rejected"):
                await _forward_event_to_runner(
                    worker, conversations.get_conversation(worker), event, conversations, runner
                )
    links.confirm_handoff(receipt["id"], accepted=accepted)
    links.disconnect(source, target, "alice", None)
    if fork:
        from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

        worker = conversations.fork_conversation(worker).id
        SqlAlchemyPermissionStore(db_uri).grant("alice", worker, 4)
    response = await auth_client.get(
        f"/v1/sessions/{worker}/items", headers={"X-Forwarded-Email": "alice"}
    )
    assert response.status_code == 200, response.text
    history = response.json()["data"]
    assert len(history) == 1
    assert history[0]["linked_input_accepted"] is accepted
    assert history[0]["content"] == event.data["content"]
    auth_client.headers["X-Forwarded-Email"] = "alice"
    for load_native_history in (
        _fetch_all_session_items_for_claude_resume,
        _fetch_all_session_items_for_codex_resume,
        fetch_all_session_items_for_pi_resume,
    ):
        assert len(await load_native_history(auth_client, worker)) == int(accepted)
    assert bool(_render_opencode_transcript_text(history)) is accepted
    app, _, harness = _build_recovery_app(history)
    async with _runner_client(app) as client:
        result = await client.post(
            "/v1/sessions", json={"session_id": worker, "agent_id": uuid4().hex}
        )
        assert result.status_code == 201, result.text
        assert result.json()["status"] == ("running" if accepted else "idle")
        if accepted:
            for _ in range(100):
                if harness.posted_bodies:
                    break
                await asyncio.sleep(0.01)
    assert len(harness.posted_bodies) == int(accepted)
