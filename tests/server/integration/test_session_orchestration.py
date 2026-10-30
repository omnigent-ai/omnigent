"""Runner dispatch through authenticated routes and real ownership/sharing stores."""

import json
from uuid import uuid4

import pytest

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER, RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.runner.tool_dispatch import execute_tool
from omnigent.runtime.requester import (
    REQUESTER_FIELD,
    REQUESTER_HEADER,
    ExecutionRequester,
    InputRequester,
)
from omnigent.server.auth import LEVEL_EDIT, LEVEL_MANAGE, LEVEL_READ
from omnigent.server.feature_flags import FeatureFlags
from omnigent.stores.conversation_store import (
    CREATED_BY_SESSION_LABEL_KEY,
    SCHEDULED_TASK_ORIGIN_LABEL_KEY,
    SIDE_CHAT_LABEL_KEY,
    SIDE_CHAT_SOURCE_LABEL_KEY,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.integration.test_managed_startup_inputs import auth_app as auth_app
from tests.server.integration.test_sessions_attribution import _bind_runner
from tests.server.integration.test_sessions_harness_override import _stub_late_runner_bind
from tests.server.integration.test_sessions_permissions import _create_session_as
from tests.server.integration.test_sessions_subagent_context import (
    two_replica_stack as two_replica_stack,
)

pytestmark = pytest.mark.asyncio


class LegacyPermissionStore(SqlAlchemyPermissionStore):
    """Reuse the SQL backend for the old interface, leaving the new check unimplemented."""

    has_other_write_grants = PermissionStore.has_other_write_grants


@pytest.fixture
def startup_permissions(db_uri, request):
    return getattr(request, "param", SqlAlchemyPermissionStore)(db_uri)


@pytest.fixture(autouse=True)
def orchestration_environment(monkeypatch, request):
    features = getattr(request, "param", None)
    if features is None:
        monkeypatch.delenv("OMNIGENT_FEATURES", raising=False)
    else:
        monkeypatch.setenv("OMNIGENT_FEATURES", features)
    return features


@pytest.fixture
async def peers(auth_client, auth_app, db_uri, monkeypatch):
    source = await _create_session_as(auth_client, "", "alice")
    target = await _create_session_as(auth_client, "", "alice")
    headers = {"X-Forwarded-Email": "alice", **_bind_runner(db_uri, source["id"])}
    auth_client.headers.update(headers)
    captured = _stub_late_runner_bind(monkeypatch)
    return source, target, captured


def _requester(user_id="alice", *, delegation_policy="requester"):
    return (
        ExecutionRequester(InputRequester(user_id=user_id))
        .snapshot()
        .model_copy(update={"delegation_policy": delegation_policy})
    )


async def _send(client, source, target, message="Please inspect the PR", *, requester="owner"):
    if requester == "owner":
        requester = _requester()
    return json.loads(
        await execute_tool(
            tool_name="sys_session_message",
            arguments=json.dumps({"session_id": target["id"], "message": message}),
            server_client=client,
            conversation_id=source["id"],
            requester=requester,
        )
    )


@pytest.mark.parametrize("policy_denied", [False, True])
@pytest.mark.parametrize("delegation_policy", ["requester", "no_edit_sharing"])
@pytest.mark.parametrize(
    "startup_permissions", [SqlAlchemyPermissionStore, LegacyPermissionStore], indirect=True
)
@pytest.mark.parametrize(
    "orchestration_environment",
    [None, "", "usage_page", "-session_orchestration"],
    indirect=True,
)
async def test_private_sender_can_message_shared_receiver(
    auth_client,
    peers,
    db_uri,
    monkeypatch,
    policy_denied,
    orchestration_environment,
    delegation_policy,
    startup_permissions,
):
    from omnigent.server.routes.sessions import routes_events

    source, target, captured = peers
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")
    permissions.grant("bob", target["id"], LEVEL_READ)
    if policy_denied:

        async def deny_task(*args, **kwargs):
            return {"verdict": "deny", "reason": "Task needs approval"}

        monkeypatch.setattr(routes_events, "_evaluate_input_policy", deny_task)
    permissions.ensure_user("__public__")
    permissions.grant("__public__", source["id"], LEVEL_READ)
    requester = _requester(
        None if delegation_policy == "no_edit_sharing" else "alice",
        delegation_policy=delegation_policy,
    )
    result = await _send(auth_client, source, target, requester=requester)
    if orchestration_environment == "-session_orchestration":
        assert "Session orchestration is disabled" in result["detail"]
        assert not captured.get("posts")
        return
    if (
        isinstance(startup_permissions, LegacyPermissionStore)
        and delegation_policy == "no_edit_sharing"
    ):
        assert result["error"] == "Session message refused", result
        assert json.loads(result["detail"])["error"]["code"] == "forbidden"
        assert "Implement PermissionStore.has_other_write_grants" in result["detail"]
        assert not captured.get("posts")
        store = SqlAlchemyConversationStore(db_uri)
        assert store.list_pending_inputs(target["id"]) == []
        before = {session.id for session in store.list_conversations().data}
        for route in ("/v1/sessions", "/v1/sessions/orchestrated"):
            response = await auth_client.post(
                route,
                headers={
                    ORCHESTRATION_SOURCE_HEADER: source["id"],
                    REQUESTER_HEADER: requester.model_dump_json(),
                },
                json={"agent_id": source["agent_id"]},
            )
            assert response.status_code == 403, response.text
            assert "Implement PermissionStore.has_other_write_grants" in response.text
        assert {session.id for session in store.list_conversations().data} == before
        assert not captured.get("posts")
        return
    if policy_denied:
        assert "Task needs approval" in result["error"]
        assert result["denied"] is True and result["queued"] is False
        assert not captured.get("posts")
        return
    assert result.get("queued") is True, result
    assert InputRequester.from_body(captured["body"]) == requester.origin.model_copy(
        update={"input_id": result["item_id"]}
    )
    assert "Please inspect the PR" in json.dumps(captured)
    items = await auth_client.get(f"/v1/sessions/{target['id']}/items")
    assert f"[Message from session {source['id']}]" in items.text
    assert '"created_by":"alice"' in items.text
    store = SqlAlchemyConversationStore(db_uri)
    assert store.get_conversation(target["id"]).parent_conversation_id is None
    assert store.get_conversation(source["id"]).parent_conversation_id is None


@pytest.mark.parametrize("actor", ["alice", "bob"])
@pytest.mark.parametrize("sharing_after_input", [LEVEL_EDIT, LEVEL_READ, None])
@pytest.mark.parametrize("delegation_policy", ["requester", "no_edit_sharing"])
@pytest.mark.parametrize("unknown_requester", [False, True])
async def test_shared_input_keeps_requester_when_permissions_change(
    auth_client, peers, db_uri, actor, sharing_after_input, delegation_policy, unknown_requester
):
    source, target, captured = peers
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")
    for session in (source, target):
        permissions.grant("bob", session["id"], LEVEL_EDIT)

    # Capture identity at authenticated ingress, before queueing or a grant change.
    runner_token = auth_client.headers.pop(RUNNER_TUNNEL_TOKEN_HEADER)
    accepted = await auth_client.post(
        f"/v1/sessions/{source['id']}/events",
        headers={"X-Forwarded-Email": actor},
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "work"}]},
        },
    )
    assert accepted.status_code == 202, accepted.text
    origin = InputRequester.from_body(captured["body"])
    assert origin.user_id == actor
    requester = (
        ExecutionRequester(InputRequester() if unknown_requester else origin)
        .snapshot()
        .model_copy(update={"delegation_policy": delegation_policy})
    )
    auth_client.headers[RUNNER_TUNNEL_TOKEN_HEADER] = runner_token
    # A changed stored harness cannot relax the captured execution policy.
    store.update_conversation(source["id"], harness_override="claude-native")
    if delegation_policy == "no_edit_sharing":
        captured.clear()
        assert (
            "no edit sharing"
            in (await _send(auth_client, source, target, requester=requester))["detail"]
        )
        assert not captured.get("posts")
        assert store.list_pending_inputs(target["id"]) == []
    if sharing_after_input is None:
        permissions.revoke("bob", source["id"])
    else:
        permissions.grant("bob", source["id"], sharing_after_input)

    captured.clear()
    result = await _send(auth_client, source, target, requester=requester)
    denied = (
        sharing_after_input == LEVEL_EDIT
        if delegation_policy == "no_edit_sharing"
        else actor == "bob" or unknown_requester
    )
    if denied:
        expected = (
            "no edit sharing"
            if delegation_policy == "no_edit_sharing"
            else "verified owner execution"
        )
        assert expected in result["detail"], result
        assert not captured.get("posts")
        assert store.list_pending_inputs(target["id"]) == []
    else:
        assert result.get("queued") is True, result
        assert InputRequester.from_body(captured["body"]) == requester.origin.model_copy(
            update={"input_id": result["item_id"]}
        )


