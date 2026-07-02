"""Tests for the schedules REST routes (single-user mode)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.routes.schedules import create_schedules_router
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.schedule_store.sqlalchemy_store import SqlAlchemyScheduleStore


@pytest.fixture()
def ctx(db_uri: str) -> tuple[TestClient, str]:
    store = SqlAlchemyScheduleStore(db_uri)
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(title="c").id
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": exc.code})

    app.include_router(create_schedules_router(store), prefix="/v1")
    return TestClient(app), conv


def test_create_list_patch_delete(ctx: tuple[TestClient, str]) -> None:
    client, conv = ctx
    created = client.post(
        "/v1/schedules",
        json={
            "conversation_id": conv,
            "name": "weekly",
            "kind": "loop",
            "prompt": "report",
            "cron": "0 22 * * FRI",
        },
    )
    assert created.status_code == 200
    sid = created.json()["id"]
    assert created.json()["kind"] == "loop"

    listed = client.get("/v1/schedules", params={"conversation_id": conv}).json()
    assert [s["id"] for s in listed["data"]] == [sid]

    patched = client.patch(f"/v1/schedules/{sid}", json={"enabled": False}).json()
    assert patched["enabled"] is False

    assert client.delete(f"/v1/schedules/{sid}").json() == {"deleted": True}
    assert client.get("/v1/schedules", params={"conversation_id": conv}).json()["data"] == []


def test_validation(ctx: tuple[TestClient, str]) -> None:
    client, conv = ctx
    # loop without cron
    bad_loop = client.post(
        "/v1/schedules",
        json={"conversation_id": conv, "name": "l", "kind": "loop", "prompt": "p"},
    )
    assert bad_loop.status_code == 400
    # monitor without command
    bad_mon = client.post(
        "/v1/schedules",
        json={"conversation_id": conv, "name": "m", "kind": "monitor", "prompt": "p"},
    )
    assert bad_mon.status_code == 400
    # bad kind
    bad_kind = client.post(
        "/v1/schedules",
        json={"conversation_id": conv, "name": "x", "kind": "cron", "prompt": "p"},
    )
    assert bad_kind.status_code == 400
    # patch unknown
    assert client.patch("/v1/schedules/sch_nope", json={"enabled": True}).status_code == 404


class _FakeAgent:
    id = "agt_1"


class _FakeAgentStore:
    """Resolves only 'reporter' — stands in for the registered-agent lookup."""

    def get_by_name(self, name: str) -> _FakeAgent | None:
        return _FakeAgent() if name == "reporter" else None


def test_create_global_loop_and_list_all(
    ctx: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, conv = ctx
    monkeypatch.setattr("omnigent.runtime.get_agent_store", lambda: _FakeAgentStore())

    client.post(
        "/v1/schedules",
        json={
            "conversation_id": conv,
            "name": "conv-loop",
            "kind": "loop",
            "prompt": "p",
            "cron": "* * * * *",
        },
    ).raise_for_status()
    glob = client.post(
        "/v1/schedules",
        json={
            "agent_name": "reporter",
            "name": "nightly",
            "kind": "loop",
            "prompt": "p",
            "cron": "0 0 * * *",
        },
    )
    assert glob.status_code == 200
    assert glob.json()["agent_name"] == "reporter"
    assert glob.json()["conversation_id"] is None

    # Global list (no conversation_id) → both; scoped → only the conversation loop.
    all_names = {s["name"] for s in client.get("/v1/schedules").json()["data"]}
    assert all_names == {"conv-loop", "nightly"}
    scoped = client.get("/v1/schedules", params={"conversation_id": conv}).json()["data"]
    assert {s["name"] for s in scoped} == {"conv-loop"}


def test_scoping_validation(ctx: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch) -> None:
    client, conv = ctx
    monkeypatch.setattr("omnigent.runtime.get_agent_store", lambda: _FakeAgentStore())

    def _post(extra: dict[str, object]) -> int:
        base = {"name": "x", "kind": "loop", "prompt": "p", "cron": "* * * * *"}
        return client.post("/v1/schedules", json={**base, **extra}).status_code

    # Neither conversation_id nor agent_name → 400.
    assert _post({}) == 400
    # Both set → 400.
    assert _post({"conversation_id": conv, "agent_name": "reporter"}) == 400
    # Unknown agent → 400.
    assert _post({"agent_name": "ghost"}) == 400
    # agent_name on a monitor → 400 (global is loop-only).
    assert (
        client.post(
            "/v1/schedules",
            json={
                "agent_name": "reporter",
                "name": "m",
                "kind": "monitor",
                "prompt": "p",
                "command": "tail -f x",
            },
        ).status_code
        == 400
    )


def test_duplicate_name_conflicts(ctx: tuple[TestClient, str]) -> None:
    client, conv = ctx
    payload = {
        "conversation_id": conv,
        "name": "dupe",
        "kind": "monitor",
        "prompt": "look {line}",
        "command": "tail -f x",
    }
    assert client.post("/v1/schedules", json=payload).status_code == 200
    assert client.post("/v1/schedules", json=payload).status_code == 409


def test_invalid_cron_rejected(ctx: tuple[TestClient, str]) -> None:
    # A malformed cron would persist as an enabled-looking loop that silently
    # disarms at its first fire — reject it at create time.
    client, conv = ctx
    resp = client.post(
        "/v1/schedules",
        json={
            "conversation_id": conv,
            "name": "bad",
            "kind": "loop",
            "prompt": "p",
            "cron": "not a cron",
        },
    )
    assert resp.status_code == 400


def test_global_loop_rate_floor(
    ctx: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A global loop spawns a fresh conversation per fire, so an over-frequent
    # cron would flood the workspace — floor the fire rate (a conversation-scoped
    # loop, which fires in place, is not floored).
    client, _ = ctx
    monkeypatch.setattr("omnigent.runtime.get_agent_store", lambda: _FakeAgentStore())

    too_fast = client.post(
        "/v1/schedules",
        json={
            "agent_name": "reporter",
            "name": "spam",
            "kind": "loop",
            "prompt": "p",
            "cron": "* * * * *",  # every minute → below the floor
        },
    )
    assert too_fast.status_code == 400

    ok = client.post(
        "/v1/schedules",
        json={
            "agent_name": "reporter",
            "name": "hourly",
            "kind": "loop",
            "prompt": "p",
            "cron": "0 * * * *",  # comfortably above the floor
        },
    )
    assert ok.status_code == 200


def test_owner_scoping_multi_user(db_uri: str) -> None:
    # Multi-user: a caller sees / can mutate only their OWN schedules. Another
    # user's schedule is invisible in the list and 404s on patch/delete, so its
    # very existence isn't leaked.
    store = SqlAlchemyScheduleStore(db_uri)
    perm = SqlAlchemyPermissionStore(db_uri)
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(title="c").id
    for user in ("alice@example.com", "bob@example.com"):
        perm.ensure_user(user)

    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": exc.code})

    app.include_router(
        create_schedules_router(
            store,
            auth_provider=UnifiedAuthProvider(source="header", local_single_user=True),
            permission_store=perm,
        ),
        prefix="/v1",
    )
    client = TestClient(app)
    alice = {"X-Forwarded-Email": "alice@example.com"}
    bob = {"X-Forwarded-Email": "bob@example.com"}

    created = client.post(
        "/v1/schedules",
        json={
            "conversation_id": conv,
            "name": "a-loop",
            "kind": "loop",
            "prompt": "p",
            "cron": "0 0 * * *",
        },
        headers=alice,
    )
    assert created.status_code == 200
    sid = created.json()["id"]

    # Bob can't see it, and can't patch/delete it (404 hides existence).
    assert client.get("/v1/schedules", headers=bob).json()["data"] == []
    assert (
        client.patch(f"/v1/schedules/{sid}", json={"enabled": False}, headers=bob).status_code
        == 404
    )
    assert client.delete(f"/v1/schedules/{sid}", headers=bob).status_code == 404

    # Alice owns it: she sees it and can delete it.
    assert [s["id"] for s in client.get("/v1/schedules", headers=alice).json()["data"]] == [sid]
    assert client.delete(f"/v1/schedules/{sid}", headers=alice).status_code == 200
