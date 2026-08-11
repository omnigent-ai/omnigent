"""Permission tests for ``POST /v1/sessions/{id}/promote``."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.testclient import TestClient

from omnigent.db.utils import generate_agent_id
from omnigent.errors import OmnigentError
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, UnifiedAuthProvider
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


def _app(db_uri: str) -> tuple[FastAPI, SqlAlchemyConversationStore, SqlAlchemyPermissionStore]:
    """Build an authenticated sessions router over the per-test database."""
    conversations = SqlAlchemyConversationStore(db_uri)
    agents = SqlAlchemyAgentStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    router = create_sessions_router(
        conversation_store=conversations,
        agent_store=agents,
        auth_provider=UnifiedAuthProvider(source="header"),
        permission_store=permissions,
    )
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_error(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(router, prefix="/v1")
    return app, conversations, permissions


def _seed_tree(
    db_uri: str,
    conversations: SqlAlchemyConversationStore,
) -> tuple[str, str]:
    """Create a bound A -> B tree and return both conversation ids."""
    agents = SqlAlchemyAgentStore(db_uri)
    agent = agents.create(generate_agent_id(), "promote-agent", "bundle/promote")
    parent = conversations.create_conversation(agent_id=agent.id)
    child = conversations.create_conversation(
        kind="sub_agent",
        parent_conversation_id=parent.id,
        agent_id=agent.id,
        sub_agent_name="reviewer",
    )
    return parent.id, child.id


def test_promote_requires_parent_owner_and_grants_direct_owner(db_uri: str) -> None:
    """Inherited owner access becomes a direct grant before B is detached."""
    app, conversations, permissions = _app(db_uri)
    parent_id, child_id = _seed_tree(db_uri, conversations)
    owner = "owner@promote.test"
    permissions.ensure_user(owner)
    permissions.grant(owner, parent_id, LEVEL_OWNER)

    response = TestClient(app).post(
        f"/v1/sessions/{child_id}/promote",
        headers={"X-Forwarded-Email": owner},
    )

    assert response.status_code == 200, response.text
    assert permissions.get_permission_level(owner, child_id) == LEVEL_OWNER
    assert response.json()["permission_level"] == LEVEL_OWNER


def test_promote_rejects_non_owner_without_mutation(db_uri: str) -> None:
    """Read access to A is insufficient to detach B."""
    app, conversations, permissions = _app(db_uri)
    parent_id, child_id = _seed_tree(db_uri, conversations)
    reader = "reader@promote.test"
    permissions.ensure_user(reader)
    permissions.grant(reader, parent_id, LEVEL_READ)

    response = TestClient(app).post(
        f"/v1/sessions/{child_id}/promote",
        headers={"X-Forwarded-Email": reader},
    )

    assert response.status_code == 403
    child = conversations.get_conversation(child_id)
    assert child is not None
    assert child.parent_conversation_id == parent_id
    assert permissions.get_permission_level(reader, child_id) is None