async def test_runner_is_trusted_for_other_owned_sessions_it_hosts(auth_client, peers, db_uri):
    source, target, _ = peers
    sibling = await _create_session_as(auth_client, "", "alice")
    store = SqlAlchemyConversationStore(db_uri)
    store.set_runner_id(sibling["id"], store.get_conversation(source["id"]).runner_id)
    assert (await _send(auth_client, sibling, target)).get("queued") is True
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")
    permissions.grant("bob", sibling["id"], LEVEL_READ)
    assert (await _send(auth_client, sibling, target)).get("queued") is True
    assert (
        "verified owner execution"
        in (await _send(auth_client, sibling, target, requester=None))["detail"]
    )


@pytest.mark.parametrize("batch", [False, True])
async def test_peer_message_tracks_runner_startup(auth_client, peers, monkeypatch, batch):
    from omnigent.server.routes import sessions
    from omnigent.server.routes._sessions import orchestration

    source, target, _ = peers
    started = []

    async def observe_startup(*args, **kwargs):
        started.append(orchestration._session_is_starting(target["id"]))

    monkeypatch.setattr(sessions, "_ensure_runner_relay_ready", observe_startup)
    event = {"type": "session_message", "data": {"message": "Start"}}
    response = await auth_client.post(
        f"/v1/sessions/{target['id']}/events",
        headers={
            ORCHESTRATION_SOURCE_HEADER: source["id"],
            REQUESTER_HEADER: _requester().model_dump_json(),
        },
        json=[event] if batch else event,
    )
    assert response.status_code == 202, response.text
    assert started == [True]
    assert not orchestration._dispatch_in_flight.get(target["id"])


@pytest.mark.parametrize(
    "condition",
    [
        "flag_off",
        "shared_read",
        "shared_edit",
        "public",
        "public_edit",
        "manage",
        "other_owner",
        "admin",
        "missing_binding",
        "wrong_binding",
        "self",
        "blank",
        "oversize",
        "missing_source",
        "missing_requester",
        "unknown_requester",
        "unknown_strict_requester",
        "stale_account",
        "invalid_requester",
        "invalid_policy",
        "closed_source",
        "closed_target",
    ],
)
@pytest.mark.parametrize("harness", ["claude-sdk", "claude-native"])
async def test_dispatch_checks_before_delivery(
    condition, auth_client, auth_app, peers, db_uri, harness
):
    source, target, captured = peers
    store = SqlAlchemyConversationStore(db_uri)
    store.update_conversation(source["id"], harness_override=harness)
    delegation_policy = "no_edit_sharing" if harness == "claude-native" else "requester"
    permissions = SqlAlchemyPermissionStore(db_uri)
    if condition == "flag_off":
        auth_app.state.feature_flags = FeatureFlags()
    if condition in {"shared_read", "shared_edit", "public", "public_edit", "manage"}:
        user = "__public__" if condition.startswith("public") else "bob"
        permissions.ensure_user(user)
        permissions.grant(
            user,
            source["id"],
            LEVEL_MANAGE
            if condition == "manage"
            else LEVEL_EDIT
            if condition in {"shared_edit", "public_edit"}
            else LEVEL_READ,
        )
    if condition in {"other_owner", "admin"}:
        other = await _create_session_as(auth_client, "", "bob")
        permissions.grant("alice", other["id"], LEVEL_EDIT)
        target = other
        if condition == "admin":
            permissions.set_admin("alice", True)
    if condition == "missing_binding":
        del auth_client.headers[RUNNER_TUNNEL_TOKEN_HEADER]
    if condition == "wrong_binding":
        auth_client.headers[RUNNER_TUNNEL_TOKEN_HEADER] = "wrong"
    if condition == "self":
        target = source
    if condition in {"closed_source", "closed_target"}:
        store.set_labels(
            (source if condition == "closed_source" else target)["id"],
            {"omnigent.closed": "true"},
        )
    headers = {ORCHESTRATION_SOURCE_HEADER: source["id"]}
    if condition == "missing_source":
        headers.clear()
    if condition not in {"shared_read", "public", "missing_requester"}:
        headers[REQUESTER_HEADER] = _requester(
            "bob" if condition in {"shared_edit", "public_edit", "manage"} else "alice",
            delegation_policy=delegation_policy,
        ).model_dump_json()
    if condition in {
        "unknown_requester",
        "unknown_strict_requester",
        "stale_account",
        "invalid_requester",
        "invalid_policy",
    }:
        origin = InputRequester(
            user_id="alice" if condition == "stale_account" else None,
            account_generation="previous-generation" if condition == "stale_account" else None,
        )
        headers[REQUESTER_HEADER] = (
            "invalid"
            if condition == "invalid_requester"
            else ExecutionRequester(origin)
            .snapshot()
            .model_copy(
                update={
                    "delegation_policy": "invalid"
                    if condition == "invalid_policy"
                    else "requester"
                    if condition == "unknown_strict_requester"
                    else delegation_policy,
                }
            )
            .model_dump_json()
        )
    captured.clear()
    before_items = store.list_items(target["id"]).data
    response = await auth_client.post(
        f"/v1/sessions/{target['id']}/events",
        headers=headers,
        json={
            "type": "session_message",
            "data": {
                "message": {
                    "blank": " ",
                    "oversize": "a" * 8001,
                }.get(condition, "DO_NOT_DELIVER")
            },
        },
    )
    if harness == "claude-native" and condition == "unknown_requester":
        assert response.status_code == 202, response.text
        assert REQUESTER_FIELD in captured["body"]
        assert InputRequester.from_body(captured["body"]).user_id is None
        return
    assert response.status_code in {400, 403}, response.text
    if harness == "claude-native" and condition == "stale_account":
        assert "Requester account is no longer current" in response.text
    if condition in {"invalid_requester", "invalid_policy"}:
        assert "Invalid requester metadata" in response.text
    assert not captured.get("posts")
    assert store.list_pending_inputs(target["id"]) == []
    assert store.list_items(target["id"]).data == before_items


