"""Only a shared session's owner changes its pull requests through the host's GitHub login.

Each change runs as the host's ``gh`` account, which is the owner's. These tests drive the
real routes with the runner offline and the session's host connected, so a change that got
past the owner check would reach the recorded host op.
"""

from __future__ import annotations

import copy
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
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_MANAGE,
    LEVEL_OWNER,
    LEVEL_READ,
    UnifiedAuthProvider,
)
from omnigent.server.pull_request_viewers import OWNER_ONLY_CHANGES_MESSAGE, READ_CAPABILITIES
from omnigent.server.read_only_code_hosts import READ_ONLY_CODE_HOST_MESSAGE
from omnigent.server.routes import _host_filesystem
from omnigent.server.routes.sessions import create_sessions_router, routes_resources
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
# The strongest grant short of ownership.
_MANAGER = "manager@example.com"
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
_ACCOUNTS = [
    {"login": "owner-login", "host": "github.com", "active": True},
    {"login": "owner-bot", "host": "github.com", "active": False},
]
_HOST_INFO: dict[str, Any] = {
    "object": "session.github.info",
    "available": True,
    "accounts": _ACCOUNTS,
    "selected_account": "owner-login",
    "auth": {"authenticated": True, "accounts": _ACCOUNTS, "selected_account": "owner-login"},
    "capabilities": {"account_switching": True, "merging": True, "line_counts": True},
    "pr": {"number": 42, "title": "Ship it", "state": "OPEN", "url": _PR},
}
# A comment as the host reads it: its rights are the host account's.
_THREAD_COMMENT: dict[str, Any] = {
    "id": "PRRC_1",
    "database_id": 5,
    "body": "Why this?",
    "reactions": [{"content": "HEART", "count": 2, "viewer_reacted": True}],
    "can_react": True,
    "can_update": True,
    "can_delete": True,
    "viewer_did_author": True,
}
_HOST_THREADS: dict[str, Any] = {
    "data": [
        {
            "id": "PRRT_1",
            "can_reply": True,
            "can_resolve": True,
            "can_unresolve": False,
            "comments": {"data": [_THREAD_COMMENT], "has_more": False},
        }
    ],
    "has_more": False,
}
_HOST_THREAD_COMMENTS: dict[str, Any] = {"data": [_THREAD_COMMENT], "has_more": False}
_HOST_READS: dict[str, dict[str, Any]] = {
    "github_info": _HOST_INFO,
    "github_review_threads": _HOST_THREADS,
    "github_review_thread_comments": _HOST_THREAD_COMMENTS,
}
_ROUTE_IDS = [f"{method} {path} {sorted(options)}" for method, path, options in _OWNER_ONLY_ROUTES]


def _permissions() -> _StubPermissionStore:
    permissions = _StubPermissionStore()
    permissions.add_grant(_OWNER, _ROOT, LEVEL_OWNER)
    permissions.add_grant(_EDITOR, _ROOT, LEVEL_EDIT)
    permissions.add_grant(_MANAGER, _ROOT, LEVEL_MANAGE)
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
        self.read_params: dict[str, dict[str, Any]] = {}

    async def read(self, *, op: str, session_id: str, **kwargs: Any) -> dict[str, Any]:
        self.reads.append(f"{session_id}:{op}")
        self.read_params[op] = kwargs.get("params") or {}
        return copy.deepcopy(_HOST_READS.get(op, {"object": "x"}))

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


def _app(db_uri: str, *, auth: bool = True, read_only_code_hosts: tuple[str, ...] = ()) -> FastAPI:
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
            read_only_code_hosts=read_only_code_hosts,
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
@pytest.mark.parametrize("caller", [_EDITOR, _MANAGER, _ADMIN, _ADMIN_NO_GRANT])
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


# One route per way a change reaches the host: a read, a host write, and the stored unlink.
_REPRESENTATIVE_ROUTES = [
    route
    for route in _OWNER_ONLY_ROUTES
    if route[:2]
    in {
        ("GET", "/resources/github/labels"),
        ("POST", "/resources/github/merge"),
        ("DELETE", "/pull-requests"),
    }
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "options"), _REPRESENTATIVE_ROUTES)
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


