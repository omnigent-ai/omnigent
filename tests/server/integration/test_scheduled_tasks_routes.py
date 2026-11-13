"""Integration tests for the scheduled-tasks CRUD routes.

Uses a real ``SqlAlchemyScheduledTaskStore`` + ``SqlAlchemyPermissionStore`` so
the full request → store → response pipeline is exercised, including RRULE
validation (400s) and live-scheduler sync on every mutation.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.automations_availability import AUTOMATIONS_HEADER
from omnigent.db.utils import builtin_agent_id
from omnigent.native.native_coding_agents import CLAUDE_NATIVE_AGENT_NAME, CODEX_NATIVE_AGENT_NAME
from omnigent.runner.identity import (
    ORCHESTRATION_SOURCE_HEADER,
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.server.managed_hosts import ManagedSandboxConfig, ManagedSandboxDeployment
from omnigent.server.routes import scheduled_tasks as scheduled_tasks_routes
from omnigent.server.scheduled.dispatch_scheduler import (
    PersistentTaskScheduler,
    ScheduledTaskDispatchHooks,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import (
    SqlAlchemyScheduledTaskStore,
)
from tests.debug_log_helpers import capture_debug_rows
from tests.server.conftest import ControllableMockClient
from tests.server.helpers import FakeSandboxLauncher

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("frozen_recurrence_clock")]


def _sandbox_deployment(*, manages_profiles: bool = False) -> ManagedSandboxDeployment:
    launcher = FakeSandboxLauncher()
    launcher.manages_profiles = manages_profiles
    return ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            provider="company",
            server_url="https://test.example",
            token_ttl_s=100,
            launcher_factory=lambda: launcher,
            manages_profiles=manages_profiles,
        )
    )


@pytest.fixture(autouse=True)
def _stub_host_workspace_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _validate_workspace(**kwargs: object) -> str:
        workspace = kwargs["workspace"]
        if not isinstance(workspace, str) or not workspace.startswith("/"):
            from omnigent.errors import ErrorCode, OmnigentError

            raise OmnigentError(
                "workspace must be an absolute path starting with /",
                code=ErrorCode.INVALID_INPUT,
            )
        return workspace

    monkeypatch.setattr(
        scheduled_tasks_routes,
        "validate_existing_host_workspace",
        _validate_workspace,
    )


def _build_app(
    db_uri: str,
    tmp_path: Path,
    *,
    persistent: bool = False,
    local_single_user: bool = False,
    projects: bool = True,
    automations: bool | Callable[[], bool] | None = None,
    session_mcp_selection: object | None = None,
) -> FastAPI:
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.stores.host_store import HostStore

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        scheduled_task_store=SqlAlchemyScheduledTaskStore(db_uri),
        # A real host store so pinned-host create authorization (existence +
        # ownership) resolves against actual host rows. Without it,
        # ``app.state.host_store`` is None and the route skips the check.
        host_store=HostStore(db_uri),
        project_store=SqlAlchemyProjectStore(db_uri) if projects else None,
        automations=automations,
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=local_single_user),
        feature_flags=resolve_feature_flags(
            {"OMNIGENT_FEATURES": "automations_v2" if persistent else "-automations_v2"}
        ),
        server_config={"scheduled_task_scheduler": {"startup_retry_seconds": 0.01}},
        scheduled_task_dispatch=(
            ScheduledTaskDispatchHooks(AsyncMock(), AsyncMock(return_value=None))
            if persistent
            else None
        ),
        session_mcp_selection=session_mcp_selection,
    )


@pytest.fixture()
def auth_app(
    runtime_init: None, db_uri: str, tmp_path: Path, request: pytest.FixtureRequest
) -> FastAPI:
    app = _build_app(db_uri, tmp_path, persistent=getattr(request, "param", False))
    _make_user(db_uri)
    _register_host(app, "4b653f6031f35d168cc0b37caa1306d1", "alice@example.com")
    return app


def _register_host(app: FastAPI, host_id: str, owner: str) -> None:
    """Persist a host owned by ``owner`` so the pinned-host owner check resolves.

    A local store row is all the create-time authorization needs — it never
    contacts the host (no ``host.stat`` / workspace RPC in the no-workspace
    path), so the host does not need to be online in the registry.
    """
    app.state.host_store.upsert_on_connect(host_id, f"{owner}-laptop", owner)


@asynccontextmanager
async def _serve(
    app: FastAPI, mock_llm: ControllableMockClient, tmp_path: Path
) -> AsyncIterator[httpx.AsyncClient]:
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)

    # Enter the lifespan so app.state.scheduled_task_scheduler exists and the
    # routes can sync to it.
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


@pytest_asyncio.fixture()
async def auth_client(
    auth_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    async with _serve(auth_app, mock_llm, tmp_path) as client:
        yield client


def _headers(email: str = "alice@example.com") -> dict[str, str]:
    return {"X-Forwarded-Email": email}


def _make_user(db_uri: str, email: str = "alice@example.com") -> None:
    SqlAlchemyPermissionStore(db_uri).ensure_user(email, is_admin=False)


_VALID_RRULE = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0"


def _create_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "nightly triage",
        "prompt": "triage the queue",
        "rrule": _VALID_RRULE,
        "agent_id": builtin_agent_id(CLAUDE_NATIVE_AGENT_NAME),
        "timezone": "America/Los_Angeles",
        "workspace": "/repo",
        "host_id": "4b653f6031f35d168cc0b37caa1306d1",
    }
    body.update(overrides)
    return body


@pytest.mark.parametrize("operation", ["create", "update"])
async def test_schedule_validation_keeps_requests_responsive(
    auth_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    import asyncio
    import threading

    url = "/v1/scheduled-tasks"
    method = "POST"
    body = _create_body(rrule="FREQ=DAILY")
    if operation == "update":
        created = await auth_client.post(url, json=body, headers=_headers())
        assert created.status_code == 200, created.text
        url += "/" + created.json()["id"]
        method = "PATCH"
        body = {"rrule": "FREQ=WEEKLY"}

    entered = threading.Event()
    release = threading.Event()

    def recurrence_clock() -> float:
        # Hold the clock boundary to check concurrent HTTP service, then expire
        # the real iterator's deadline without an expensive recurrence.
        if not entered.is_set():
            entered.set()
            assert release.wait(5), "recurrence validation blocked the event loop"
            return 0
        return 1

    monkeypatch.setattr("omnigent.server.scheduled._bounded_rrule.thread_time", recurrence_clock)
    pending = asyncio.create_task(auth_client.request(method, url, json=body, headers=_headers()))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        response = await asyncio.wait_for(
            auth_client.get("/v1/scheduled-tasks", headers=_headers()), timeout=2
        )
        assert response.status_code == 200, response.text
        assert not pending.done()
    finally:
        release.set()
        result = await pending
    assert result.status_code == 400, result.text
    assert "time limit" in result.text


async def test_create_lists_and_gets(auth_client: httpx.AsyncClient, db_uri: str) -> None:
    _make_user(db_uri)
    headers = {
        **_headers(),
        "x-omnigent-automation-source": "ui",
        "x-omnigent-automation-template": "jira",
    }
    with capture_debug_rows("server") as rows:
        resp = await auth_client.post(
            "/v1/scheduled-tasks",
            json=_create_body(name="private-name", prompt="private-prompt", max_cost_usd=2.5),
            headers=headers,
        )
    assert resp.status_code == 200, resp.text
    [event] = [row for row in rows if row["event_name"] == "scheduled_task_created"]
    assert event["user_id"] == "alice@example.com"
    assert event["attributes"]["task_id"] == resp.json()["id"]
    assert event["attributes"]["creation_source"] == "ui"
    assert event["attributes"]["template_id"] == "jira"
    assert "private-name" not in json.dumps(event)
    assert "private-prompt" not in json.dumps(event)
    created = resp.json()
    assert created["name"] == "private-name"
    assert created["rrule"] == _VALID_RRULE
    assert created["owner_user_id"] == "alice@example.com"
    assert created["workspace"] == "/repo"
    assert created["host_id"] == "4b653f6031f35d168cc0b37caa1306d1"
    assert created["max_cost_usd"] == 2.5
    assert created["base_branch"] is None
    assert created["execution_target"] == "connected_host"
    task_id = created["id"]

    listed = await auth_client.get("/v1/scheduled-tasks", headers=_headers())
    assert listed.status_code == 200
    ids = [t["id"] for t in listed.json()["scheduled_tasks"]]
    assert task_id in ids

    got = await auth_client.get(f"/v1/scheduled-tasks/{task_id}", headers=_headers())
    assert got.status_code == 200
    assert got.json()["id"] == task_id
    assert got.json()["max_cost_usd"] == 2.5


@pytest.mark.parametrize(
    ("mode", "expected_status"),
    [
        ("bound-token", 200),
        ("legacy-token-only", 200),
        ("allow-listed", 200),
        ("loopback", 200),
        ("wrong-token", 403),
        ("unowned-source", 403),
        ("unbound-source", 403),
        ("missing-source", 403),
        ("empty-source", 403),
        ("missing-token", 403),
        ("loopback-missing-bound-token", 403),
        ("invalid-allow-listed-token", 403),
    ],
)
async def test_agent_mutations_validate_origin_at_http_boundary(
    auth_app: FastAPI,
    auth_client: httpx.AsyncClient,
    db_uri: str,
    mode: str,
    expected_status: int,
) -> None:
    token = "scheduled-approval-token"
    conversations = SqlAlchemyConversationStore(db_uri)
    source = conversations.create_conversation()
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.grant("alice@example.com", source.id, 1 if mode == "unowned-source" else 4)
    runner_id = (
        "stable-runner"
        if mode in {"allow-listed", "loopback", "invalid-allow-listed-token"}
        else token_bound_runner_id(token)
    )
    if mode != "unbound-source":
        conversations.set_runner_id(source.id, runner_id)
    headers = {
        **_headers(),
        ORCHESTRATION_SOURCE_HEADER: source.id,
        RUNNER_TUNNEL_TOKEN_HEADER: token,
    }
    if mode == "legacy-token-only":
        headers.pop(ORCHESTRATION_SOURCE_HEADER)
    elif mode == "empty-source":
        headers[ORCHESTRATION_SOURCE_HEADER] = ""
    elif mode == "missing-source":
        headers[ORCHESTRATION_SOURCE_HEADER] = uuid.uuid4().hex
    elif mode == "wrong-token":
        headers[RUNNER_TUNNEL_TOKEN_HEADER] = "different-token"
    elif mode in {"loopback", "missing-token", "loopback-missing-bound-token"}:
        headers.pop(RUNNER_TUNNEL_TOKEN_HEADER)
    if mode in {"allow-listed", "invalid-allow-listed-token"}:
        auth_app.state.runner_tunnel_tokens = frozenset(
            {token if mode == "allow-listed" else "another-token"}
        )
    peer = "127.0.0.1" if mode.startswith("loopback") else "192.0.2.10"
    transport = httpx.ASGITransport(app=auth_app, client=(peer, 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/v1/scheduled-tasks", json=_create_body(), headers=headers)
        assert created.status_code == expected_status, created.text
        if expected_status == 403:
            listed = await auth_client.get("/v1/scheduled-tasks", headers=_headers())
            assert listed.json()["scheduled_tasks"] == []
            created = await auth_client.post(
                "/v1/scheduled-tasks", json=_create_body(), headers=_headers()
            )
            assert created.status_code == 200, created.text
        task_id = created.json()["id"]
        updated = await client.patch(
            f"/v1/scheduled-tasks/{task_id}", json={"state": "paused"}, headers=headers
        )
        assert updated.status_code == expected_status, updated.text
        stored = await auth_client.get(f"/v1/scheduled-tasks/{task_id}", headers=_headers())
        assert stored.json()["state"] == ("paused" if expected_status == 200 else "active")


async def test_automations_gate_is_checked_per_request(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    mock_llm: ControllableMockClient,
) -> None:
    gate = {"state": "on"}

    def automations() -> bool:
        if gate["state"] == "broken":
            raise RuntimeError("gate unavailable")
        return gate["state"] == "on"

    app = _build_app(db_uri, tmp_path, automations=automations)
    _make_user(db_uri)
    _register_host(app, "4b653f6031f35d168cc0b37caa1306d1", "alice@example.com")

    session_id = _owned_session(db_uri)

    async with _serve(app, mock_llm, tmp_path) as client:

        async def enabled_in_info() -> bool:
            info = (await client.get("/v1/info", headers=_headers())).json()
            assert info["scheduled_task_inbox"] == info["automations_enabled"]
            # The runner reads the same gate off its bundle download to hide the tools.
            bundle = await client.get(
                f"/v1/sessions/{session_id}/agent/contents", headers=_headers()
            )
            assert bundle.status_code == 200, bundle.text
            expected = "1" if info["automations_enabled"] else "0"
            assert bundle.headers[AUTOMATIONS_HEADER] == expected
            return info["automations_enabled"]

        created = await client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
        assert created.status_code == 200, created.text
        task_url = f"/v1/scheduled-tasks/{created.json()['id']}"
        assert await enabled_in_info() is True

        # A failing gate is off too, so a broken deployment flag hides automations.
        for state in ("off", "broken"):
            gate["state"] = state
            for method, url in (
                ("GET", "/v1/scheduled-tasks"),
                ("GET", "/v1/scheduled-tasks/inbox"),
                ("POST", "/v1/scheduled-tasks"),
                ("GET", task_url),
                ("PATCH", task_url),
                ("POST", f"{task_url}/run"),
                ("GET", f"{task_url}/runs"),
                ("DELETE", task_url),
            ):
                body = _create_body() if method == "POST" else {} if method == "PATCH" else None
                response = await client.request(method, url, json=body, headers=_headers())
                assert response.status_code == 404, (state, method, url, response.text)
            assert await enabled_in_info() is False

        gate["state"] = "on"
        got = await client.get(task_url, headers=_headers())
        assert got.status_code == 200, got.text
        assert await enabled_in_info() is True


async def test_create_no_workspace_task_persists_null_host_and_workspace(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A task that does no code work omits workspace + host_id; the row persists
    both as null and the connected-host workspace validation is skipped."""
    _make_user(db_uri)
    body = _create_body()
    del body["workspace"]
    del body["host_id"]
    resp = await auth_client.post("/v1/scheduled-tasks", json=body, headers=_headers())
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert created["workspace"] is None
    assert created["host_id"] is None
    task_id = created["id"]

    # The null binding survives a round-trip read.
    got = await auth_client.get(f"/v1/scheduled-tasks/{task_id}", headers=_headers())
    assert got.status_code == 200
    assert got.json()["workspace"] is None
    assert got.json()["host_id"] is None