async def test_sharing_during_receiver_startup_preserves_owner_delivery(
    auth_client, peers, db_uri, monkeypatch
):
    from omnigent.server.routes import sessions

    source, target, _ = peers
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")

    async def share_while_starting(*args, **kwargs):
        permissions.grant("bob", source["id"], LEVEL_READ)

    monkeypatch.setattr(sessions, "_ensure_runner_relay_ready", share_while_starting)
    result = await _send(auth_client, source, target)
    assert result.get("queued") is True, result
    items = await auth_client.get(f"/v1/sessions/{target['id']}/items")
    assert "Please inspect" in items.text


@pytest.mark.parametrize("operation", ["message", "child", "same_host"])
async def test_requester_routes_refuse_servers_with_only_legacy_orchestration(
    auth_client, auth_app, peers, db_uri, monkeypatch, operation
):
    source, target, captured = peers
    routes = auth_app.router.routes
    assert any(route.path == "/v1/sessions/orchestrated" for route in routes)
    assert any(route.path == "/v1/sessions/{session_id}/events" for route in routes)
    monkeypatch.setattr(
        auth_app.router,
        "routes",
        [route for route in routes if not route.path.startswith("/v1/requester/")],
    )
    store = SqlAlchemyConversationStore(db_uri)
    before = {session.id for session in store.list_conversations().data}
    if operation == "message":
        result = await _send(auth_client, source, target)
    else:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps(
                    {
                        "agent_id": source["agent_id"],
                        "placement": operation,
                        "message": "Work must keep its requester",
                    }
                ),
                requester=_requester(),
                server_client=auth_client,
                conversation_id=source["id"],
            )
        )
    assert "error" in result, result
    assert not captured.get("posts")
    assert {session.id for session in store.list_conversations().data} == before
    assert (
        "Work must keep its requester"
        not in (await auth_client.get(f"/v1/sessions/{target['id']}/items")).text
    )