def _lookup_fails(*_args: object, **_kwargs: object) -> bool:
    raise RuntimeError("permission store unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caller", "selected", "failing_lookup"),
    [
        (_EDITOR, "owner-login", None),
        (_MANAGER, "owner-login", None),
        (_ADMIN, "owner-login", None),
        (_ADMIN_NO_GRANT, "owner-login", None),
        (_VIEWER, None, None),
        # Unsure who's asking, the server shows the least: even the owner, or an editor.
        (_OWNER, None, "_is_session_owner"),
        (_EDITOR, None, "check_session_access"),
    ],
)
async def test_only_the_owner_sees_the_host_s_accounts_and_change_controls(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caller: str,
    selected: str | None,
    failing_lookup: str | None,
) -> None:
    """An editor still sees which login the owner's changes run as; a viewer doesn't."""
    if failing_lookup is not None:
        monkeypatch.setattr(routes_resources, failing_lookup, _lookup_fails)
    response = await _call(client, "GET", "/resources/github", {}, caller=caller)

    assert response.status_code == 200, response.text
    info = response.json()
    assert "accounts" not in info
    assert info["auth"]["accounts"] is None
    assert info.get("selected_account") == selected
    assert info["auth"]["selected_account"] == selected
    assert info["owner_only_changes"] is True
    assert {name for name, on in info["capabilities"].items() if on} == {"line_counts"}
    assert set(info["capabilities"]) >= READ_CAPABILITIES
    assert info["pr"]["number"] == 42


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", [_OWNER, _ADMIN_OWNER])
async def test_the_owner_sees_the_host_s_info_unchanged(
    client: httpx.AsyncClient, caller: str
) -> None:
    response = await _call(client, "GET", "/resources/github", {}, caller=caller)

    assert response.status_code == 200, response.text
    info = response.json()
    assert info["accounts"] == _ACCOUNTS
    assert info["auth"]["accounts"] == _ACCOUNTS
    assert info["capabilities"] == _HOST_INFO["capabilities"]
    assert "owner_only_changes" not in info
    assert "read_only" not in info


@pytest.mark.asyncio
async def test_with_permissions_off_the_info_is_unchanged(
    db_uri: str, host: _RecordingHost
) -> None:
    transport = httpx.ASGITransport(app=_app(db_uri, auth=False))
    async with httpx.AsyncClient(transport=transport, base_url="http://server") as client:
        response = await _call(client, "GET", "/resources/github", {}, caller=None)

    assert response.status_code == 200, response.text
    assert response.json()["accounts"] == _ACCOUNTS
    assert host.reads == [f"{_ROOT}:github_info"]


