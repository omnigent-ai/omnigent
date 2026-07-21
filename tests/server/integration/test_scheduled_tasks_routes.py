"""Integration tests for the scheduled-tasks CRUD routes.

Uses a real ``SqlAlchemyScheduledTaskStore`` + ``SqlAlchemyPermissionStore`` so
the full request → store → response pipeline is exercised, including RRULE
validation (400s) and live-scheduler sync on every mutation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.db.utils import builtin_agent_id
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import app as server_app
from omnigent.server.app import create_app
from omnigent.server.routes import scheduled_tasks as scheduled_tasks_routes
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import (
    SqlAlchemyScheduledTaskStore,
)
from tests.server.conftest import ControllableMockClient

pytestmark = pytest.mark.asyncio


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


@pytest.fixture()
def auth_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
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
        auth_provider=UnifiedAuthProvider(source="header"),
    )


def _register_host(app: FastAPI, host_id: str, owner: str) -> None:
    """Persist a host owned by ``owner`` so the pinned-host owner check resolves.

    A local store row is all the create-time authorization needs — it never
    contacts the host (no ``host.stat`` / workspace RPC in the no-workspace
    path), so the host does not need to be online in the registry.
    """
    app.state.host_store.upsert_on_connect(host_id, f"{owner}-laptop", owner)


@pytest_asyncio.fixture()
async def auth_client(
    auth_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)

    # Enter the lifespan so app.state.scheduled_task_scheduler exists and the
    # routes can sync to it.
    async with auth_app.router.lifespan_context(auth_app):
        transport = httpx.ASGITransport(app=auth_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


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
        "agent_id": builtin_agent_id(server_app._CLAUDE_NATIVE_AGENT_NAME),
        "timezone": "America/Los_Angeles",
        "workspace": "/repo",
        "host_id": "4b653f6031f35d168cc0b37caa1306d1",
    }
    body.update(overrides)
    return body


async def test_create_lists_and_gets(auth_client: httpx.AsyncClient, db_uri: str) -> None:
    _make_user(db_uri)
    resp = await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert created["name"] == "nightly triage"
    assert created["rrule"] == _VALID_RRULE
    assert created["owner_user_id"] == "alice@example.com"
    assert created["workspace"] == "/repo"
    assert created["host_id"] == "4b653f6031f35d168cc0b37caa1306d1"
    assert "base_branch" not in created
    assert "execution_target" not in created
    task_id = created["id"]

    listed = await auth_client.get("/v1/scheduled-tasks", headers=_headers())
    assert listed.status_code == 200
    ids = [t["id"] for t in listed.json()["scheduled_tasks"]]
    assert task_id in ids

    got = await auth_client.get(f"/v1/scheduled-tasks/{task_id}", headers=_headers())
    assert got.status_code == 200
    assert got.json()["id"] == task_id


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


async def test_create_rejects_invalid_rrule(auth_client: httpx.AsyncClient, db_uri: str) -> None:
    _make_user(db_uri)
    # FREQ=SECONDLY fires far below the 1-hour floor.
    resp = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(rrule="FREQ=SECONDLY"),
        headers=_headers(),
    )
    assert resp.status_code == 400, resp.text


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
    fails at fire time. No host was registered, so the owner check 404s."""
    _make_user(db_uri)
    body = _create_body()
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
    _register_host(auth_app, "4b653f6031f35d168cc0b37caa1306d1", "bob@example.com")
    body = _create_body()
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


async def test_delete_removes_task(auth_client: httpx.AsyncClient, db_uri: str) -> None:
    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    task_id = created["id"]

    deleted = await auth_client.delete(f"/v1/scheduled-tasks/{task_id}", headers=_headers())
    assert deleted.status_code == 200, deleted.text

    got = await auth_client.get(f"/v1/scheduled-tasks/{task_id}", headers=_headers())
    assert got.status_code == 404


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