@pytest.mark.parametrize("requester_user", ["alice", "bob", None])
async def test_child_inherits_source_requester(auth_client, peers, db_uri, requester_user):
    source, target, captured = peers
    response = await auth_client.post(
        "/v1/sessions",
        headers={REQUESTER_HEADER: _requester(requester_user).model_dump_json()}
        if requester_user is not None
        else {},
        json={
            "agent_id": source["agent_id"],
            "parent_session_id": source["id"],
            "initial_items": [
                {
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Child work"}],
                    },
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    child = response.json()
    [initial_message] = [
        body for path, body in captured["posts"] if path == f"/v1/sessions/{child['id']}/events"
    ]
    origin = InputRequester.from_body(initial_message)
    # A headerless runner keeps child work usable without inventing owner authority.
    assert origin.user_id == requester_user
    requester = ExecutionRequester(origin).snapshot()
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")
    permissions.grant("bob", source["id"], LEVEL_READ)
    captured.clear()
    result = await _send(auth_client, child, target, requester=requester)
    if requester_user == "alice":
        assert result.get("queued") is True, result
        assert InputRequester.from_body(captured["body"]).user_id == "alice"
    else:
        assert "verified owner execution" in result["detail"]
        assert not captured.get("posts")


@pytest.mark.parametrize("descendant", ["side_chat", "nested_side_chat", "child"])
@pytest.mark.parametrize("share", ["source", "side_chat"])
@pytest.mark.parametrize("delegation_policy", ["requester", "no_edit_sharing"])
async def test_side_chat_inherits_sharing_for_new_controls(
    auth_client, peers, db_uri, descendant, share, delegation_policy
):
    source, target, captured = peers
    response = await auth_client.post(
        f"/v1/sessions/{source['id']}/fork", json={"side_chat": True}
    )
    assert response.status_code == 201, response.text
    side = response.json()
    _bind_runner(db_uri, side["id"])
    manager = side
    if descendant != "side_chat":
        response = await auth_client.post(
            f"/v1/sessions/{side['id']}/fork"
            if descendant == "nested_side_chat"
            else "/v1/sessions",
            json={"side_chat": True}
            if descendant == "nested_side_chat"
            else {"agent_id": side["agent_id"], "parent_session_id": side["id"]},
        )
        assert response.status_code == 201, response.text
        manager = response.json()
        if descendant == "nested_side_chat":
            _bind_runner(db_uri, manager["id"])
    assert (await _send(auth_client, manager, target)).get("queued") is True
    requester = _requester("bob", delegation_policy=delegation_policy)
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")
    shared_id = source["id"] if share == "source" else side["id"]
    permissions.grant(
        "bob", shared_id, LEVEL_EDIT if delegation_policy == "no_edit_sharing" else LEVEL_READ
    )
    if delegation_policy == "no_edit_sharing" and descendant == "child" and share == "source":
        for index in range(100):
            reader = f"a-reader-{index:03d}"
            permissions.ensure_user(reader)
            permissions.grant(reader, shared_id, LEVEL_READ)
        first_page, cursor = permissions.list_for_session(shared_id)
        assert cursor is not None and all(grant.user_id != "bob" for grant in first_page)
    store = SqlAlchemyConversationStore(db_uri)
    before_sessions = {s.id for s in store.list_conversations().data}
    before_grants = permissions.list_for_user("alice")
    captured.clear()
    expected = (
        "no edit sharing" if delegation_policy == "no_edit_sharing" else "verified owner execution"
    )
    assert expected in (await _send(auth_client, manager, target, requester=requester))["detail"]
    for placement in ("same_host", "managed"):
        result = json.loads(
            await execute_tool(
                requester=requester,
                tool_name="sys_session_create",
                arguments=json.dumps({"agent_id": side["agent_id"], "placement": placement}),
                server_client=auth_client,
                conversation_id=manager["id"],
            )
        )
        assert expected in result["detail"], result
    assert not captured.get("posts")
    assert {s.id for s in store.list_conversations().data} == before_sessions
    assert permissions.list_for_user("alice") == before_grants
    assert store.list_pending_inputs(target["id"]) == []
    # Collaborator work is restricted; owner reads and new owner work still succeed.
    history = await auth_client.get(f"/v1/sessions/{manager['id']}/items")
    assert history.status_code == 200, history.text
    _bind_runner(db_uri, target["id"])
    assert (await _send(auth_client, target, manager)).get("queued") is True
    permissions.revoke("bob", shared_id)
    if delegation_policy == "no_edit_sharing":
        result = await _send(auth_client, manager, target, requester=requester)
        assert result.get("queued") is True, result
        assert InputRequester.from_body(captured["body"]) == requester.origin.model_copy(
            update={"input_id": result["item_id"]}
        )
    else:
        assert (await _send(auth_client, manager, target)).get("queued") is True


@pytest.mark.parametrize("source_link", ["missing", "cycle", "closed", "shared_without_marker"])
@pytest.mark.parametrize("delegation_policy", ["requester", "no_edit_sharing"])
async def test_side_chat_requires_resolvable_source(
    auth_client, peers, db_uri, source_link, delegation_policy
):
    source, target, captured = peers
    response = await auth_client.post(
        f"/v1/sessions/{source['id']}/fork", json={"side_chat": True}
    )
    assert response.status_code == 201, response.text
    side = response.json()
    _bind_runner(db_uri, side["id"])
    store = SqlAlchemyConversationStore(db_uri)
    store.set_labels(
        side["id"],
        {
            SIDE_CHAT_SOURCE_LABEL_KEY: {
                "missing": uuid4().hex,
                "cycle": side["id"],
            }.get(source_link, source["id"])
        },
    )
    expected = "Session is not owned or available"
    if source_link == "closed":
        store.set_labels(source["id"], {"omnigent.closed": "true"})
    elif source_link == "shared_without_marker":
        response = await auth_client.patch(
            f"/v1/sessions/{side['id']}", json={"labels": {SIDE_CHAT_LABEL_KEY: ""}}
        )
        assert response.status_code == 200, response.text
        assert store.get_conversation(side["id"]).labels[SIDE_CHAT_LABEL_KEY] == ""
        permissions = SqlAlchemyPermissionStore(db_uri)
        permissions.ensure_user("bob")
        permissions.grant("bob", source["id"], LEVEL_EDIT)
        expected = (
            "no edit sharing"
            if delegation_policy == "no_edit_sharing"
            else "verified owner execution"
        )
    captured.clear()
    result = await _send(
        auth_client,
        side,
        target,
        requester=_requester(
            "bob" if source_link == "shared_without_marker" else "alice",
            delegation_policy=delegation_policy,
        ),
    )
    assert expected in result["detail"], result
    assert not captured.get("posts")


@pytest.mark.parametrize("execution_target", ["connected_host", "managed_sandbox"])
@pytest.mark.parametrize("full_fork", [False, True])
async def test_scheduled_origin_allows_new_controls_children_and_reads(
    auth_client, auth_app, peers, db_uri, monkeypatch, execution_target, full_fork
):
    import httpx

    from omnigent.server.routes.sessions import routes_core
    from omnigent.server.scheduled.fire import FireDeps, _send_prompt, build_on_fire
    from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
    from tests._helpers.polling import poll_until_async

    source, target, captured = peers
    conversations = SqlAlchemyConversationStore(db_uri)
    schedules = SqlAlchemyScheduledTaskStore(db_uri)
    task = schedules.create(
        scheduled_task_id=uuid4().hex,
        name="Scheduled worker",
        prompt="Ordinary scheduled work",
        rrule="FREQ=HOURLY",
        user_id="alice",
        agent_id=source["agent_id"],
        timezone="UTC",
        execution_target=execution_target,
        host_id=uuid4().hex,
        workspace="/tmp",
    )
    labels_at_launch = []

    async def launch(session, task):
        labels_at_launch.append(session.labels)

        def record(request):
            captured.setdefault("posts", []).append(
                (request.url.path, json.loads(request.content))
            )
            return httpx.Response(202, json={})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(record), base_url="http://runner"
        ) as runner:
            await _send_prompt(deps, session, runner, task)

    deps = FireDeps(
        scheduled_task_store=schedules,
        agent_store=auth_app.state.agent_store,
        conversation_store=conversations,
        permission_store=SqlAlchemyPermissionStore(db_uri),
        host_store=auth_app.state.host_store,
        host_registry=auth_app.state.host_registry,
        sandbox_config=auth_app.state.sandbox_config,
    )
    on_fire = build_on_fire(deps, launch_dispatch=launch)
    assert await on_fire(0, task.id)
    await poll_until_async(lambda: schedules.get(task.id).last_run_conversation_id, timeout_s=5)
    scheduled_id = schedules.get(task.id).last_run_conversation_id
    assert labels_at_launch[0][SCHEDULED_TASK_ORIGIN_LABEL_KEY] == task.id
    delivered = [body for path, body in captured.get("posts", []) if path.endswith("/events")]
    assert len(delivered) == 1
    origin = InputRequester.from_body(delivered[0])
    assert origin.user_id == task.user_id
    assert origin.account_generation == task.account_generation
    requester = ExecutionRequester(origin).snapshot()
    schedules.delete(task.id)
    assert schedules.list_runs(task.id)[0] == []

    if full_fork:
        response = await auth_client.post(f"/v1/sessions/{scheduled_id}/fork", json={})
        assert response.status_code == 201, response.text
        scheduled_id = response.json()["id"]
        assert (
            conversations.get_conversation(scheduled_id).labels[SCHEDULED_TASK_ORIGIN_LABEL_KEY]
            == task.id
        )

    # Managed creation succeeds now; record its launch instead of provisioning.
    launched_by = []

    async def launch(**kwargs):
        launched_by.append(kwargs["owner"])

    monkeypatch.setattr(routes_core, "_run_managed_launch", launch)
    scheduled = {"id": scheduled_id}
    auth_client.headers.update(_bind_runner(db_uri, scheduled_id))
    for depth in range(3):
        captured.clear()
        # A scheduled run, its forks and children message the owner's sessions
        # like any private session they own.
        result = await _send(auth_client, scheduled, target, requester=requester)
        assert result.get("queued") is True, result
        assert "Please inspect the PR" in json.dumps(captured)
        for placement in ("same_host", "managed"):
            result = json.loads(
                await execute_tool(
                    requester=requester,
                    tool_name="sys_session_create",
                    arguments=json.dumps({"agent_id": source["agent_id"], "placement": placement}),
                    server_client=auth_client,
                    conversation_id=scheduled["id"],
                )
            )
            if placement == "managed":
                assert result.get("conversation_id"), result
            else:
                # Same-host placement needs a live host this test does not
                # register; it must not fail on the scheduled origin.
                assert result.get("error") != "access_denied", result
        history = json.loads(
            await execute_tool(
                tool_name="sys_session_get_history",
                arguments=json.dumps({"conversation_id": target["id"]}),
                server_client=auth_client,
                conversation_id=scheduled["id"],
            )
        )
        assert "error" not in history, history
        # Ordinary child creation retains its existing authorization and parentage.
        if depth < 2:
            response = await auth_client.post(
                "/v1/sessions",
                json={"agent_id": source["agent_id"], "parent_session_id": scheduled["id"]},
            )
            assert response.status_code == 201, response.text
            scheduled = response.json()

    await poll_until_async(lambda: len(launched_by) == 3, timeout_s=5)
    assert launched_by == ["alice"] * 3
    for labels in (
        {SCHEDULED_TASK_ORIGIN_LABEL_KEY: ""},
        {SCHEDULED_TASK_ORIGIN_LABEL_KEY: "other"},
    ):
        response = await auth_client.patch(f"/v1/sessions/{scheduled_id}", json={"labels": labels})
        assert response.status_code == 400, response.text
        response = await auth_client.post(
            "/v1/sessions", json={"agent_id": source["agent_id"], "labels": labels}
        )
        assert response.status_code == 400, response.text
    response = await auth_client.patch(f"/v1/sessions/{scheduled_id}", json={"labels": {}})
    assert response.status_code == 200, response.text
    assert (
        conversations.get_conversation(scheduled_id).labels[SCHEDULED_TASK_ORIGIN_LABEL_KEY]
        == task.id
    )


@pytest.mark.parametrize("placement", ["same_host", "managed"])
@pytest.mark.parametrize("delegation_policy", ["requester", "no_edit_sharing"])
@pytest.mark.parametrize(
    "condition,orchestration_environment",
    [
        ("flag_off", "-session_orchestration"),
        ("shared", None),
        ("binding", None),
        ("legacy", None),
    ],
    indirect=["orchestration_environment"],
)
async def test_creation_refuses_before_launch(
    auth_client, auth_app, peers, db_uri, placement, condition, delegation_policy
):
    from tests.server.integration.test_sessions_permissions import _register_online_host

    source, _, captured = peers
    host_id = uuid4().hex
    _register_online_host(auth_app, host_id, "alice")
    SqlAlchemyConversationStore(db_uri).set_host_id(source["id"], host_id, workspace="/tmp")
    assert auth_app.state.sandbox_config.managed_launch_supported
    if condition == "shared":
        permissions = SqlAlchemyPermissionStore(db_uri)
        permissions.ensure_user("bob")
        permissions.grant(
            "bob",
            source["id"],
            LEVEL_EDIT if delegation_policy == "no_edit_sharing" else LEVEL_READ,
        )
    elif condition == "binding":
        auth_client.headers[RUNNER_TUNNEL_TOKEN_HEADER] = "wrong"
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    before_sessions = {s.id for s in store.list_conversations().data}
    before_grants = permissions.list_for_user("alice")
    result = json.loads(
        await execute_tool(
            requester=None
            if condition == "legacy"
            else _requester(
                "bob" if condition == "shared" else "alice", delegation_policy=delegation_policy
            ),
            tool_name="sys_session_create",
            arguments=json.dumps({"agent_id": source["agent_id"], "placement": placement}),
            server_client=auth_client,
            conversation_id=source["id"],
        )
    )
    expected = {
        "flag_off": "Session orchestration is disabled",
        "shared": "no edit sharing"
        if delegation_policy == "no_edit_sharing"
        else "verified owner execution",
        "legacy": "verified owner execution",
        "binding": "requires the sender's bound runner",
    }[condition]
    assert expected in result["detail"], result
    if condition == "flag_off":
        response = await auth_client.post(
            "/v1/sessions",
            headers={
                ORCHESTRATION_SOURCE_HEADER: source["id"],
                REQUESTER_HEADER: _requester().model_dump_json(),
            },
            json={
                "agent_id": source["agent_id"],
                "host_type": "managed" if placement == "managed" else "external",
            },
        )
        assert response.status_code == 403 and expected in response.text
    assert not captured.get("posts")
    assert auth_app.state.host_registry.get(host_id).outbound_queue.empty()
    assert {s.id for s in store.list_conversations().data} == before_sessions
    assert permissions.list_for_user("alice") == before_grants


async def test_same_host_still_requires_host_ownership(auth_client, auth_app, peers, db_uri):
    from tests.server.integration.test_sessions_permissions import _register_online_host

    source, _, captured = peers
    host_id = "a" * 32
    _register_online_host(auth_app, host_id, "bob")
    SqlAlchemyConversationStore(db_uri).set_host_id(source["id"], host_id, workspace="/tmp")
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    before_sessions = {s.id for s in store.list_conversations().data}
    before_grants = permissions.list_for_user("alice")
    result = json.loads(
        await execute_tool(
            requester=_requester(),
            tool_name="sys_session_create",
            arguments=json.dumps({"agent_id": source["agent_id"], "placement": "same_host"}),
            server_client=auth_client,
            conversation_id=source["id"],
        )
    )
    assert "not your host" in result["detail"], result
    assert not captured.get("posts")
    assert auth_app.state.host_registry.get(host_id).outbound_queue.empty()
    assert {s.id for s in store.list_conversations().data} == before_sessions
    assert permissions.list_for_user("alice") == before_grants


@pytest.mark.parametrize(
    "child_manager,move_before_recheck,policy_denied,delegation_policy,requester_user",
    [
        (False, False, False, "requester", "alice"),
        (True, False, False, "requester", "alice"),
        (False, True, False, "requester", "alice"),
        (True, True, False, "requester", "alice"),
        (False, False, True, "requester", "alice"),
        (False, False, False, "no_edit_sharing", "bob"),
        (True, False, False, "no_edit_sharing", None),
        (False, False, True, "no_edit_sharing", None),
    ],
)
async def test_same_host_creation_uses_current_source_and_stays_private(
    auth_client,
    auth_app,
    peers,
    db_uri,
    monkeypatch,
    move_before_recheck,
    child_manager,
    policy_denied,
    delegation_policy,
    requester_user,
):
    from omnigent.host.frames import HostLaunchRunnerFrame, HostStatFrame, decode_host_frame
    from omnigent.server.routes.sessions import routes_core, routes_events
    from omnigent.server.sharing_settings import DefaultPublicSessions
    from tests.server.integration.test_sessions_permissions import _register_online_host

    source, _, captured = peers
    requester = _requester(requester_user, delegation_policy=delegation_policy)
    hosts = [uuid4().hex for _ in range(3)]
    for host_id, owner in zip(hosts, ("alice", "alice", "bob"), strict=True):
        _register_online_host(auth_app, host_id, owner)
        auth_app.state.host_store.upsert_on_connect(host_id, host_id, owner)
    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(source["id"], hosts[0], workspace="/original")
    manager = source
    if child_manager:
        response = await auth_client.post(
            "/v1/sessions",
            json={"agent_id": source["agent_id"], "parent_session_id": source["id"]},
        )
        assert response.status_code == 201, response.text
        manager = response.json()
        assert store.get_conversation(manager["id"]).host_id is None
    auth_app.state.default_public_sessions = lambda: DefaultPublicSessions.ALL
    original_check = routes_core.require_orchestration_source
    checks = 0

    def check_source(*args, **kwargs):
        nonlocal checks
        checks += 1
        if checks == 2 and move_before_recheck:
            store.set_host_id(source["id"], hosts[1], workspace="/moved")
        return original_check(*args, **kwargs)

    monkeypatch.setattr(routes_core, "require_orchestration_source", check_source)
    frames = []

    def answer_host(connection, payload):
        frame = decode_host_frame(payload)
        frames.append((connection.host_id, frame))
        if isinstance(frame, HostStatFrame):
            connection.pending_stats[frame.request_id].set_result(
                {"status": "ok", "exists": True, "type": "directory", "canonical_path": frame.path}
            )
        elif isinstance(frame, HostLaunchRunnerFrame):
            connection.pending_launches[frame.request_id].set_result({"status": "launched"})
        else:
            pytest.fail(f"Unexpected host frame: {frame}")

    monkeypatch.setattr(auth_app.state.host_registry, "send_text", answer_host)
    if policy_denied:

        async def deny_task(*args, **kwargs):
            return {"verdict": "deny", "reason": "Task needs approval"}

        monkeypatch.setattr(routes_events, "_evaluate_input_policy", deny_task)
    if policy_denied or delegation_policy == "no_edit_sharing":
        before = {s.id for s in store.list_conversations().data}
        result = json.loads(
            await execute_tool(
                requester=requester,
                tool_name="sys_session_create",
                arguments=json.dumps(
                    {
                        "agent_id": source["agent_id"],
                        "placement": "same_host",
                        "message": "Start",
                    }
                ),
                server_client=auth_client,
                conversation_id=manager["id"],
            )
        )
        if policy_denied:
            assert result["status"] == "created_initial_message_failed", result
            assert "Task needs approval" in result["error"]
            assert result["initial_message"]["denied"] is True
            assert all(path == "/v1/sessions" for path, _ in captured.get("posts", []))
        else:
            assert "error" not in result, result
            assert result["initial_message"]["queued"] is True
            [initial_message] = [
                body for path, body in captured["posts"] if path.endswith("/events")
            ]
            assert InputRequester.from_body(initial_message) == requester.origin.model_copy(
                update={"input_id": result["initial_message"]["item_id"]}
            )
            assert f"[Message from session {manager['id']}]" in json.dumps(initial_message)
        created_id = result["conversation_id"]
        assert {s.id for s in store.list_conversations().data} - before == {created_id}
    else:
        response = await auth_client.post(
            "/v1/sessions/orchestrated",
            headers={
                ORCHESTRATION_SOURCE_HEADER: manager["id"],
                REQUESTER_HEADER: _requester().model_dump_json(),
            },
            json={"agent_id": source["agent_id"], "host_id": hosts[2], "workspace": "/requested"},
        )
        assert response.status_code == 201, response.text
        created_id = response.json()["id"]
    created = store.get_conversation(created_id)
    expected_host = hosts[1] if move_before_recheck else hosts[0]
    expected_workspace = "/moved" if move_before_recheck else "/original"
    assert checks == 2
    assert created.parent_conversation_id is None
    assert created.labels[CREATED_BY_SESSION_LABEL_KEY] == manager["id"]
    assert (created.host_id, created.workspace) == (expected_host, expected_workspace)
    assert frames and all(host == expected_host for host, _ in frames)
    launches = [frame for _, frame in frames if isinstance(frame, HostLaunchRunnerFrame)]
    assert len(launches) == 1 and launches[0].workspace == expected_workspace
    grants, _ = SqlAlchemyPermissionStore(db_uri).list_for_session(created.id)
    assert [grant.user_id for grant in grants] == ["alice"]

    assert CREATED_BY_SESSION_LABEL_KEY not in store.get_conversation(manager["id"]).labels
    # Discovery and label-write rules are independent of the routing variants above.
    if (
        child_manager
        or move_before_recheck
        or policy_denied
        or delegation_policy == "no_edit_sharing"
    ):
        return

    info = json.loads(
        await execute_tool(
            tool_name="sys_session_get_info",
            arguments=json.dumps({"session_id": created_id}),
            server_client=auth_client,
            conversation_id=manager["id"],
        )
    )
    assert info["created_by_session_id"] == manager["id"]
    listing = json.loads(
        await execute_tool(
            tool_name="sys_session_list",
            arguments="{}",
            server_client=auth_client,
            conversation_id=manager["id"],
        )
    )
    worker = next(row for row in listing["sessions"] if row["session_id"] == created_id)
    assert worker["created_by_session_id"] == manager["id"]
    assert worker["parent_session_id"] is None
    for value in ("", source["id"]):
        labels = {CREATED_BY_SESSION_LABEL_KEY: value}
        response = await auth_client.patch(f"/v1/sessions/{created_id}", json={"labels": labels})
        assert response.status_code == 400, response.text
        response = await auth_client.post(
            "/v1/sessions", json={"agent_id": source["agent_id"], "labels": labels}
        )
        assert response.status_code == 400, response.text
    response = await auth_client.patch(f"/v1/sessions/{created_id}", json={"labels": {}})
    assert response.status_code == 200, response.text
    assert (
        SqlAlchemyConversationStore(db_uri)
        .get_conversation(created_id)
        .labels[CREATED_BY_SESSION_LABEL_KEY]
        == manager["id"]
    )


@pytest.mark.parametrize("shape", ["parent", "multipart", "initial_items", "skill"])
async def test_independent_creation_rejects_child_or_bundle_shape(auth_client, peers, shape):
    from tests._helpers.session import post_session_bundle

    source, _, _ = peers
    headers = {
        ORCHESTRATION_SOURCE_HEADER: source["id"],
        REQUESTER_HEADER: _requester().model_dump_json(),
    }
    if shape == "parent":
        response = await auth_client.post(
            "/v1/sessions/orchestrated",
            headers=headers,
            json={"agent_id": source["agent_id"], "parent_session_id": source["id"]},
        )
        expected = "Orchestration creates independent sessions"
    elif shape in ("initial_items", "skill"):
        first = {
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "Unattributed task"}],
            },
        }
        payload = (
            {"initial_items": [first]}
            if shape == "initial_items"
            else {
                "initial_input": {
                    "type": "slash_command",
                    "data": {"kind": "skill", "name": "work"},
                }
            }
        )
        response = await auth_client.post(
            "/v1/sessions/orchestrated",
            headers=headers,
            json={"agent_id": source["agent_id"], "host_type": "managed", **payload},
        )
        expected = "Use initial_input" if shape == "initial_items" else "first task must be text"
    else:
        response = await post_session_bundle(
            auth_client.post, "/v1/sessions", b"unused", metadata={}, headers=headers
        )
        expected = "Independent sessions require agent_id"
    assert response.status_code == 400, response.text
    assert expected in response.text


