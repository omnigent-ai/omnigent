"""Account revocation across HTTP, credential, background, and launch boundaries."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from tests.server.helpers import build_agent_bundle
from tests.server.test_accounts import _build_accounts_app, _login


def create_session(client, *, guardrails=None):
    return client.post(
        "/v1/sessions",
        data={"metadata": "{}"},
        files={
            "bundle": (
                "agent.tar.gz",
                build_agent_bundle(name="review-agent", guardrails=guardrails),
                "application/gzip",
            )
        },
    )


@pytest.fixture
def setup_app(tmp_path, monkeypatch):
    import omnigent.server.app as app_module

    captured = {}
    original = app_module.create_app

    def capture(**kwargs):
        from omnigent.stores.scheduled_task_store.sqlalchemy_store import (
            SqlAlchemyScheduledTaskStore,
        )

        kwargs["scheduled_task_store"] = SqlAlchemyScheduledTaskStore(
            str(kwargs["account_store"]._engine.url)
        )
        captured.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(app_module, "create_app", capture)
    monkeypatch.delenv("OMNIGENT_OIDC_ISSUER", raising=False)
    yield_from = _build_accounts_app(tmp_path, monkeypatch, init_admin_password="admin-pw-12345")
    client = next(yield_from)
    try:
        admin = _login(client, "admin", "admin-pw-12345")
        invite = admin.post("/auth/invite", json={}).json()["token"]
        alice = TestClient(client.app)
        response = alice.post(
            "/auth/register",
            json={"invite": invite, "username": "alice", "password": "alice-pw-1234"},
        )
        assert response.status_code == 200, response.text
        yield admin, alice, captured, original
    finally:
        yield_from.close()


def test_login_cannot_issue_refresh_after_delete(setup_app, monkeypatch):
    import omnigent.server.routes.device_auth as device_auth

    admin, alice, stores, _ = setup_app
    reached, resume = Event(), Event()
    original_issue = device_auth.issue_login_grant

    def paused_issue(*args, **kwargs):
        reached.set()
        assert resume.wait(15)
        return original_issue(*args, **kwargs)

    monkeypatch.setattr(device_auth, "issue_login_grant", paused_issue)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            alice.post,
            "/auth/login",
            json={"username": "alice", "password": "alice-pw-1234", "issue_refresh": True},
        )
        try:
            assert reached.wait(15)
            assert admin.delete("/auth/users/alice").status_code == 204
            assert stores["account_store"].get_user("alice") is None
        finally:
            resume.set()
        login = pending.result(timeout=15)
    assert login.status_code == 401, login.text
    assert "refresh_token" not in login.json()
    assert stores["account_store"].get_user("alice") is None
    assert create_session(alice).status_code == 401


def test_old_cookie_rejected_after_username_reuse(setup_app):
    admin, alice, _stores, _ = setup_app
    assert alice.get("/auth/me").status_code == 200
    assert admin.delete("/auth/users/alice").status_code == 204
    assert alice.get("/auth/me").status_code == 401
    invite = admin.post("/auth/invite", json={}).json()["token"]
    replacement = TestClient(admin.app)
    response = replacement.post(
        "/auth/register",
        json={"invite": invite, "username": "alice", "password": "different-password-1234"},
    )
    assert response.status_code == 200, response.text
    replay = alice.get("/auth/me")
    assert replay.status_code == 401, replay.text


def test_reset_provisions_shared_user_credentials(setup_app):
    admin, alice, stores, _ = setup_app
    session_id = create_session(alice).json()["session_id"]
    shared = alice.put(
        f"/v1/sessions/{session_id}/permissions", json={"user_id": "new-member", "level": 1}
    )
    assert shared.status_code == 200, shared.text
    accounts = stores["account_store"]
    assert accounts.login_snapshot("new-member") == (None, None)
    reset = admin.post("/auth/users/new-member/reset")
    assert reset.status_code == 200, reset.text
    member = TestClient(admin.app)
    login = member.post(
        "/auth/login",
        json={"username": "new-member", "password": reset.json()["new_password"]},
    )
    assert login.status_code == 200, login.text
    assert member.get("/auth/me").json()["id"] == "new-member"
    assert member.get(f"/v1/sessions/{session_id}").status_code == 200
    generation = accounts.get_user("new-member").account_generation
    assert generation is not None
    assert admin.post("/auth/users/new-member/reset").status_code == 200
    assert accounts.get_user("new-member").account_generation == generation


@pytest.mark.parametrize("operation", ["cost", "approval"])
@pytest.mark.parametrize("actor", ["owner", "admin"])
@pytest.mark.parametrize("revocation", ["none", "delete", "replace"])
def test_daily_budget_write_obeys_account_lifecycle(
    setup_app, monkeypatch, operation, actor, revocation
):
    from omnigent.db.utils import now_epoch, utc_day

    admin, alice, stores, _ = setup_app
    conversations = stores["conversation_store"]
    today = utc_day(now_epoch())
    guardrails = {
        "policies": {
            "daily_budget": {
                "type": "function",
                "function": {
                    "path": "omnigent.policies.builtins.cost.user_daily_cost_budget",
                    "arguments": {"max_cost_usd": 1.0, "ask_thresholds_usd": [0.05]},
                },
            }
        }
    }

    def report_cost(client, session_id, amount):
        return client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "external_session_usage", "data": {"cumulative_cost_usd": amount}},
        )

    def evaluate(client, session_id):
        response = client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "function_call",
                "data": {
                    "evaluate_policy": True,
                    "name": "sys_os_shell",
                    "arguments": "{}",
                    "agent": "review-agent",
                    "call_id": "budget-probe",
                },
            },
        )
        assert response.status_code == 202, response.text
        return response.json()

    session_id = create_session(alice, guardrails=guardrails).json()["session_id"]
    assert report_cost(alice, session_id, 0.1).status_code == 202
    request_client = alice if actor == "owner" else admin
    if operation == "approval":
        verdict = evaluate(request_client, session_id)
        assert verdict["verdict"] == "pending", verdict
        body = {
            "type": "approval",
            "data": {"elicitation_id": verdict["elicitation_id"], "action": "accept"},
        }
        writer_name = "set_daily_ask_approved"
    else:
        body = {"type": "external_session_usage", "data": {"cumulative_cost_usd": 0.2}}
        writer_name = "add_daily_cost"

    reached, resume = Event(), Event()
    original = getattr(conversations, writer_name)

    def paused_write(*args, **kwargs):
        reached.set()
        assert resume.wait(15), "budget writer did not resume"
        return original(*args, **kwargs)

    def replace_account():
        invite = admin.post("/auth/invite", json={}).json()["token"]
        replacement = TestClient(admin.app)
        registered = replacement.post(
            "/auth/register",
            json={"invite": invite, "username": "alice", "password": "replacement-pw-1234"},
        )
        assert registered.status_code == 200, registered.text
        return replacement

    monkeypatch.setattr(conversations, writer_name, paused_write)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(request_client.post, f"/v1/sessions/{session_id}/events", json=body)
        try:
            assert reached.wait(15), "request did not reach the budget write"
            if revocation != "none":
                deleted = admin.delete("/auth/users/alice")
                assert deleted.status_code == 204, deleted.text
                if revocation == "replace":
                    replacement = replace_account()
        finally:
            resume.set()
        response = pending.result(timeout=15)
    monkeypatch.setattr(conversations, writer_name, original)

    if revocation == "none":
        assert response.status_code == 202, response.text
        assert conversations.get_daily_cost_state("alice", today) == {
            "cost_usd": 0.2 if operation == "cost" else 0.1,
            "ask_approved_usd": 0.05 if operation == "approval" else 0.0,
        }
        if operation == "approval":
            assert evaluate(alice, session_id)["verdict"] == "allow"
        return

    assert conversations.get_daily_cost_state("alice", today) == {
        "cost_usd": 0.0,
        "ask_approved_usd": 0.0,
    }, response.text
    assert response.status_code == (401 if actor == "owner" else 409), response.text
    if revocation == "delete":
        replacement = replace_account()
    new_session = create_session(replacement, guardrails=guardrails).json()["session_id"]
    assert report_cost(replacement, new_session, 0.1).status_code == 202
    assert conversations.get_daily_cost_state("alice", today) == {
        "cost_usd": 0.1,
        "ask_approved_usd": 0.0,
    }
    assert evaluate(replacement, new_session)["verdict"] == "pending"


@pytest.mark.asyncio
async def test_accounts_auth_checks_once_per_request_without_blocking_loop(setup_app):
    import asyncio

    import httpx

    from omnigent.db.account_authority import account_generation, current_account_user

    admin, alice, stores, _ = setup_app
    auth = stores["auth_provider"]
    reached, resume = asyncio.Event(), Event()
    loop = asyncio.get_running_loop()
    checked = []
    validate = stores["account_store"].accepts_generation
    caller_identity = current_account_user()

    def paused_check(user_id, generation):
        if user_id == "alice":
            checked.append(generation)
            if len(checked) == 1:
                loop.call_soon_threadsafe(reached.set)
                assert resume.wait(2), "account validation blocked the request loop"
        return validate(user_id, generation)

    auth.set_account_check(paused_check)

    @admin.app.get("/_test/auth-context")
    async def auth_context(request: Request):
        user = auth.get_user_id(request)
        return {
            "user": user,
            "actor": current_account_user(),
            "generation": account_generation(user) if user is not None else None,
        }

    transport = httpx.ASGITransport(app=admin.app)
    async with (
        httpx.AsyncClient(
            transport=transport, base_url="http://testserver", cookies=alice.cookies
        ) as member,
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as public,
    ):
        pending = asyncio.create_task(member.get("/_test/auth-context"))
        try:
            await asyncio.wait_for(reached.wait(), timeout=1)
            health = await asyncio.wait_for(public.get("/health"), timeout=1)
            assert health.status_code == 200, health.text
        finally:
            resume.set()
            response = await pending
        generation = stores["account_store"].get_user("alice").account_generation
        assert response.status_code == 200, response.text
        assert response.json() == {"user": "alice", "actor": "alice", "generation": generation}
        assert checked == [generation]
        assert (await member.get("/auth/me")).status_code == 200
        assert current_account_user() == caller_identity
        assert checked == [generation, generation]
        deleted = await asyncio.to_thread(admin.delete, "/auth/users/alice")
        assert deleted.status_code == 204, deleted.text
        assert (await member.get("/auth/me")).status_code == 401
        assert checked == [generation, generation, generation]


def test_cached_auth_identity_does_not_cross_provider_or_workspace(setup_app):
    from omnigent.db.account_authority import current_account_user
    from omnigent.db.db_models import workspace_scope
    from omnigent.server.auth import UnifiedAuthProvider

    admin, alice, stores, _ = setup_app
    auth = stores["auth_provider"]
    other_provider = UnifiedAuthProvider(source="accounts", accounts_config=auth._accounts_config)
    other_provider.set_account_check(lambda *_: False)

    @admin.app.get("/_test/auth-isolation")
    def isolated_identity(request: Request):
        assert auth.get_user_id(request) == "alice"
        with workspace_scope(42):
            assert auth.get_user_id(request) is None
            assert current_account_user() is None
        assert other_provider.get_user_id(request) is None
        assert current_account_user() is None
        assert auth.get_user_id(request) == "alice"
        return {"actor": current_account_user()}

    response = alice.get("/_test/auth-isolation")
    assert response.status_code == 200, response.text
    assert response.json() == {"actor": "alice"}


@pytest.mark.asyncio
async def test_accounts_host_handshake_revalidates_each_connection(setup_app):
    import asyncio
    import uuid

    from asgiref.testing import ApplicationCommunicator

    from tests.server.integration.test_host_tunnel_route import (
        _make_hello,
        _wait_registered,
        _websocket_scope,
    )

    admin, alice, stores, _ = setup_app
    auth = stores["auth_provider"]
    checked = []
    validate = stores["account_store"].accepts_generation

    def record_check(user_id, generation):
        if user_id == "alice":
            checked.append(generation)
        return validate(user_id, generation)

    auth.set_account_check(record_check)
    generation = stores["account_store"].get_user("alice").account_generation
    host_id = uuid.uuid4().hex
    scope = _websocket_scope(f"/v1/hosts/{host_id}/tunnel")
    cookie = "; ".join(f"{key}={value}" for key, value in alice.cookies.items())
    scope["headers"] = [(b"cookie", cookie.encode())]
    connection = ApplicationCommunicator(admin.app, dict(scope))
    await connection.send_input({"type": "websocket.connect"})
    try:
        response = await connection.receive_output(timeout=5)
        assert response["type"] == "websocket.accept", response
        await connection.send_input({"type": "websocket.receive", "text": _make_hello()})
        registry = admin.app.state.host_registry
        await asyncio.wait_for(_wait_registered(registry, host_id), timeout=5)
        assert registry.get(host_id).account_generation == generation
        assert checked == [generation]
    finally:
        await connection.send_input({"type": "websocket.disconnect", "code": 1000})
        await connection.wait(timeout=5)

    assert (await asyncio.to_thread(admin.delete, "/auth/users/alice")).status_code == 204
    rejected = ApplicationCommunicator(admin.app, dict(scope))
    await rejected.send_input({"type": "websocket.connect"})
    response = await rejected.receive_output(timeout=5)
    assert response["type"] == "websocket.close", response
    await rejected.wait(timeout=5)
    assert checked == [generation, generation]


@pytest.mark.asyncio
async def test_late_schedule_registration_stops_after_account_deletion(setup_app, monkeypatch):
    import asyncio

    from tests.server.scheduled.test_scheduler import FakeClock, FakeScheduleSeam

    admin, alice, stores, _ = setup_app
    agent_id = create_session(alice).json()["agent_id"]
    scheduler = admin.app.state.scheduled_task_scheduler
    tasks = scheduler._store
    clock, timers = FakeClock(), FakeScheduleSeam()
    monkeypatch.setattr(scheduler, "_now", clock.now)
    monkeypatch.setattr(scheduler, "_schedule_call", timers)
    monkeypatch.setattr(scheduler, "_cancel_call", timers.cancel)
    snapshot_taken, task_committed, delete_resume, create_resume = (Event() for _ in range(4))
    delete = stores["account_store"].delete_user
    create = tasks.create

    def pause_delete(user_id):
        snapshot_taken.set()
        assert delete_resume.wait(15)
        return delete(user_id)

    def pause_create(*args, **kwargs):
        task = create(*args, **kwargs)
        task_committed.set()
        assert create_resume.wait(15)
        return task

    monkeypatch.setattr(stores["account_store"], "delete_user", pause_delete)
    monkeypatch.setattr(tasks, "create", pause_create)
    with ThreadPoolExecutor(max_workers=2) as pool:
        deleting = pool.submit(admin.delete, "/auth/users/alice")
        try:
            assert await asyncio.to_thread(snapshot_taken.wait, 10)
            creating = pool.submit(
                alice.post,
                "/v1/scheduled-tasks",
                json={
                    "name": "late registration",
                    "prompt": "hello",
                    "rrule": "FREQ=DAILY",
                    "agent_id": agent_id,
                    "timezone": "UTC",
                },
            )
            assert await asyncio.to_thread(task_committed.wait, 10)
            delete_resume.set()
            deleted = await asyncio.to_thread(deleting.result, 10)
            assert deleted.status_code == 204, deleted.text
            create_resume.set()
            created = await asyncio.to_thread(creating.result, 10)
            assert created.status_code == 200, created.text
        finally:
            delete_resume.set()
            create_resume.set()
    task_id = created.json()["id"]
    assert tasks.get(task_id).state == "deleted"
    assert scheduler.job_count == 1
    clock.advance(timers.live()[0].delay)
    await timers.fire_latest()
    assert scheduler.job_count == 0
    assert timers.live() == []
    assert tasks.list_runs(task_id)[0] == []


def test_replica_cache_cannot_restore_account(setup_app):
    from omnigent.server.auth import create_auth_provider

    admin, alice, stores, create_app = setup_app
    replica_args = dict(stores)
    replica_auth = create_auth_provider()
    replica_args["auth_provider"] = replica_auth
    replica = TestClient(create_app(**replica_args))
    replica.cookies.update(alice.cookies)
    assert replica.get("/auth/me").status_code == 200
    assert admin.delete("/auth/users/alice").status_code == 204
    assert stores["account_store"].get_user("alice") is None
    created = create_session(replica)
    assert created.status_code == 401, created.text
    assert stores["account_store"].get_user("alice") is None
    replica_auth._cookie_cache.clear()
    assert replica.get("/auth/me").status_code == 401
    assert alice.get("/auth/me").status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize(
    "pause_at", ["before_owner", "after_owner", "before_stat", "before_workspace_stat"]
)
@pytest.mark.parametrize("replacement_owner", ["alice", "admin"])
async def test_stale_schedule_cannot_contact_replacement_host(
    setup_app, monkeypatch, pinned, pause_at, replacement_owner
):
    import asyncio
    import json
    import uuid

    from omnigent.db.account_authority import account_authority_scope
    from omnigent.host.frames import HostHelloFrame
    from omnigent.server.routes import _workspace_validation as workspace_module
    from omnigent.server.scheduled import fire as fire_module

    admin, alice, stores, _ = setup_app
    agent_id = create_session(alice).json()["agent_id"]
    tasks = stores["scheduled_task_store"]
    hosts = stores["host_store"]
    registry = admin.app.state.host_registry
    host_id = uuid.uuid4().hex

    def connect(connected_id, owner="alice"):
        host = hosts.upsert_on_connect(connected_id, "scheduled-host", owner)
        with account_authority_scope(owner, host.account_generation):
            return registry.register(
                connected_id,
                object(),
                HostHelloFrame(version="0.15.0", frame_protocol_version=1, name="scheduled-host"),
                owner,
            )

    connect(host_id)
    task = tasks.create(
        uuid.uuid4().hex,
        "old registration",
        "hello",
        "FREQ=DAILY",
        "alice",
        agent_id,
        "UTC",
        host_id=host_id if pinned else None,
        workspace="/tmp/review-workspace" if pause_at == "before_workspace_stat" else None,
    )
    deps = fire_module.FireDeps(
        scheduled_task_store=tasks,
        agent_store=stores["agent_store"],
        conversation_store=stores["conversation_store"],
        permission_store=stores["permission_store"],
        host_store=hosts,
        host_registry=registry,
        agent_cache=stores["agent_cache"],
    )
    reached, resume = asyncio.Event(), asyncio.Event()
    owner_check = fire_module._owner_is_deleted
    resolve_workspace = fire_module._resolve_default_workspace
    validate_workspace = workspace_module.validate_workspace

    async def pause_owner(*args):
        if pause_at == "before_owner":
            reached.set()
            await resume.wait()
        result = await owner_check(*args)
        if pause_at == "after_owner":
            reached.set()
            await resume.wait()
        return result

    async def pause_workspace(*args):
        if pause_at == "before_stat":
            reached.set()
            await resume.wait()
        return await resolve_workspace(*args)

    async def pause_workspace_validation(**kwargs):
        if pause_at == "before_workspace_stat":
            reached.set()
            await resume.wait()
        return await validate_workspace(**kwargs)

    sent = []
    send = registry.send_text

    def reply_to_stat(conn, data):
        send(conn, data)
        frame = json.loads(data)
        sent.append(frame)
        if frame["kind"] == "host.stat":
            conn.pending_stats[frame["request_id"]].set_result(
                {
                    "status": "ok",
                    "exists": True,
                    "type": "directory",
                    "canonical_path": "/tmp/review-workspace",
                }
            )

    async def unexpected_dispatch(*args):
        pytest.fail("retired scheduled work must not dispatch")

    monkeypatch.setattr(fire_module, "_owner_is_deleted", pause_owner)
    monkeypatch.setattr(fire_module, "_resolve_default_workspace", pause_workspace)
    monkeypatch.setattr(workspace_module, "validate_workspace", pause_workspace_validation)
    monkeypatch.setattr(registry, "send_text", reply_to_stat)
    before = len(stores["conversation_store"].list_conversations().data)
    firing = asyncio.create_task(
        fire_module._run_fire(
            deps,
            0,
            task.id,
            unexpected_dispatch,
            fire_module._make_connected_host_preflight(deps),
        )
    )
    try:
        await asyncio.wait_for(reached.wait(), timeout=10)
        deleted = await asyncio.to_thread(admin.delete, "/auth/users/alice")
        assert deleted.status_code == 204, deleted.text
        invite = admin.post("/auth/invite", json={}).json()["token"]
        replacement = TestClient(admin.app)
        registered = await asyncio.to_thread(
            replacement.post,
            "/auth/register",
            json={"invite": invite, "username": "alice", "password": "new-password-1234"},
        )
        assert registered.status_code == 200, registered.text
        replacement_id = (
            host_id
            if pinned or pause_at in ("before_stat", "before_workspace_stat")
            else uuid.uuid4().hex
        )
        conn = connect(replacement_id, replacement_owner)
        assert conn.account_generation != task.account_generation
        resume.set()
        await asyncio.wait_for(firing, timeout=10)
        assert sent == []
        assert conn.outbound_queue.empty()
        assert len(stores["conversation_store"].list_conversations().data) == before
        runs, _ = tasks.list_runs(task.id)
        assert len(runs) == 1 and runs[0].conversation_id is None
    finally:
        resume.set()
        if not firing.done():
            firing.cancel()
        await asyncio.gather(firing, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("admitted", [False, True])
async def test_scheduled_dispatch_obeys_revocation_boundary(setup_app, monkeypatch, admitted):
    import asyncio
    import json
    import uuid

    from omnigent.host.frames import HostHelloFrame
    from omnigent.server.routes import _host_launch
    from omnigent.server.scheduled.fire import FireDeps, _make_connected_host_dispatch, _run_fire
    from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore

    admin, alice, stores, _ = setup_app
    created = create_session(alice)
    assert created.status_code == 201, created.text
    agent_id = created.json()["agent_id"]
    host_id = uuid.uuid4().hex
    host = stores["host_store"].upsert_on_connect(host_id, "review-host", "alice")
    registry = admin.app.state.host_registry
    from omnigent.db.account_authority import account_authority_scope

    with account_authority_scope("alice", host.account_generation):
        conn = registry.register(
            host_id,
            object(),
            HostHelloFrame(version="0.15.0", frame_protocol_version=1, name="review-host"),
            "alice",
        )
    tasks = SqlAlchemyScheduledTaskStore(str(stores["account_store"]._engine.url))
    task = tasks.create(
        uuid.uuid4().hex,
        "review-fire",
        "hello",
        "FREQ=DAILY",
        "alice",
        agent_id,
        "UTC",
        host_id=host_id,
        workspace="/tmp/review-workspace",
    )
    deps = FireDeps(
        scheduled_task_store=tasks,
        agent_store=stores["agent_store"],
        conversation_store=stores["conversation_store"],
        permission_store=stores["permission_store"],
        host_store=stores["host_store"],
        host_registry=registry,
    )
    reached, resume = Event(), Event()
    original_resolve = _host_launch.resolve_host_launch

    def pause_after_resolution(**kwargs):
        target = original_resolve(**kwargs)
        reached.set()
        assert resume.wait(15)
        return target

    if admitted:
        original_admit = registry.launch_authorizer

        def pause_after_admission(*args):
            original_admit(*args)
            reached.set()
            assert resume.wait(15)

        registry.launch_authorizer = pause_after_admission
    else:
        monkeypatch.setattr(_host_launch, "resolve_host_launch", pause_after_resolution)
    fire = asyncio.create_task(
        _run_fire(deps, 0, task.id, _make_connected_host_dispatch(deps), None)
    )
    try:
        assert await asyncio.to_thread(reached.wait, 10)
        deleted = await asyncio.to_thread(admin.delete, "/auth/users/alice")
        assert deleted.status_code == 204, deleted.text
        assert stores["account_store"].get_user("alice") is None
        assert stores["host_store"].get_host(host_id) is None
        assert tasks.get(task.id).state == "deleted"
        resume.set()
        if admitted:
            frame = json.loads(await asyncio.wait_for(conn.outbound_queue.get(), timeout=10))
            assert frame["kind"] == "host.launch_runner"
            conn.pending_launches.pop(frame["request_id"]).set_result(
                {"status": "failed", "error": "test transport: runner was not started"}
            )
        await asyncio.wait_for(fire, timeout=10)
        assert conn.outbound_queue.empty()
        runs, _ = tasks.list_runs(task.id)
        assert len(runs) == 1 and runs[0].status == "failed"
    finally:
        resume.set()
        if not fire.done():
            fire.cancel()
        await asyncio.gather(fire, return_exceptions=True)


@pytest.mark.parametrize("delete_during_request", [False, True])
@pytest.mark.parametrize(
    ("host_owner", "revoked_owner"), [("alice", "alice"), ("admin", "alice"), ("admin", "admin")]
)
def test_runner_token_uses_saved_account_authority(
    setup_app, monkeypatch, delete_during_request, host_owner, revoked_owner
):
    import uuid

    from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id

    admin, alice, stores, _ = setup_app
    created = create_session(alice)
    assert created.status_code == 201, created.text
    session_id = created.json()["session_id"]
    binding = "test-runner-binding"
    runner_id = token_bound_runner_id(binding)
    host_id = uuid.uuid4().hex
    stores["host_store"].upsert_on_connect(host_id, "runner-host", host_owner)
    conversations = stores["conversation_store"]
    conversations.set_host_id(session_id, host_id, workspace="/tmp/review-workspace")
    conversations.replace_runner_id(session_id, runner_id)
    runner = TestClient(admin.app)

    def mint():
        return runner.post(
            f"/v1/runners/{runner_id}/token",
            headers={RUNNER_TUNNEL_TOKEN_HEADER: binding},
        )

    issued = mint()
    assert issued.status_code == 200, issued.text
    runner.headers["Authorization"] = f"Bearer {issued.json()['token']}"
    assert runner.get("/auth/me").status_code == 200
    # A separate administrator can delete either the host or session owner.
    stores["account_store"].set_admin("alice", True)
    deleter = alice if revoked_owner == "admin" else admin
    reached, resume = Event(), Event()
    original = stores["account_store"].with_runner_authority

    def paused(*args):
        reached.set()
        assert resume.wait(15)
        return original(*args)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = None
        if delete_during_request:
            monkeypatch.setattr(stores["account_store"], "with_runner_authority", paused)
            pending = pool.submit(mint)
            assert reached.wait(15)
        try:
            assert deleter.delete(f"/auth/users/{revoked_owner}").status_code == 204
            invite = deleter.post("/auth/invite", json={}).json()["token"]
            replacement = TestClient(admin.app)
            assert (
                replacement.post(
                    "/auth/register",
                    json={
                        "invite": invite,
                        "username": revoked_owner,
                        "password": "new-password-1234",
                    },
                ).status_code
                == 200
            )
        finally:
            resume.set()
        if pending is not None:
            assert pending.result(timeout=15).status_code == 401
    assert runner.get("/auth/me").status_code == (401 if revoked_owner == "alice" else 200)
    assert mint().status_code == 401


def test_oauth_connection_state_cannot_cross_username_reuse(setup_app):
    from urllib.parse import parse_qs, urlsplit

    from omnigent.server.routes.connections_base import ConnectStart, create_connection_router

    admin, alice, stores, _ = setup_app
    completed = []

    class Provider:
        provider = "test-provider"
        store = None

        def signing_key(self):
            return b"test-state-key-32-bytes-long-12345"

        def begin(self, request, build_state):
            return ConnectStart(authorize_url=f"https://provider.example/?state={build_state({})}")

        async def complete(self, user_id, code, claims):
            completed.append(user_id)

    admin.app.include_router(
        create_connection_router(Provider(), auth_provider=stores["auth_provider"]), prefix="/v1"
    )
    started = alice.get("/v1/connections/test-provider/connect", follow_redirects=False)
    assert started.status_code == 302
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    callback = "/v1/connections/test-provider/callback"
    params = {"code": "test-provider-code", "state": state}
    assert (
        "connected"
        in alice.get(callback, params=params, follow_redirects=False).headers["location"]
    )
    assert completed == ["alice"]
    assert admin.delete("/auth/users/alice").status_code == 204
    invite = admin.post("/auth/invite", json={}).json()["token"]
    replacement = TestClient(admin.app)
    assert (
        replacement.post(
            "/auth/register",
            json={"invite": invite, "username": "alice", "password": "new-password-1234"},
        ).status_code
        == 200
    )
    assert (
        "error"
        in replacement.get(callback, params=params, follow_redirects=False).headers["location"]
    )
    assert completed == ["alice"]


@pytest.mark.parametrize("cached_admin", [False, True])
def test_replacement_account_cannot_reuse_replica_permission_cache(setup_app, cached_admin):
    from omnigent.server.auth import create_auth_provider
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    admin, alice, stores, create_app = setup_app
    if cached_admin:
        stores["account_store"].set_admin("alice", True)
    created = create_session(admin if cached_admin else alice)
    assert created.status_code == 201
    session_id = created.json()["session_id"]
    replica_args = dict(stores)
    replica_args["auth_provider"] = create_auth_provider()
    permissions = SqlAlchemyPermissionStore(stores["permission_store"].storage_location)
    permissions._resolve_cache_ttl_s = 60
    replica_args["permission_store"] = permissions
    replica = TestClient(create_app(**replica_args))
    replica.cookies.update(alice.cookies)
    path = f"/v1/sessions/{session_id}/items"
    assert replica.get(path).status_code == 200
    assert permissions._resolve_cache
    assert admin.delete("/auth/users/alice").status_code == 204
    invite = admin.post("/auth/invite", json={}).json()["token"]
    replacement = TestClient(admin.app)
    assert (
        replacement.post(
            "/auth/register",
            json={"invite": invite, "username": "alice", "password": "new-password-1234"},
        ).status_code
        == 200
    )
    replica.cookies.clear()
    replica.cookies.update(replacement.cookies)
    assert replica.get("/auth/me").status_code == 200
    assert replica.get(path).status_code in (403, 404)


def test_replacement_account_cannot_read_predecessor_tasks(setup_app):
    import uuid

    from omnigent.server.routes.scheduled_tasks import create_scheduled_tasks_router
    from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore

    admin, alice, stores, _ = setup_app
    tasks = SqlAlchemyScheduledTaskStore(stores["account_store"].storage_location)
    admin.app.include_router(
        create_scheduled_tasks_router(
            tasks,
            agent_store=stores["agent_store"],
            conversation_store=stores["conversation_store"],
            auth_provider=stores["auth_provider"],
        ),
        prefix="/v1",
    )
    task = tasks.create(
        uuid.uuid4().hex,
        "private task",
        "private predecessor prompt",
        "FREQ=DAILY",
        "alice",
        uuid.uuid4().hex,
        "UTC",
        workspace="/private/workspace",
    )
    tasks.create_run(
        run_id=uuid.uuid4().hex,
        scheduled_task_id=task.id,
        status="succeeded",
        scheduled_at=100,
    )
    path = f"/v1/scheduled-tasks/{task.id}"
    assert alice.get(path).status_code == 200
    assert alice.get(path + "/runs").status_code == 200
    assert admin.delete("/auth/users/alice").status_code == 204
    invite = admin.post("/auth/invite", json={}).json()["token"]
    replacement = TestClient(admin.app)
    assert (
        replacement.post(
            "/auth/register",
            json={"invite": invite, "username": "alice", "password": "new-password-1234"},
        ).status_code
        == 200
    )
    listing = replacement.get("/v1/scheduled-tasks")
    assert listing.status_code == 200 and listing.json()["scheduled_tasks"] == []
    assert replacement.get(path).status_code == 404
    assert replacement.get(path + "/runs").status_code == 404


@pytest.mark.parametrize("launch_kind", ["initial", "inline", "transfer", "admin_relaunch"])
def test_http_launch_rechecks_revocation_before_binding(setup_app, monkeypatch, launch_kind):
    import uuid

    from omnigent.db.account_authority import account_authority_scope
    from omnigent.host.frames import HostHelloFrame
    from omnigent.server.routes import _host_launch, _workspace_validation, hosts

    admin, alice, stores, _ = setup_app
    created = create_session(alice)
    assert created.status_code == 201
    if launch_kind == "transfer":
        source_host_id = uuid.uuid4().hex
        stores["host_store"].upsert_on_connect(source_host_id, "source-host", "alice")
        stores["conversation_store"].set_host_id(
            created.json()["session_id"], source_host_id, workspace="/tmp/source"
        )
        assert stores["conversation_store"].set_runner_id(
            created.json()["session_id"], uuid.uuid4().hex
        )
        cleared = alice.patch(
            f"/v1/sessions/{created.json()['session_id']}", json={"runner_id": ""}
        )
        assert cleared.status_code == 200, cleared.text
    host_id = uuid.uuid4().hex
    host_owner = "admin" if launch_kind == "admin_relaunch" else "alice"
    requester = TestClient(admin.app)
    requester.cookies.update((admin if launch_kind == "admin_relaunch" else alice).cookies)
    host = stores["host_store"].upsert_on_connect(host_id, "launch-host", host_owner)
    if launch_kind == "admin_relaunch":
        stores["conversation_store"].set_host_id(
            created.json()["session_id"], host_id, workspace="/tmp/workspace"
        )
    registry = admin.app.state.host_registry
    with account_authority_scope(host_owner, host.account_generation):
        conn = registry.register(
            host_id,
            object(),
            HostHelloFrame(version="0.15.0", frame_protocol_version=1, name="launch-host"),
            host_owner,
        )

    async def stat(**kwargs):
        return {
            "status": "ok",
            "exists": True,
            "type": "directory",
            "canonical_path": kwargs["path"],
        }

    monkeypatch.setattr(_workspace_validation, "_ask_host_stat", stat)
    reached, resume = Event(), Event()
    snapshots = []
    original = _host_launch.resolve_host_launch

    def paused(**kwargs):
        target = original(**kwargs)
        snapshots.append(target.conv.id)
        reached.set()
        assert resume.wait(15)
        return target

    monkeypatch.setattr(_host_launch, "resolve_host_launch", paused)
    monkeypatch.setattr(hosts, "resolve_host_launch", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        if launch_kind == "inline":
            pending = pool.submit(
                requester.post,
                "/v1/sessions",
                json={
                    "agent_id": created.json()["agent_id"],
                    "host_id": host_id,
                    "workspace": "/tmp/workspace",
                },
            )
        else:
            pending = pool.submit(
                requester.post,
                f"/v1/hosts/{host_id}/runners",
                json={"session_id": created.json()["session_id"], "workspace": "/tmp/workspace"},
            )
        try:
            assert reached.wait(15)
            assert admin.delete("/auth/users/alice").status_code == 204
        finally:
            resume.set()
        response = pending.result(timeout=15)
    assert response.status_code == 401, response.text
    assert conn.outbound_queue.empty()
    assert stores["conversation_store"].get_conversation(snapshots[0]).runner_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow", ["switch_host", "cli_resume", "admin_launch"])
async def test_existing_session_transfers_between_hosts(setup_app, workflow):
    """Exercise production auth/admission and host frames through both client flows."""
    import asyncio
    import contextlib
    import uuid

    import httpx
    from asgiref.testing import ApplicationCommunicator

    from omnigent.host.daemon_launch import launch_or_reuse_daemon_runner
    from omnigent.host.frames import HostHelloFrame, encode_host_frame
    from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
    from tests.server.integration.test_session_host_launch import (
        _serve_one_launch,
        _websocket_scope,
    )

    admin, alice, stores, _ = setup_app
    app = admin.app
    created = await asyncio.to_thread(create_session, alice)
    assert created.status_code == 201, created.text
    session_id = created.json()["session_id"]
    source_id, destination_id = uuid.uuid4().hex, uuid.uuid4().hex
    comms = []
    bindings = {}
    launcher = admin if workflow == "admin_launch" else alice
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        cookies=launcher.cookies,
    ) as client:
        try:
            for host_id in (source_id, destination_id):
                scope = _websocket_scope(f"/v1/hosts/{host_id}/tunnel")
                cookie = client.build_request("GET", "/").headers["cookie"]
                scope["headers"] = [(b"cookie", cookie.encode())]
                comm = ApplicationCommunicator(app, scope)
                comms.append(comm)
                await comm.send_input({"type": "websocket.connect"})
                assert (await comm.receive_output(timeout=10))["type"] == "websocket.accept"
                await comm.send_input(
                    {
                        "type": "websocket.receive",
                        "text": encode_host_frame(
                            HostHelloFrame(
                                version="0.15.0", frame_protocol_version=1, name=host_id
                            )
                        ),
                    }
                )
                async with asyncio.timeout(10):
                    while app.state.host_registry.get(host_id) is None:
                        await asyncio.sleep(0.01)

            async def launch(host_id, comm, *, resume=False):
                serving = asyncio.create_task(_serve_one_launch(comm, launch_status="launched"))
                try:
                    if resume:
                        runner_id = await launch_or_reuse_daemon_runner(
                            client, host_id=host_id, session_id=session_id, workspace="/tmp/dest"
                        )
                    else:
                        response = await client.post(
                            f"/v1/hosts/{host_id}/runners",
                            json={"session_id": session_id, "workspace": "/tmp/dest"},
                        )
                        assert response.status_code == 200, response.text
                        runner_id = response.json()["runner_id"]
                    frame = await asyncio.wait_for(serving, timeout=10)
                    assert frame.session_id == session_id
                    assert token_bound_runner_id(frame.binding_token) == runner_id
                    bindings[runner_id] = frame.binding_token
                    return runner_id
                finally:
                    serving.cancel()
                    await asyncio.gather(serving, return_exceptions=True)

            original_runner = await launch(source_id, comms[0])
            if workflow != "cli_resume":
                cleared = await client.patch(
                    f"/v1/sessions/{session_id}",
                    json={"runner_id": "", "model_override": None, "silent": True},
                )
                assert cleared.status_code == 200, cleared.text
                before = stores["conversation_store"].get_conversation(session_id)
                assert before.runner_id is None
                assert before.host_id == source_id
            replacement_runner = await launch(
                destination_id, comms[1], resume=workflow == "cli_resume"
            )
            assert replacement_runner != original_runner
            after = stores["conversation_store"].get_conversation(session_id)
            assert after.host_id == destination_id
            assert after.runner_id == replacement_runner
            assert after.workspace == "/tmp/dest"
            assert stores["conversation_store"].get_session_owner(session_id) == "alice"
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as runner:
                issued = await runner.post(
                    f"/v1/runners/{replacement_runner}/token",
                    headers={RUNNER_TUNNEL_TOKEN_HEADER: bindings[replacement_runner]},
                )
                assert issued.status_code == 200, issued.text
                authenticated = await runner.get(
                    "/auth/me", headers={"Authorization": f"Bearer {issued.json()['token']}"}
                )
                assert authenticated.status_code == 200, authenticated.text
                assert authenticated.json()["id"] == "alice"
        finally:
            for comm in reversed(comms):
                with contextlib.suppress(Exception):
                    await comm.send_input({"type": "websocket.disconnect", "code": 1000})
                    await comm.wait(timeout=10)


@pytest.mark.parametrize("operation", ["reset", "share_lookup", "share_ensure", "share_grant"])
def test_target_write_cannot_cross_registration(setup_app, monkeypatch, operation):
    admin, _, stores, _ = setup_app
    requester = TestClient(admin.app)
    requester.cookies.update(admin.cookies)
    created = create_session(admin)
    assert created.status_code == 201
    session_id = created.json()["session_id"]
    reached, resume = Event(), Event()
    if operation == "reset":
        store, method = stores["account_store"], "update_password"
    else:
        store = stores["permission_store"]
        method = {"share_lookup": "get", "share_ensure": "ensure_user", "share_grant": "grant"}[
            operation
        ]
    original = getattr(store, method)

    def paused(user_id, *args, **kwargs):
        if user_id == "alice":
            reached.set()
            assert resume.wait(15)
        return original(user_id, *args, **kwargs)

    monkeypatch.setattr(store, method, paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        if operation == "reset":
            pending = pool.submit(requester.post, "/auth/users/alice/reset")
        else:
            pending = pool.submit(
                requester.put,
                f"/v1/sessions/{session_id}/permissions",
                json={"user_id": "alice", "level": 1},
            )
        try:
            assert reached.wait(15)
            assert admin.delete("/auth/users/alice").status_code == 204
            invite = admin.post("/auth/invite", json={}).json()["token"]
            replacement = TestClient(admin.app)
            assert (
                replacement.post(
                    "/auth/register",
                    json={
                        "invite": invite,
                        "username": "alice",
                        "password": "replacement-password-1234",
                    },
                ).status_code
                == 200
            )
        finally:
            resume.set()
        response = pending.result(timeout=15)
    assert response.status_code == 409, response.text
    assert admin.get("/auth/me").status_code == 200
    assert (
        replacement.post(
            "/auth/login",
            json={"username": "alice", "password": "replacement-password-1234"},
        ).status_code
        == 200
    )
    assert stores["permission_store"].get("alice", session_id) is None
