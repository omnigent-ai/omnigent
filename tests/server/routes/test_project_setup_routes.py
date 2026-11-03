"""Project setup routes run code on, and read logs from, the owner's machine."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, LEVEL_READ, UnifiedAuthProvider
from tests.server.routes.test_session_resources import (
    _ConversationStore,
    _sessions_app,
    runner_globals_reset,  # noqa: F401 — reuse runner-state isolation
)
from tests.server.routes.test_shell_permission_gate import _StubPermissionStore

_SESSION_ID = "64a784c3aa907d1774f44313546947c6"


@pytest.fixture
async def client(
    runner_globals_reset: None,  # noqa: F811 — imported fixture
) -> AsyncIterator[httpx.AsyncClient]:
    del runner_globals_reset
    permissions = _StubPermissionStore()
    for user, level in (("viewer", LEVEL_READ), ("editor", LEVEL_EDIT), ("owner", LEVEL_OWNER)):
        permissions.add_grant(f"{user}@example.com", _SESSION_ID, level)
    app = _sessions_app(
        _ConversationStore(),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        permission_store=permissions,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://server"
    ) as http_client:
        yield http_client


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "project-setup/rerun"), ("GET", "project-setup/log")],
    ids=["rerun", "log"],
)
@pytest.mark.parametrize(
    ("caller", "expected_status"),
    # The owner passes the access check and reaches "this session has no host".
    [("viewer", 403), ("editor", 403), ("owner", 409)],
)
async def test_project_setup_routes_are_owner_only(
    client: httpx.AsyncClient, method: str, path: str, caller: str, expected_status: int
) -> None:
    response = await client.request(
        method,
        f"/v1/sessions/{_SESSION_ID}/{path}",
        headers={"X-Forwarded-Email": f"{caller}@example.com"},
    )

    assert response.status_code == expected_status, response.text
