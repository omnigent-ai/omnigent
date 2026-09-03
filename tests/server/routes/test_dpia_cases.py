from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from starlette.requests import HTTPConnection

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import AuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.dpia_case_store.sqlalchemy_store import SqlAlchemyDpiaCaseStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


class _HeaderAuth(AuthProvider):
    def get_user_id(self, request: HTTPConnection) -> str | None:
        return request.headers.get("x-test-user")


@pytest.fixture()
def dpia_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    permission_store = SqlAlchemyPermissionStore(db_uri)
    permission_store.ensure_user("officer@example.com", is_admin=True)
    permission_store.ensure_user("member@example.com", is_admin=False)
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        auth_provider=_HeaderAuth(),
        permission_store=permission_store,
        dpia_case_store=SqlAlchemyDpiaCaseStore(db_uri),
    )


@pytest_asyncio.fixture()
async def client(dpia_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=dpia_app),
        base_url="http://test",
    ) as value:
        yield value


def _body(value: str, revision: int) -> dict[str, object]:
    return {
        "expected_revision": revision,
        "snapshot": {
            "id": "student-success-alert",
            "title": "Student Success Alert",
            "value": value,
            "processingModel": {"caseId": "student-success-alert", "version": 1},
            "audit": [],
        },
    }


@pytest.mark.asyncio
async def test_dpia_cases_require_authentication(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/dpia/cases")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_dpia_case_writes_require_admin_authority(client: httpx.AsyncClient) -> None:
    response = await client.put(
        "/v1/dpia/cases/student-success-alert",
        headers={"x-test-user": "member@example.com"},
        json=_body("unauthorized", 0),
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_dpia_case_api_versions_snapshots_and_rejects_stale_writes(
    client: httpx.AsyncClient,
) -> None:
    headers = {"x-test-user": "officer@example.com"}
    created = await client.put(
        "/v1/dpia/cases/student-success-alert",
        headers=headers,
        json=_body("initial", 0),
    )
    updated = await client.put(
        "/v1/dpia/cases/student-success-alert",
        headers=headers,
        json=_body("updated", 1),
    )
    conflict = await client.put(
        "/v1/dpia/cases/student-success-alert",
        headers=headers,
        json=_body("stale", 1),
    )
    current = await client.get(
        "/v1/dpia/cases/student-success-alert",
        headers=headers,
    )
    revisions = await client.get(
        "/v1/dpia/cases/student-success-alert/revisions",
        headers=headers,
    )
    first = await client.get(
        "/v1/dpia/cases/student-success-alert/revisions/1",
        headers=headers,
    )

    assert created.status_code == 200
    assert created.json()["revision"] == 1
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    assert conflict.status_code == 409
    assert conflict.json()["error"]["current_revision"] == 2
    assert current.json()["snapshot"]["value"] == "updated"
    assert [item["revision"] for item in revisions.json()["revisions"]] == [1, 2]
    assert first.json()["snapshot"]["value"] == "initial"


@pytest.mark.asyncio
async def test_dpia_case_api_rejects_oversized_snapshot(client: httpx.AsyncClient) -> None:
    response = await client.put(
        "/v1/dpia/cases/student-success-alert",
        headers={"x-test-user": "officer@example.com"},
        json=_body("x" * (2 * 1024 * 1024), 0),
    )

    assert response.status_code == 400
    assert "2 MiB" in response.json()["error"]["message"]
