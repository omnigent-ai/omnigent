"""Owner settings and runtime identity through signed accounts authentication."""

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
    orchestration_scope,
)
from tests.server.helpers import build_agent_bundle
from tests.server.integration.test_sessions_attribution import _noop_relay_ready
from tests.server.integration.test_sessions_harness_override import _stub_runner_client
from tests.server.test_accounts import _build_accounts_app

pytestmark = pytest.mark.asyncio
SETTING = "/v1/session-orchestration/settings"


@pytest.fixture(autouse=True)
def _enable_links(monkeypatch, request):
    monkeypatch.setenv("OMNIGENT_FEATURES", getattr(request, "param", "session_links"))
    with orchestration_scope(True):
        yield


@pytest.fixture
def db_uri(tmp_path):
    return f"sqlite:///{tmp_path}/test.db"


@pytest.fixture
def accounts_client(tmp_path, monkeypatch):
    yield from _build_accounts_app(tmp_path, monkeypatch, init_admin_password="admin-pw-12345")


@pytest.fixture
def auth_app(accounts_client):
    return accounts_client.app


@pytest.fixture
def credentials(accounts_client, db_uri):
    from omnigent.server.accounts_store import SqlAlchemyAccountStore
    from omnigent.server.passwords import hash_password

    store = SqlAlchemyAccountStore(db_uri)
    result = {}
    for user in ("alice", "bob"):
        store.create_user_with_password(user, hash_password("test-password-123"))
        response = accounts_client.post(
            "/auth/login",
            json={"username": user, "password": "test-password-123", "issue_refresh": True},
        )
        assert response.status_code == 200, response.text
        result[user] = {"Authorization": f"Bearer {response.json()['token']}"}
    accounts_client.cookies.clear()
    return result


async def _create_session(client, headers, *, title="Session"):
    response = await client.post(
        "/v1/sessions",
        headers=headers,
        data={"metadata": json.dumps({"title": title})},
        files={
            "bundle": ("agent.tar.gz", build_agent_bundle(name="test-agent"), "application/gzip")
        },
    )
    assert response.status_code == 201, response.text
    snapshot = await client.get(f"/v1/sessions/{response.json()['session_id']}", headers=headers)
    assert snapshot.status_code == 200, snapshot.text
    return snapshot.json()


def _runner_headers(app, binding):
    from omnigent.db.account_authority import account_authority_scope
    from omnigent.server.accounts_store import SqlAlchemyAccountStore

    service = app.state.session_links
    account = SqlAlchemyAccountStore(service.permissions.storage_location).get_user("alice")
    assert account is not None
    with account_authority_scope("alice", account.account_generation):
        token = service.auth.mint_bound_runner_token("alice", 3600, token_bound_runner_id(binding))
    assert token
    return {"Authorization": f"Bearer {token}", RUNNER_TUNNEL_TOKEN_HEADER: binding}


@pytest.mark.parametrize("_enable_links", ["", "session_links"], indirect=True)
async def test_global_authority_dependency_preserves_websocket_startup(auth_app, accounts_client):
    from fastapi import WebSocket

    @auth_app.websocket("/test-startup")
    async def startup_socket(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"connected": True})
        await websocket.close()

    auth_app.router.routes.insert(0, auth_app.router.routes.pop())
    with accounts_client.websocket_connect("/test-startup") as websocket:
        assert websocket.receive_json() == {"connected": True}


@pytest.mark.parametrize("_enable_links", [""], indirect=True)
async def test_disabled_links_are_unavailable_even_to_session_owners(auth_client, credentials):
    manager = (await _create_session(auth_client, credentials["alice"]))["id"]
    for method, path, body in (
        ("GET", SETTING, None),
        ("PUT", SETTING, {"enabled": True}),
        ("GET", f"/v1/sessions/{manager}/communication-activity", None),
        (
            "POST",
            f"/v1/sessions/{manager}/linked-requests",
            {"target_id": uuid4().hex, "message": "work", "operation_id": "test"},
        ),
    ):
        response = await auth_client.request(method, path, headers=credentials["alice"], json=body)
        assert response.status_code == 404, response.text


