"""Owner settings and runtime identity through signed accounts authentication."""

import json
from uuid import uuid4

import pytest

from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.session_authority import (
    INPUT_AUTHORITY_FIELD,
    REQUEST_AUTHORITY_HEADER,
    SOURCE_SESSION_HEADER,
    RequestAuthority,
)
from tests.server.helpers import build_agent_bundle
from tests.server.integration.test_sessions_attribution import _noop_relay_ready
from tests.server.integration.test_sessions_harness_override import _stub_runner_client
from tests.server.test_accounts import _build_accounts_app

pytestmark = pytest.mark.asyncio
SETTING = "/v1/session-communication/settings"


@pytest.fixture(autouse=True)
def _enable_links(monkeypatch, request):
    monkeypatch.setenv("OMNIGENT_FEATURES", getattr(request, "param", "session_messaging"))
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

    service = app.state.session_messaging
    account = SqlAlchemyAccountStore(service.permissions.storage_location).get_user("alice")
    assert account is not None
    with account_authority_scope("alice", account.account_generation):
        token = service.auth.mint_bound_runner_token("alice", 3600, token_bound_runner_id(binding))
    assert token
    return {"Authorization": f"Bearer {token}", RUNNER_TUNNEL_TOKEN_HEADER: binding}


@pytest.fixture
async def messaging(auth_app, auth_client, credentials, monkeypatch):
    from omnigent.server.routes import sessions as sessions_mod

    service = auth_app.state.session_messaging
    manager = (await _create_session(auth_client, credentials["alice"]))["id"]
    worker = (await _create_session(auth_client, credentials["alice"]))["id"]
    for session_id, token in ((manager, "manager-runner"), (worker, "worker-runner")):
        service.conversations.set_runner_id(session_id, token_bound_runner_id(token))
    captured = _stub_runner_client(monkeypatch)
    monkeypatch.setattr(sessions_mod, "_ensure_runner_relay_ready", _noop_relay_ready)

    async def input_headers(user="alice", session_id=manager, token="manager-runner"):
        result = await auth_client.post(
            f"/v1/sessions/{session_id}/events",
            headers=credentials[user],
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "coordinate"}],
                },
                "_trusted_transport": {INPUT_AUTHORITY_FIELD: uuid4().hex},
            },
        )
        assert result.status_code == 202, result.text
        body = captured["body"]
        authority = RequestAuthority.from_input(body)
        assert authority.verified
        return {
            **_runner_headers(auth_app, token),
            SOURCE_SESSION_HEADER: session_id,
            REQUEST_AUTHORITY_HEADER: json.dumps(authority.to_wire()),
        }

    return service, manager, worker, captured, input_headers


async def test_setting_requires_owner_credentials_and_preserves_version(
    auth_app, auth_client, credentials
):
    initial = await auth_client.get(SETTING, headers=credentials["alice"])
    assert initial.json() == {"enabled": False, "version": None}
    changed = await auth_client.put(SETTING, headers=credentials["alice"], json={"enabled": True})
    assert changed.status_code == 200, changed.text
    assert (await auth_client.get(SETTING, headers=credentials["bob"])).json()["enabled"] is False
    for headers, status in (
        (credentials["alice"], 409),
        (_runner_headers(auth_app, "runner"), 403),
    ):
        response = await auth_client.put(SETTING, headers=headers, json={"enabled": False})
        assert response.status_code == status, response.text
    assert (await auth_client.get(SETTING, headers=credentials["alice"])).json() == changed.json()


async def test_header_auth_requires_a_credential_provenance_integration(auth_app):
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.server.session_messaging import SessionMessaging

    service = auth_app.state.session_messaging
    with pytest.raises(ValueError, match="credential provenance"):
        SessionMessaging(
            service.conversations, service.permissions, UnifiedAuthProvider(source="header")
        )


