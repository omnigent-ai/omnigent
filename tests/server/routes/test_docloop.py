"""Notebook registration uses the same session grants as native chat."""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, LEVEL_READ, AuthProvider
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


class Identity(AuthProvider):
    def get_user_id(self, request):
        return request.headers.get("x-test-user")


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "PATCH"])
@pytest.mark.parametrize(
    ("user", "level", "expected"),
    [
        (None, None, 401),
        ("alice", None, 404),
        ("alice", LEVEL_READ, 403),
        ("alice", LEVEL_EDIT, 503),
        ("alice", LEVEL_OWNER, 503),
    ],
)
async def test_auth_and_grants_precede_runner_lookup(db_uri, method, user, level, expected):
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    session = conversations.create_conversation()
    if user and level:
        permissions.ensure_user(user)
        permissions.grant(user, session.id, level)
    lookups = []

    def offline(session_id):
        lookups.append(session_id)
        raise OmnigentError("Runner unavailable", code=ErrorCode.CONFLICT)

    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def error_response(_request, exc):
        status = {ErrorCode.UNAUTHORIZED: 401, ErrorCode.FORBIDDEN: 403, ErrorCode.NOT_FOUND: 404}[
            exc.code
        ]
        return JSONResponse({"error": "Access denied"}, status_code=status)

    app.include_router(
        create_sessions_router(
            conversations,
            None,
            auth_provider=Identity(),
            permission_store=permissions,
            runner_router=SimpleNamespace(client_for_session_resources=offline),
            docloop_notebook_enabled=True,
        ),
        prefix="/v1",
    )
    headers = {"x-test-user": user} if user else {}
    headers["X-Docloop-Edit"] = "1"
    edit = {
        "binding_id": "a" * 64,
        "revision": "b" * 64,
        "change_id": "970bca03-919e-48cf-9c9a-6507d61f4ad4",
        "changes": [],
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://central"
    ) as client:
        response = await client.request(
            method, f"/v1/sessions/{session.id}/docloop/document", headers=headers, json=edit
        )
    assert response.status_code == expected, response.text
    assert lookups == ([session.id] if expected == 503 else [])