@pytest.mark.parametrize(
    "provider,delegation_policy,requester_user",
    [
        ("modal", "requester", "alice"),
        ("daytona", "requester", "alice"),
        ("embedding", "requester", "alice"),
        ("modal", "no_edit_sharing", "bob"),
        ("modal", "no_edit_sharing", None),
    ],
)
async def test_managed_creation_uses_default_provider_and_saves_private_first_task(
    auth_client, auth_app, peers, db_uri, monkeypatch, provider, delegation_policy, requester_user
):
    import asyncio
    from dataclasses import replace

    from omnigent.server.managed_hosts import parse_sandbox_config
    from omnigent.server.routes.sessions import routes_core
    from omnigent.server.sharing_settings import DefaultPublicSessions

    source, _, _ = peers
    requester = _requester(requester_user, delegation_policy=delegation_policy)
    deployment = parse_sandbox_config(
        {
            "server_url": "https://managed-test.example.com",
            "providers": [
                {"provider": "lakebox"},
                {"provider": "modal" if provider == "embedding" else provider},
            ],
        }
    )
    assert deployment is not None
    if provider == "embedding":
        deployment = replace(deployment, configs=(replace(deployment.default, provider=provider),))
    auth_app.state.sandbox_config = deployment
    auth_app.state.default_public_sessions = lambda: DefaultPublicSessions.ALL
    launched = asyncio.Event()
    release = asyncio.Event()
    captured = {}

    async def launch(**kwargs):
        captured.update(kwargs)
        launched.set()
        await release.wait()

    monkeypatch.setattr(routes_core, "_run_managed_launch", launch)
    try:
        result = json.loads(
            await execute_tool(
                requester=requester,
                tool_name="sys_session_create",
                arguments=json.dumps(
                    {
                        "agent_id": source["agent_id"],
                        "placement": "managed",
                        "message": "First task",
                    }
                ),
                server_client=auth_client,
                conversation_id=source["id"],
            )
        )
        assert "error" not in result, result
        await asyncio.wait_for(launched.wait(), timeout=2)
        session_id = result["conversation_id"]
        assert captured["provider"] is None
        assert captured["sandbox_config"].for_provider(None).provider == provider
        assert captured["owner"] == "alice"
        store = SqlAlchemyConversationStore(db_uri)
        assert store.get_conversation(session_id).parent_conversation_id is None
        assert (
            store.get_conversation(session_id).labels[CREATED_BY_SESSION_LABEL_KEY] == source["id"]
        )
        assert "First task" in str(store.list_pending_inputs(session_id))
        [pending] = store.list_pending_inputs(session_id)
        assert pending.data.startup_options.requester == requester.origin.model_copy(
            update={"input_id": pending.id}
        )
        assert f"[Message from session {source['id']}]" in str(
            store.list_pending_inputs(session_id)
        )
        grants, _ = SqlAlchemyPermissionStore(db_uri).list_for_session(session_id)
        assert [g.user_id for g in grants] == ["alice"]
    finally:
        release.set()
        await auth_app.state.startup_inputs.shutdown()


