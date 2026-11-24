"""Only a shared session's owner changes its pull requests through the host's GitHub login.

Each change runs as the host's ``gh`` account, which is the owner's. These tests drive the
real routes with the runner offline and the session's host connected, so a change that got
past the owner check would reach the recorded host op.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from omnigent.errors import OmnigentError
from omnigent.runtime import set_runner_router
from omnigent.server import session_pull_requests as server_prs
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, LEVEL_READ, UnifiedAuthProvider
from omnigent.server.pull_request_viewers import OWNER_ONLY_CHANGES_MESSAGE
from omnigent.server.routes import _host_filesystem
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.routes.test_session_resources import (
    _FakeRunnerRouter,
    _OfflineRunnerClient,
    _pr_info,
)
from tests.server.routes.test_session_resources import (
    runner_globals_reset as runner_globals_reset,
)
from tests.server.routes.test_shell_permission_gate import _StubPermissionStore

_ROOT = "79b22ebd2309e48fdeb450c65611d51b"
_CHILD = "5d29bee4350489d66feafecfebd94a97"
_PR = "https://github.com/example/one/pull/42"
_HOST = "f54bb9272002938a3a934bfcb6bb228a"
_WORKSPACE = "/workspace/project"

_OWNER = "owner@example.com"
_EDITOR = "editor@example.com"
_VIEWER = "viewer@example.com"
# The admin flag with a read grant, with no grant at all, and with the owner grant.
_ADMIN = "admin@example.com"
_ADMIN_NO_GRANT = "admin-no-grant@example.com"
_ADMIN_OWNER = "admin-owner@example.com"
# An admin who owns the sub-agent session but not its root.
_ADMIN_CHILD_OWNER = "admin-child-owner@example.com"

# Every route that changes a pull request, or only feeds a control that does.
_OWNER_ONLY_ROUTES: list[tuple[str, str, dict[str, Any]]] = [
    ("DELETE", "/pull-requests", {"params": {"url": _PR}}),
    ("POST", "/resources/github/prs", {"json": {"url": _PR, "action": "attach"}}),
    ("POST", "/resources/github/preferences", {"json": {"account": "", "pr_url": _PR}}),
    ("POST", "/resources/github/preferences", {"json": {"remote": "upstream"}}),
    ("GET", "/resources/github/labels", {"params": {"pr_url": _PR}}),
    ("POST", "/resources/github/labels", {"json": {"pr_url": _PR, "add": ["bug"]}}),
    ("GET", "/resources/github/merge-options", {"params": {"pr_url": _PR}}),
    ("POST", "/resources/github/merge", {"json": {"pr_url": _PR, "merge_method": "squash"}}),
    (
        "POST",
        "/resources/github/merge-as",
        {"json": {"pr_url": _PR, "merge_method": "squash", "account": "other"}},
    ),
    ("POST", "/resources/github/disable-auto-merge", {"json": {"pr_url": _PR}}),
    ("GET", "/resources/github/reviewers", {"params": {"pr_url": _PR}}),
    (
        "POST",
        "/resources/github/reviewers",
        {"json": {"pr_url": _PR, "add": [{"login": "octocat", "kind": "user"}]}},
    ),
    (
        "POST",
        "/resources/github/review-threads",
        {"json": {"pr_url": _PR, "body": "Nit", "anchor": {"line": 2}}},
    ),
    (
        "POST",
        "/resources/github/review-threads/thread-1/replies",
        {"json": {"pr_url": _PR, "body": "Done"}},
    ),
    ("POST", "/resources/github/comments", {"json": {"pr_url": _PR, "body": "Hello"}}),
    ("POST", "/resources/github/details", {"json": {"pr_url": _PR, "title": "New title"}}),
    (
        "POST",
        "/resources/github/attachments",
        {"data": {"pr_url": _PR}, "files": {"file": ("shot.png", b"\x89PNG", "image/png")}},
    ),
]
# Exercised with a real orchestration source in the session-orchestration integration tests.
_ORCHESTRATED_ROUTE = ("POST", "/resources/github/prs/orchestrated")
_READ_ROUTES = {
    ("GET", "/pull-requests"),
    ("GET", "/resources/github"),
    ("GET", "/resources/github/changes"),
    ("GET", "/resources/github/commits"),
    ("GET", "/resources/github/diff"),
    ("GET", "/resources/github/commit-diff"),
    ("GET", "/resources/github/compare-diff"),
    ("GET", "/resources/github/diff/{relative_path:path}"),
    ("GET", "/resources/github/review-threads"),
    ("GET", "/resources/github/review-threads/{thread_id}/comments"),
}
_ROUTE_IDS = [f"{method} {path} {sorted(options)}" for method, path, options in _OWNER_ONLY_ROUTES]


def _permissions() -> _StubPermissionStore:
    permissions = _StubPermissionStore()
    permissions.add_grant(_OWNER, _ROOT, LEVEL_OWNER)
    permissions.add_grant(_EDITOR, _ROOT, LEVEL_EDIT)
    permissions.add_grant(_VIEWER, _ROOT, LEVEL_READ)
    permissions.add_grant(_ADMIN, _ROOT, LEVEL_READ)
    permissions.add_grant(_ADMIN_OWNER, _ROOT, LEVEL_OWNER)
    permissions.add_grant(_ADMIN_CHILD_OWNER, _CHILD, LEVEL_OWNER)
    for admin in (_ADMIN, _ADMIN_NO_GRANT, _ADMIN_OWNER, _ADMIN_CHILD_OWNER):
        permissions.add_admin(admin)
    return permissions


class _RecordingHost:
    """The session's connected host; records every op the server sends it."""

    def __init__(self) -> None:
        self.reads: list[str] = []
        self.writes: list[str] = []

    async def read(self, *, op: str, session_id: str, **_kwargs: Any) -> dict[str, Any]:
        self.reads.append(f"{session_id}:{op}")
        return {"object": "session.github.info", "available": True}

    async def write(self, *, op: str, session_id: str, **_kwargs: Any) -> dict[str, Any]:
        self.writes.append(f"{session_id}:{op}")
        return {"object": "session.github.info", "available": True}

    @property
    def calls(self) -> list[str]:
        return self.reads + self.writes