_THREADS_PATH = "/resources/github/review-threads"
_COMMENTS_PATH = "/resources/github/review-threads/PRRT_1/comments"
_RIGHTS_OFF = {"can_react": False, "can_update": False, "can_delete": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", [_OWNER, _ADMIN_OWNER])
async def test_the_owner_reads_threads_with_the_host_accounts_rights(
    client: httpx.AsyncClient, caller: str
) -> None:
    threads = await _call(client, "GET", _THREADS_PATH, {"params": {"pr_url": _PR}}, caller=caller)
    comments = await _call(
        client, "GET", _COMMENTS_PATH, {"params": {"pr_url": _PR, "after": "c"}}, caller=caller
    )

    assert threads.status_code == 200, threads.text
    assert threads.json() == _HOST_THREADS
    assert comments.status_code == 200, comments.text
    assert comments.json() == _HOST_THREAD_COMMENTS


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", [_EDITOR, _MANAGER, _ADMIN])
async def test_anyone_else_reads_threads_with_ids_and_counts_but_no_rights(
    client: httpx.AsyncClient, caller: str
) -> None:
    """The edit, delete, reply and resolve controls act as the owner's account."""
    response = await _call(
        client, "GET", _THREADS_PATH, {"params": {"pr_url": _PR}}, caller=caller
    )

    assert response.status_code == 200, response.text
    [thread] = response.json()["data"]
    assert (thread["id"], thread["can_reply"], thread["can_resolve"]) == ("PRRT_1", False, False)
    [comment] = thread["comments"]["data"]
    assert (comment["id"], comment["database_id"]) == ("PRRC_1", 5)
    assert comment["reactions"] == [{"content": "HEART", "count": 2, "viewer_reacted": False}]
    assert {name: comment[name] for name in _RIGHTS_OFF} == _RIGHTS_OFF
    assert comment["viewer_did_author"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", [_EDITOR, _VIEWER])
async def test_anyone_else_reads_a_threads_comments_without_the_rights(
    client: httpx.AsyncClient, caller: str
) -> None:
    response = await _call(
        client, "GET", _COMMENTS_PATH, {"params": {"pr_url": _PR, "after": "c"}}, caller=caller
    )

    assert response.status_code == 200, response.text
    [comment] = response.json()["data"]
    assert comment["database_id"] == 5
    assert {name: comment[name] for name in _RIGHTS_OFF} == _RIGHTS_OFF
    assert comment["reactions"][0]["count"] == 2


_REVIEW_READS = [
    ("github_review_threads", _THREADS_PATH, {"pr_url": _PR}),
    ("github_review_thread_comments", _COMMENTS_PATH, {"pr_url": _PR, "after": "c"}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("op", "path", "params"), _REVIEW_READS, ids=["threads", "comments"])
@pytest.mark.parametrize(
    ("caller", "asks"),
    [(_OWNER, True), (_ADMIN_OWNER, True), (_EDITOR, False), (_MANAGER, False), (_ADMIN, False)],
)
async def test_the_server_asks_the_host_for_its_accounts_rights_for_the_owner_alone(
    client: httpx.AsyncClient,
    host: _RecordingHost,
    op: str,
    path: str,
    params: dict[str, Any],
    caller: str,
    asks: bool,
) -> None:
    """A host sends what its account may do only when asked, so no one else's read asks."""
    response = await _call(client, "GET", path, {"params": params}, caller=caller)

    assert response.status_code == 200, response.text
    assert host.read_params[op].get("account_rights") is (True if asks else None)


@pytest.mark.asyncio
@pytest.mark.parametrize(("op", "path", "params"), _REVIEW_READS, ids=["threads", "comments"])
async def test_unsure_who_reads_the_server_does_not_ask_for_the_accounts_rights(
    client: httpx.AsyncClient,
    host: _RecordingHost,
    monkeypatch: pytest.MonkeyPatch,
    op: str,
    path: str,
    params: dict[str, Any],
) -> None:
    monkeypatch.setattr(routes_resources, "_is_session_owner", _lookup_fails)
    response = await _call(client, "GET", path, {"params": params}, caller=_OWNER)

    assert response.status_code == 200, response.text
    assert "account_rights" not in host.read_params[op]


# A page from a host older than account rights: it sends only whether the account may reply.
_OLDER_HOST_THREADS: dict[str, Any] = {
    "data": [{"id": "PRRT_1", "can_reply": True, "comments": {"data": [{"id": "PRRC_1"}]}}],
    "has_more": False,
}


@pytest.mark.asyncio
@pytest.mark.parametrize(("caller", "can_reply"), [(_OWNER, True), (_EDITOR, False)])
async def test_an_older_hosts_threads_read_as_before(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caller: str,
    can_reply: bool,
) -> None:
    """The owner gets no rights the host didn't send; anyone else, none of the account's."""
    monkeypatch.setitem(_HOST_READS, "github_review_threads", _OLDER_HOST_THREADS)
    response = await _call(
        client, "GET", _THREADS_PATH, {"params": {"pr_url": _PR}}, caller=caller
    )

    assert response.status_code == 200, response.text
    [thread] = response.json()["data"]
    assert thread["can_reply"] is can_reply
    if caller == _OWNER:
        assert response.json() == _OLDER_HOST_THREADS
    else:
        assert thread["comments"]["data"][0]["viewer_did_author"] is False


@pytest.mark.asyncio
async def test_unsure_who_reads_the_threads_the_server_shows_no_rights(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_resources, "_is_session_owner", _lookup_fails)
    response = await _call(
        client, "GET", _THREADS_PATH, {"params": {"pr_url": _PR}}, caller=_OWNER
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["can_reply"] is False


# The owner's routes that change the pull request on its code host.
_CODE_HOST_WRITES = [
    route
    for route in _OWNER_ONLY_ROUTES
    if route[0] == "POST"
    and route[1] not in {"/resources/github/prs", "/resources/github/preferences"}
]


@pytest.fixture
async def read_only_client(db_uri: str, host: _RecordingHost) -> AsyncIterator[httpx.AsyncClient]:
    """A server that serves pull requests on ``github.com`` read-only."""
    del host
    transport = httpx.ASGITransport(app=_app(db_uri, read_only_code_hosts=("github.com",)))
    async with httpx.AsyncClient(transport=transport, base_url="http://server") as session:
        yield session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "options"),
    _CODE_HOST_WRITES,
    ids=[f"{method} {path}" for method, path, _options in _CODE_HOST_WRITES],
)
async def test_the_owner_s_change_on_a_read_only_host_is_refused_before_the_host(
    read_only_client: httpx.AsyncClient,
    host: _RecordingHost,
    method: str,
    path: str,
    options: dict[str, Any],
) -> None:
    response = await _call(read_only_client, method, path, options, caller=_OWNER)

    assert response.status_code == 403, response.text
    assert response.json()["error"]["message"] == READ_ONLY_CODE_HOST_MESSAGE
    assert host.writes == []


@pytest.mark.asyncio
async def test_the_account_choice_on_a_read_only_host_still_reaches_the_host(
    read_only_client: httpx.AsyncClient, host: _RecordingHost
) -> None:
    options = {"json": {"account": "", "pr_url": _PR}}
    response = await _call(
        read_only_client, "POST", "/resources/github/preferences", options, caller=_OWNER
    )

    assert response.status_code == 200, response.text
    assert len(host.calls) == 1, host.calls


@pytest.mark.asyncio
async def test_a_read_only_host_s_info_is_marked_and_offers_no_changes(
    read_only_client: httpx.AsyncClient,
) -> None:
    response = await _call(read_only_client, "GET", "/resources/github", {}, caller=_OWNER)

    assert response.status_code == 200, response.text
    info = response.json()
    assert info["read_only"] is True
    assert info["capabilities"] == {
        "account_switching": True,
        "merging": False,
        "line_counts": True,
    }
    assert info["pr"]["number"] == 42


@pytest.fixture
async def unsynced_read_only_client(
    db_uri: str, host: _RecordingHost, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    """A read-only ``github.com`` server that hasn't imported the session's PRs yet."""
    del host
    monkeypatch.setattr(routes_resources.server_prs, "vouch", lambda _stored, _pr_url: {})
    transport = httpx.ASGITransport(app=_app(db_uri, read_only_code_hosts=("github.com",)))
    async with httpx.AsyncClient(transport=transport, base_url="http://server") as session:
        yield session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        # Every route needs a ``pr_url`` string; an empty one names no pull request.
        {"pr_url": "", "merge_method": "squash"},
        {"pr_url": "\u00a0" + _PR, "merge_method": "squash"},
    ],
    ids=["empty-pr-url", "padded-url"],
)
async def test_a_change_the_server_cant_place_off_a_read_only_host_is_refused(
    unsynced_read_only_client: httpx.AsyncClient, host: _RecordingHost, body: dict[str, Any]
) -> None:
    """Without the stored PRs, the host would pick or normalize the PR the change goes to."""
    response = await _call(
        unsynced_read_only_client, "POST", "/resources/github/merge", {"json": body}, caller=_OWNER
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["message"] == READ_ONLY_CODE_HOST_MESSAGE
    assert host.writes == []