@pytest.mark.parametrize("configured", [False, True])
async def test_managed_creation_requires_launchable_provider(
    auth_client, auth_app, peers, monkeypatch, configured
):
    from dataclasses import replace

    from omnigent.server.routes.sessions import routes_core

    source, _, _ = peers
    deployment = auth_app.state.sandbox_config
    auth_app.state.sandbox_config = (
        replace(deployment, configs=(replace(deployment.default, managed_launch_supported=False),))
        if configured
        else None
    )

    async def unexpected_creation(*args, **kwargs):
        pytest.fail("Unavailable managed creation must not create a session")

    monkeypatch.setattr(routes_core, "_create_session_from_existing_agent", unexpected_creation)
    result = json.loads(
        await execute_tool(
            requester=_requester(),
            tool_name="sys_session_create",
            arguments=json.dumps({"agent_id": source["agent_id"], "placement": "managed"}),
            server_client=auth_client,
            conversation_id=source["id"],
        )
    )
    assert "Managed sandboxes are unavailable" in result["detail"]


async def test_orchestrated_endpoint_requires_source(auth_client, peers, monkeypatch):
    from omnigent.server.routes.sessions import routes_core

    source, _, _ = peers

    async def unexpected_creation(*args, **kwargs):
        pytest.fail("Missing source must not create a session")

    monkeypatch.setattr(routes_core, "_create_session_from_existing_agent", unexpected_creation)
    response = await auth_client.post(
        "/v1/sessions/orchestrated", json={"agent_id": source["agent_id"]}
    )
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("requester_user", ["alice", "bob", None])
@pytest.mark.parametrize("delegation_policy", ["requester", "no_edit_sharing"])
async def test_startup_queue_admission_and_replay(
    auth_client, auth_app, peers, db_uri, requester_user, delegation_policy
):
    from omnigent.server.startup_inputs import STARTUP_LABEL

    source, target, captured = peers
    store = SqlAlchemyConversationStore(db_uri)
    store.set_labels(target["id"], {STARTUP_LABEL: "pending"})
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")
    startup = auth_app.state.startup_inputs
    requester = _requester(requester_user, delegation_policy=delegation_policy)
    result = await _send(auth_client, source, target, requester=requester)
    pending = store.list_pending_inputs(target["id"])
    assert not captured.get("posts")
    if requester_user != "alice" and delegation_policy == "requester":
        assert "verified owner execution" in result["detail"]
        assert pending == []
    else:
        assert result["queued"] is True
        assert len(pending) == 1
        permissions.grant("bob", source["id"], LEVEL_READ)
        await startup._deliver(auth_app, target["id"], pending[0])
        await startup._deliver(auth_app, target["id"], pending[0])
        delivered = [body for path, body in captured.get("posts", []) if path.endswith("/events")]
        assert len(delivered) == 1
        assert InputRequester.from_body(delivered[0]) == requester.origin.model_copy(
            update={"input_id": pending[0].id}
        )
        assert f"[Message from session {source['id']}]" in json.dumps(delivered)
        assert store.list_pending_inputs(target["id"]) == []
        assert (
            "verified owner execution"
            in (await _send(auth_client, source, target, requester=None))["detail"]
        )