class _ConnectedHosts:
    """A host registry in which the session's host is connected to this replica."""

    def get(self, host_id: str) -> object | None:
        return object() if host_id == _HOST else None


@pytest.fixture
def host(runner_globals_reset: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[_RecordingHost]:
    del runner_globals_reset
    recording = _RecordingHost()
    monkeypatch.setattr(_host_filesystem, "read_workspace_from_host", recording.read)
    monkeypatch.setattr(_host_filesystem, "write_workspace_from_host", recording.write)
    set_runner_router(_FakeRunnerRouter(_OfflineRunnerClient()))  # type: ignore[arg-type]
    yield recording


def _conversations(db_uri: str) -> SqlAlchemyConversationStore:
    conversations = SqlAlchemyConversationStore(db_uri)
    conversations.create_conversation(conversation_id=_ROOT, host_id=_HOST, workspace=_WORKSPACE)
    conversations.create_conversation(
        conversation_id=_CHILD,
        kind="sub_agent",
        parent_conversation_id=_ROOT,
        host_id=_HOST,
        workspace=_WORKSPACE,
    )
    pull_requests = server_prs.open_store(conversations)
    assert pull_requests is not None
    for session_id in (_ROOT, _CHILD):
        server_prs.ingest_info(pull_requests, session_id, _pr_info(), requested_at=1.0)
    return conversations


def _app(db_uri: str, *, auth: bool = True) -> FastAPI:
    application = FastAPI()

    @application.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    application.include_router(
        create_sessions_router(
            _conversations(db_uri),
            object(),  # type: ignore[arg-type]  # No agents are loaded on these routes.
            host_registry=_ConnectedHosts(),  # type: ignore[arg-type]
            **(
                {
                    "auth_provider": UnifiedAuthProvider(source="header", local_single_user=False),
                    "permission_store": _permissions(),
                }
                if auth
                else {}
            ),
        ),
        prefix="/v1",
    )
    return application


@pytest.fixture
async def client(db_uri: str, host: _RecordingHost) -> AsyncIterator[httpx.AsyncClient]:
    del host
    transport = httpx.ASGITransport(app=_app(db_uri))
    async with httpx.AsyncClient(transport=transport, base_url="http://server") as session:
        yield session


async def _call(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    options: dict[str, Any],
    *,
    caller: str | None,
    session_id: str = _ROOT,
) -> httpx.Response:
    headers = {"X-Forwarded-Email": caller} if caller else {}
    return await client.request(
        method, f"/v1/sessions/{session_id}{path}", headers=headers, **options
    )


def _linked_urls(db_uri: str, session_id: str = _ROOT) -> list[str]:
    pull_requests = server_prs.open_store(SqlAlchemyConversationStore(db_uri))
    assert pull_requests is not None
    return [entry.url for entry in pull_requests.list_page(session_id, limit=100).entries]


def test_every_pull_request_route_is_owner_only_or_a_listed_read(db_uri: str) -> None:
    """A new route under these paths fails here until it is classified."""
    prefix = "/v1/sessions/{session_id}"
    registered = {
        (method, route.path.removeprefix(prefix))
        for route in _app(db_uri).routes
        if isinstance(route, APIRoute)
        and route.path.startswith((f"{prefix}/resources/github", f"{prefix}/pull-requests"))
        for method in route.methods
        if method != "HEAD"
    }
    owner_only = {
        (method, path.replace("thread-1", "{thread_id}")) for method, path, _ in _OWNER_ONLY_ROUTES
    }
    owner_only.add(_ORCHESTRATED_ROUTE)

    assert owner_only.isdisjoint(_READ_ROUTES)
    assert registered == owner_only | _READ_ROUTES
    assert all(method == "GET" for method, _ in _READ_ROUTES)


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "options"), _OWNER_ONLY_ROUTES, ids=_ROUTE_IDS)
@pytest.mark.parametrize("caller", [_EDITOR, _ADMIN, _ADMIN_NO_GRANT])
async def test_a_change_from_anyone_but_the_owner_is_refused_before_the_host(
    client: httpx.AsyncClient,
    host: _RecordingHost,
    db_uri: str,
    method: str,
    path: str,
    options: dict[str, Any],
    caller: str,
) -> None:
    response = await _call(client, method, path, options, caller=caller)

    assert response.status_code == 403, response.text
    assert response.json()["error"]["message"] == OWNER_ONLY_CHANGES_MESSAGE
    assert host.calls == []
    assert _linked_urls(db_uri) == [_PR, "https://github.com/example/two/pull/7"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "options"), _OWNER_ONLY_ROUTES, ids=_ROUTE_IDS)
