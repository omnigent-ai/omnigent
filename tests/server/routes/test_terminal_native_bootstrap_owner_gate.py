"""Owner gating for the native-bootstrap terminal-create exemption.

``POST /v1/sessions/{id}/resources/terminals`` normally only launches
terminal names the agent spec *declares*. A "native bootstrap" request
(``ensure_native_terminal``/``bridge_inject_dir`` + a registered native
terminal name + ``session_key == "main"``) is exempt from that gate: the
body's ``spec`` (``command``/``args``/``env``) is forwarded verbatim to the
runner, which synthesizes and executes it.

The exemption's only legitimate senders are the local CLI wrappers
(``omnigent claude``/``antigravity``/…), which always run as the session
**owner**. Without an owner check, an ``EDIT`` grantee on a shared session
could send that same body and gain arbitrary command execution on the
owner's runner. These tests pin the gate at the server boundary:

- a non-owner ``EDIT`` collaborator's native-bootstrap body does NOT skip
  the declared-name gate — it is rejected with 400 and the request never
  reaches the runner (decisive: no synthesized command runs), and
- the owner's identical body is admitted and proxied to the runner.
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
    RESERVED_USER_PUBLIC,
    UnifiedAuthProvider,
)
from omnigent.server.routes.sessions import create_sessions_router

_TERMINALS_PATH = "/v1/sessions/conv_share/resources/terminals"

# A native-bootstrap body in the exact shape the ``omnigent claude`` wrapper
# sends: a registered native terminal name, session_key "main", the markers,
# and a ``spec`` carrying the command the runner would synthesize and run.
_ATTACK_BODY: dict[str, Any] = {
    "terminal": "claude",
    "session_key": "main",
    "ensure_native_terminal": True,
    "bridge_inject_dir": True,
    "spec": {
        "command": "sh",
        "args": ["-c", "touch /tmp/pwned"],
        "env": {},
    },
}


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
    """Agent store that resolves no spec, so no terminal name is declared."""

    def get(self, agent_id: str) -> None:
        return None


class _RecordingRunnerClient:
    """Runner client that records POSTs and returns a canned terminal resource.

    A POST reaching here means the create passed the server-side gate — the
    rejection tests assert this list stays empty.
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
                "id": "terminal_claude_main",
                "object": "session.resource",
                "type": "terminal",
                "session_id": "conv_share",
                "name": "claude:main",
                "metadata": {
                    "terminal_name": "claude",
                    "session_key": "main",
                    "running": True,
                },
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
        del session_id
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
    # owner@ owns the session; editor@ is a non-owner collaborator with the
    # EDIT grant that create_session_terminal requires (LEVEL_EDIT).
    perm_store.add_grant("owner@example.com", "conv_share", LEVEL_OWNER)
    perm_store.add_grant("editor@example.com", "conv_share", LEVEL_EDIT)
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
            # Strict (deployed multi-user) posture: a request resolves to a
            # user via X-Forwarded-Email and the permission store gates it.
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
async def test_native_bootstrap_rejected_for_non_owner_editor(
    client: httpx.AsyncClient,
    runner_client: _RecordingRunnerClient,
) -> None:
    """A non-owner EDIT collaborator cannot use the native-bootstrap exemption.

    The editor sends the exact wrapper body (native name + markers + a
    ``spec.command``). Without owner gating this skipped the declared-name
    gate and the runner synthesized+ran the command; with it, the request
    falls through to the declared-name gate and is rejected — the stub agent
    declares no terminals, so ``claude`` is undeclared — and never reaches
    the runner.
    """
    resp = await client.post(
        _TERMINALS_PATH,
        json=_ATTACK_BODY,
        headers={"X-Forwarded-Email": "editor@example.com"},
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_input"
    # Decisive: the attacker-supplied command never reached the runner.
    assert runner_client.posts == []


@pytest.mark.asyncio
async def test_native_bootstrap_allowed_for_owner(
    client: httpx.AsyncClient,
    runner_client: _RecordingRunnerClient,
) -> None:
    """The owner's wrapper body is admitted and proxied to the runner.

    Same body as the rejected editor case — the only difference is the
    caller's grant level — so this proves the gate keys on ownership, not on
    the body shape, and that the legitimate ``omnigent claude`` boot path is
    preserved.
    """
    resp = await client.post(
        _TERMINALS_PATH,
        json=_ATTACK_BODY,
        headers={"X-Forwarded-Email": "owner@example.com"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == "terminal_claude_main"
    # The exemption held for the owner: the body reached the runner verbatim.
    assert runner_client.posts == [(_TERMINALS_PATH, _ATTACK_BODY)]


@pytest.mark.asyncio
async def test_native_bootstrap_rejected_for_read_only_collaborator(
    client: httpx.AsyncClient,
    runner_client: _RecordingRunnerClient,
) -> None:
    """A read-only collaborator is rejected before the exemption is even considered.

    LEVEL_EDIT is required to reach the terminal-create body at all, so a
    viewer (no grant here) is refused by ``_validate_session`` with 403/404
    and never touches the runner. This pins that the owner gate is defense in
    depth *behind* the existing edit gate, not a replacement for it.
    """
    resp = await client.post(
        _TERMINALS_PATH,
        json=_ATTACK_BODY,
        headers={"X-Forwarded-Email": "viewer@example.com"},
    )

    assert resp.status_code in (403, 404), resp.text
    assert runner_client.posts == []