@pytest.mark.parametrize("two_replica_stack", ["authenticated"], indirect=True)
@pytest.mark.parametrize("target_kind", ["root", "child", "side_chat"])
@pytest.mark.parametrize("legacy_host", [False, True])
async def test_peer_message_readdresses_across_replicas(
    two_replica_stack, db_uri, monkeypatch, target_kind, legacy_host
):
    import httpx

    from omnigent.cli_auth import OMNIGENT_SLICE_KEY_HEADER, open_server_client
    from omnigent.runner._entry import _RunnerDatabricksAuth
    from omnigent.server.routes import sessions
    from omnigent.server.routes._sessions.helpers import _get_runner_client_impl
    from tests.server.integration.test_sessions_permissions import _register_online_host

    stack = two_replica_stack
    source = await _create_session_as(stack.keyless_client, "", "alice")
    target = await _create_session_as(stack.tunnel_client, "", "alice")
    source_headers = {"X-Forwarded-Email": "alice", **_bind_runner(db_uri, source["id"])}
    _bind_runner(db_uri, target["id"])
    host_id = uuid4().hex
    _register_online_host(stack.tunnel_replica, host_id, "alice")
    stack.conv_store.set_host_id(target["id"], host_id, workspace="/worker")
    captured = _stub_late_runner_bind(monkeypatch)
    capture_client = sessions._get_runner_client

    async def resolve_runner(session_id, router, **kwargs):
        if router is stack.tunnel_replica.state.runner_router:
            return await capture_client(session_id, router, **kwargs)
        return await _get_runner_client_impl(session_id, router, **kwargs)

    monkeypatch.setattr(sessions, "_get_runner_client", resolve_runner)
    if target_kind == "child":
        response = await stack.tunnel_client.post(
            "/v1/sessions",
            headers={"X-Forwarded-Email": "alice"},
            json={"agent_id": target["agent_id"], "parent_session_id": target["id"]},
        )
        assert response.status_code == 201, response.text
        target = response.json()
    elif target_kind == "side_chat":
        side = await _create_session_as(stack.tunnel_client, "", "alice")
        _bind_runner(db_uri, side["id"])
        stack.conv_store.set_labels(
            side["id"], {"omnigent.side_chat": "1", "omnigent.side_chat.source_id": target["id"]}
        )
        target = side
    captured.clear()
    monkeypatch.setenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", "1")
    monkeypatch.setenv("OMNIGENT_RUNNER_SLICE_KEY", "sender-host")
    received = []
    transports = [
        httpx.ASGITransport(app=app) for app in (stack.keyless_replica, stack.tunnel_replica)
    ]

    async def ingress(request):
        key = request.headers.get(OMNIGENT_SLICE_KEY_HEADER)
        on_target = key is None if legacy_host else key == host_id
        response = await transports[int(on_target)].handle_async_request(request)
        await response.aread()
        received.append((request.method, request.url.path, key, response.status_code))
        return response

    async with open_server_client(
        "http://sharded",
        headers=source_headers,
        auth=_RunnerDatabricksAuth(lambda: "test-bearer", "http://sharded"),
        transport=httpx.MockTransport(ingress),
    ) as client:
        result = await _send(client, source, target)
        assert client.headers[OMNIGENT_SLICE_KEY_HEADER] == "sender-host"
    assert result.get("queued") is True, result
    posts = [(key, status) for method, path, key, status in received if method == "POST"]
    assert posts == (
        [("sender-host", 400), (host_id, 400), (None, 202)]
        if legacy_host
        else [("sender-host", 400), (host_id, 202)]
    )
    delivered = [body for path, body in captured.get("posts", []) if path.endswith("/events")]
    assert len(delivered) == 1
    assert f"[Message from session {source['id']}]" in json.dumps(delivered)


