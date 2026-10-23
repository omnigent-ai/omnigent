"""Runner dispatch through authenticated routes and real ownership/sharing stores."""

import json
from uuid import uuid4

import pytest

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER, RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.runner.tool_dispatch import execute_tool
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.stores.conversation_store import SCHEDULED_TASK_ORIGIN_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.integration.test_managed_startup_inputs import auth_app as auth_app
from tests.server.integration.test_managed_startup_inputs import (
    startup_permissions as startup_permissions,
)
from tests.server.integration.test_sessions_attribution import _bind_runner
from tests.server.integration.test_sessions_harness_override import _stub_late_runner_bind
from tests.server.integration.test_sessions_permissions import _create_session_as

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def peers(auth_client, auth_app, db_uri, monkeypatch):
    source = await _create_session_as(auth_client, "", "alice")
    target = await _create_session_as(auth_client, "", "alice")
    headers = {"X-Forwarded-Email": "alice", **_bind_runner(db_uri, source["id"])}
    auth_client.headers.update(headers)
    auth_app.state.feature_flags = FeatureFlags(frozenset({Feature.SESSION_ORCHESTRATION}))
    captured = _stub_late_runner_bind(monkeypatch)
    return source, target, captured


async def _send(client, source, target, message="Please inspect the PR"):
    return json.loads(
        await execute_tool(
            tool_name="sys_session_message",
            arguments=json.dumps({"session_id": target["id"], "message": message}),
            server_client=client,
            conversation_id=source["id"],
        )
    )


async def test_private_sender_can_message_shared_receiver(auth_client, peers, db_uri):
    source, target, captured = peers
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")
    permissions.grant("bob", target["id"], LEVEL_READ)
    result = await _send(auth_client, source, target)
    assert result.get("queued") is True, result
    assert "Please inspect the PR" in json.dumps(captured)
    items = await auth_client.get(f"/v1/sessions/{target['id']}/items")
    assert f"[Message from session {source['id']}]" in items.text
    assert '"created_by":"alice"' in items.text
    store = SqlAlchemyConversationStore(db_uri)
    assert store.get_conversation(target["id"]).parent_conversation_id is None
    assert store.get_conversation(source["id"]).parent_conversation_id is None


