"""Tests for the schedules REST routes (single-user mode)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.server.routes.schedules import create_schedules_router
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
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