async def test_off_is_rechecked_after_runner_readiness(
    auth_client, credentials, messaging, monkeypatch
):
    from omnigent.server.routes import sessions as sessions_mod

    service, _manager, worker, captured, input_headers = messaging
    enabled = await auth_client.put(SETTING, headers=credentials["alice"], json={"enabled": True})
    headers = await input_headers()
    captured.clear()

    async def turn_off(*args, **kwargs):
        service.setting("alice", enabled=False, expected_version=enabled.json()["version"])

    monkeypatch.setattr(sessions_mod, "_ensure_runner_relay_ready", turn_off)
    denied = await auth_client.post(
        f"/v1/sessions/{worker}/events",
        headers=headers,
        json={"type": "session_message", "data": {"message": "must not deliver"}},
    )
    assert denied.status_code == 403, denied.text
    assert not captured


async def test_send_explicit_reply_activity_and_off(auth_app, auth_client, credentials, messaging):
    service, manager, worker, captured, input_headers = messaging
    headers = await input_headers()
    payload = {"type": "session_message", "data": {"message": "Do the work"}}
    path = f"/v1/sessions/{worker}/events"
    assert (await auth_client.post(path, headers=headers, json=payload)).status_code == 403
    enabled = await auth_client.put(SETTING, headers=credentials["alice"], json={"enabled": True})
    sent = await auth_client.post(path, headers=headers, json=payload)
    assert sent.status_code == 202, sent.text
    assert service.conversations.get_conversation(worker).parent_conversation_id is None
    worker_authority = RequestAuthority.from_input(captured["body"])
    assert worker_authority.verified
    worker_headers = {
        **_runner_headers(auth_app, "worker-runner"),
        SOURCE_SESSION_HEADER: worker,
        REQUEST_AUTHORITY_HEADER: json.dumps(worker_authority.to_wire()),
    }
    replied = await auth_client.post(
        f"/v1/sessions/{manager}/events",
        headers=worker_headers,
        json={"type": "session_message", "data": {"message": "Done"}},
    )
    assert replied.status_code == 202, replied.text
    activity = await auth_client.get(
        f"/v1/sessions/{manager}/communication-activity", headers=credentials["alice"]
    )
    assert activity.json()["data"][0]["target_id"] == worker
    service.permissions.grant("bob", manager, 2)
    hidden = await auth_client.get(
        f"/v1/sessions/{manager}/communication-activity", headers=credentials["bob"]
    )
    assert hidden.json()["data"] == []
    disabled = await auth_client.put(
        SETTING,
        headers=credentials["alice"],
        json={"enabled": False, "expected_version": enabled.json()["version"]},
    )
    assert disabled.status_code == 200
    assert (await auth_client.post(path, headers=headers, json=payload)).status_code == 403
    assert (
        await auth_client.post(
            f"/v1/sessions/{manager}/events", headers=worker_headers, json=payload
        )
    ).status_code == 403


