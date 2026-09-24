"""Default ownership and archive filters through the real session-list route."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, RESERVED_USER_PUBLIC, UnifiedAuthProvider
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture
def session_ids(db_uri: str) -> dict[str, str]:
    agents = SqlAlchemyAgentStore(db_uri)
    agent_id = uuid4().hex
    agents.create(agent_id=agent_id, name="visibility", bundle_location="visibility/bundle")
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    for user in (ALICE, BOB, RESERVED_USER_PUBLIC):
        permissions.ensure_user(user)
    ids = {}
    for scope in ("owned", "shared", "public", "private"):
        for archived in (False, True):
            name = f"{scope}_{'archived' if archived else 'active'}"
            session = conversations.create_conversation(title=name, agent_id=agent_id)
            permissions.grant(ALICE if scope == "owned" else BOB, session.id, LEVEL_OWNER)
            if scope == "shared":
                permissions.grant(ALICE, session.id, LEVEL_READ)
            elif scope == "public":
                permissions.grant(RESERVED_USER_PUBLIC, session.id, LEVEL_READ)
            if archived:
                conversations.update_conversation(session.id, archived=True)
            ids[name] = session.id
    return ids


def _app(db_uri: str, *, authenticated: bool = True) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle_error(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        create_sessions_router(
            conversation_store=SqlAlchemyConversationStore(db_uri),
            agent_store=SqlAlchemyAgentStore(db_uri),
            permission_store=SqlAlchemyPermissionStore(db_uri),
            auth_provider=(
                UnifiedAuthProvider(source="header", local_single_user=False)
                if authenticated
                else None
            ),
        ),
        prefix="/v1",
    )
    return app


@pytest.mark.parametrize("visibility", [None, "mine", "all", "shared", "archived"])
@pytest.mark.parametrize("include_archived", [False, True])
def test_authenticated_visibility_and_archive_filters(
    db_uri: str, session_ids: dict[str, str], visibility: str | None, include_archived: bool
) -> None:
    params = {"include_archived": str(include_archived).lower()}
    if visibility is not None:
        params["visibility"] = visibility
    active = {"owned_active"}
    archived = {"owned_archived"}
    # Public-link access alone does not add a session to a user's list.
    if visibility in {"all", "archived"}:
        active |= {"shared_active"}
        archived |= {"shared_archived"}
    expected = active | archived if include_archived else active
    if visibility == "shared":
        expected = {"shared_active"}
    elif visibility == "archived":
        expected = archived

    with TestClient(_app(db_uri)) as client:
        response = client.get("/v1/sessions", params=params, headers={"X-Forwarded-Email": ALICE})
    assert response.status_code == 200
    assert {row["id"] for row in response.json()["data"]} == {session_ids[key] for key in expected}


@pytest.mark.parametrize("visibility", [None, "mine", "all", "shared", "archived"])
@pytest.mark.parametrize("include_archived", [False, True])
def test_no_auth_preserves_local_listing(
    db_uri: str, session_ids: dict[str, str], visibility: str | None, include_archived: bool
) -> None:
    params = {"include_archived": str(include_archived).lower()}
    if visibility is not None:
        params["visibility"] = visibility
    expected = {
        session_id
        for name, session_id in session_ids.items()
        if (
            name.endswith("archived")
            if visibility == "archived"
            else include_archived or name.endswith("active")
        )
    }
    with TestClient(_app(db_uri, authenticated=False)) as client:
        response = client.get("/v1/sessions", params=params)
    assert response.status_code == 200
    assert {row["id"] for row in response.json()["data"]} == expected


def test_default_pagination_keeps_owned_archives(db_uri: str, session_ids: dict[str, str]) -> None:
    params = {"include_archived": "true", "limit": "1"}
    seen = []
    with TestClient(_app(db_uri)) as client:
        for _ in range(len(session_ids)):
            response = client.get(
                "/v1/sessions", params=params, headers={"X-Forwarded-Email": ALICE}
            )
            assert response.status_code == 200
            page = response.json()
            seen.extend(row["id"] for row in page["data"])
            if not page["has_more"]:
                break
            assert page["last_id"]
            params["after"] = page["last_id"]
        else:
            pytest.fail("session pagination did not terminate")
    assert len(seen) == 2
    assert set(seen) == {session_ids["owned_active"], session_ids["owned_archived"]}


def test_missing_identity_still_rejected(db_uri: str, session_ids: dict[str, str]) -> None:
    with TestClient(_app(db_uri)) as client:
        response = client.get("/v1/sessions")
    assert response.status_code == 401


def test_openapi_declares_mine_default(db_uri: str) -> None:
    parameters = _app(db_uri).openapi()["paths"]["/v1/sessions"]["get"]["parameters"]
    visibility = next(param for param in parameters if param["name"] == "visibility")
    assert visibility["schema"]["default"] == "mine"