async def test_a_viewer_keeps_the_read_only_refusal(
    client: httpx.AsyncClient,
    host: _RecordingHost,
    method: str,
    path: str,
    options: dict[str, Any],
) -> None:
    response = await _call(client, method, path, options, caller=_VIEWER)

    assert response.status_code == 403, response.text
    assert response.json()["error"]["message"] != OWNER_ONLY_CHANGES_MESSAGE
    assert host.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "options"), _OWNER_ONLY_ROUTES, ids=_ROUTE_IDS)
@pytest.mark.parametrize("caller", [_OWNER, _ADMIN_OWNER])
async def test_the_owner_s_change_reaches_the_host(
    client: httpx.AsyncClient,
    host: _RecordingHost,
    method: str,
    path: str,
    options: dict[str, Any],
    caller: str,
) -> None:
    response = await _call(client, method, path, options, caller=caller)

    assert response.status_code == 200, response.text
    if path == "/pull-requests":
        # The stored unlink stands on its own; the host is told when it can be.
        assert host.writes == [f"{_ROOT}:github_prs_update"]
    else:
        assert len(host.calls) == 1, host.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "options"), _OWNER_ONLY_ROUTES, ids=_ROUTE_IDS)
async def test_with_permissions_off_everyone_is_the_owner(
    db_uri: str, host: _RecordingHost, method: str, path: str, options: dict[str, Any]
) -> None:
    transport = httpx.ASGITransport(app=_app(db_uri, auth=False))
    async with httpx.AsyncClient(transport=transport, base_url="http://server") as client:
        response = await _call(client, method, path, options, caller=None)

    assert response.status_code == 200, response.text
    assert host.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caller", "expected"),
    [(_OWNER, 200), (_EDITOR, 403), (_ADMIN, 403), (_ADMIN_CHILD_OWNER, 403)],
)
async def test_a_sub_agent_s_pull_requests_belong_to_its_root_s_owner(
    client: httpx.AsyncClient, host: _RecordingHost, caller: str, expected: int
) -> None:
    """The owner grant on the sub-agent session itself doesn't count; its root's does."""
    response = await _call(
        client,
        "POST",
        "/resources/github/comments",
        {"json": {"pr_url": _PR, "body": "Hello"}},
        caller=caller,
        session_id=_CHILD,
    )

    assert response.status_code == expected, response.text
    if expected == 403:
        assert response.json()["error"]["message"] == OWNER_ONLY_CHANGES_MESSAGE
        assert host.calls == []
    else:
        assert host.writes == [f"{_CHILD}:github_comment_create"]


@pytest.mark.asyncio
async def test_a_refused_link_never_checks_the_session_s_room(
    client: httpx.AsyncClient, host: _RecordingHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.stores.session_pull_request_store import SessionPullRequestStore

    checked: list[str] = []
    monkeypatch.setattr(
        SessionPullRequestStore,
        "check_room",
        lambda _self, session_id, url: checked.append(f"{session_id}:{url}"),
    )
    other = "https://github.com/example/three/pull/9"

    refused = await _call(
        client,
        "POST",
        "/resources/github/prs",
        {"json": {"url": other, "action": "attach"}},
        caller=_EDITOR,
    )
    assert refused.status_code == 403, refused.text
    assert checked == []

    allowed = await _call(
        client,
        "POST",
        "/resources/github/prs",
        {"json": {"url": other, "action": "attach"}},
        caller=_OWNER,
    )
    assert allowed.status_code == 200, allowed.text
    assert checked == [f"{_ROOT}:{other}"]
    assert host.writes == [f"{_ROOT}:github_prs_update"]
