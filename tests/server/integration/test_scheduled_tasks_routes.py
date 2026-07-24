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
    # The free-text error blob is not exposed on the run list.
    assert "error" not in newest


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
        # The relay publishes "running" as the turn starts, then "idle" at the
        # terminal (completed) edge. Drive the terminal edge.
        _publish_status(conv_id, "idle")
        row = _wait_for_run_status(db_uri, task_id, run_id, "succeeded")
    finally:
        session_live_state.configure(None)
        _session_status_cache.pop(conv_id, None)

    assert row is not None
    assert row.status == "succeeded"
    assert row.finished_at is not None
    assert row.error_code is None


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