async def test_create_rejects_workspace_without_host(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A workspace with no host is a broken binding, not a no-workspace task."""
    _make_user(db_uri)
    body = _create_body()
    del body["host_id"]
    resp = await auth_client.post("/v1/scheduled-tasks", json=body, headers=_headers())
    assert resp.status_code == 400, resp.text


async def test_create_managed_sandbox_rejects_pinned_host(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A managed_sandbox task runs in a fresh sandbox; a pinned host/workspace is a 422."""
    _make_user(db_uri)
    # _create_body pins host_id + workspace by default — invalid with a sandbox.
    body = _create_body(execution_target="managed_sandbox")
    resp = await auth_client.post("/v1/scheduled-tasks", json=body, headers=_headers())
    assert resp.status_code == 422, resp.text


async def test_create_managed_sandbox_requires_configured_sandboxes(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """managed_sandbox is rejected (400) when the server has no sandbox config."""
    _make_user(db_uri)
    body = _create_body(execution_target="managed_sandbox")
    del body["host_id"]
    del body["workspace"]
    resp = await auth_client.post("/v1/scheduled-tasks", json=body, headers=_headers())
    assert resp.status_code == 400, resp.text


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("operation", ["create", "patch"])
async def test_profile_schedules_are_rejected_before_persistence(
    auth_app, auth_client, db_uri, enabled, managed, operation
):
    from omnigent.server.sandbox_profiles import SandboxProfileCatalog

    _make_user(db_uri)
    deployment = _sandbox_deployment(manages_profiles=managed)
    factory = Mock(side_effect=AssertionError("Schedule validation must not construct providers"))
    deployment.default.launcher_factory = factory
    auth_app.state.sandbox_config = deployment
    auth_app.state.sandbox_profiles = (
        None
        if not enabled
        else SandboxProfileCatalog(SqlAlchemyConversationStore(db_uri), deployment)
    )
    if operation == "patch":
        created = await auth_client.post(
            "/v1/scheduled-tasks", json=_create_body(), headers=_headers()
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["id"]
        response = await auth_client.patch(
            f"/v1/scheduled-tasks/{task_id}",
            json={"execution_target": "managed_sandbox"},
            headers=_headers(),
        )
    else:
        body = _create_body(execution_target="managed_sandbox")
        del body["host_id"], body["workspace"]
        response = await auth_client.post("/v1/scheduled-tasks", json=body, headers=_headers())
    assert response.status_code == (400 if managed else 200), response.text
    factory.assert_not_called()
    if managed:
        assert "cannot select a profile yet" in response.text
        tasks = (await auth_client.get("/v1/scheduled-tasks", headers=_headers())).json()[
            "scheduled_tasks"
        ]
        if operation == "create":
            assert tasks == []
        else:
            assert tasks[0]["execution_target"] == "connected_host"


async def test_profile_schedules_with_a_default_profile_are_saved(auth_app, auth_client, db_uri):
    import dataclasses

    from omnigent.server.sandbox_profiles import SandboxProfileCatalog

    _make_user(db_uri)
    deployment = _sandbox_deployment(manages_profiles=True)
    factory = Mock(side_effect=AssertionError("Schedule validation must not construct providers"))
    deployment = ManagedSandboxDeployment.single(
        dataclasses.replace(
            deployment.default, launcher_factory=factory, default_profile_id="build"
        )
    )
    auth_app.state.sandbox_config = deployment
    auth_app.state.sandbox_profiles = SandboxProfileCatalog(
        SqlAlchemyConversationStore(db_uri), deployment
    )
    body = _create_body(execution_target="managed_sandbox")
    del body["host_id"], body["workspace"]
    response = await auth_client.post("/v1/scheduled-tasks", json=body, headers=_headers())
    assert response.status_code == 200, response.text
    assert response.json()["execution_target"] == "managed_sandbox"
    factory.assert_not_called()


async def test_patch_execution_target_roundtrip_clears_host_and_workspace(
    auth_app: FastAPI, auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A pinned task can switch to managed_sandbox and back without a 400.

    Regression for the round-trip bug: switching to managed_sandbox must clear
    BOTH host_id and workspace (via the store's explicit-null), otherwise a
    retained workspace makes the switch back to connected_host fail validation
    with "host_id required when workspace is set".
    """
    _make_user(db_uri)

    auth_app.state.sandbox_config = _sandbox_deployment()
    try:
        created = (
            await auth_client.post(
                "/v1/scheduled-tasks", json=_create_body(base_branch="HEAD"), headers=_headers()
            )
        ).json()
        tid = created["id"]
        assert created["host_id"] is not None and created["workspace"] is not None

        to_managed = await auth_client.patch(
            f"/v1/scheduled-tasks/{tid}",
            json={"execution_target": "managed_sandbox"},
            headers=_headers(),
        )
        assert to_managed.status_code == 200, to_managed.text
        b = to_managed.json()
        assert b["execution_target"] == "managed_sandbox"
        assert b["host_id"] is None and b["workspace"] is None
        assert b["base_branch"] is None

        back = await auth_client.patch(
            f"/v1/scheduled-tasks/{tid}",
            json={"execution_target": "connected_host"},
            headers=_headers(),
        )
        assert back.status_code == 200, back.text
        assert back.json()["execution_target"] == "connected_host"
    finally:
        auth_app.state.sandbox_config = None


@pytest.mark.parametrize("workspace", ["https://github.com/example/runtime", "/repo", "bad-url"])
async def test_sandbox_repository_create_edit_and_target_switch(
    auth_app: FastAPI, auth_client: httpx.AsyncClient, db_uri: str, workspace: str
) -> None:
    _make_user(db_uri)
    auth_app.state.sandbox_config = _sandbox_deployment()
    try:
        response = await auth_client.post(
            "/v1/scheduled-tasks",
            json=_create_body(
                execution_target="managed_sandbox", host_id=None, workspace=workspace
            ),
            headers=_headers(),
        )
        if not workspace.startswith("https://"):
            assert response.status_code == 400, response.text
            return
        assert response.status_code == 200, response.text
        task = response.json()
        assert task["workspace"] == workspace
        assert task["host_id"] is None
        path = f"/v1/scheduled-tasks/{task['id']}"
        renamed = await auth_client.patch(path, json={"name": "Renamed"}, headers=_headers())
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["workspace"] == workspace
        changed = await auth_client.patch(
            path, json={"workspace": "https://github.com/example/universe"}, headers=_headers()
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["workspace"] == "https://github.com/example/universe"
        without_directory = await auth_client.patch(
            path,
            json={
                "execution_target": "connected_host",
                "host_id": _HOST_ID,
                "base_branch": "HEAD",
            },
            headers=_headers(),
        )
        assert without_directory.status_code == 400, without_directory.text
        assert "random worktree requires" in without_directory.text
        cleared = await auth_client.patch(path, json={"workspace": None}, headers=_headers())
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["workspace"] is None
        invalid = await auth_client.patch(path, json={"workspace": "/repo"}, headers=_headers())
        assert invalid.status_code == 400, invalid.text
        back = await auth_client.patch(
            path,
            json={"execution_target": "connected_host", "host_id": _HOST_ID},
            headers=_headers(),
        )
        assert back.status_code == 200, back.text
        assert back.json()["workspace"] is None
        invalid_clear = await auth_client.patch(path, json={"workspace": None}, headers=_headers())
        assert invalid_clear.status_code == 422, invalid_clear.text
        assert invalid_clear.json() == {
            "detail": [
                {
                    "type": "value_error",
                    "loc": ["body"],
                    "msg": "Value error, workspace cannot be null",
                    "input": {"workspace": None},
                    "ctx": {"error": {}},
                }
            ]
        }
        sandbox = await auth_client.patch(
            path,
            json={"execution_target": "managed_sandbox", "workspace": workspace},
            headers=_headers(),
        )
        assert sandbox.status_code == 200, sandbox.text
        assert sandbox.json()["workspace"] == workspace
        assert sandbox.json()["host_id"] is None
    finally:
        auth_app.state.sandbox_config = None


@pytest.mark.parametrize("edit", ["rename", "agent", "clear", "invalid", "repository"])
async def test_edit_legacy_sandbox_workspace(
    auth_app: FastAPI, auth_client: httpx.AsyncClient, db_uri: str, edit: str
) -> None:
    _make_user(db_uri)
    auth_app.state.sandbox_config = _sandbox_deployment()
    try:
        response = await auth_client.post(
            "/v1/scheduled-tasks",
            json=_create_body(execution_target="managed_sandbox", host_id=None, workspace=None),
            headers=_headers(),
        )
        assert response.status_code == 200, response.text
        task_id = response.json()["id"]
        store = SqlAlchemyScheduledTaskStore(db_uri)
        session_id = uuid.uuid4().hex
        store.update(task_id, workspace="/home/me", session_mode="continue", session_id=session_id)
        changes = {
            "rename": {"name": "Renamed"},
            "agent": {"agent_id": builtin_agent_id(CODEX_NATIVE_AGENT_NAME)},
            "clear": {"workspace": None},
            "invalid": {"workspace": "/another/directory"},
            "repository": {"workspace": "https://github.com/example/runtime"},
        }
        updated = await auth_client.patch(
            f"/v1/scheduled-tasks/{task_id}", json=changes[edit], headers=_headers()
        )
        if edit == "invalid":
            assert updated.status_code == 400, updated.text
            assert store.get(task_id).workspace == "/home/me"
        else:
            assert updated.status_code == 200, updated.text
            expected_workspace = changes[edit].get("workspace")
            assert updated.json()["workspace"] == expected_workspace
            assert store.get(task_id).workspace == expected_workspace
            expected_session = None if edit in {"agent", "repository"} else session_id
            assert updated.json()["session_id"] == expected_session
            assert store.get(task_id).session_id == expected_session
    finally:
        auth_app.state.sandbox_config = None


async def test_create_rejects_invalid_rrule(auth_client: httpx.AsyncClient, db_uri: str) -> None:
    _make_user(db_uri)
    # FREQ=SECONDLY fires far below the 1-hour floor.
    with capture_debug_rows("server") as rows:
        resp = await auth_client.post(
            "/v1/scheduled-tasks",
            json=_create_body(rrule="FREQ=SECONDLY"),
            headers=_headers(),
        )
    assert resp.status_code == 400, resp.text

    assert not [row for row in rows if row["event_name"] == "scheduled_task_created"]


async def test_create_rejects_unknown_agent(auth_client: httpx.AsyncClient, db_uri: str) -> None:
    _make_user(db_uri)
    resp = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(agent_id="missing_agent"),
        headers=_headers(),
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.parametrize("model_override", ["--danger", "bad model"])
async def test_create_rejects_invalid_model_override(
    auth_client: httpx.AsyncClient, db_uri: str, model_override: str
) -> None:
    _make_user(db_uri)
    resp = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(model_override=model_override),
        headers=_headers(),
    )
    assert resp.status_code == 400, resp.text


async def test_create_rejects_invalid_reasoning_effort(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    _make_user(db_uri)
    resp = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(reasoning_effort="extreme"),
        headers=_headers(),
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.parametrize(
    ("agent_name", "mode"),
    [(CLAUDE_NATIVE_AGENT_NAME, "acceptEdits")]
    + [
        (CODEX_NATIVE_AGENT_NAME, mode)
        for mode in (
            "default",
            "ask-for-approval",
            "approve-for-me",
            "full-access",
            "read-only",
            "bypass",
        )
    ],
)
async def test_create_persists_permission_mode(
    auth_client: httpx.AsyncClient, db_uri: str, agent_name: str, mode: str
) -> None:
    """A valid permission mode round-trips on create, read, and update."""
    _make_user(db_uri)
    resp = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(agent_id=builtin_agent_id(agent_name), permission_mode=mode),
        headers=_headers(),
    )
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert created["permission_mode"] == mode
    task_id = created["id"]

    got = await auth_client.get(f"/v1/scheduled-tasks/{task_id}", headers=_headers())
    assert got.status_code == 200
    assert got.json()["permission_mode"] == mode
    if mode == "full-access":
        # Patch out of and back into modes that skip approval; read-backs prove each saved.
        for patched_mode in ("read-only", "bypass"):
            changed = await auth_client.patch(
                f"/v1/scheduled-tasks/{task_id}",
                json={"permission_mode": patched_mode},
                headers=_headers(),
            )
            assert changed.status_code == 200, changed.text
            assert changed.json()["permission_mode"] == patched_mode
            reread = await auth_client.get(f"/v1/scheduled-tasks/{task_id}", headers=_headers())
            assert reread.json()["permission_mode"] == patched_mode


def _mcp_registry(app: FastAPI) -> None:
    from omnigent.server.mcp_registry import McpRegistry, McpRegistryConfig, McpService

    app.state.mcp_registry = McpRegistry(
        McpRegistryConfig(
            services=[
                McpService(
                    id=name,
                    title=name,
                    url="https://example.test/mcp",
                    auth="none",
                    tools=["read_ticket"],
                    allowed_users=[] if name == "restricted" else None,
                )
                for name in ("github", "linear", "restricted")
            ]
        ),
        None,
    )


async def test_mcp_services_round_trip_on_create_and_update(
    auth_app: FastAPI, auth_client: httpx.AsyncClient
) -> None:
    _mcp_registry(auth_app)
    created = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(mcp_services=["github", "github", "linear"]),
        headers=_headers(),
    )
    assert created.status_code == 200, created.text
    assert created.json()["mcp_services"] == ["github", "linear"]
    task_url = f"/v1/scheduled-tasks/{created.json()['id']}"

    renamed = await auth_client.patch(task_url, json={"name": "renamed"}, headers=_headers())
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["mcp_services"] == ["github", "linear"]
    replaced = await auth_client.patch(
        task_url, json={"mcp_services": ["linear"]}, headers=_headers()
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["mcp_services"] == ["linear"]
    cleared = await auth_client.patch(task_url, json={"mcp_services": []}, headers=_headers())
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["mcp_services"] == []


@pytest.mark.parametrize(
    ("service", "status"), [("missing", 403), ("restricted", 403), ("../bad", 400)]
)
async def test_mcp_services_the_owner_cannot_use_are_rejected(
    auth_app: FastAPI, auth_client: httpx.AsyncClient, service: str, status: int
) -> None:
    _mcp_registry(auth_app)
    created = await auth_client.post(
        "/v1/scheduled-tasks", json=_create_body(mcp_services=[service]), headers=_headers()
    )
    assert created.status_code == status, created.text
    listed = await auth_client.get("/v1/scheduled-tasks", headers=_headers())
    assert listed.json()["scheduled_tasks"] == []

    saved = await auth_client.post(
        "/v1/scheduled-tasks", json=_create_body(mcp_services=["github"]), headers=_headers()
    )
    task_url = f"/v1/scheduled-tasks/{saved.json()['id']}"
    patched = await auth_client.patch(
        task_url, json={"mcp_services": [service]}, headers=_headers()
    )
    assert patched.status_code == status, patched.text
    kept = await auth_client.get(task_url, headers=_headers())
    assert kept.json()["mcp_services"] == ["github"]


async def test_mcp_services_use_the_deployment_selection(
    runtime_init: None, db_uri: str, tmp_path: Path, mock_llm: ControllableMockClient
) -> None:
    checked: list[tuple[tuple[str, ...], str | None]] = []

    class _Selection:
        def validate(self, request, services, *, spec, user_id):
            checked.append((tuple(services), user_id))
            return tuple(services)

        def session_labels(self, services):
            return {}

        def selected_services(self, labels):
            return ()

    app = _build_app(db_uri, tmp_path, session_mcp_selection=_Selection())
    _make_user(db_uri)
    _register_host(app, "4b653f6031f35d168cc0b37caa1306d1", "alice@example.com")
    async with _serve(app, mock_llm, tmp_path) as client:
        created = await client.post(
            "/v1/scheduled-tasks",
            json=_create_body(mcp_services=["system.ai.github"]),
            headers=_headers(),
        )
        info = await client.get("/v1/info")
    assert created.status_code == 200, created.text
    assert created.json()["mcp_services"] == ["system.ai.github"]
    assert checked == [(("system.ai.github",), "alice@example.com")]
    assert info.json()["scheduled_task_mcp_services"] is True


async def test_mcp_services_that_do_not_fit_the_column_are_rejected(
    auth_app: FastAPI, auth_client: httpx.AsyncClient
) -> None:
    class _AcceptAll:
        def validate(self, request, services, *, spec, user_id):
            return tuple(services)

        def session_labels(self, services):
            return {}

        def selected_services(self, labels):
            return ()

    auth_app.state.session_mcp_selection = _AcceptAll()
    long_names = [f"system.ai.{'x' * 100}{i:02d}" for i in range(40)]
    rejected = await auth_client.post(
        "/v1/scheduled-tasks", json=_create_body(mcp_services=long_names), headers=_headers()
    )
    assert rejected.status_code == 400, rejected.text
    assert "Too many MCP services" in rejected.text


@pytest.mark.parametrize("operation", ["create", "update"])
async def test_permission_save_requires_resolved_harness(
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    created = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(permission_mode="acceptEdits"),
        headers=_headers(),
    )
    assert created.status_code == 200, created.text
    task_url = f"/v1/scheduled-tasks/{created.json()['id']}"
    monkeypatch.setattr(
        AgentCache,
        "load",
        Mock(side_effect=PermissionError("agent cache directory is inaccessible")),
    )

    if operation == "create":
        response = await auth_client.post(
            "/v1/scheduled-tasks", json=_create_body(permission_mode="plan"), headers=_headers()
        )
    else:
        response = await auth_client.patch(
            task_url, json={"permission_mode": "plan"}, headers=_headers()
        )
    assert response.status_code == 400, response.text
    assert "Cannot resolve the agent's harness" in response.text
    saved = await auth_client.get(task_url, headers=_headers())
    assert saved.status_code == 200, saved.text
    assert saved.json()["permission_mode"] == "acceptEdits"

    cleared = await auth_client.patch(task_url, json={"permission_mode": None}, headers=_headers())
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["permission_mode"] is None


@pytest.mark.parametrize("permission_mode", ["unknown-permission-mode", "--danger", "Auto"])
async def test_create_rejects_invalid_permission_mode(
    auth_client: httpx.AsyncClient, db_uri: str, permission_mode: str
) -> None:
    _make_user(db_uri)
    resp = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(permission_mode=permission_mode),
        headers=_headers(),
    )
    assert resp.status_code == 400, resp.text
    assert "Unsupported permission_mode" in resp.text
    assert "JSON null (not the string 'null') for the agent default" in resp.text


async def test_create_rejects_relative_workspace(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    _make_user(db_uri)
    resp = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(workspace="relative/path"),
        headers=_headers(),
    )
    assert resp.status_code == 400, resp.text


async def test_create_pinned_host_without_workspace_persists_null_workspace(
    auth_app: FastAPI, auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A pinned host with NO workspace is allowed (e.g. an MCP-only task) WHEN
    the caller owns the host: the row persists the host and a null workspace, and
    the connected-host workspace RPC is skipped. The fire path defaults the
    workspace to host HOME. Ownership is still authorized at create (local read),
    so an owned/existing host is required — see the rejection tests below."""
    _make_user(db_uri)
    _register_host(auth_app, "4b653f6031f35d168cc0b37caa1306d1", "alice@example.com")
    body = _create_body()
    del body["workspace"]
    resp = await auth_client.post("/v1/scheduled-tasks", json=body, headers=_headers())
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert created["host_id"] == "4b653f6031f35d168cc0b37caa1306d1"
    assert created["workspace"] is None

    got = await auth_client.get(f"/v1/scheduled-tasks/{created['id']}", headers=_headers())
    assert got.status_code == 200
    assert got.json()["host_id"] == "4b653f6031f35d168cc0b37caa1306d1"
    assert got.json()["workspace"] is None


async def test_create_pinned_host_without_workspace_rejects_nonexistent_host(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A pinned host with NO workspace that references a NONEXISTENT host is
    rejected at create (404) instead of persisting an unvalidated host that only
    fails at fire time."""
    _make_user(db_uri)
    body = _create_body(host_id="dead1111beef2222dead3333beef4444")
    del body["workspace"]  # host_id set, no workspace → the fixed authz gap
    resp = await auth_client.post("/v1/scheduled-tasks", json=body, headers=_headers())
    assert resp.status_code == 404, resp.text


async def test_create_pinned_host_without_workspace_rejects_nonowned_host(
    auth_app: FastAPI, auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A pinned host with NO workspace owned by ANOTHER user is rejected at
    create (403) — create-time authorization mirrors the fire-path owner check so
    a caller cannot persist a reference to a host they do not own."""
    _make_user(db_uri, email="alice@example.com")
    _make_user(db_uri, email="bob@example.com")
    # The host belongs to bob; alice pins it with no workspace.
    _register_host(auth_app, "eeee5555ffff6666aaaa7777bbbb8888", "bob@example.com")
    body = _create_body(host_id="eeee5555ffff6666aaaa7777bbbb8888")
    del body["workspace"]
    resp = await auth_client.post(
        "/v1/scheduled-tasks", json=body, headers=_headers("alice@example.com")
    )
    assert resp.status_code == 403, resp.text


async def test_patch_add_host_without_workspace_authorizes_owner(
    auth_app: FastAPI, auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """PATCH shares ``_validate_launch_inputs``: adding a host_id with no
    workspace authorizes the pin. An owned host succeeds; a non-owned host is
    rejected (403)."""
    _make_user(db_uri, email="alice@example.com")
    _make_user(db_uri, email="bob@example.com")
    # Start from a no-host, no-workspace task (a valid MCP-only task).
    body = _create_body()
    del body["workspace"]
    del body["host_id"]
    created = (await auth_client.post("/v1/scheduled-tasks", json=body, headers=_headers())).json()
    task_id = created["id"]

    # PATCH in a host alice owns, still no workspace → 200.
    _register_host(auth_app, "aaaa1111bbbb2222cccc3333dddd4444", "alice@example.com")
    ok = await auth_client.patch(
        f"/v1/scheduled-tasks/{task_id}",
        json={"host_id": "aaaa1111bbbb2222cccc3333dddd4444"},
        headers=_headers(),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["host_id"] == "aaaa1111bbbb2222cccc3333dddd4444"
    assert ok.json()["workspace"] is None

    # PATCH in a host bob owns → 403 (not authorized), no drift from the fire path.
    _register_host(auth_app, "eeee5555ffff6666aaaa7777bbbb8888", "bob@example.com")
    denied = await auth_client.patch(
        f"/v1/scheduled-tasks/{task_id}",
        json={"host_id": "eeee5555ffff6666aaaa7777bbbb8888"},
        headers=_headers("alice@example.com"),
    )
    assert denied.status_code == 403, denied.text


@pytest.mark.parametrize("method", ["create", "patch"])
@pytest.mark.parametrize("workspace", [None, "/repo"])
@pytest.mark.parametrize("detached", [False, True])
async def test_rejects_existing_sandbox_before_workspace_rpc(
    auth_app: FastAPI,
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    workspace: str | None,
    detached: bool,
) -> None:
    """Managed host pins are invalid even after their sandbox has been detached."""
    host = auth_app.state.host_store.register_managed_host(
        host_id="aaaa1111bbbb2222cccc3333dddd4444",
        name="existing-sandbox",
        user_id="alice@example.com",
        token="test-launch-token",
        provider="agent_sandbox",
        sandbox_id="sandbox-existing",
        token_expires_at=2_000_000_000,
    )
    if detached:
        assert auth_app.state.host_store.detach_stale_managed_sandbox(
            host.host_id,
            sandbox_id=host.sandbox_id,
            expected_updated_at=host.updated_at,
        )
    workspace_probe = AsyncMock(side_effect=AssertionError("must reject before host RPC"))
    monkeypatch.setattr(
        scheduled_tasks_routes, "validate_existing_host_workspace", workspace_probe
    )

    if method == "create":
        response = await auth_client.post(
            "/v1/scheduled-tasks",
            json=_create_body(host_id=host.host_id, workspace=workspace),
            headers=_headers(),
        )
    else:
        created = await auth_client.post(
            "/v1/scheduled-tasks",
            json=_create_body(host_id=None, workspace=None),
            headers=_headers(),
        )
        assert created.status_code == 200, created.text
        response = await auth_client.patch(
            f"/v1/scheduled-tasks/{created.json()['id']}",
            json={
                "host_id": host.host_id,
                **({"workspace": workspace} if workspace is not None else {}),
            },
            headers=_headers(),
        )
        unchanged = await auth_client.get(
            f"/v1/scheduled-tasks/{created.json()['id']}", headers=_headers()
        )
        assert unchanged.json()["host_id"] is None
        assert unchanged.json()["workspace"] is None

    assert response.status_code == 400, response.text
    assert "select a new sandbox for each run" in response.text
    workspace_probe.assert_not_awaited()


@pytest.mark.parametrize("workspace", [None, "/repo"])
async def test_existing_sandbox_pin_checks_ownership_first(
    auth_app: FastAPI,
    auth_client: httpx.AsyncClient,
    db_uri: str,
    workspace: str | None,
) -> None:
    _make_user(db_uri, email="bob@example.com")
    host = auth_app.state.host_store.register_managed_host(
        host_id="aaaa1111bbbb2222cccc3333dddd4444",
        name="bobs-sandbox",
        user_id="bob@example.com",
        token="test-launch-token",
        provider="agent_sandbox",
        sandbox_id="sandbox-bob",
        token_expires_at=2_000_000_000,
    )
    response = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(host_id=host.host_id, workspace=workspace),
        headers=_headers(),
    )
    assert response.status_code == 403, response.text


async def _make_project(
    client: httpx.AsyncClient, name: str, owner: str = "alice@example.com"
) -> str:
    resp = await client.post("/v1/projects", json={"name": name}, headers=_headers(owner))
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def test_create_files_runs_into_an_owned_project(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    _make_user(db_uri)
    _make_user(db_uri, "bob@example.com")
    info = (await auth_client.get("/v1/info", headers=_headers())).json()
    assert info["scheduled_task_projects"] is True
    mine = await _make_project(auth_client, "mine")
    bobs = await _make_project(auth_client, "bob's", owner="bob@example.com")

    for project_id in (bobs, "not-a-project-id", ""):
        resp = await auth_client.post(
            "/v1/scheduled-tasks", json=_create_body(project_id=project_id), headers=_headers()
        )
        assert resp.status_code == 404, resp.text
    # Names are resolved by the client through the projects API, not here.
    by_name = await auth_client.post(
        "/v1/scheduled-tasks", json=_create_body(project_name="mine"), headers=_headers()
    )
    assert by_name.status_code == 422, by_name.text

    created = await auth_client.post(
        "/v1/scheduled-tasks", json=_create_body(project_id=mine), headers=_headers()
    )
    assert created.status_code == 200, created.text
    assert created.json()["project_id"] == mine
    # Stored by id, so renaming the project keeps the task filed in it.
    renamed = await auth_client.patch(
        f"/v1/projects/{mine}", json={"name": "renamed"}, headers=_headers()
    )
    assert renamed.status_code == 200, renamed.text
    got = await auth_client.get(f"/v1/scheduled-tasks/{created.json()['id']}", headers=_headers())
    assert got.json()["project_id"] == mine


async def test_update_moves_and_clears_the_task_project(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    _make_user(db_uri)
    _make_user(db_uri, "bob@example.com")
    first = await _make_project(auth_client, "first")
    second = await _make_project(auth_client, "second")
    bobs = await _make_project(auth_client, "bob's", owner="bob@example.com")
    created = (
        await auth_client.post(
            "/v1/scheduled-tasks", json=_create_body(project_id=first), headers=_headers()
        )
    ).json()
    task_url = f"/v1/scheduled-tasks/{created['id']}"

    moved = await auth_client.patch(task_url, json={"project_id": second}, headers=_headers())
    assert moved.status_code == 200, moved.text
    assert moved.json()["project_id"] == second
    renamed = await auth_client.patch(task_url, json={"name": "renamed"}, headers=_headers())
    assert renamed.json()["project_id"] == second

    for rejected_id in (bobs, ""):
        rejected = await auth_client.patch(
            task_url, json={"project_id": rejected_id}, headers=_headers()
        )
        assert rejected.status_code == 404, rejected.text
    assert (await auth_client.get(task_url, headers=_headers())).json()["project_id"] == second

    cleared = await auth_client.patch(task_url, json={"project_id": None}, headers=_headers())
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["project_id"] is None


async def test_task_projects_are_refused_without_a_project_store(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    mock_llm: ControllableMockClient,
) -> None:
    app = _build_app(db_uri, tmp_path, projects=False)
    _make_user(db_uri)
    _register_host(app, "4b653f6031f35d168cc0b37caa1306d1", "alice@example.com")
    async with _serve(app, mock_llm, tmp_path) as client:
        info = (await client.get("/v1/info", headers=_headers())).json()
        assert info["scheduled_task_projects"] is False
        filed = await client.post(
            "/v1/scheduled-tasks", json=_create_body(project_id="p" * 32), headers=_headers()
        )
        assert filed.status_code == 404, filed.text
        created = await client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
        assert created.status_code == 200, created.text
        task_url = f"/v1/scheduled-tasks/{created.json()['id']}"
        moved = await client.patch(task_url, json={"project_id": "p" * 32}, headers=_headers())
        assert moved.status_code == 404, moved.text


async def test_single_user_task_projects_are_the_projects_api_projects(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    mock_llm: ControllableMockClient,
) -> None:
    """The local runtime's projects belong to "local", though its tasks store no owner."""
    app = _build_app(db_uri, tmp_path, local_single_user=True)
    body = _create_body()
    del body["workspace"]
    del body["host_id"]
    async with _serve(app, mock_llm, tmp_path) as client:
        existing = (await client.post("/v1/projects", json={"name": "existing"})).json()
        by_id = await client.post(
            "/v1/scheduled-tasks", json={**body, "project_id": existing["id"]}
        )
        assert by_id.status_code == 200, by_id.text
        assert by_id.json()["project_id"] == existing["id"]


async def test_create_rejects_unsupported_public_fields(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    _make_user(db_uri)
    resp = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(base_branch="main", execution_target="managed_sandbox"),
        headers=_headers(),
    )
    assert resp.status_code == 422, resp.text


async def test_update_changes_fields_and_validates_rrule(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    task_id = created["id"]

    # Valid partial update.
    patched = await auth_client.patch(
        f"/v1/scheduled-tasks/{task_id}",
        json={"name": "renamed", "state": "paused"},
        headers=_headers(),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "renamed"
    assert patched.json()["state"] == "paused"

    # Invalid rrule on update is a 400.
    bad = await auth_client.patch(
        f"/v1/scheduled-tasks/{task_id}",
        json={"rrule": "FREQ=SECONDLY"},
        headers=_headers(),
    )
    assert bad.status_code == 400, bad.text

    # Deletion is a DELETE operation, not an arbitrary PATCH state.
    deleted_state = await auth_client.patch(
        f"/v1/scheduled-tasks/{task_id}",
        json={"state": "deleted"},
        headers=_headers(),
    )
    assert deleted_state.status_code == 422, deleted_state.text


async def test_update_switches_the_bound_agent(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """PATCH ``agent_id`` rebinds a task to a different harness.

    An existing automation must be switchable in place — the alternative is
    recreating it, which loses the task id and its run history.
    """
    from omnigent.native.native_coding_agents import CODEX_NATIVE_AGENT_NAME

    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    task_id = created["id"]
    assert created["agent_id"] == builtin_agent_id(CLAUDE_NATIVE_AGENT_NAME)

    target = builtin_agent_id(CODEX_NATIVE_AGENT_NAME)
    patched = await auth_client.patch(
        f"/v1/scheduled-tasks/{task_id}",
        json={"agent_id": target},
        headers=_headers(),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["agent_id"] == target
    # Same task row, not a recreate.
    assert patched.json()["id"] == task_id

    got = await auth_client.get(f"/v1/scheduled-tasks/{task_id}", headers=_headers())
    assert got.json()["agent_id"] == target


async def test_update_agent_switch_clears_the_old_harnesss_settings(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A switch drops per-agent settings the new harness can't use.

    ``permission_mode`` is a Claude-only flag and a model id is provider-bound,
    so carrying them onto a codex task would break the fire with an unknown
    ``--permission-mode`` / a model its CLI has never heard of.
    """
    from omnigent.native.native_coding_agents import CODEX_NATIVE_AGENT_NAME

    _make_user(db_uri)
    created = (
        await auth_client.post(
            "/v1/scheduled-tasks",
            json=_create_body(
                permission_mode="bypassPermissions",
                model_override="databricks-claude-sonnet-4-6",
                reasoning_effort="high",
            ),
            headers=_headers(),
        )
    ).json()
    task_id = created["id"]
    assert created["permission_mode"] == "bypassPermissions"

    patched = await auth_client.patch(
        f"/v1/scheduled-tasks/{task_id}",
        json={"agent_id": builtin_agent_id(CODEX_NATIVE_AGENT_NAME)},
        headers=_headers(),
    )
    assert patched.status_code == 200, patched.text
    got = (await auth_client.get(f"/v1/scheduled-tasks/{task_id}", headers=_headers())).json()
    assert got["permission_mode"] is None
    assert got["model_override"] is None
    assert got["reasoning_effort"] is None


async def test_update_agent_switch_revalidates_workspace_against_the_new_agent(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A switch re-checks the pinned workspace against the TARGET agent.

    The ``os_env`` boundary is per-agent, so a workspace validated for the old
    harness proves nothing about the new one. The autouse fixture stubs the
    validator, so capture the agent it receives to pin down WHICH agent the
    boundary check runs against — asserting a 200 alone would pass even if the
    route kept checking the old agent.
    """
    from omnigent.native.native_coding_agents import CODEX_NATIVE_AGENT_NAME

    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()

    seen: list[str] = []

    async def _recording_validate(**kwargs: object) -> str:
        agent = kwargs["agent"]
        seen.append(getattr(agent, "id", ""))
        workspace = kwargs["workspace"]
        assert isinstance(workspace, str)
        return workspace

    monkeypatch.setattr(
        scheduled_tasks_routes, "validate_existing_host_workspace", _recording_validate
    )

    target = builtin_agent_id(CODEX_NATIVE_AGENT_NAME)
    patched = await auth_client.patch(
        f"/v1/scheduled-tasks/{created['id']}",
        json={"agent_id": target},
        headers=_headers(),
    )
    assert patched.status_code == 200, patched.text
    # Ran once, against the agent being switched TO — not the one being left.
    assert seen == [target], seen


async def test_update_agent_switch_keeps_settings_resent_in_the_same_patch(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Settings sent alongside a switch are kept and gated on the NEW agent."""
    from omnigent.native.native_coding_agents import CODEX_NATIVE_AGENT_NAME

    _make_user(db_uri)
    created = (
        await auth_client.post(
            "/v1/scheduled-tasks",
            json=_create_body(agent_id=builtin_agent_id(CODEX_NATIVE_AGENT_NAME)),
            headers=_headers(),
        )
    ).json()

    patched = await auth_client.patch(
        f"/v1/scheduled-tasks/{created['id']}",
        json={
            "agent_id": builtin_agent_id(CLAUDE_NATIVE_AGENT_NAME),
            "permission_mode": "acceptEdits",
        },
        headers=_headers(),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["permission_mode"] == "acceptEdits"


async def test_update_agent_switch_gates_permission_mode_on_the_new_agent(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A Claude-only mode sent with a switch to codex is rejected, not persisted."""
    from omnigent.native.native_coding_agents import CODEX_NATIVE_AGENT_NAME

    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()

    resp = await auth_client.patch(
        f"/v1/scheduled-tasks/{created['id']}",
        json={
            "agent_id": builtin_agent_id(CODEX_NATIVE_AGENT_NAME),
            "permission_mode": "acceptEdits",
        },
        headers=_headers(),
    )
    assert resp.status_code == 400, resp.text
    assert "permission_mode" in resp.text
    # Rejected before any write: the task keeps its original agent.
    got = await auth_client.get(f"/v1/scheduled-tasks/{created['id']}", headers=_headers())
    assert got.json()["agent_id"] == builtin_agent_id(CLAUDE_NATIVE_AGENT_NAME)


async def test_update_rejects_unknown_agent(auth_client: httpx.AsyncClient, db_uri: str) -> None:
    """Switching to a nonexistent agent 404s instead of persisting a dead binding."""
    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()

    resp = await auth_client.patch(
        f"/v1/scheduled-tasks/{created['id']}",
        json={"agent_id": "ag_does_not_exist"},
        headers=_headers(),
    )
    assert resp.status_code == 404, resp.text


async def test_update_rejects_null_agent(auth_client: httpx.AsyncClient, db_uri: str) -> None:
    """A task must always have an agent — an explicit null is a 422, not a no-op."""
    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()

    resp = await auth_client.patch(
        f"/v1/scheduled-tasks/{created['id']}",
        json={"agent_id": None},
        headers=_headers(),
    )
    assert resp.status_code == 422, resp.text


async def test_create_rejects_permission_mode_for_non_claude_agent(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Claude's permission vocabulary cannot be saved on a Codex agent."""
    from omnigent.native.native_coding_agents import CODEX_NATIVE_AGENT_NAME

    _make_user(db_uri)
    resp = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(
            agent_id=builtin_agent_id(CODEX_NATIVE_AGENT_NAME),
            permission_mode="acceptEdits",
        ),
        headers=_headers(),
    )
    assert resp.status_code == 400, resp.text
    assert "permission_mode" in resp.text


async def test_update_clears_permission_mode_with_explicit_null(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """PATCH ``permission_mode: null`` clears a previously-set mode end-to-end.

    The edit dialog resets the control to Default by sending an explicit null,
    so a task launched with ``bypassPermissions`` must actually revert to the
    agent default (not silently keep the dangerous mode). Asserts the PERSISTED
    value via a read-back, not just the response.
    """
    _make_user(db_uri)
    created = (
        await auth_client.post(
            "/v1/scheduled-tasks",
            json=_create_body(permission_mode="bypassPermissions"),
            headers=_headers(),
        )
    ).json()
    task_id = created["id"]
    assert created["permission_mode"] == "bypassPermissions"

    patched = await auth_client.patch(
        f"/v1/scheduled-tasks/{task_id}",
        json={"permission_mode": None},
        headers=_headers(),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["permission_mode"] is None

    got = await auth_client.get(f"/v1/scheduled-tasks/{task_id}", headers=_headers())
    assert got.json()["permission_mode"] is None


@pytest.mark.parametrize("include_agent_id", [False, True], ids=["omitted-agent", "same-agent"])
async def test_update_omitting_permission_mode_leaves_it_unchanged(
    auth_client: httpx.AsyncClient, db_uri: str, include_agent_id: bool
) -> None:
    """Omitting permission_mode retains the override when the agent does not change."""
    _make_user(db_uri)
    created = (
        await auth_client.post(
            "/v1/scheduled-tasks",
            json=_create_body(permission_mode="acceptEdits"),
            headers=_headers(),
        )
    ).json()
    task_id = created["id"]

    fields = {"name": "renamed"}
    if include_agent_id:
        fields["agent_id"] = created["agent_id"]
    patched = await auth_client.patch(
        f"/v1/scheduled-tasks/{task_id}",
        json=fields,
        headers=_headers(),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["permission_mode"] == "acceptEdits"
    got = await auth_client.get(f"/v1/scheduled-tasks/{task_id}", headers=_headers())
    assert got.json()["agent_id"] == created["agent_id"]
    assert got.json()["permission_mode"] == "acceptEdits"


async def test_update_rejects_invalid_model_override(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()

    bad = await auth_client.patch(
        f"/v1/scheduled-tasks/{created['id']}",
        json={"model_override": "--danger"},
        headers=_headers(),
    )
    assert bad.status_code == 400, bad.text


async def test_update_rejects_invalid_reasoning_effort(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()

    bad = await auth_client.patch(
        f"/v1/scheduled-tasks/{created['id']}",
        json={"reasoning_effort": "extreme"},
        headers=_headers(),
    )
    assert bad.status_code == 400, bad.text


@pytest.mark.parametrize("claimed", [False, True])
@pytest.mark.parametrize("shared", [False, True])
async def test_delete_removes_task(
    auth_client: httpx.AsyncClient, db_uri: str, claimed: bool, shared: bool
) -> None:
    from omnigent.db.db_models import current_workspace_id
    from omnigent.server.scheduled.schedule import continued_sandbox_host_id
    from omnigent.stores.conversation_store import SCHEDULED_TASK_ORIGIN_LABEL_KEY
    from omnigent.stores.host_store import HostStore

    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    task_id = created["id"]
    store = SqlAlchemyScheduledTaskStore(db_uri)
    if claimed:
        task = store.get(task_id)
        from omnigent.server.scheduled.schedule import next_scheduled_time

        assert store.initialize_next_run_at(task, next_scheduled_time(task, task.created_at))
        task = store.get(task_id)
        assert task is not None and task.next_run_at is not None
        assert (
            store.claim_scheduled_run(
                task,
                now=task.next_run_at,
                next_run_at=task.next_run_at + 3600,
                host_id=task.host_id,
                lease_seconds=120,
            )
            is not None
        )

    hosts = HostStore(db_uri)
    host_id = continued_sandbox_host_id(task_id, current_workspace_id())
    hosts.register_managed_host(
        host_id=host_id,
        name="automation",
        user_id="alice@example.com",
        token="retained",
        provider="modal",
        sandbox_id="retained",
        token_expires_at=2_000_000_000,
    )
    hosts.upsert_on_connect(host_id=host_id, name="automation", user_id="alice@example.com")
    conversations = SqlAlchemyConversationStore(db_uri)
    session = conversations.create_conversation(
        host_id=host_id, workspace="/work", labels={SCHEDULED_TASK_ORIGIN_LABEL_KEY: task_id}
    )
    if shared:
        peer = conversations.create_conversation(host_id=host_id, workspace="/work")
        conversations.update_conversation(peer.id, archived=True)
    assert await conversations.delete_conversation(session.id)
    assert hosts.get_host(host_id).status == "online"

    deleted = await auth_client.delete(f"/v1/scheduled-tasks/{task_id}", headers=_headers())
    assert deleted.status_code == 200, deleted.text
    retained_host = hosts.get_host(host_id, include_deleted=True)
    assert retained_host is not None
    assert (retained_host.deleted_at is None) is shared
    if shared:
        assert retained_host.status == "online"
        assert conversations.get_conversation(peer.id).host_id == host_id
    else:
        assert retained_host.sandbox_id == "retained"
        assert hosts.resolve_launch_token(host_id, "retained") is None

    got = await auth_client.get(f"/v1/scheduled-tasks/{task_id}", headers=_headers())
    assert got.status_code == 404
    listed = await auth_client.get("/v1/scheduled-tasks", headers=_headers())
    assert listed.json()["scheduled_tasks"] == []
    if claimed:
        assert store.get(task_id).state == "deleted"
        assert len(store.list_runs(task_id)[0]) == 1
        for method, suffix, body in [
            ("get", "/runs", None),
            ("post", "/run", None),
            ("patch", "", {"state": "active"}),
            ("delete", "", None),
        ]:
            response = await auth_client.request(
                method,
                f"/v1/scheduled-tasks/{task_id}{suffix}",
                headers=_headers(),
                json=body,
            )
            assert response.status_code == 404


async def test_delete_cleanup_failure_still_returns_success_off_event_loop(
    auth_client,
    auth_app,
    db_uri,
    monkeypatch,
):
    import threading

    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    request_thread = threading.get_ident()
    cleanup_threads = []

    def fail_cleanup(self, task_id, *, after=None):
        cleanup_threads.append(threading.get_ident())
        raise RuntimeError("cleanup interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(SqlAlchemyScheduledTaskStore, "_purge_deleted_task_page", fail_cleanup)
        response = await auth_client.delete(
            f"/v1/scheduled-tasks/{created['id']}", headers=_headers()
        )
    assert response.status_code == 200, response.text
    assert cleanup_threads and request_thread not in cleanup_threads
    assert auth_app.state.scheduled_task_scheduler.job_count == 0
    assert (
        await auth_client.get(f"/v1/scheduled-tasks/{created['id']}", headers=_headers())
    ).status_code == 404
    store = SqlAlchemyScheduledTaskStore(db_uri)
    assert store.get(created["id"]).state == "deleted"
    store.purge_deleted_tasks()
    assert store.get(created["id"]) is None


@pytest.mark.parametrize("operation", ["create", "update"])
async def test_schedule_search_failure_returns_400_and_rolls_back(
    auth_client,
    auth_app,
    db_uri,
    monkeypatch,
    operation,
):
    from omnigent.server.scheduled.rrule import RRuleSearchError
    from omnigent.stores.scheduled_task_store import sqlalchemy_store

    store = auth_app.state.scheduled_task_scheduler._store
    store.configure_dispatch(True)
    existing = None
    if operation == "update":
        created = (
            await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
        ).json()
        existing = store.get(created["id"])

    def fail_actual_schedule(*args):
        raise RRuleSearchError("search budget exceeded")

    monkeypatch.setattr(sqlalchemy_store, "next_scheduled_time", fail_actual_schedule)
    with capture_debug_rows("server") as rows:
        if operation == "create":
            response = await auth_client.post(
                "/v1/scheduled-tasks", json=_create_body(), headers=_headers()
            )
        else:
            response = await auth_client.patch(
                f"/v1/scheduled-tasks/{existing.id}",
                json={"rrule": "FREQ=DAILY;BYHOUR=12"},
                headers=_headers(),
            )
    assert not any(row["event_name"] == "scheduled_task_created" for row in rows)
    assert response.status_code == 400, response.text
    assert "Could not calculate the next run time for this schedule." in response.text
    expected_outcome = (
        "The task was not created." if operation == "create" else "Your changes were not saved."
    )
    assert expected_outcome in response.text
    if existing is None:
        assert store.list() == []
    else:
        assert store.get(existing.id) == existing


async def test_other_users_task_is_not_visible(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    _make_user(db_uri, "alice@example.com")
    _make_user(db_uri, "bob@example.com")
    created = (
        await auth_client.post(
            "/v1/scheduled-tasks", json=_create_body(), headers=_headers("alice@example.com")
        )
    ).json()
    task_id = created["id"]

    # Bob cannot see or fetch Alice's task.
    got = await auth_client.get(
        f"/v1/scheduled-tasks/{task_id}", headers=_headers("bob@example.com")
    )
    assert got.status_code == 404
    listed = await auth_client.get("/v1/scheduled-tasks", headers=_headers("bob@example.com"))
    assert listed.json()["scheduled_tasks"] == []


@pytest.mark.parametrize("tz", ["Not/A_Timezone", "", "../UTC"])
async def test_create_rejects_invalid_timezone(
    auth_client: httpx.AsyncClient, db_uri: str, tz: str
) -> None:
    _make_user(db_uri)
    resp = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(timezone=tz),
        headers=_headers(),
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.parametrize("tz", ["Bogus/Zone", "", "../UTC"])
async def test_update_rejects_invalid_timezone(
    auth_client: httpx.AsyncClient, db_uri: str, tz: str
) -> None:
    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    task_id = created["id"]

    bad = await auth_client.patch(
        f"/v1/scheduled-tasks/{task_id}",
        json={"timezone": tz},
        headers=_headers(),
    )
    assert bad.status_code == 400, bad.text


async def test_scheduler_synced_on_create_and_delete(
    auth_client: httpx.AsyncClient, auth_app: FastAPI, db_uri: str
) -> None:
    _make_user(db_uri)
    scheduler = auth_app.state.scheduled_task_scheduler
    before = scheduler.job_count

    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    assert scheduler.job_count == before + 1

    await auth_client.delete(f"/v1/scheduled-tasks/{created['id']}", headers=_headers())
    assert scheduler.job_count == before


# ── GET /v1/scheduled-tasks/{id}/runs (run history) ──────────────────────────


def _seed_run(db_uri: str, task_id: str, run_id: str, **overrides: object) -> None:
    """Seed a run row directly (the sweep/fire path writes these in prod).

    Tests run at the default workspace (no tenant middleware), matching the
    route's read scope.
    """
    from omnigent.stores.scheduled_task_store.sqlalchemy_store import (
        SqlAlchemyScheduledTaskStore,
    )

    store = SqlAlchemyScheduledTaskStore(db_uri)
    kwargs: dict[str, object] = {
        "run_id": run_id,
        "scheduled_task_id": task_id,
        "status": "succeeded",
        "scheduled_at": 1000,
        "conversation_id": "conv_seed",
        "fired_at": 1001,
        "finished_at": 1002,
    }
    kwargs.update(overrides)
    store.create_run(**kwargs)  # type: ignore[arg-type]


async def test_list_runs_returns_history_for_owned_task(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """An owned task's run history comes back most-recent-first with run fields."""
    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    task_id = created["id"]

    import uuid

    older_id = uuid.uuid4().hex
    newer_id = uuid.uuid4().hex
    conv_id = uuid.uuid4().hex
    _seed_run(
        db_uri, task_id, older_id, scheduled_at=1000, status="succeeded", conversation_id=conv_id
    )
    _seed_run(
        db_uri,
        task_id,
        newer_id,
        scheduled_at=2000,
        status="failed",
        error="the sandbox did not register within 720 seconds — " + "x" * 2000,
        error_code="incomplete",
        finished_at=2002,
        conversation_id=conv_id,
    )

    resp = await auth_client.get(f"/v1/scheduled-tasks/{task_id}/runs", headers=_headers())
    assert resp.status_code == 200, resp.text
    runs = resp.json()["runs"]
    assert [r["id"] for r in runs] == [newer_id, older_id]  # scheduled_at DESC
    newest = runs[0]
    assert newest["status"] == "failed"
    assert newest["error_code"] == "incomplete"
    assert newest["finished_at"] == 2002
    assert newest["conversation_id"] == conv_id
    assert newest["scheduled_task_id"] == task_id
    # The free-text error reaches the run list only as a capped detail.
    assert "error" not in newest
    detail = newest["error_detail"]
    assert detail.startswith("the sandbox did not register within 720 seconds — ")
    assert len(detail) == 1000
    assert runs[1]["error_detail"] is None
    # The run's session doesn't exist, so it carries no session read state.
    assert newest["conversation_updated_at"] is None
    assert newest["conversation_status"] is None
    assert newest["viewer_last_seen"] is None
    assert newest["viewer_unread"] is None


@pytest.fixture()
def clean_session_read_state() -> Iterator[None]:
    """Clear the module-level session read-state cache around a test."""
    from omnigent.server.routes._sessions import helpers

    helpers._read_last_seen.clear()
    helpers._read_explicit_unread.clear()
    yield
    helpers._read_last_seen.clear()
    helpers._read_explicit_unread.clear()


@pytest.mark.usefixtures("clean_session_read_state")
async def test_list_runs_includes_conversation_read_state_fields(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Run rows include batched conversation read-state fields; skipped runs get nulls.

    The ``/runs`` endpoint enriches each run that has a ``conversation_id`` with
    ``conversation_updated_at``, ``conversation_status``, and ``viewer_unread``
    so the frontend can compute the unread dot without per-row session GETs.
    Runs with no conversation (skipped/failed host-less) get null for all three.
    """
    import time
    import uuid

    from omnigent.runtime import pending_elicitations
    from omnigent.stores.conversation_store import RUNNER_LIVENESS_TTL_S
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    task_id = created["id"]

    from omnigent.server.routes._sessions.helpers import _set_read_state

    # Create a real conversation in the store so the batch-fetch can hydrate it.
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation(conversation_id=uuid.uuid4().hex)
    conv_store.set_session_live_status(conv.id, "idle")
    conv_store.set_session_usage(conv.id, {"total_cost_usd": 1.234})
    _set_read_state("alice@example.com", conv.id, conv.updated_at - 1, True)
    # A known session the viewer saw but never flagged, waiting on its harness.
    waiting = conv_store.create_conversation(conversation_id=uuid.uuid4().hex)
    conv_store.set_session_live_status(waiting.id, "waiting")
    conv_store.set_pending_elicitation_count(waiting.id, 2)
    runner_id = f"runner_{uuid.uuid4().hex}"
    conv_store.set_runner_id(waiting.id, runner_id)
    conv_store.touch_runner_liveness([runner_id], now=int(time.time()))
    _set_read_state("alice@example.com", waiting.id, 1_700_000_100, False)

    in_process = conv_store.create_conversation(conversation_id=uuid.uuid4().hex)
    elicitation_id = uuid.uuid4().hex
    pending_elicitations.record_publish(
        in_process.id,
        {"type": "response.elicitation_request", "elicitation_id": elicitation_id},
    )
    in_process_run_id = uuid.uuid4().hex
    _seed_run(
        db_uri,
        task_id,
        in_process_run_id,
        scheduled_at=1600,
        status="running",
        conversation_id=in_process.id,
    )

    succeeded_id = uuid.uuid4().hex
    running_id = uuid.uuid4().hex
    skipped_id = uuid.uuid4().hex
    _seed_run(
        db_uri,
        task_id,
        succeeded_id,
        scheduled_at=1000,
        status="succeeded",
        conversation_id=conv.id,
    )
    _seed_run(
        db_uri,
        task_id,
        running_id,
        scheduled_at=1500,
        status="running",
        conversation_id=waiting.id,
    )
    _seed_run(
        db_uri,
        task_id,
        skipped_id,
        scheduled_at=2000,
        status="skipped",
        error_code="no_online_host",
        conversation_id=None,
    )

    resp = await auth_client.get(f"/v1/scheduled-tasks/{task_id}/runs", headers=_headers())
    assert resp.status_code == 200, resp.text
    runs = {r["id"]: r for r in resp.json()["runs"]}

    # The succeeded run with a real conversation gets enriched fields.
    succ = runs[succeeded_id]
    assert succ["conversation_updated_at"] == conv_store.get_conversation(conv.id).updated_at
    assert succ["conversation_status"] == "idle"
    assert succ["viewer_last_seen"] == conv.updated_at - 1
    assert succ["viewer_unread"] is True
    assert succ["session_cost_usd"] == 1.234
    assert succ["conversation_pending_approvals"] == 0

    # Status reads as the sessions list reports it ("waiting" is "running"), and
    # an unflagged session is explicitly not unread rather than unknown.
    running = runs[running_id]
    assert running["conversation_status"] == "running"
    assert running["viewer_last_seen"] == 1_700_000_100
    assert running["viewer_unread"] is False
    assert running["session_cost_usd"] is None
    assert running["conversation_pending_approvals"] == 2
    assert runs[in_process_run_id]["conversation_pending_approvals"] == 1
    pending_elicitations.resolve(in_process.id, elicitation_id)

    # The skipped run (no conversation_id) gets null for all three fields.
    skip = runs[skipped_id]
    assert skip["conversation_updated_at"] is None
    assert skip["conversation_status"] is None
    assert skip["viewer_last_seen"] is None
    assert skip["viewer_unread"] is None
    assert skip["session_cost_usd"] is None
    assert skip["conversation_pending_approvals"] == 0

    # Another replica's fresh runner remains actionable; stale/disconnected runners do not.
    now = int(time.time())
    for last_seen, pending in (
        (now - 5, 2),
        (now - RUNNER_LIVENESS_TTL_S - 5, 0),
        (None, 0),
        (now, 2),
    ):
        if last_seen is None:
            conv_store.clear_runner_liveness(runner_id)
        else:
            conv_store.touch_runner_liveness([runner_id], now=last_seen)
        resp = await auth_client.get(f"/v1/scheduled-tasks/{task_id}/runs", headers=_headers())
        assert resp.status_code == 200
        waiting_run = next(row for row in resp.json()["runs"] if row["id"] == running_id)
        assert waiting_run["conversation_pending_approvals"] == pending
        assert (
            conv_store.get_session_activity([waiting.id])[waiting.id].pending_elicitation_count
            == 2
        )

    conv_store.clear_runner_id(waiting.id)
    assert pending_elicitations.count_for(waiting.id) == 0
    resp = await auth_client.get(f"/v1/scheduled-tasks/{task_id}/runs", headers=_headers())
    assert resp.status_code == 200
    waiting_run = next(row for row in resp.json()["runs"] if row["id"] == running_id)
    assert waiting_run["conversation_pending_approvals"] == 0
    assert conv_store.get_session_activity([waiting.id])[waiting.id].pending_elicitation_count == 2

    # Continuing the same session reports its current total on every run.
    _seed_run(
        db_uri,
        task_id,
        uuid.uuid4().hex,
        scheduled_at=2500,
        status="succeeded",
        conversation_id=conv.id,
    )
    conv_store.set_session_usage(conv.id, {"total_cost_usd": 2.5})
    resp = await auth_client.get(f"/v1/scheduled-tasks/{task_id}/runs", headers=_headers())
    assert resp.status_code == 200
    shared = [row for row in resp.json()["runs"] if row["conversation_id"] == conv.id]
    assert len(shared) == 2
    assert all(row["session_cost_usd"] == 2.5 for row in shared)


@pytest.mark.usefixtures("clean_session_read_state")
async def test_list_runs_reads_the_local_users_read_state(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    mock_llm: ControllableMockClient,
) -> None:
    """On the single-user local runtime, ``viewer_unread`` matches the session list's.

    Session read state is keyed by the raw "local" identity; ``/runs`` must read
    the same key rather than the ``None`` the task-ownership check uses.
    """
    import uuid

    from omnigent.server.routes._sessions.helpers import _set_read_state

    app = _build_app(db_uri, tmp_path, local_single_user=True)
    async with _serve(app, mock_llm, tmp_path) as client:
        created = await client.post(
            "/v1/scheduled-tasks", json=_create_body(host_id=None, workspace=None)
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["id"]
        conv_store = SqlAlchemyConversationStore(db_uri)
        conv = conv_store.create_conversation(conversation_id=uuid.uuid4().hex)
        _seed_run(db_uri, task_id, uuid.uuid4().hex, conversation_id=conv.id)
        _set_read_state("local", conv.id, conv.updated_at - 1, True)

        resp = await client.get(f"/v1/scheduled-tasks/{task_id}/runs")
        assert resp.status_code == 200, resp.text
        assert resp.json()["runs"][0]["viewer_unread"] is True


async def test_list_runs_empty_for_task_with_no_runs(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A task that has never fired returns an empty run list (not a 404)."""
    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    resp = await auth_client.get(f"/v1/scheduled-tasks/{created['id']}/runs", headers=_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["runs"] == []
    assert body["next_cursor"] is None


async def test_list_runs_cursor_pagination(auth_client: httpx.AsyncClient, db_uri: str) -> None:
    """Paging through runs via ``limit`` + ``after`` yields every run once, in
    order, with a null cursor on the final page."""
    import uuid

    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    task_id = created["id"]

    total = 25
    # scheduled_at ascending with i so newest-first order is deterministic.
    seeded_newest_first: list[str] = []
    for i in range(total):
        rid = uuid.uuid4().hex
        _seed_run(db_uri, task_id, rid, scheduled_at=1000 + i, conversation_id=uuid.uuid4().hex)
        seeded_newest_first.insert(0, rid)

    collected: list[str] = []
    after: str | None = None
    pages = 0
    while True:
        params = {"limit": 10}
        if after is not None:
            params["after"] = after
        resp = await auth_client.get(
            f"/v1/scheduled-tasks/{task_id}/runs", params=params, headers=_headers()
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        collected.extend(r["id"] for r in body["runs"])
        pages += 1
        after = body["next_cursor"]
        if after is None:
            break
        assert pages < 10, "pagination did not terminate"

    # No gaps, no dupes, correct newest-first order across pages.
    assert collected == seeded_newest_first
    assert len(collected) == total


async def test_list_runs_404_for_nonexistent_task(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Runs for an unknown task id 404 (owner-scoped, not enumerable)."""
    _make_user(db_uri)
    resp = await auth_client.get(
        "/v1/scheduled-tasks/ffffffffffffffffffffffffffffffff/runs", headers=_headers()
    )
    assert resp.status_code == 404, resp.text


async def test_list_runs_404_for_nonowned_task(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A task owned by another user 404s its runs (no cross-user enumeration)."""
    _make_user(db_uri, "alice@example.com")
    _make_user(db_uri, "bob@example.com")
    created = (
        await auth_client.post(
            "/v1/scheduled-tasks", json=_create_body(), headers=_headers("alice@example.com")
        )
    ).json()
    # Bob asks for Alice's task runs → 404.
    resp = await auth_client.get(
        f"/v1/scheduled-tasks/{created['id']}/runs", headers=_headers("bob@example.com")
    )
    assert resp.status_code == 404, resp.text


# ── lazy-on-read orphan backstop ─────────────────────────────────────────────


async def test_list_runs_force_fails_stale_running_run(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Reading history force-fails a run left ``running`` past the 6h max age.

    The lazy-on-read backstop for a genuine orphan (terminal event never
    fired). ``scheduled_at`` is set well beyond the max age, so the read
    transitions it to ``failed(incomplete)`` with ``finished_at`` stamped.
    """
    import time
    import uuid

    from omnigent.server.scheduled.run_reconciler import STALE_RUN_MAX_AGE_SECONDS

    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    task_id = created["id"]
    run_id = uuid.uuid4().hex
    stale_scheduled_at = int(time.time()) - STALE_RUN_MAX_AGE_SECONDS - 60
    _seed_run(
        db_uri,
        task_id,
        run_id,
        status="running",
        scheduled_at=stale_scheduled_at,
        fired_at=stale_scheduled_at + 1,
        finished_at=None,
        conversation_id=uuid.uuid4().hex,
    )

    resp = await auth_client.get(f"/v1/scheduled-tasks/{task_id}/runs", headers=_headers())
    assert resp.status_code == 200, resp.text
    run = resp.json()["runs"][0]
    assert run["status"] == "failed"
    assert run["error_code"] == "incomplete"
    assert run["finished_at"] is not None


async def test_list_runs_leaves_young_running_run_untouched(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A recently-fired ``running`` run is NOT force-failed on read.

    Only runs past the max age are reaped; a young in-flight run is left for
    the event hook to complete normally.
    """
    import time
    import uuid

    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    task_id = created["id"]
    run_id = uuid.uuid4().hex
    recent = int(time.time()) - 30  # 30s ago, well within the max age
    _seed_run(
        db_uri,
        task_id,
        run_id,
        status="running",
        scheduled_at=recent,
        fired_at=recent + 1,
        finished_at=None,
        conversation_id=uuid.uuid4().hex,
    )

    resp = await auth_client.get(f"/v1/scheduled-tasks/{task_id}/runs", headers=_headers())
    assert resp.status_code == 200, resp.text
    run = resp.json()["runs"][0]
    assert run["status"] == "running"
    assert run["finished_at"] is None


async def test_list_tasks_force_fails_stale_running_run(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """The LIST endpoint also force-fails a stale ``running`` run (no conv read).

    Reading GET /v1/scheduled-tasks reaps the owner's stale orphans so a
    Tasks-list badge never shows one as ``running``. Verified via the detail
    endpoint afterward (the list response itself carries no run rows yet).
    """
    import time
    import uuid

    from omnigent.server.scheduled.run_reconciler import STALE_RUN_MAX_AGE_SECONDS

    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    task_id = created["id"]
    run_id = uuid.uuid4().hex
    stale_at = int(time.time()) - STALE_RUN_MAX_AGE_SECONDS - 60
    _seed_run(
        db_uri,
        task_id,
        run_id,
        status="running",
        scheduled_at=stale_at,
        fired_at=stale_at + 1,
        finished_at=None,
        conversation_id=uuid.uuid4().hex,
    )

    # Hitting the LIST endpoint triggers the lazy stale backstop.
    list_resp = await auth_client.get("/v1/scheduled-tasks", headers=_headers())
    assert list_resp.status_code == 200, list_resp.text

    # The run was force-failed as a side effect of the list read.
    detail = await auth_client.get(f"/v1/scheduled-tasks/{task_id}/runs", headers=_headers())
    run = detail.json()["runs"][0]
    assert run["status"] == "failed"
    assert run["error_code"] == "incomplete"
    assert run["finished_at"] is not None


async def test_list_tasks_leaves_young_running_run_untouched(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """The LIST endpoint does NOT reap a young in-flight run."""
    import time
    import uuid

    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    task_id = created["id"]
    run_id = uuid.uuid4().hex
    recent = int(time.time()) - 30
    _seed_run(
        db_uri,
        task_id,
        run_id,
        status="running",
        scheduled_at=recent,
        fired_at=recent + 1,
        finished_at=None,
        conversation_id=uuid.uuid4().hex,
    )

    list_resp = await auth_client.get("/v1/scheduled-tasks", headers=_headers())
    assert list_resp.status_code == 200, list_resp.text

    detail = await auth_client.get(f"/v1/scheduled-tasks/{task_id}/runs", headers=_headers())
    run = detail.json()["runs"][0]
    assert run["status"] == "running"
    assert run["finished_at"] is None


# ── event-hook wiring: _publish_status -> persist_scheduled_run_completion ────
#
# These lock the PRIMARY completion mechanism at the _publish_status seam (not
# by re-calling the hook directly): a real terminal edge published the way the
# SSE relay publishes it must reach the hook and transition the run, resolving
# under the run's workspace_scope via the shared session_live_state executor.
# Layer exercised: the sync _publish_status(...) call (its no-subscriber
# session_stream.publish is a no-op) → session_live_state.persist_scheduled_run_
# completion → the ThreadPoolExecutor(max_workers=1) worker → store.update_run.
# A full runner/relay round-trip is covered by the live E2E; this covers the
# server-side wiring so a future _publish_status refactor can't silently break
# scheduled-run completion.


def _seed_running_run_for_conv(db_uri: str, conversation_id: str) -> tuple[str, str]:
    """Create a task + a ``running`` run bound to ``conversation_id``.

    :returns: ``(task_id, run_id)``.
    """
    import uuid

    from omnigent.stores.scheduled_task_store.sqlalchemy_store import (
        SqlAlchemyScheduledTaskStore,
    )

    store = SqlAlchemyScheduledTaskStore(db_uri)
    task_id = uuid.uuid4().hex
    store.create(
        scheduled_task_id=task_id,
        name="hook-wiring",
        prompt="p",
        rrule="FREQ=HOURLY;BYMINUTE=0",
        user_id=None,
        agent_id=uuid.uuid4().hex,
        timezone="UTC",
    )
    run_id = uuid.uuid4().hex
    store.create_run(
        run_id=run_id,
        scheduled_task_id=task_id,
        status="running",
        scheduled_at=1000,
        conversation_id=conversation_id,
        fired_at=1001,
    )
    return task_id, run_id


def _wait_for_run_status(
    db_uri: str, task_id: str, run_id: str, want: str, timeout_s: float = 10.0
):  # type: ignore[no-untyped-def]
    """Poll the store until ``run_id`` reaches ``want`` (or timeout).

    The hook write lands on session_live_state's background single-worker
    executor, so the assertion must wait for that thread rather than read
    synchronously.
    """
    import time

    from omnigent.stores.scheduled_task_store.sqlalchemy_store import (
        SqlAlchemyScheduledTaskStore,
    )

    store = SqlAlchemyScheduledTaskStore(db_uri)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for r in store.list_runs(task_id)[0]:
            if r.id == run_id and r.status == want:
                return r
        time.sleep(0.02)
    # Return the current row (whatever status) so the caller's assert reports it.
    for r in store.list_runs(task_id):
        if r.id == run_id:
            return r
    return None


async def test_publish_status_idle_edge_transitions_scheduled_run_to_succeeded(
    db_uri: str,
) -> None:
    """A completed-turn edge through _publish_status flips the run to succeeded.

    Drives the real _publish_status(conversation_id, "idle") the relay emits and
    asserts the run transitions running -> succeeded with finished_at set, via
    the hook + executor path (workspace_scope contract exercised, not bypassed).

    Async to satisfy the module's ``pytestmark = pytest.mark.asyncio``; the body
    is synchronous (the hook write lands on session_live_state's background
    executor, polled below) — no awaits needed.
    """
    import uuid

    from omnigent.server import session_live_state
    from omnigent.server.routes.sessions import _publish_status, _session_status_cache
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.scheduled_task_store.sqlalchemy_store import (
        SqlAlchemyScheduledTaskStore,
    )

    conv_id = uuid.uuid4().hex
    task_id, run_id = _seed_running_run_for_conv(db_uri, conv_id)

    session_live_state.configure(
        SqlAlchemyConversationStore(db_uri), SqlAlchemyScheduledTaskStore(db_uri)
    )
    try:
        _publish_status(conv_id, "running")
        _publish_status(conv_id, "idle")
        row = _wait_for_run_status(db_uri, task_id, run_id, "succeeded")
    finally:
        session_live_state.configure(None)
        _session_status_cache.pop(conv_id, None)

    assert row is not None
    assert row.status == "succeeded"
    assert row.finished_at is not None
    assert row.error_code is None


async def test_publish_status_idle_after_cache_loss_completes_run(
    db_uri: str,
) -> None:
    """Run completion still works after a restart loses cached session status."""
    import uuid

    from omnigent.server import session_live_state
    from omnigent.server.routes.sessions import _publish_status, _session_status_cache
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.scheduled_task_store.sqlalchemy_store import (
        SqlAlchemyScheduledTaskStore,
    )

    conv_id = uuid.uuid4().hex
    task_id, run_id = _seed_running_run_for_conv(db_uri, conv_id)
    session_live_state.configure(
        SqlAlchemyConversationStore(db_uri), SqlAlchemyScheduledTaskStore(db_uri)
    )
    try:
        _session_status_cache.pop(conv_id, None)
        _publish_status(conv_id, "idle")
        run = _wait_for_run_status(db_uri, task_id, run_id, "succeeded")
    finally:
        session_live_state.configure(None)
        _session_status_cache.pop(conv_id, None)

    assert run is not None
    assert run.status == "succeeded"


async def test_publish_status_idle_after_waiting_completes_run(db_uri: str) -> None:
    """An idle that follows ``waiting`` (e.g. Stop while parked) completes the run."""
    import uuid

    from omnigent.server import session_live_state
    from omnigent.server.routes.sessions import _publish_status, _session_status_cache
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.scheduled_task_store.sqlalchemy_store import (
        SqlAlchemyScheduledTaskStore,
    )

    conv_id = uuid.uuid4().hex
    task_id, run_id = _seed_running_run_for_conv(db_uri, conv_id)
    session_live_state.configure(
        SqlAlchemyConversationStore(db_uri), SqlAlchemyScheduledTaskStore(db_uri)
    )
    try:
        _publish_status(conv_id, "waiting")
        _publish_status(conv_id, "idle")
        row = _wait_for_run_status(db_uri, task_id, run_id, "succeeded")
    finally:
        session_live_state.configure(None)
        _session_status_cache.pop(conv_id, None)

    assert row is not None
    assert row.status == "succeeded"


@pytest.mark.parametrize(
    "patch", [{"host_id": "4b653f6031f35d168cc0b37caa1306d1"}, {"base_branch": "HEAD"}]
)
async def test_patch_pinning_host_on_managed_task_is_rejected(
    auth_app: FastAPI, auth_client: httpx.AsyncClient, db_uri: str, patch: dict[str, str]
) -> None:
    """Pinning host_id on an already-managed task is rejected, not silently dropped."""
    _make_user(db_uri)

    auth_app.state.sandbox_config = _sandbox_deployment()
    try:
        body = _create_body(execution_target="managed_sandbox")
        del body["host_id"]
        del body["workspace"]
        created = (
            await auth_client.post("/v1/scheduled-tasks", json=body, headers=_headers())
        ).json()
        tid = created["id"]

        # No execution_target in the PATCH, so the request model can't catch it;
        # the handler must reject against the effective (managed) target.
        resp = await auth_client.patch(
            f"/v1/scheduled-tasks/{tid}",
            json=patch,
            headers=_headers(),
        )
        assert resp.status_code == 400, resp.text
    finally:
        auth_app.state.sandbox_config = None


async def test_publish_status_failed_edge_transitions_scheduled_run_to_failed(
    db_uri: str,
) -> None:
    """A failed-turn edge through _publish_status flips the run to failed+code.

    Async for the module ``pytestmark`` (see the idle-edge test); body is sync.
    """
    import uuid

    from omnigent.server import session_live_state
    from omnigent.server.routes.sessions import _publish_status, _session_status_cache
    from omnigent.server.schemas import ErrorDetail
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.scheduled_task_store.sqlalchemy_store import (
        SqlAlchemyScheduledTaskStore,
    )

    conv_id = uuid.uuid4().hex
    task_id, run_id = _seed_running_run_for_conv(db_uri, conv_id)

    session_live_state.configure(
        SqlAlchemyConversationStore(db_uri), SqlAlchemyScheduledTaskStore(db_uri)
    )
    try:
        _publish_status(
            conv_id, "failed", ErrorDetail(code="runner_disconnected", message="dropped")
        )
        row = _wait_for_run_status(db_uri, task_id, run_id, "failed")
    finally:
        session_live_state.configure(None)
        _session_status_cache.pop(conv_id, None)

    assert row is not None
    assert row.status == "failed"
    assert row.finished_at is not None
    assert row.error_code == "runner_disconnected"


# ── serializer fields: last_run_status + next_run_at ─────────────────────────


async def test_list_and_get_carry_last_run_status_and_next_run_at(
    auth_client: httpx.AsyncClient, auth_app: FastAPI, db_uri: str
) -> None:
    """LIST + GET surface the latest run's status and the scheduler's next fire.

    ``last_run_status`` comes from the windowed store query (the most-recent run
    by scheduled_at DESC); ``next_run_at`` comes from the live scheduler, so an
    armed active task reports a non-null ISO timestamp.
    """
    import uuid

    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    task_id = created["id"]
    # A freshly created task has never run.
    assert created["last_run_status"] is None
    # It is armed on the scheduler, so next_run_at is a non-null ISO string.
    assert isinstance(created["next_run_at"], str)

    # Seed two runs; the newer (failed) one is the reported status.
    _seed_run(
        db_uri,
        task_id,
        uuid.uuid4().hex,
        scheduled_at=1000,
        status="succeeded",
        conversation_id=uuid.uuid4().hex,
    )
    _seed_run(
        db_uri,
        task_id,
        uuid.uuid4().hex,
        scheduled_at=2000,
        status="failed",
        finished_at=2002,
        conversation_id=uuid.uuid4().hex,
    )

    list_body = (await auth_client.get("/v1/scheduled-tasks", headers=_headers())).json()
    row = next(t for t in list_body["scheduled_tasks"] if t["id"] == task_id)
    assert row["last_run_status"] == "failed"
    assert isinstance(row["next_run_at"], str)

    get_body = (await auth_client.get(f"/v1/scheduled-tasks/{task_id}", headers=_headers())).json()
    assert get_body["last_run_status"] == "failed"
    assert isinstance(get_body["next_run_at"], str)


async def test_paused_task_has_null_next_run_at(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A paused task is not armed, so next_run_at is null."""
    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    task_id = created["id"]
    patched = (
        await auth_client.patch(
            f"/v1/scheduled-tasks/{task_id}", json={"state": "paused"}, headers=_headers()
        )
    ).json()
    assert patched["state"] == "paused"
    assert patched["next_run_at"] is None


# ── POST /v1/scheduled-tasks/{id}/run (run now) ──────────────────────────────


async def test_run_now_triggers_and_records_a_run(
    auth_client: httpx.AsyncClient, auth_app: FastAPI, db_uri: str
) -> None:
    """POST /run returns 202 and drives the wired run-now trigger for the task.

    The route delegates to ``app.state.scheduled_task_run_now`` (built over the
    shared fire path). Here we stub that trigger with a recorder that writes a
    ``running`` run row, then assert the row appears in the task's history and as
    ``last_run_status`` — the integration contract the real trigger fulfills.
    """
    import time
    import uuid

    from omnigent.stores.scheduled_task_store.sqlalchemy_store import (
        SqlAlchemyScheduledTaskStore,
    )

    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    task_id = created["id"]

    calls: list[tuple[int, str]] = []
    now = int(time.time())

    async def _fake_run_now(workspace_id: int, scheduled_task_id: str) -> bool:
        calls.append((workspace_id, scheduled_task_id))
        # Use a current-time fire so the LIST endpoint's stale backstop (6h age
        # from fired_at) does NOT reap this fresh in-flight run to ``failed``.
        SqlAlchemyScheduledTaskStore(db_uri).create_run(
            run_id=uuid.uuid4().hex,
            scheduled_task_id=scheduled_task_id,
            status="running",
            scheduled_at=now,
            conversation_id=uuid.uuid4().hex,
            fired_at=now,
        )
        return True

    auth_app.state.scheduled_task_run_now = _fake_run_now

    resp = await auth_client.post(f"/v1/scheduled-tasks/{task_id}/run", headers=_headers())
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"triggered": True, "id": task_id}
    # The trigger was invoked with the task's workspace + id.
    assert calls == [(0, task_id)]

    runs = (
        await auth_client.get(f"/v1/scheduled-tasks/{task_id}/runs", headers=_headers())
    ).json()["runs"]
    assert len(runs) == 1
    assert runs[0]["status"] == "running"
    # And the completion badge source now reports it.
    list_body = (await auth_client.get("/v1/scheduled-tasks", headers=_headers())).json()
    row = next(t for t in list_body["scheduled_tasks"] if t["id"] == task_id)
    assert row["last_run_status"] == "running"


@pytest.mark.parametrize("auth_app", [True], indirect=True)
async def test_database_scheduler_run_now_returns_recorded_run_id(
    auth_client: httpx.AsyncClient,
) -> None:
    created = await auth_client.post(
        "/v1/scheduled-tasks", json=_create_body(), headers=_headers()
    )
    task_id = created.json()["id"]
    response = await auth_client.post(f"/v1/scheduled-tasks/{task_id}/run", headers=_headers())
    assert response.status_code == 202, response.text
    runs = (
        await auth_client.get(f"/v1/scheduled-tasks/{task_id}/runs", headers=_headers())
    ).json()["runs"]
    assert len(runs) == 1
    assert response.json() == {"triggered": True, "id": task_id, "run_id": runs[0]["id"]}


async def test_run_now_allows_paused_task(
    auth_client: httpx.AsyncClient, auth_app: FastAPI, db_uri: str
) -> None:
    """Run-now is a manual override — a paused task is still triggerable (202)."""
    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    task_id = created["id"]
    await auth_client.patch(
        f"/v1/scheduled-tasks/{task_id}", json={"state": "paused"}, headers=_headers()
    )

    triggered: list[str] = []

    async def _fake_run_now(workspace_id: int, scheduled_task_id: str) -> bool:
        triggered.append(scheduled_task_id)
        return True

    auth_app.state.scheduled_task_run_now = _fake_run_now
    resp = await auth_client.post(f"/v1/scheduled-tasks/{task_id}/run", headers=_headers())
    assert resp.status_code == 202, resp.text
    assert triggered == [task_id]


async def test_run_now_conflict_when_already_in_flight(
    auth_client: httpx.AsyncClient, auth_app: FastAPI, db_uri: str
) -> None:
    """A trigger that reports it was skipped (already in flight) surfaces as 409."""
    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    task_id = created["id"]

    async def _fake_run_now(workspace_id: int, scheduled_task_id: str) -> bool:
        return False  # already in flight

    auth_app.state.scheduled_task_run_now = _fake_run_now
    resp = await auth_client.post(f"/v1/scheduled-tasks/{task_id}/run", headers=_headers())
    assert resp.status_code == 409, resp.text


async def test_run_now_404_for_nonowned_task(
    auth_client: httpx.AsyncClient, auth_app: FastAPI, db_uri: str
) -> None:
    """Running another user's task 404s (not enumerable across users)."""
    _make_user(db_uri, "alice@example.com")
    _make_user(db_uri, "bob@example.com")
    created = (
        await auth_client.post(
            "/v1/scheduled-tasks", json=_create_body(), headers=_headers("alice@example.com")
        )
    ).json()

    async def _fake_run_now(workspace_id: int, scheduled_task_id: str) -> bool:
        raise AssertionError("run_now must not be reached for a non-owned task")

    auth_app.state.scheduled_task_run_now = _fake_run_now
    resp = await auth_client.post(
        f"/v1/scheduled-tasks/{created['id']}/run", headers=_headers("bob@example.com")
    )
    assert resp.status_code == 404, resp.text


async def test_run_now_503_when_scheduler_not_running(
    auth_client: httpx.AsyncClient, auth_app: FastAPI, db_uri: str
) -> None:
    """With no run-now trigger wired, the endpoint reports the subsystem is down."""
    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    auth_app.state.scheduled_task_run_now = None
    resp = await auth_client.post(f"/v1/scheduled-tasks/{created['id']}/run", headers=_headers())
    assert resp.status_code == 503, resp.text


@pytest.mark.parametrize("auth_app", [True], indirect=True)
async def test_database_scheduler_run_now_404_for_nonowned_task(
    auth_app: FastAPI, auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    _make_user(db_uri, "bob@example.com")
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    resp = await auth_client.post(
        f"/v1/scheduled-tasks/{created['id']}/run", headers=_headers("bob@example.com")
    )
    assert resp.status_code == 404, resp.text
    assert SqlAlchemyScheduledTaskStore(db_uri).list_pending_runs() == []


@pytest.mark.parametrize("auth_app", [True], indirect=True)
async def test_database_scheduler_run_now_503_when_scheduler_not_running(
    auth_app: FastAPI,
    auth_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    await auth_app.state.scheduled_task_scheduler.stop()
    monkeypatch.setattr(auth_app.state, "scheduled_task_scheduler", None)
    resp = await auth_client.post(f"/v1/scheduled-tasks/{created['id']}/run", headers=_headers())
    assert resp.status_code == 503, resp.text
    assert SqlAlchemyScheduledTaskStore(db_uri).list_pending_runs() == []


@pytest.mark.parametrize("auth_app", [True], indirect=True)
@pytest.mark.parametrize("scheduler_available", [True, False])
async def test_database_scheduler_crud_uses_persisted_next_run(
    auth_app: FastAPI,
    auth_client: httpx.AsyncClient,
    db_uri: str,
    scheduler_available: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime

    # The opt-in scheduler has no per-task timers to update; each request persists
    # its schedule and renders that same value without another scheduler lookup.
    assert isinstance(auth_app.state.scheduled_task_scheduler, PersistentTaskScheduler)
    assert auth_app.state.scheduled_task_scheduler.is_started
    store = SqlAlchemyScheduledTaskStore(db_uri)
    if not scheduler_available:
        await auth_app.state.scheduled_task_scheduler.stop()
        monkeypatch.setattr(auth_app.state, "scheduled_task_scheduler", None)
    created = await auth_client.post(
        "/v1/scheduled-tasks", json=_create_body(), headers=_headers()
    )
    assert created.status_code == 200, created.text
    task = created.json()
    assert (
        datetime.fromisoformat(task["next_run_at"]).timestamp()
        == store.get(task["id"]).next_run_at
    )
    url = f"/v1/scheduled-tasks/{task['id']}"
    fetched = await auth_client.get(url, headers=_headers())
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["next_run_at"] == task["next_run_at"]
    listed = await auth_client.get("/v1/scheduled-tasks", headers=_headers())
    assert listed.status_code == 200, listed.text
    assert listed.json()["scheduled_tasks"][0]["next_run_at"] == task["next_run_at"]
    for edit in ({"rrule": "FREQ=DAILY;BYHOUR=17"}, {"timezone": "Asia/Tokyo"}):
        changed = await auth_client.patch(url, json=edit, headers=_headers())
        assert changed.status_code == 200, changed.text
        assert all(changed.json()[key] == value for key, value in edit.items())
        assert changed.json()["next_run_at"] != fetched.json()["next_run_at"]
        assert (
            datetime.fromisoformat(changed.json()["next_run_at"]).timestamp()
            == store.get(task["id"]).next_run_at
        )
        fetched = changed
    paused = await auth_client.patch(url, json={"state": "paused"}, headers=_headers())
    assert paused.status_code == 200, paused.text
    assert paused.json()["next_run_at"] is None
    resumed = await auth_client.patch(url, json={"state": "active"}, headers=_headers())
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["next_run_at"] is not None
    deleted = await auth_client.delete(url, headers=_headers())
    assert deleted.status_code == 200, deleted.text
    assert store.get(task["id"]) is None


@pytest.mark.parametrize("load_fails", [False, True])
async def test_legacy_retry_keeps_tasks_created_and_edited_during_load(
    auth_app, db_uri, monkeypatch, load_fails
):
    import asyncio
    from threading import Event

    from tests._helpers.polling import poll_until_async

    store = SqlAlchemyScheduledTaskStore(db_uri)
    existing = store.create(
        "0123456789abcdef0123456789abcdef",
        "existing",
        "prompt",
        "FREQ=DAILY;BYHOUR=9",
        "alice@example.com",
        builtin_agent_id(CLAUDE_NATIVE_AGENT_NAME),
        "UTC",
    )
    entered, release = asyncio.Event(), Event()
    loop = asyncio.get_running_loop()
    load = SqlAlchemyScheduledTaskStore.list_active_all_workspaces
    attempts = 0

    def fail_then_snapshot(self):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary database outage")
        snapshot = load(self)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10), "Startup snapshot was never released"
        if load_fails and attempts == 2:
            raise RuntimeError("database read failed after route mutations")
        return snapshot

    monkeypatch.setattr(
        SqlAlchemyScheduledTaskStore, "list_active_all_workspaces", fail_then_snapshot
    )
    async with auth_app.router.lifespan_context(auth_app):
        assert auth_app.state.scheduled_task_scheduler is None
        try:
            await asyncio.wait_for(entered.wait(), 10)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=auth_app), base_url="http://test"
            ) as client:
                created = await client.post(
                    "/v1/scheduled-tasks", json=_create_body(), headers=_headers()
                )
                assert created.status_code == 200, created.text
                edited = await client.patch(
                    f"/v1/scheduled-tasks/{existing.id}",
                    json={"rrule": "FREQ=DAILY;BYHOUR=17", "timezone": "Asia/Tokyo"},
                    headers=_headers(),
                )
                assert edited.status_code == 200, edited.text
                assert created.json()["next_run_at"] is not None
                assert edited.json()["next_run_at"] is not None
        finally:
            release.set()
        await poll_until_async(
            lambda: (
                auth_app.state.scheduled_task_scheduler is not None
                and auth_app.state.scheduled_task_scheduler.is_started
            ),
            timeout_s=10,
        )
        scheduler = auth_app.state.scheduled_task_scheduler
        assert scheduler.job_count == 2
        assert scheduler.next_run_at(created.json()["id"]) == created.json()["next_run_at"]
        assert scheduler.next_run_at(existing.id) == edited.json()["next_run_at"]


_HOST_ID = "4b653f6031f35d168cc0b37caa1306d1"


def _owned_session(
    db_uri: str, owner: str = "alice@example.com", *, archived: bool = False, **fields: object
) -> str:
    """A top-level session on the test host, with an owner grant for *owner*."""
    from omnigent.server.auth import LEVEL_OWNER

    conversations = SqlAlchemyConversationStore(db_uri)
    values: dict[str, object] = {
        "agent_id": builtin_agent_id(CLAUDE_NATIVE_AGENT_NAME),
        "host_id": _HOST_ID,
        "workspace": "/repo",
    }
    values.update(fields)
    conv = conversations.create_conversation(**values)  # type: ignore[arg-type]
    if archived:
        conversations.update_conversation(conv.id, archived=True)
    SqlAlchemyPermissionStore(db_uri).grant(owner, conv.id, LEVEL_OWNER)
    return conv.id


async def test_picking_a_session_pins_the_host_it_runs_on(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """The session cannot move, so an unpinned task routes every run to its host."""
    session_id = _owned_session(db_uri)
    created = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(
            session_mode="continue", session_id=session_id, host_id=None, workspace=None
        ),
        headers=_headers(),
    )
    assert created.status_code == 200, created.text
    assert created.json()["host_id"] == _HOST_ID

    unpinned = (
        await auth_client.post(
            "/v1/scheduled-tasks",
            json=_create_body(session_mode="continue", host_id=None, workspace=None),
            headers=_headers(),
        )
    ).json()
    assert unpinned["host_id"] is None
    picked = await auth_client.patch(
        f"/v1/scheduled-tasks/{unpinned['id']}",
        json={"session_id": _owned_session(db_uri)},
        headers=_headers(),
    )
    assert picked.status_code == 200, picked.text
    assert picked.json()["host_id"] == _HOST_ID


async def test_session_mode_round_trips_and_a_null_session_starts_a_new_one(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """``continue`` keeps the first run's session until a PATCH clears it or picks ``new``."""
    default = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    assert (default["session_mode"], default["session_id"]) == ("new", None)

    created = (
        await auth_client.post(
            "/v1/scheduled-tasks",
            json=_create_body(session_mode="continue"),
            headers=_headers(),
        )
    ).json()
    assert (created["session_mode"], created["session_id"]) == ("continue", None)
    task_id = created["id"]
    first_session = _owned_session(db_uri)
    schedules = SqlAlchemyScheduledTaskStore(db_uri)
    snapshot = schedules.get(task_id)
    assert schedules.adopt_session(task_id, first_session, snapshot=snapshot) == "adopted"

    renamed = await auth_client.patch(
        f"/v1/scheduled-tasks/{task_id}", json={"name": "renamed"}, headers=_headers()
    )
    assert renamed.json()["session_id"] == first_session

    cleared = await auth_client.patch(
        f"/v1/scheduled-tasks/{task_id}", json={"session_id": None}, headers=_headers()
    )
    assert cleared.status_code == 200, cleared.text
    assert (cleared.json()["session_mode"], cleared.json()["session_id"]) == ("continue", None)

    picked = await auth_client.patch(
        f"/v1/scheduled-tasks/{task_id}", json={"session_id": first_session}, headers=_headers()
    )
    assert picked.status_code == 200, picked.text
    assert picked.json()["session_id"] == first_session

    to_new = await auth_client.patch(
        f"/v1/scheduled-tasks/{task_id}", json={"session_mode": "new"}, headers=_headers()
    )
    assert (to_new.json()["session_mode"], to_new.json()["session_id"]) == ("new", None)


async def test_create_can_continue_a_session_the_owner_picked(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    session_id = _owned_session(db_uri)

    resp = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(session_mode="continue", session_id=session_id),
        headers=_headers(),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["session_mode"], body["session_id"]) == ("continue", session_id)
    # The task runs the session's own agent rather than taking a copy.
    assert body["agent_id"] == builtin_agent_id(CLAUDE_NATIVE_AGENT_NAME)

    # A session that skips approval takes only a task that selects such a mode itself.
    bypassing = _owned_session(
        db_uri, labels={"omnigent.claude_native.permission_mode": "bypassPermissions"}
    )
    for permission_mode, status in ((None, 400), ("auto", 400), ("bypassPermissions", 200)):
        picked = await auth_client.post(
            "/v1/scheduled-tasks",
            json=_create_body(
                session_mode="continue", session_id=bypassing, permission_mode=permission_mode
            ),
            headers=_headers(),
        )
        assert picked.status_code == status, (permission_mode, picked.text)
        if status == 400:
            assert "which skips approval" in picked.json()["error"]["message"]


async def test_a_picked_session_must_run_the_saved_mcp_services(
    auth_app: FastAPI, auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    # Continuing reuses the session as it is, so a run could not add services to it.
    _mcp_registry(auth_app)
    plain = _owned_session(db_uri)
    with_github = _owned_session(db_uri, labels={"omnigent.mcp_service.github": "1"})
    for session_id, services, status in (
        (plain, ["github"], 400),
        (with_github, ["github", "linear"], 400),
        (with_github, ["github"], 200),
        (plain, [], 200),
        (with_github, [], 200),
    ):
        resp = await auth_client.post(
            "/v1/scheduled-tasks",
            json=_create_body(
                session_mode="continue", session_id=session_id, mcp_services=services
            ),
            headers=_headers(),
        )
        assert resp.status_code == status, (session_id, services, resp.text)
        if status == 400:
            assert "runs different MCP services" in resp.json()["error"]["message"]

    task = (
        await auth_client.post(
            "/v1/scheduled-tasks",
            json=_create_body(
                session_mode="continue", session_id=with_github, mcp_services=["github"]
            ),
            headers=_headers(),
        )
    ).json()
    url = f"/v1/scheduled-tasks/{task['id']}"
    # Naming a session alongside new services skips the reset that drops a stale one.
    for edit in (
        {"session_id": plain},
        {"session_id": with_github, "mcp_services": ["linear"]},
    ):
        rejected = await auth_client.patch(url, json=edit, headers=_headers())
        assert rejected.status_code == 400, (edit, rejected.text)
        kept = (await auth_client.get(url, headers=_headers())).json()
        assert (kept["session_id"], kept["mcp_services"]) == (with_github, ["github"])

    moved = await auth_client.patch(
        url, json={"session_id": plain, "mcp_services": []}, headers=_headers()
    )
    assert moved.status_code == 200, moved.text
    assert (moved.json()["session_id"], moved.json()["mcp_services"]) == (plain, [])


async def test_a_picked_session_must_be_the_owners_and_fit_the_task(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    _make_user(db_uri, "bob@example.com")
    bobs = _owned_session(db_uri, "bob@example.com")
    cases = {
        "someone else's": (bobs, 404),
        # Nothing else about a session the caller can't see is told.
        "someone else's archived": (_owned_session(db_uri, "bob@example.com", archived=True), 404),
        "someone else's sub-agent": (
            _owned_session(db_uri, "bob@example.com", parent_conversation_id=bobs),
            404,
        ),
        "missing": ("cd" * 16, 404),
        "archived": (_owned_session(db_uri, archived=True), 400),
        "on another workspace": (_owned_session(db_uri, workspace="/elsewhere"), 400),
    }
    for label, (session_id, status) in cases.items():
        resp = await auth_client.post(
            "/v1/scheduled-tasks",
            json=_create_body(session_mode="continue", session_id=session_id),
            headers=_headers(),
        )
        assert resp.status_code == status, (label, resp.text)

    new_mode = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(session_id=_owned_session(db_uri)),
        headers=_headers(),
    )
    assert new_mode.status_code == 422, new_mode.text


def _session_with_history(
    db_uri: str, owner: str = "alice@example.com", *, finished: bool = True, **fields: object
) -> str:
    """An owned session with one finished exchange and a turn still in progress.

    The turn in progress has sent progress text, a completed message like a
    reply, and waits on its tool call. With ``finished=False`` the session holds
    only the first turn's message, with no reply yet.
    """
    from omnigent.entities import FunctionCallData, NewConversationItem

    session_id = _owned_session(db_uri, owner, **fields)
    items = [_message("user", "resp_1", "summarize the alerts")]
    if finished:
        items += [
            _message("assistant", "resp_1", "three alerts"),
            _message("user", "resp_2", "and the next turn"),
            _message("assistant", "resp_2", "checking the next one"),
            NewConversationItem(
                type="function_call",
                response_id="resp_2",
                data=FunctionCallData(
                    agent="claude-native", name="Bash", arguments="{}", call_id="call_1"
                ),
            ),
        ]
    SqlAlchemyConversationStore(db_uri).append(session_id, items)
    return session_id


def _message(role: str, response_id: str, text: str) -> Any:
    from omnigent.entities import MessageData, NewConversationItem

    part = "input_text" if role == "user" else "output_text"
    return NewConversationItem(
        type="message",
        response_id=response_id,
        data=MessageData(
            role=role,
            content=[{"type": part, "text": text}],
            agent=None if role == "user" else "claude-native",
        ),
    )


def _item_texts(db_uri: str, session_id: str) -> list[str]:
    """Each message's text, and the type of any other item."""
    texts = []
    for item in SqlAlchemyConversationStore(db_uri).list_items(session_id).data:
        if item.type != "message":
            texts.append(item.type)
            continue
        part = item.data.content[0]
        texts.append(part["text"] if isinstance(part, dict) else part.text)
    return texts


async def test_create_from_a_session_copies_it_for_the_runs(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    from omnigent.server.auth import LEVEL_OWNER
    from omnigent.stores.conversation_store import SCHEDULED_TASK_SOURCE_LABEL_KEY

    source_id = _session_with_history(db_uri)

    resp = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(source_session_id=source_id, prompt=""),
        headers=_headers(),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["session_mode"], body["prompt"]) == ("new", "")
    source_copy_id = body["source_session_id"]
    assert source_copy_id not in (None, source_id)
    source_copy = SqlAlchemyConversationStore(db_uri).get_conversation(source_copy_id)
    assert source_copy is not None
    assert source_copy.title == "nightly triage"
    assert source_copy.labels[SCHEDULED_TASK_SOURCE_LABEL_KEY] == body["id"]
    # Copied through the last finished reply; the turn in progress stays behind.
    assert _item_texts(db_uri, source_copy_id) == ["summarize the alerts", "three alerts"]
    assert _item_texts(db_uri, source_id)[2:] == [
        "and the next turn",
        "checking the next one",
        "function_call",
    ]
    assert SqlAlchemyPermissionStore(db_uri).check_access(
        "alice@example.com", source_copy_id, LEVEL_OWNER
    )
    fetched = await auth_client.get(f"/v1/scheduled-tasks/{body['id']}", headers=_headers())
    assert fetched.json()["source_session_id"] == source_copy_id


@pytest.mark.parametrize(
    ("live_status", "last_copied"), [("idle", "all fixed"), ("running", "three alerts")]
)
async def test_a_running_sessions_newest_reply_stays_out_of_the_copy(
    auth_client: httpx.AsyncClient, db_uri: str, live_status: str, last_copied: str
) -> None:
    from omnigent.entities import FunctionCallOutputData, NewConversationItem

    source_id = _session_with_history(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    # The tool call returns and the turn replies, which ends the turn only once it goes idle.
    output = FunctionCallOutputData(call_id="call_1", output="ok")
    store.append(
        source_id,
        [
            NewConversationItem(type="function_call_output", response_id="resp_2", data=output),
            _message("assistant", "resp_2", "all fixed"),
        ],
    )
    store.set_session_live_status(source_id, live_status)

    resp = await auth_client.post(
        "/v1/scheduled-tasks", json=_create_body(source_session_id=source_id), headers=_headers()
    )

    assert resp.status_code == 200, resp.text
    assert _item_texts(db_uri, resp.json()["source_session_id"])[-1] == last_copied


async def test_a_source_session_must_be_the_owners_top_level_session(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    _make_user(db_uri, "bob@example.com")
    parent = _owned_session(db_uri)
    existing = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(source_session_id=_session_with_history(db_uri)),
        headers=_headers(),
    )
    assert existing.status_code == 200, existing.text
    cases = {
        "someone else's": (_owned_session(db_uri, "bob@example.com"), 404),
        "missing": ("cd" * 16, 404),
        "a sub-agent's": (_owned_session(db_uri, parent_conversation_id=parent), 400),
        "an automation's copy": (existing.json()["source_session_id"], 400),
        "one with no finished reply yet": (_session_with_history(db_uri, finished=False), 400),
    }
    for label, (session_id, status) in cases.items():
        resp = await auth_client.post(
            "/v1/scheduled-tasks",
            json=_create_body(source_session_id=session_id),
            headers=_headers(),
        )
        assert resp.status_code == status, (label, resp.text)

    continuing = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(source_session_id=parent, session_mode="continue"),
        headers=_headers(),
    )
    assert continuing.status_code == 422, continuing.text


async def test_a_task_that_starts_from_a_session_can_switch_agents_but_not_continue(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    codex = builtin_agent_id(CODEX_NATIVE_AGENT_NAME)
    claude = builtin_agent_id(CLAUDE_NATIVE_AGENT_NAME)
    # Runs can use another agent than the copied session's; the copy keeps its own.
    created = (
        await auth_client.post(
            "/v1/scheduled-tasks",
            json=_create_body(source_session_id=_session_with_history(db_uri), agent_id=codex),
            headers=_headers(),
        )
    ).json()
    assert created["agent_id"] == codex
    copy = SqlAlchemyConversationStore(db_uri).get_conversation(created["source_session_id"])
    assert copy is not None and copy.agent_id == claude
    url = f"/v1/scheduled-tasks/{created['id']}"

    switched = await auth_client.patch(url, json={"agent_id": claude}, headers=_headers())
    assert switched.status_code == 200, switched.text
    assert switched.json()["agent_id"] == claude
    continuing = await auth_client.patch(
        url, json={"session_mode": "continue"}, headers=_headers()
    )
    assert continuing.status_code == 400, continuing.text
    cleared = await auth_client.patch(url, json={"prompt": ""}, headers=_headers())
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["source_session_id"] == created["source_session_id"]


async def test_the_copy_carries_history_for_its_own_agent_not_the_tasks(
    auth_client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server.scheduled import source_session
    from omnigent.stores.conversation_store import FORK_CARRY_HISTORY_LABEL_KEY

    claude = builtin_agent_id(CLAUDE_NATIVE_AGENT_NAME)
    # Only the session's agent rebuilds a copied transcript; the task's doesn't.
    monkeypatch.setattr(source_session, "carries_native_history", lambda agent: agent.id == claude)
    created = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(
            source_session_id=_session_with_history(db_uri),
            agent_id=builtin_agent_id(CODEX_NATIVE_AGENT_NAME),
        ),
        headers=_headers(),
    )
    assert created.status_code == 200, created.text

    copy = SqlAlchemyConversationStore(db_uri).get_conversation(
        created.json()["source_session_id"]
    )
    assert copy is not None and copy.agent_id == claude
    assert copy.labels.get(FORK_CARRY_HISTORY_LABEL_KEY) == "1"


async def test_runs_need_an_agent_that_can_open_the_sessions_file_attachments(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    from omnigent.entities import MessageData, NewConversationItem
    from omnigent.native.native_coding_agents import PI_NATIVE_AGENT_NAME

    source_id = _owned_session(db_uri)
    stored = SqlAlchemyFileStore(db_uri).create(
        filename="sample.zip", bytes=4, session_id=source_id
    )
    SqlAlchemyConversationStore(db_uri).append(
        source_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_1",
                data=MessageData(
                    role="user",
                    content=[
                        {"type": "input_text", "text": "summarize this"},
                        {"type": "input_file", "file_id": stored.id, "filename": "sample.zip"},
                    ],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="resp_1",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "done"}],
                    agent="claude-native",
                ),
            ),
        ],
    )
    pi = builtin_agent_id(PI_NATIVE_AGENT_NAME)

    # Pi can't open the zip the copied history refers to, as with a fork.
    rejected = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(source_session_id=source_id, agent_id=pi),
        headers=_headers(),
    )
    assert rejected.status_code == 400, rejected.text
    assert "sample.zip" in rejected.json()["error"]["message"]

    created = await auth_client.post(
        "/v1/scheduled-tasks", json=_create_body(source_session_id=source_id), headers=_headers()
    )
    assert created.status_code == 200, created.text
    switched = await auth_client.patch(
        f"/v1/scheduled-tasks/{created.json()['id']}", json={"agent_id": pi}, headers=_headers()
    )
    assert switched.status_code == 400, switched.text
    assert "sample.zip" in switched.json()["error"]["message"]


@pytest.mark.parametrize("source_drops_its_file", [False, True])
async def test_a_failed_create_deletes_the_copy_with_its_file_rows(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    source_drops_its_file: bool,
) -> None:
    from omnigent.errors import ErrorCode, OmnigentError
    from omnigent.server.scheduled import source_session

    source_id = _session_with_history(db_uri)
    files = SqlAlchemyFileStore(db_uri)
    files.create(filename="notes.txt", bytes=5, session_id=source_id)
    copies: list[tuple[str, list[str]]] = []
    copy_session = source_session.copy_session

    def _recording_copy(**kwargs: Any) -> Any:
        conv = copy_session(**kwargs)
        copies.append((conv.id, [stored.filename for stored in files.list(conv.id).data]))
        return conv

    def _failing_create(*args: Any, **kwargs: Any) -> None:
        if source_drops_its_file:
            # The copy then holds the blob's last row, and the blob won't delete.
            files.delete_all_for_session(source_id)
        raise OmnigentError("store unavailable", code=ErrorCode.INVALID_INPUT)

    def _failing_blob_delete(self: Any, key: str) -> None:
        raise OSError("storage unavailable")

    monkeypatch.setattr(source_session, "copy_session", _recording_copy)
    monkeypatch.setattr(SqlAlchemyScheduledTaskStore, "create", _failing_create)
    monkeypatch.setattr(LocalArtifactStore, "delete", _failing_blob_delete)
    resp = await auth_client.post(
        "/v1/scheduled-tasks", json=_create_body(source_session_id=source_id), headers=_headers()
    )
    assert resp.status_code == 400, resp.text

    # The copy and the file row it had are gone; the source keeps its own file.
    ((copy_id, copied_files),) = copies
    assert copied_files == ["notes.txt"]
    assert SqlAlchemyConversationStore(db_uri).get_conversation(copy_id) is None
    assert files.list(copy_id).data == []
    kept = [] if source_drops_its_file else ["notes.txt"]
    assert [stored.filename for stored in files.list(source_id).data] == kept


async def test_a_copy_that_cannot_take_every_file_is_deleted(
    auth_client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_id = _session_with_history(db_uri)
    files = SqlAlchemyFileStore(db_uri)
    for filename in ("notes.txt", "more.txt"):
        files.create(filename=filename, bytes=5, session_id=source_id)
    conversations = SqlAlchemyConversationStore(db_uri)
    before = {conv.id for conv in conversations.list_conversations(limit=100).data}
    create = SqlAlchemyFileStore.create
    copy_rows: list[str] = []

    def _second_copy_row_fails(self: Any, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("session_id") != source_id:
            copy_rows.append(kwargs["session_id"])
            if len(copy_rows) == 2:
                raise RuntimeError("database unavailable")
        return create(self, *args, **kwargs)

    monkeypatch.setattr(SqlAlchemyFileStore, "create", _second_copy_row_fails)
    resp = await auth_client.post(
        "/v1/scheduled-tasks", json=_create_body(source_session_id=source_id), headers=_headers()
    )

    assert resp.status_code == 500, resp.text
    # The partial copy and the row it did get are gone; the source keeps its files.
    assert {conv.id for conv in conversations.list_conversations(limit=100).data} == before
    assert files.list(copy_rows[0]).data == []
    assert sorted(stored.filename for stored in files.list(source_id).data) == [
        "more.txt",
        "notes.txt",
    ]


@pytest.mark.parametrize("stage", ["copy", "user", "grant"])
async def test_a_cancelled_create_deletes_the_copy_once_its_writes_settle(
    auth_client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    import threading

    from omnigent.server.scheduled import source_session

    source_id = _session_with_history(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    before = {conv.id for conv in conversations.list_conversations(limit=100).data}
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    def _held(call: Callable[..., Any]) -> Callable[..., Any]:
        # The write commits, then its thread waits past the cancellation.
        def held(*args: Any, **kwargs: Any) -> Any:
            result = call(*args, **kwargs)
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
            return result

        return held

    if stage == "copy":
        monkeypatch.setattr(source_session, "copy_session", _held(source_session.copy_session))
    else:
        name = "ensure_user" if stage == "user" else "grant"
        monkeypatch.setattr(
            SqlAlchemyPermissionStore, name, _held(getattr(SqlAlchemyPermissionStore, name))
        )
    pending = asyncio.create_task(
        auth_client.post(
            "/v1/scheduled-tasks",
            json=_create_body(source_session_id=source_id),
            headers=_headers(),
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    pending.cancel()
    await asyncio.sleep(0)
    # A second cancellation must not interrupt the cleanup.
    pending.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, 5)

    assert {conv.id for conv in conversations.list_conversations(limit=100).data} == before


async def test_a_copy_takes_only_attachments_the_deployment_accepts(
    auth_client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_id = _session_with_history(db_uri)
    SqlAlchemyFileStore(db_uri).create(filename="bundle.zip", bytes=5, session_id=source_id)
    conversations = SqlAlchemyConversationStore(db_uri)
    before = {conv.id for conv in conversations.list_conversations(limit=100).data}
    monkeypatch.setattr(
        "omnigent.server.server_config.load_server_config",
        lambda: {"filesystem_attachment_denied_extensions": ["zip"]},
    )

    resp = await auth_client.post(
        "/v1/scheduled-tasks", json=_create_body(source_session_id=source_id), headers=_headers()
    )

    assert resp.status_code == 415, resp.text
    # Refused before anything is copied.
    assert {conv.id for conv in conversations.list_conversations(limit=100).data} == before


@pytest.mark.parametrize("failing", ["label clear", "task delete"])
async def test_deleting_the_automation_keeps_its_copy_as_an_ordinary_session(
    auth_client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    from omnigent.stores.conversation_store import SCHEDULED_TASK_SOURCE_LABEL_KEY

    source_id = _session_with_history(db_uri, title="Daily alerts")
    created = (
        await auth_client.post(
            "/v1/scheduled-tasks",
            json=_create_body(source_session_id=source_id),
            headers=_headers(),
        )
    ).json()
    copy_id = created["source_session_id"]
    conversations = SqlAlchemyConversationStore(db_uri)
    copy = conversations.get_conversation(copy_id)
    assert copy is not None and copy.title == "Daily alerts"

    # Either step failing fails the delete with the automation and its label kept,
    # so a retry can finish.
    def _unavailable(self: Any, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("database unavailable")

    with monkeypatch.context() as broken:
        if failing == "label clear":
            broken.setattr(SqlAlchemyConversationStore, "delete_label", _unavailable)
        else:
            broken.setattr(SqlAlchemyScheduledTaskStore, "delete", _unavailable)
        with pytest.raises(RuntimeError):
            await auth_client.delete(f"/v1/scheduled-tasks/{created['id']}", headers=_headers())
    still = await auth_client.get(f"/v1/scheduled-tasks/{created['id']}", headers=_headers())
    assert still.status_code == 200, still.text
    labelled = conversations.get_conversation(copy_id)
    assert labelled is not None
    assert labelled.labels[SCHEDULED_TASK_SOURCE_LABEL_KEY] == created["id"]

    deleted = await auth_client.delete(f"/v1/scheduled-tasks/{created['id']}", headers=_headers())

    assert deleted.status_code == 200, deleted.text
    kept = conversations.get_conversation(copy_id)
    assert kept is not None
    assert SCHEDULED_TASK_SOURCE_LABEL_KEY not in kept.labels


async def test_a_patch_that_picks_a_session_takes_no_agent_copy(
    auth_client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A picked session keeps the agent it runs, so switching to it copies nothing."""
    from omnigent.native.native_coding_agents import CODEX_NATIVE_AGENT_NAME

    created = (
        await auth_client.post(
            "/v1/scheduled-tasks", json=_create_body(session_mode="continue"), headers=_headers()
        )
    ).json()
    copies: list[str] = []

    def _copy(
        agent_store: object, artifact_store: object, agent: SimpleNamespace, user_id: object
    ) -> SimpleNamespace:
        # Stands in for an agent shared by another user, which binding copies;
        # this app has no fixture for one.
        copies.append(agent.id)
        return SimpleNamespace(id=uuid.uuid4().hex)

    monkeypatch.setattr(scheduled_tasks_routes, "agent_for_user", _copy)
    codex = builtin_agent_id(CODEX_NATIVE_AGENT_NAME)
    session_id = _owned_session(db_uri, agent_id=codex)

    patched = await auth_client.patch(
        f"/v1/scheduled-tasks/{created['id']}",
        json={"agent_id": codex, "session_id": session_id},
        headers=_headers(),
    )

    assert patched.status_code == 200, patched.text
    assert (patched.json()["agent_id"], patched.json()["session_id"]) == (codex, session_id)

    # Picking a session that skips approval checks the requested mode, else the stored one.
    full_access = _owned_session(
        db_uri,
        agent_id=codex,
        terminal_launch_args=["--sandbox", "danger-full-access", "--ask-for-approval", "never"],
    )
    for update, status in (
        ({"permission_mode": "full-access"}, 200),
        ({}, 200),
        ({"permission_mode": "approve-for-me"}, 400),
        ({"permission_mode": None}, 400),
    ):
        picked = await auth_client.patch(
            f"/v1/scheduled-tasks/{created['id']}",
            json={"session_id": full_access, **update},
            headers=_headers(),
        )
        assert picked.status_code == status, (update, picked.text)
        if status == 400:
            assert "which skips approval" in picked.json()["error"]["message"]
    # Changing only the mode is checked against the session the task continues.
    for permission_mode, status in (("approve-for-me", 400), (None, 400), ("bypass", 200)):
        changed = await auth_client.patch(
            f"/v1/scheduled-tasks/{created['id']}",
            json={"permission_mode": permission_mode},
            headers=_headers(),
        )
        assert changed.status_code == status, (permission_mode, changed.text)
        if status == 400:
            assert "which skips approval" in changed.json()["error"]["message"]
    assert copies == []


@pytest.mark.parametrize(
    "edit", [{"workspace": "/other"}, {"mcp_services": ["github"]}], ids=["pin", "mcp"]
)
async def test_changing_where_a_task_runs_starts_a_new_session(
    auth_app: FastAPI, auth_client: httpx.AsyncClient, db_uri: str, edit: dict[str, object]
) -> None:
    """A continued session cannot follow a new pin or MCP selection, so the next run starts
    a new one."""
    _mcp_registry(auth_app)
    session_id = _owned_session(db_uri)
    created = (
        await auth_client.post(
            "/v1/scheduled-tasks",
            json=_create_body(session_mode="continue", session_id=session_id),
            headers=_headers(),
        )
    ).json()

    resent = await auth_client.patch(
        f"/v1/scheduled-tasks/{created['id']}",
        json={"name": "renamed", "host_id": _HOST_ID, "workspace": "/repo"},
        headers=_headers(),
    )
    assert resent.json()["session_id"] == session_id

    moved = await auth_client.patch(
        f"/v1/scheduled-tasks/{created['id']}", json=edit, headers=_headers()
    )

    assert moved.status_code == 200, moved.text
    assert (moved.json()["session_mode"], moved.json()["session_id"]) == ("continue", None)


async def test_automation_inbox_owner_scope_and_cursor(auth_client, db_uri):
    created = await auth_client.post(
        "/v1/scheduled-tasks", json=_create_body(), headers=_headers()
    )
    task_id = created.json()["id"]
    store = SqlAlchemyScheduledTaskStore(db_uri)
    run_id = uuid.uuid4().hex
    store.create_run(
        run_id=run_id,
        scheduled_task_id=task_id,
        status="failed",
        scheduled_at=100,
        finished_at=101,
        error_code="host_offline",
        error="private launch details",
    )
    response = await auth_client.get("/v1/scheduled-tasks/inbox", headers=_headers())
    assert response.status_code == 200, response.text
    assert response.json() == {
        "data": [
            {
                "task_id": task_id,
                "task_name": created.json()["name"],
                "run_id": run_id,
                "scheduled_at": 100,
                "finished_at": 101,
                "error_code": "host_offline",
            }
        ],
        "next_cursor": None,
    }
    second = await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    second_task_id = second.json()["id"]
    store.create_run(
        run_id=uuid.uuid4().hex,
        scheduled_task_id=second_task_id,
        status="failed",
        scheduled_at=200,
    )
    first_page = (
        await auth_client.get("/v1/scheduled-tasks/inbox", headers=_headers(), params={"limit": 1})
    ).json()
    assert len(first_page["data"]) == 1 and first_page["next_cursor"] is not None
    second_page = (
        await auth_client.get(
            "/v1/scheduled-tasks/inbox",
            headers=_headers(),
            params={"limit": 1, "after": first_page["next_cursor"]},
        )
    ).json()
    assert len(second_page["data"]) == 1 and second_page["next_cursor"] is None
    assert {item["task_id"] for item in first_page["data"] + second_page["data"]} == {
        task_id,
        second_task_id,
    }
    other = await auth_client.get("/v1/scheduled-tasks/inbox", headers=_headers("bob@example.com"))
    assert other.status_code == 200 and other.json()["data"] == []
    for params in ({"limit": 0}, {"limit": 101}, {"after": "bad"}, {"after": "-1:" + task_id}):
        invalid = await auth_client.get(
            "/v1/scheduled-tasks/inbox", headers=_headers(), params=params
        )
        assert invalid.status_code == 422


async def test_automation_inbox_does_not_duplicate_a_deleted_session(auth_client, db_uri):
    created = await auth_client.post(
        "/v1/scheduled-tasks", json=_create_body(), headers=_headers()
    )
    session_id = _owned_session(db_uri)
    store = SqlAlchemyScheduledTaskStore(db_uri)
    store.create_run(
        run_id=uuid.uuid4().hex,
        scheduled_task_id=created.json()["id"],
        status="failed",
        scheduled_at=100,
        conversation_id=session_id,
        error_code="launch_failed",
    )
    for delete_session in (False, True):
        if delete_session:
            assert await SqlAlchemyConversationStore(db_uri).delete_conversation(session_id)
        response = await auth_client.get("/v1/scheduled-tasks/inbox", headers=_headers())
        assert response.status_code == 200 and response.json()["data"] == []


async def test_random_worktree_can_be_enabled_preserved_and_disabled(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    response = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(base_branch="HEAD", session_mode="continue"),
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    task = response.json()
    assert task["base_branch"] == "HEAD"
    store = SqlAlchemyScheduledTaskStore(db_uri)
    session_id = _owned_session(db_uri)
    store.update(task["id"], session_id=session_id)
    url = f"/v1/scheduled-tasks/{task['id']}"
    for patch, expected in [
        ({"name": "renamed"}, "HEAD"),
        ({"base_branch": None}, None),
        ({"base_branch": "HEAD"}, "HEAD"),
    ]:
        store.update(task["id"], session_id=session_id)
        response = await auth_client.patch(url, json=patch, headers=_headers())
        assert response.status_code == 200, response.text
        assert response.json()["base_branch"] == expected
        if "base_branch" in patch:
            assert response.json()["session_id"] is None
        else:
            assert response.json()["session_id"] == session_id
        stored = SqlAlchemyScheduledTaskStore(db_uri).get(task["id"])
        assert stored is not None and stored.base_branch == expected


@pytest.mark.parametrize("missing", ["host_id", "workspace"])
async def test_random_worktree_requires_pinned_repository(
    auth_client: httpx.AsyncClient, missing: str
) -> None:
    response = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(base_branch="HEAD", **{missing: None}),
        headers=_headers(),
    )
    assert response.status_code == 422, response.text
    assert "random worktree requires" in response.text


@pytest.mark.parametrize(
    "execution_target,session_mode,minutes,status",
    [
        ("connected_host", "new", 10, 200),
        ("connected_host", "continue", 10, 200),
        ("managed_sandbox", "continue", 10, 200),
        ("managed_sandbox", "new", 10, 400),
        ("managed_sandbox", "new", 60, 200),
    ],
)
async def test_create_interval_policy(
    auth_client, auth_app, execution_target, session_mode, minutes, status
):
    auth_app.state.sandbox_config = _sandbox_deployment()
    response = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(
            session_mode=session_mode,
            execution_target=execution_target,
            host_id=None,
            workspace=None,
            rrule=f"FREQ=MINUTELY;INTERVAL={minutes}",
        ),
        headers=_headers(),
    )
    assert response.status_code == status, response.text
    if status == 200:
        assert response.json()["session_mode"] == session_mode
        assert response.json()["execution_target"] == execution_target
        assert response.json()["session_id"] is None


async def test_update_validates_effective_schedule_and_session_mode(auth_client, auth_app):
    auth_app.state.sandbox_config = _sandbox_deployment()
    response = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(execution_target="managed_sandbox", host_id=None, workspace=None),
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    url = f"/v1/scheduled-tasks/{response.json()['id']}"
    for patch, expected in [
        ({"rrule": "FREQ=MINUTELY;INTERVAL=10"}, 400),
        ({"rrule": "FREQ=MINUTELY;INTERVAL=10", "session_mode": "continue"}, 200),
        ({"rrule": "FREQ=MINUTELY;INTERVAL=15"}, 200),
        ({"rrule": "FREQ=MINUTELY;INTERVAL=9"}, 400),
        ({"session_mode": "new"}, 400),
        ({"name": "Renamed"}, 200),
        ({"execution_target": "connected_host", "session_mode": "new"}, 200),
        ({"execution_target": "managed_sandbox"}, 400),
        ({"execution_target": "managed_sandbox", "session_mode": "continue"}, 200),
    ]:
        response = await auth_client.patch(url, json=patch, headers=_headers())
        assert response.status_code == expected, response.text
    saved = (await auth_client.get(url, headers=_headers())).json()
    assert (saved["session_mode"], saved["rrule"]) == ("continue", "FREQ=MINUTELY;INTERVAL=15")
    response = await auth_client.patch(
        url, json={"session_mode": "new", "rrule": "FREQ=HOURLY"}, headers=_headers()
    )
    assert response.status_code == 200, response.text
    assert response.json()["session_mode"] == "new"


@pytest.mark.parametrize("policy_field", ["session_mode", "execution_target"])
@pytest.mark.parametrize("recurrence_first", [False, True])
async def test_concurrent_schedule_edits_reject_stale_validation(
    auth_client, auth_app, monkeypatch, policy_field, recurrence_first
):
    auth_app.state.sandbox_config = _sandbox_deployment()
    initial = _create_body(
        execution_target="managed_sandbox" if policy_field == "session_mode" else "connected_host",
        session_mode="continue" if policy_field == "session_mode" else "new",
        host_id=None,
        workspace=None,
    )
    response = await auth_client.post("/v1/scheduled-tasks", json=initial, headers=_headers())
    assert response.status_code == 200, response.text
    url = f"/v1/scheduled-tasks/{response.json()['id']}"
    recurrence_patch = {"rrule": "FREQ=MINUTELY;INTERVAL=10"}
    policy_patch = {policy_field: "new" if policy_field == "session_mode" else "managed_sandbox"}
    validate = scheduled_tasks_routes._validate_rrule_or_400
    validated = asyncio.Barrier(2)
    first_saved = asyncio.Event()

    async def validate_together(rrule, session_mode, execution_target):
        await validate(rrule, session_mode, execution_target)
        await validated.wait()
        if (rrule == recurrence_patch["rrule"]) != recurrence_first:
            await first_saved.wait()

    async def patch(fields, first):
        try:
            return await auth_client.patch(url, json=fields, headers=_headers())
        finally:
            if first:
                first_saved.set()

    with monkeypatch.context() as patcher:
        patcher.setattr(scheduled_tasks_routes, "_validate_rrule_or_400", validate_together)
        async with asyncio.timeout(10):
            recurrence_response, policy_response = await asyncio.gather(
                patch(recurrence_patch, recurrence_first),
                patch(policy_patch, not recurrence_first),
            )
    winner, loser = (
        (recurrence_response, policy_response)
        if recurrence_first
        else (policy_response, recurrence_response)
    )
    assert winner.status_code == 200, winner.text
    assert loser.status_code == 409, loser.text
    assert "Reload and try again" in loser.text
    saved = (await auth_client.get(url, headers=_headers())).json()
    assert (saved["rrule"], saved["session_mode"], saved["execution_target"]) == (
        winner.json()["rrule"],
        winner.json()["session_mode"],
        winner.json()["execution_target"],
    )
    assert saved["state"] == "active"
    retry = await auth_client.patch(
        url, json=policy_patch if recurrence_first else recurrence_patch, headers=_headers()
    )
    assert retry.status_code == 400, retry.text
    assert "Minimum interval is 60 minutes" in retry.text