@pytest.mark.parametrize("two_replica_stack", ["managed"], indirect=True)
async def test_managed_create_is_addressed_before_provisioning(
    two_replica_stack, db_uri, monkeypatch
):
    import asyncio

    import httpx

    from omnigent.cli_auth import OMNIGENT_SLICE_KEY_HEADER, open_server_client
    from omnigent.runner._entry import _RunnerDatabricksAuth
    from omnigent.server.routes.sessions import routes_core

    stack = two_replica_stack
    source = await _create_session_as(stack.keyless_client, "", "alice")
    headers = {"X-Forwarded-Email": "alice", **_bind_runner(db_uri, source["id"])}
    monkeypatch.setenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", "1")
    monkeypatch.setenv("OMNIGENT_RUNNER_SLICE_KEY", "sender-host")
    launched = asyncio.Event()
    captured = {}

    async def launch(**kwargs):
        captured.update(kwargs)
        launched.set()

    monkeypatch.setattr(routes_core, "_run_managed_launch", launch)
    transports = [
        httpx.ASGITransport(app=app) for app in (stack.keyless_replica, stack.tunnel_replica)
    ]
    reserved = []

    async def ingress(request):
        key = request.headers.get(OMNIGENT_SLICE_KEY_HEADER)
        if request.method == "POST":
            body = json.loads(request.content)
            assert key == body["host_id"] and key != "sender-host"
            reserved.append(key)
        response = await transports[int(key != "sender-host")].handle_async_request(request)
        await response.aread()
        return response

    async with open_server_client(
        "http://sharded",
        headers=headers,
        auth=_RunnerDatabricksAuth(lambda: "test-bearer", "http://sharded"),
        transport=httpx.MockTransport(ingress),
    ) as client:
        result = json.loads(
            await execute_tool(
                requester=_requester(),
                tool_name="sys_session_create",
                arguments=json.dumps(
                    {
                        "agent_id": source["agent_id"],
                        "placement": "managed",
                        "message": "First task",
                    }
                ),
                server_client=client,
                conversation_id=source["id"],
            )
        )
    assert "error" not in result, result
    await asyncio.wait_for(launched.wait(), timeout=2)
    assert reserved == [captured["host_id"]]
    assert captured["host_registry"] is stack.tunnel_replica.state.host_registry
    worker = stack.conv_store.get_conversation(result["conversation_id"])
    assert worker.labels["omnigent.sandbox.requested_host_id"] == reserved[0]
    pending = stack.conv_store.list_pending_inputs(worker.id)
    assert len(pending) == 1
    assert (
        pending[0].data.content[0]["text"] == f"[Message from session {source['id']}]\nFirst task"
    )
    await stack.tunnel_replica.state.startup_inputs.shutdown()


@pytest.mark.parametrize("two_replica_stack", ["authenticated"], indirect=True)
@pytest.mark.parametrize("child_manager", [False, True])
@pytest.mark.parametrize("routing", ["host", "keyless", "wrong_replica", "disconnect", "offline"])
async def test_same_host_creation_across_replicas(
    two_replica_stack, db_uri, monkeypatch, child_manager, routing
):
    import httpx

    from omnigent.cli_auth import OMNIGENT_SLICE_KEY_HEADER, open_server_client
    from omnigent.host.frames import HostLaunchRunnerFrame, HostStatFrame, decode_host_frame
    from omnigent.runner._entry import _RunnerDatabricksAuth
    from omnigent.server.routes import _host_launch
    from tests.server.integration.test_sessions_permissions import _register_online_host

    stack = two_replica_stack
    source = await _create_session_as(stack.tunnel_client, "", "alice")
    headers = {"X-Forwarded-Email": "alice", **_bind_runner(db_uri, source["id"])}
    host_id = uuid4().hex
    _register_online_host(stack.tunnel_replica, host_id, "alice")
    stack.conv_store.set_host_id(source["id"], host_id, workspace="/worker")
    manager = source
    if child_manager:
        response = await stack.tunnel_client.post(
            "/v1/sessions",
            headers=headers,
            json={"agent_id": source["agent_id"], "parent_session_id": source["id"]},
        )
        assert response.status_code == 201, response.text
        manager = response.json()
        assert stack.conv_store.get_conversation(manager["id"]).host_id is None
    before = {s.id for s in stack.conv_store.list_conversations().data}
    launches = []

    def answer_host(connection, payload):
        frame = decode_host_frame(payload)
        if isinstance(frame, HostStatFrame):
            connection.pending_stats[frame.request_id].set_result(
                {"status": "ok", "exists": True, "type": "directory", "canonical_path": frame.path}
            )
            if routing in {"disconnect", "offline"}:
                # Workspace validation succeeded; the tunnel vanishes before launch.
                monkeypatch.setattr(
                    stack.tunnel_replica.state.host_registry, "get", lambda _: None
                )
        elif isinstance(frame, HostLaunchRunnerFrame):
            launches.append(frame)
            connection.pending_launches[frame.request_id].set_result({"status": "launched"})
        else:
            pytest.fail(f"Unexpected host frame: {frame}")

    monkeypatch.setattr(stack.tunnel_replica.state.host_registry, "send_text", answer_host)
    monkeypatch.setattr(_host_launch, "_host_sharded", routing != "offline")
    monkeypatch.setenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", "0" if routing == "keyless" else "1")
    monkeypatch.setenv("OMNIGENT_RUNNER_SLICE_KEY", host_id)
    transports = [
        httpx.ASGITransport(app=app) for app in (stack.keyless_replica, stack.tunnel_replica)
    ]
    posts = []

    async def ingress(request):
        key = request.headers.get(OMNIGENT_SLICE_KEY_HEADER)
        on_target = key is None if routing in {"keyless", "wrong_replica"} else key == host_id
        response = await transports[int(on_target)].handle_async_request(request)
        await response.aread()
        if request.method == "POST":
            posts.append((key, response.status_code))
        return response

    async with open_server_client(
        "http://sharded",
        headers=headers,
        auth=_RunnerDatabricksAuth(lambda: "test-bearer", "http://sharded"),
        transport=httpx.MockTransport(ingress),
    ) as client:
        result = json.loads(
            await execute_tool(
                requester=_requester(),
                tool_name="sys_session_create",
                arguments=json.dumps(
                    {
                        "agent_id": source["agent_id"],
                        "placement": "same_host",
                        "title": "Review worker",
                    }
                ),
                server_client=client,
                conversation_id=manager["id"],
            )
        )
        if routing in {"disconnect", "offline"}:
            listing = json.loads(
                await execute_tool(
                    tool_name="sys_session_list",
                    arguments="{}",
                    server_client=client,
                    conversation_id=manager["id"],
                )
            )
            workers = [s for s in listing["sessions"] if s["title"] == result["title"]]
            assert len(workers) == 1
            recovered = json.loads(
                await execute_tool(
                    tool_name="sys_session_get_info",
                    arguments=json.dumps({"session_id": workers[0]["session_id"]}),
                    server_client=client,
                    conversation_id=manager["id"],
                )
            )
            assert recovered["agent_id"] == result["agent_id"]
            assert recovered["host_id"] == host_id
    created = {s.id for s in stack.conv_store.list_conversations().data} - before
    assert len(posts) == 1
    if routing in {"wrong_replica", "disconnect", "offline"}:
        assert "do not automatically retry" in result["error"], result
        assert posts == [(host_id, 409 if routing == "offline" else 400)]
        assert len(created) == (0 if routing == "wrong_replica" else 1)
        assert launches == []
    else:
        assert "error" not in result, result
        assert posts == [(None if routing == "keyless" else host_id, 201)]
        assert created == {result["conversation_id"]}
        worker = stack.conv_store.get_conversation(result["conversation_id"])
        assert worker.parent_conversation_id is None
        assert worker.host_id == host_id and worker.workspace == "/worker"
        assert len(launches) == 1 and launches[0].session_id == worker.id