@pytest.mark.parametrize(
    "condition",
    [
        "flag_off",
        "shared_read",
        "shared_edit",
        "public",
        "other_owner",
        "admin",
        "missing_binding",
        "wrong_binding",
        "self",
        "blank",
        "oversize",
        "missing_source",
    ],
)
async def test_refusal_before_delivery(condition, auth_client, auth_app, peers, db_uri):
    source, target, captured = peers
    permissions = SqlAlchemyPermissionStore(db_uri)
    if condition == "flag_off":
        auth_app.state.feature_flags = FeatureFlags()
    if condition in {"shared_read", "shared_edit", "public"}:
        user = "__public__" if condition == "public" else "bob"
        permissions.ensure_user(user)
        permissions.grant(
            user, source["id"], LEVEL_EDIT if condition == "shared_edit" else LEVEL_READ
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
    captured.clear()
    response = await auth_client.post(
        f"/v1/sessions/{target['id']}/events",
        headers={}
        if condition == "missing_source"
        else {ORCHESTRATION_SOURCE_HEADER: source["id"]},
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
    assert response.status_code in {400, 403}, response.text
    assert not captured.get("posts")


async def test_sharing_during_receiver_startup_blocks_delivery(
    auth_client, peers, db_uri, monkeypatch
):
    from omnigent.server.routes import sessions

    source, target, captured = peers
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")

    async def share_while_starting(*args, **kwargs):
        permissions.grant("bob", source["id"], LEVEL_READ)

    monkeypatch.setattr(sessions, "_ensure_runner_relay_ready", share_while_starting)
    result = await _send(auth_client, source, target)
    assert "error" in result, result
    assert not any("/events" in path for path, _ in captured.get("posts", []))
    items = await auth_client.get(f"/v1/sessions/{target['id']}/items")
    assert "Please inspect" not in items.text


async def test_child_inherits_source_sharing(auth_client, peers, db_uri):
    source, target, captured = peers
    response = await auth_client.post(
        "/v1/sessions",
        json={
            "agent_id": source["agent_id"],
            "parent_session_id": source["id"],
        },
    )
    assert response.status_code == 201, response.text
    child = response.json()
    assert (await _send(auth_client, child, target)).get("queued") is True
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")
    permissions.grant("bob", source["id"], LEVEL_READ)
    captured.clear()
    assert "Shared sessions cannot" in (await _send(auth_client, child, target))["detail"]
    assert not captured.get("posts")


@pytest.mark.parametrize("execution_target", ["connected_host", "managed_sandbox"])
async def test_scheduled_origin_blocks_new_controls_but_preserves_children_and_reads(
    auth_client, auth_app, peers, db_uri, execution_target
):
    from omnigent.server.scheduled.fire import FireDeps, build_on_fire
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

    on_fire = build_on_fire(
        FireDeps(
            scheduled_task_store=schedules,
            agent_store=auth_app.state.agent_store,
            conversation_store=conversations,
            permission_store=SqlAlchemyPermissionStore(db_uri),
            host_store=auth_app.state.host_store,
            host_registry=auth_app.state.host_registry,
            sandbox_config=auth_app.state.sandbox_config,
        ),
        launch_dispatch=launch,
    )
    assert await on_fire(0, task.id)
    await poll_until_async(lambda: schedules.get(task.id).last_run_conversation_id, timeout_s=5)
    scheduled_id = schedules.get(task.id).last_run_conversation_id
    assert labels_at_launch[0][SCHEDULED_TASK_ORIGIN_LABEL_KEY] == task.id
    schedules.delete(task.id)
    assert schedules.list_runs(task.id)[0] == []

    scheduled = {"id": scheduled_id}
    auth_client.headers.update(_bind_runner(db_uri, scheduled_id))
    for depth in range(3):
        captured.clear()
        result = await _send(auth_client, scheduled, target)
        assert "Scheduled sessions cannot" in result["detail"], result
        for placement in ("same_host", "managed"):
            result = json.loads(
                await execute_tool(
                    tool_name="sys_session_create",
                    arguments=json.dumps({"agent_id": source["agent_id"], "placement": placement}),
                    server_client=auth_client,
                    conversation_id=scheduled["id"],
                )
            )
            assert result.get("error") == "access_denied", result
        assert not captured.get("posts")
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
@pytest.mark.parametrize("condition", ["flag_off", "shared", "binding"])
async def test_creation_refuses_before_launch(
    auth_client, auth_app, peers, db_uri, placement, condition
):
    from tests.server.integration.test_sessions_permissions import _register_online_host

    source, _, captured = peers
    host_id = uuid4().hex
    _register_online_host(auth_app, host_id, "alice")
    SqlAlchemyConversationStore(db_uri).set_host_id(source["id"], host_id, workspace="/tmp")
    assert auth_app.state.sandbox_config.managed_launch_supported
    if condition == "flag_off":
        auth_app.state.feature_flags = FeatureFlags()
    elif condition == "shared":
        permissions = SqlAlchemyPermissionStore(db_uri)
        permissions.ensure_user("bob")
        permissions.grant("bob", source["id"], LEVEL_READ)
    else:
        auth_client.headers[RUNNER_TUNNEL_TOKEN_HEADER] = "wrong"
    result = json.loads(
        await execute_tool(
            tool_name="sys_session_create",
            arguments=json.dumps({"agent_id": source["agent_id"], "placement": placement}),
            server_client=auth_client,
            conversation_id=source["id"],
        )
    )
    expected = {
        "flag_off": "Session orchestration is disabled",
        "shared": "Shared sessions cannot",
        "binding": "requires the sender's bound runner",
    }[condition]
    assert expected in result["detail"], result
    assert not captured.get("posts")
    assert auth_app.state.host_registry.get(host_id).outbound_queue.empty()


async def test_same_host_still_requires_host_ownership(auth_client, auth_app, peers, db_uri):
    from tests.server.integration.test_sessions_permissions import _register_online_host

    source, _, captured = peers
    host_id = "a" * 32
    _register_online_host(auth_app, host_id, "bob")
    SqlAlchemyConversationStore(db_uri).set_host_id(source["id"], host_id, workspace="/tmp")
    result = json.loads(
        await execute_tool(
            tool_name="sys_session_create",
            arguments=json.dumps({"agent_id": source["agent_id"], "placement": "same_host"}),
            server_client=auth_client,
            conversation_id=source["id"],
        )
    )
    assert "error" in result, result
    assert not captured.get("posts")
    assert auth_app.state.host_registry.get(host_id).outbound_queue.empty()


@pytest.mark.parametrize("move_before_recheck", [False, True])
async def test_same_host_creation_uses_current_source_and_stays_private(
    auth_client, auth_app, peers, db_uri, monkeypatch, move_before_recheck
):
    from omnigent.host.frames import HostLaunchRunnerFrame, HostStatFrame, decode_host_frame
    from omnigent.server.routes.sessions import routes_core
    from omnigent.server.sharing_settings import DefaultPublicSessions
    from tests.server.integration.test_sessions_permissions import _register_online_host

    source, _, _ = peers
    hosts = [uuid4().hex for _ in range(3)]
    for host_id, owner in zip(hosts, ("alice", "alice", "bob"), strict=True):
        _register_online_host(auth_app, host_id, owner)
        auth_app.state.host_store.upsert_on_connect(host_id, host_id, owner)
    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(source["id"], hosts[0], workspace="/original")
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
    response = await auth_client.post(
        "/v1/sessions/orchestrated",
        headers={ORCHESTRATION_SOURCE_HEADER: source["id"]},
        json={"agent_id": source["agent_id"], "host_id": hosts[2], "workspace": "/requested"},
    )
    assert response.status_code == 201, response.text
    created = store.get_conversation(response.json()["id"])
    expected_host = hosts[1] if move_before_recheck else hosts[0]
    expected_workspace = "/moved" if move_before_recheck else "/original"
    assert checks == 2
    assert created.parent_conversation_id is None
    assert (created.host_id, created.workspace) == (expected_host, expected_workspace)
    assert frames and all(host == expected_host for host, _ in frames)
    launches = [frame for _, frame in frames if isinstance(frame, HostLaunchRunnerFrame)]
    assert len(launches) == 1 and launches[0].workspace == expected_workspace
    grants, _ = SqlAlchemyPermissionStore(db_uri).list_for_session(created.id)
    assert [grant.user_id for grant in grants] == ["alice"]


@pytest.mark.parametrize("shape", ["parent", "multipart"])
async def test_independent_creation_rejects_child_or_bundle_shape(auth_client, peers, shape):
    from tests._helpers.session import post_session_bundle

    source, _, _ = peers
    headers = {ORCHESTRATION_SOURCE_HEADER: source["id"]}
    if shape == "parent":
        response = await auth_client.post(
            "/v1/sessions/orchestrated",
            headers=headers,
            json={"agent_id": source["agent_id"], "parent_session_id": source["id"]},
        )
        expected = "Orchestration creates independent sessions"
    else:
        response = await post_session_bundle(
            auth_client.post, "/v1/sessions", b"unused", metadata={}, headers=headers
        )
        expected = "Independent sessions require agent_id"
    assert response.status_code == 400, response.text
    assert expected in response.text


@pytest.mark.parametrize("provider", ["modal", "daytona", "embedding"])
async def test_managed_creation_uses_default_provider_and_saves_private_first_task(
    auth_client, auth_app, peers, db_uri, monkeypatch, provider
):
    import asyncio
    from dataclasses import replace

    from omnigent.server.managed_hosts import parse_sandbox_config
    from omnigent.server.routes.sessions import routes_core
    from omnigent.server.sharing_settings import DefaultPublicSessions

    source, _, _ = peers
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
        assert "First task" in str(store.list_pending_inputs(session_id))
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


@pytest.mark.parametrize("placement", ["same_host", "managed"])
async def test_unsupported_route_never_uses_ordinary_creation(
    auth_client, auth_app, peers, monkeypatch, placement
):
    from omnigent.server.routes.sessions import routes_core

    source, _, _ = peers
    monkeypatch.setattr(
        auth_app.router,
        "routes",
        [
            r
            for r in auth_app.router.routes
            if getattr(r, "path", None) != "/v1/sessions/orchestrated"
        ],
    )

    async def unexpected_creation(*args, **kwargs):
        pytest.fail("Unsupported orchestration must not fall back to ordinary creation")

    monkeypatch.setattr(routes_core, "_create_session_from_existing_agent", unexpected_creation)
    result = json.loads(
        await execute_tool(
            tool_name="sys_session_create",
            arguments=json.dumps({"agent_id": source["agent_id"], "placement": placement}),
            server_client=auth_client,
            conversation_id=source["id"],
        )
    )
    assert "unavailable on this server" in result["error"]