@pytest.mark.parametrize(
    "fault",
    [
        "editor",
        "missing",
        "forged",
        "runner",
        "source",
        "lost-owner",
        "mixed",
        "workspace",
        "restart",
        "fork",
        "recreated-account",
    ],
)
async def test_authority_cannot_be_borrowed(auth_app, auth_client, credentials, messaging, fault):
    from omnigent.db.db_models import workspace_scope

    service, manager, worker, captured, input_headers = messaging
    service.permissions.grant("bob", manager, 2)
    service.permissions.grant("bob", worker, 2)
    await auth_client.put(SETTING, headers=credentials["alice"], json={"enabled": True})
    headers = await input_headers("bob" if fault == "editor" else "alice")
    if fault == "missing":
        headers.pop(REQUEST_AUTHORITY_HEADER)
    elif fault == "forged":
        value = json.loads(headers[REQUEST_AUTHORITY_HEADER])
        value["proofs"] = ["forged"]
        headers[REQUEST_AUTHORITY_HEADER] = json.dumps(value)
    elif fault == "runner":
        headers[RUNNER_TUNNEL_TOKEN_HEADER] = "other-runner"
    elif fault == "source":
        headers[SOURCE_SESSION_HEADER] = worker
    elif fault == "lost-owner":
        service.permissions.revoke("alice", worker)
        service.permissions.grant("alice", worker, 2)
    elif fault == "mixed":
        owner = RequestAuthority.from_wire(json.loads(headers[REQUEST_AUTHORITY_HEADER]))
        editor = RequestAuthority.from_wire(
            json.loads((await input_headers("bob"))[REQUEST_AUTHORITY_HEADER])
        )
        headers[REQUEST_AUTHORITY_HEADER] = json.dumps(owner.consume(editor).to_wire())
    elif fault == "workspace":
        value = json.loads(headers[REQUEST_AUTHORITY_HEADER])
        claims = service.verify(value["proofs"][0])
        with workspace_scope(234):
            value["proofs"] = [service.sign(claims)]
        headers[REQUEST_AUTHORITY_HEADER] = json.dumps(value)
    elif fault == "restart":
        service.conversations.set_runner_id(manager, token_bound_runner_id("replacement-runner"))
        headers.update(_runner_headers(auth_app, "replacement-runner"))
    elif fault == "fork":
        fork = await auth_client.post(
            f"/v1/sessions/{manager}/fork", headers=credentials["alice"], json={}
        )
        assert fork.status_code == 201, fork.text
        fork_id = fork.json()["id"]
        service.conversations.set_runner_id(fork_id, token_bound_runner_id("manager-runner"))
        headers[SOURCE_SESSION_HEADER] = fork_id
    elif fault == "recreated-account":
        from omnigent.server.accounts_store import SqlAlchemyAccountStore
        from omnigent.server.passwords import hash_password

        accounts = SqlAlchemyAccountStore(service.permissions.storage_location)
        assert accounts.delete_user("alice")
        accounts.create_user_with_password("alice", hash_password("replacement-password"))
        for session_id in (manager, worker):
            service.permissions.grant("alice", session_id, 4)
        service.setting("alice", enabled=True)
        headers.update(_runner_headers(auth_app, "manager-runner"))
    denied = await auth_client.post(
        f"/v1/sessions/{worker}/events",
        headers=headers,
        json={"type": "session_message", "data": {"message": "must not deliver"}},
    )
    assert denied.status_code == (401 if fault == "recreated-account" else 403), denied.text
    assert "must not deliver" not in json.dumps(captured)


async def test_chain_retains_origin_and_rechecks_leaf_owner(
    auth_app, auth_client, credentials, messaging
):
    service, _manager, worker, captured, input_headers = messaging
    leaf = (await _create_session(auth_client, credentials["alice"]))["id"]
    service.conversations.set_runner_id(leaf, token_bound_runner_id("leaf-runner"))
    await auth_client.put(SETTING, headers=credentials["alice"], json={"enabled": True})
    headers = await input_headers()
    origin = service.verify(json.loads(headers[REQUEST_AUTHORITY_HEADER])["proofs"][0])
    sent = await auth_client.post(
        f"/v1/sessions/{worker}/events",
        headers=headers,
        json={"type": "session_message", "data": {"message": "Coordinate the next worker"}},
    )
    assert sent.status_code == 202, sent.text
    worker_authority = RequestAuthority.from_input(captured["body"])
    worker_headers = {
        **_runner_headers(auth_app, "worker-runner"),
        SOURCE_SESSION_HEADER: worker,
        REQUEST_AUTHORITY_HEADER: json.dumps(worker_authority.to_wire()),
    }
    payload = {"type": "session_message", "data": {"message": "Continue the work"}}
    delivered = await auth_client.post(
        f"/v1/sessions/{leaf}/events", headers=worker_headers, json=payload
    )
    assert delivered.status_code == 202, delivered.text
    leaf_authority = RequestAuthority.from_input(captured["body"])
    inherited = service.verify(leaf_authority.proofs[0])
    assert inherited["requester_id"] == origin["requester_id"] == "alice"
    assert inherited["origin_input_id"] == origin["origin_input_id"]
    assert inherited["hop_count"] == 2
    service.permissions.revoke("alice", leaf)
    service.permissions.grant("alice", leaf, 2)
    captured.clear()
    denied = await auth_client.post(
        f"/v1/sessions/{leaf}/events", headers=worker_headers, json=payload
    )
    assert denied.status_code == 403, denied.text
    assert not captured