async def test_owner_setting_requires_ui_or_cli_credentials(
    auth_app, auth_client, credentials, accounts_client
):
    initial = await auth_client.get(SETTING, headers=credentials["alice"])
    assert initial.json()["enabled"] is False
    enabled = await auth_client.put(SETTING, headers=credentials["alice"], json={"enabled": True})
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["enabled"] is True
    # This is Alice's personal switch, even if Bob can edit all of her sessions.
    assert (await auth_client.get(SETTING, headers=credentials["bob"])).json()["enabled"] is False
    stale = await auth_client.put(SETTING, headers=credentials["alice"], json={"enabled": False})
    assert stale.status_code == 409, stale.text
    denied = await auth_client.put(
        SETTING,
        headers=_runner_headers(auth_app, "runner"),
        json={"enabled": False, "expected_version": enabled.json()["version"]},
    )
    assert denied.status_code == 403, denied.text
    login = accounts_client.post(
        "/auth/login", json={"username": "alice", "password": "test-password-123"}
    )
    assert login.status_code == 200, login.text
    disabled = accounts_client.put(
        SETTING, json={"enabled": False, "expected_version": enabled.json()["version"]}
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False


async def test_runtime_reads_use_consumed_requester(
    auth_app, auth_client, credentials, monkeypatch
):
    from omnigent.server.routes import sessions as sessions_mod

    manager = (await _create_session(auth_client, credentials["alice"], title="Manager"))["id"]
    worker = (await _create_session(auth_client, credentials["alice"], title="Worker"))["id"]
    service = auth_app.state.session_links
    runner_token = "manager-test-runner"
    service.conversations.set_runner_id(manager, token_bound_runner_id(runner_token))
    service.permissions.grant("bob", manager, 2)
    captured = _stub_runner_client(monkeypatch)
    monkeypatch.setattr(sessions_mod, "_ensure_runner_relay_ready", _noop_relay_ready)
    sent = await auth_client.post(
        f"/v1/sessions/{manager}/events",
        headers=credentials["bob"],
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "check worker"}]},
        },
    )
    assert sent.status_code == 202, sent.text
    body = next(body for path, body in captured["posts"] if path.endswith("/events"))
    headers = {
        **_runner_headers(auth_app, runner_token),
        SOURCE_SESSION_HEADER: manager,
        REQUEST_AUTHORITY_HEADER: json.dumps(RequestAuthority.from_input(body).to_wire()),
    }
    private_read = await auth_client.get(f"/v1/sessions/{worker}", headers=headers)
    assert private_read.status_code in (403, 404), private_read.text
    service.permissions.grant("bob", worker, 2)
    permitted = await auth_client.get(f"/v1/sessions/{worker}", headers=headers)
    assert permitted.status_code == 200, permitted.text
    enabled = await auth_client.put(SETTING, headers=credentials["alice"], json={"enabled": True})
    assert enabled.status_code == 200, enabled.text
    # Shared editor access is sufficient to read, never to borrow owner orchestration.
    denied = await auth_client.post(
        f"/v1/sessions/{manager}/linked-requests",
        headers=headers,
        json={"target_id": worker, "message": "work", "operation_id": "editor"},
    )
    assert denied.status_code == 403, denied.text
    service.permissions.revoke("bob", worker)
    assert (await auth_client.get(f"/v1/sessions/{worker}", headers=headers)).status_code in (
        403,
        404,
    )


@pytest.mark.parametrize("delegated", [False, True], ids=["human", "parent-tool"])
async def test_child_kickoff_retains_the_consumed_requester(
    auth_app, auth_client, credentials, monkeypatch, delegated
):
    from omnigent.server.routes import sessions as sessions_mod

    parent = await _create_session(auth_client, credentials["alice"])
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
    headers = credentials["bob"]
    sent = await auth_client.post(
        f"/v1/sessions/{parent['id']}/events", headers=headers, json=message
    )
    assert sent.status_code == 202, sent.text
    parent_body = next(body for path, body in captured["posts"] if path.endswith("/events"))
    if delegated:
        headers = {
            **_runner_headers(auth_app, runner_token),
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
async def test_linked_delivery_receipt_controls_recovery(
    auth_client, auth_app, credentials, db_uri, accepted, fork
):
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

    manager = (await _create_session(auth_client, credentials["alice"]))["id"]
    worker = (await _create_session(auth_client, credentials["alice"]))["id"]
    conversations = SqlAlchemyConversationStore(db_uri)
    links = SessionLinkStore(db_uri)
    endpoints = []
    for session_id in (manager, worker):
        conversations.set_runner_id(session_id, "test-runner")
        endpoints.append(Endpoint(session_id, session_id, "test-runner"))
    source, target = endpoints
    from omnigent.server.accounts_store import SqlAlchemyAccountStore

    account = SqlAlchemyAccountStore(db_uri).get_user("alice")
    assert account is not None
    generation = account.account_generation
    links.set_enabled("alice", generation, True, None)
    input_id = links.capture_input(source, "alice", generation)
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
    links.set_enabled("alice", generation, False, links.settings("alice")["version"])
    if fork:
        from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

        worker = conversations.fork_conversation(worker).id
        SqlAlchemyPermissionStore(db_uri).grant("alice", worker, 4)
    response = await auth_client.get(f"/v1/sessions/{worker}/items", headers=credentials["alice"])
    assert response.status_code == 200, response.text
    history = response.json()["data"]
    assert len(history) == 1
    assert history[0]["linked_input_accepted"] is accepted
    assert history[0]["content"] == event.data["content"]
    auth_client.headers.update(credentials["alice"])
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
