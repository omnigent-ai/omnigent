"""Permission gating for the environment ``/shell`` proxy endpoint.

A shared session's shell runs commands on the runner. When the runner is
not isolated (``sandbox_active: false``), that shell can read files the
session owner can reach — so write-capable shell access must be gated the
same way interactive terminal attach is (see ``test_terminal_attach.py``).

The shell proxy at
``POST /v1/sessions/{id}/resources/environments/{env}/shell`` runs
``_validate_session(required_level=LEVEL_EDIT)`` *before* proxying. These
tests pin that gate end to end at the server boundary:

- a read-only collaborator is rejected with 403 and the request never
  reaches the runner (decisive: the secret-capable shell is unreachable),
- an edit collaborator is allowed through and the command is proxied,
- an unauthenticated caller is rejected.

The deeper gap — an *edit* collaborator on an unsafe runner reading
out-of-root/sensitive files via shell — is pinned by
the strict-xfail matrix in ``test_filesystem_path_isolation_e2e.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.entities import Conversation, ResolvedAccess, SessionPermission
from omnigent.errors import OmnigentError
from omnigent.runtime import _globals, set_runner_client, set_runner_router
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_OWNER,
    LEVEL_READ,
    RESERVED_USER_PUBLIC,
    UnifiedAuthProvider,
)
from omnigent.server.routes.sessions import create_sessions_router

# The server route is mounted under /v1 and proxies to the runner at the
# same path, so the client URL and the recorded runner path are identical.
_SHELL_PATH = "/v1/sessions/conv_share/resources/environments/default/shell"


class _StubConversationStore:
    """In-memory conversation store exposing ``get_conversation``."""

    def __init__(self) -> None:
        self._conversations: dict[str, Conversation] = {}

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self._conversations.get(conversation_id)

    def add(self, conversation_id: str) -> None:
        self._conversations[conversation_id] = Conversation(
            id=conversation_id,
            created_at=0,
            updated_at=0,
            root_conversation_id=conversation_id,
            agent_id="ag_test",
        )


class _StubPermissionStore:
    """In-memory permission store with the methods access checks use."""

    def __init__(self) -> None:
        self._grants: dict[tuple[str, str], SessionPermission] = {}
        self._admins: set[str] = set()

    def get(self, user_id: str, conversation_id: str) -> SessionPermission | None:
        return self._grants.get((user_id, conversation_id))

    def is_admin(self, user_id: str) -> bool:
        return user_id in self._admins

    def add_grant(self, user_id: str, conversation_id: str, level: int) -> None:
        self._grants[(user_id, conversation_id)] = SessionPermission(
            user_id=user_id,
            conversation_id=conversation_id,
            level=level,
        )

    def check_access(self, user_id: str | None, conversation_id: str, required_level: int) -> bool:
        if user_id is None:
            return False
        grant = self.get(user_id, conversation_id)
        if grant is not None and grant.level >= required_level:
            return True
        public_grant = self.get(RESERVED_USER_PUBLIC, conversation_id)
        if public_grant is not None and public_grant.level >= required_level:
            return True
        return False

    def get_permission_level(self, user_id: str | None, conversation_id: str) -> int | None:
        if user_id is None:
            return None
        if self.is_admin(user_id):
            return LEVEL_OWNER
        grant = self.get(user_id, conversation_id)
        if grant is not None:
            return grant.level
        public_grant = self.get(RESERVED_USER_PUBLIC, conversation_id)
        if public_grant is not None:
            return public_grant.level
        return None

    def resolve_access(self, user_id: str | None, conversation_id: str) -> ResolvedAccess:
        # Mirror the real store's single-round-trip resolution: admin flag
        # plus the user's and public grants, with resolution policy
        # (admin bypass, public fallback) left to the server's
        # ``resolved_allows`` / ``resolved_level`` helpers.
        if user_id is None:
            return ResolvedAccess(
                is_admin=False,
                user_grant_level=None,
                public_grant_level=None,
            )
        user_grant = self.get(user_id, conversation_id)
        public_grant = self.get(RESERVED_USER_PUBLIC, conversation_id)
        return ResolvedAccess(
            is_admin=self.is_admin(user_id),
            user_grant_level=user_grant.level if user_grant is not None else None,
            public_grant_level=public_grant.level if public_grant is not None else None,
        )


class _StubAgentStore:
    def get(self, agent_id: str) -> None:
        return None


class _RecordingRunnerClient:
    """Runner client that records POSTs and returns a canned shell result.

    A POST reaching here means the permission gate let the request through —
    the tests assert this list stays empty for rejected callers.
    """

    def __init__(self) -> None:
        self.posts: list[tuple[str, Any]] = []

    async def post(
        self,
        url: str,
        *,
        json: Any = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        del timeout
        self.posts.append((url, json))
        return httpx.Response(
            status_code=200,
            json={
                "object": "session.environment.shell_result",
                "stdout": "ok\n",
                "stderr": "",
                "exit_code": 0,
                "timed_out": False,
                "cwd": "/workspace",
            },
            request=httpx.Request("POST", url),
        )


class _RoutedRunner:
    def __init__(self, client: _RecordingRunnerClient) -> None:
        self.runner_id = "runner_one"
        self.client = client


class _FakeRunnerRouter:
    def __init__(self, client: _RecordingRunnerClient) -> None:
        self.client = client

    def client_for_session_resources(self, session_id: str) -> _RoutedRunner:
        return _RoutedRunner(self.client)


@pytest.fixture
def runner_globals_reset() -> Iterator[None]:
    prior_client = _globals._runner_client
    prior_router = _globals._runner_router
    set_runner_client(None)
    set_runner_router(None)
    yield
    set_runner_client(prior_client)
    set_runner_router(prior_router)


@pytest.fixture
def runner_client() -> _RecordingRunnerClient:
    return _RecordingRunnerClient()


@pytest.fixture
def app(runner_globals_reset: None, runner_client: _RecordingRunnerClient) -> FastAPI:
    del runner_globals_reset
    conv_store = _StubConversationStore()
    conv_store.add("conv_share")
    perm_store = _StubPermissionStore()
    perm_store.add_grant("owner@example.com", "conv_share", LEVEL_EDIT)
    perm_store.add_grant("viewer@example.com", "conv_share", LEVEL_READ)
    set_runner_router(_FakeRunnerRouter(runner_client))  # type: ignore[arg-type]

    application = FastAPI()

    @application.exception_handler(OmnigentError)
    async def _handle_omnigent_error(
        request: Request,
        exc: OmnigentError,
    ) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    application.include_router(
        create_sessions_router(
            conv_store,  # type: ignore[arg-type]
            _StubAgentStore(),  # type: ignore[arg-type]
            # local_single_user=False: this suite verifies the strict
            # (deployed multi-user) posture where a headerless request is
            # rejected, so opt out of the suite-wide single-user default
            # set in tests/conftest.py (OMNIGENT_LOCAL_SINGLE_USER=1).
            auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
            permission_store=perm_store,  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://server") as c:
        yield c


@pytest.mark.asyncio
async def test_shell_rejects_read_only_collaborator_before_runner(
    client: httpx.AsyncClient,
    runner_client: _RecordingRunnerClient,
) -> None:
    """A read-only collaborator cannot run shell, and the runner is never hit."""
    resp = await client.post(
        _SHELL_PATH,
        json={"command": "cat ~/.ssh/id_rsa"},
        headers={"X-Forwarded-Email": "viewer@example.com"},
    )
    assert resp.status_code == 403, resp.text
    # Decisive: the command never reached the runner's (unconfined) shell.
    assert runner_client.posts == []


@pytest.mark.asyncio
async def test_shell_rejects_unauthenticated_before_runner(
    client: httpx.AsyncClient,
    runner_client: _RecordingRunnerClient,
) -> None:
    """Unauthenticated shell exec is rejected.

    Without ``X-Forwarded-Email``, strict header mode fails closed
    with 401 — the request never resolves to a user,
    let alone reaches the permission check or the runner.
    """
    resp = await client.post(_SHELL_PATH, json={"command": "echo hi"})
    assert resp.status_code == 401, resp.text
    assert runner_client.posts == []


@pytest.mark.asyncio
async def test_shell_allows_edit_collaborator_and_proxies(
    client: httpx.AsyncClient,
    runner_client: _RecordingRunnerClient,
) -> None:
    """An edit collaborator is allowed through and the command is proxied."""
    resp = await client.post(
        _SHELL_PATH,
        json={"command": "echo hi"},
        headers={"X-Forwarded-Email": "owner@example.com"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["stdout"] == "ok\n"
    # The edit-level command reached the runner verbatim.
    assert runner_client.posts == [(_SHELL_PATH, {"command": "echo hi"})]
