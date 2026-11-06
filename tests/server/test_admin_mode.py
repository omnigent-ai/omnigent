"""Admin mode applies after raw permission caching and across request transports."""

from __future__ import annotations

import asyncio

import pytest
from starlette.requests import HTTPConnection

from omnigent.db.db_models import workspace_scope
from omnigent.entities import ResolvedAccess
from omnigent.server.accounts_config import AccountsConfig
from omnigent.server.admin_mode import (
    ADMIN_MODE_COOKIE,
    AdminModeMiddleware,
    _enabled_cookie,
    admin_mode_cookie_name,
    admin_mode_scope,
    effective_admin,
)
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.permissions import resolved_allows, resolved_level
from omnigent.server.routes._sessions.helpers import _permission_level_from_grants
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "websocket"])
async def test_admin_mode_is_request_scoped_across_transports_and_workers(transport: str) -> None:
    raw = ResolvedAccess(is_admin=True, user_grant_level=1, public_grant_level=None)

    async def connection(scope, receive, send):
        await asyncio.sleep(0)
        enabled = scope["expected"]
        assert effective_admin(True) is enabled
        assert effective_admin(False) is False
        assert resolved_allows(raw, 2) is enabled
        assert resolved_level(raw) == (4 if enabled else 1)
        assert await asyncio.to_thread(resolved_level, raw) == (4 if enabled else 1)
        assert _permission_level_from_grants("admin", [], True) == (4 if enabled else None)

    middleware = AdminModeMiddleware(
        connection, UnifiedAuthProvider(source="header", local_single_user=False)
    )
    requests = []
    for marker, cookie, expected in [
        (None, None, True),
        ("off", None, True),
        ("on", None, True),
        ("off", "off", False),
        ("off", "on", False),
        ("off", "invalid", False),
        ("off", _enabled_cookie("admin"), True),
        ("off", _enabled_cookie("another-admin"), False),
    ]:
        headers = [(b"x-forwarded-email", b"admin"), (b"host", b"test")]
        scope = {"type": transport, "path": "/v1/me", "headers": headers, "expected": expected}
        cookies = [f"{ADMIN_MODE_COOKIE}={marker}"] if marker is not None else []
        if cookie is not None:
            name = admin_mode_cookie_name(HTTPConnection(scope), "")
            cookies.append(f"{name}={cookie}")
        if cookies:
            headers.append((b"cookie", "; ".join(cookies).encode()))
        requests.append(middleware(scope, None, None))
    await asyncio.gather(*requests)
    assert effective_admin(True)


@pytest.mark.asyncio
async def test_browser_state_isolated_by_mount_port_and_workspace() -> None:
    async def connection(scope, receive, send):
        assert effective_admin(True) is scope["expected"]

    provider = UnifiedAuthProvider(source="header", local_single_user=False)
    source_scope = {"type": "http", "path": "/v1/me", "headers": [(b"host", b"test:6767")]}
    with workspace_scope(1):
        name = admin_mode_cookie_name(HTTPConnection(source_scope), "")
    cookies = f"{ADMIN_MODE_COOKIE}=off; {name}=off"
    for host, base_path, workspace, expected in [
        ("test:6767", "", 1, False),
        ("test:6767", "/", 1, False),
        ("test:6767", "/omni", 1, True),
        ("test:6768", "", 1, True),
        ("other:6767", "", 1, True),
        ("test:6767", "", 2, True),
    ]:
        with workspace_scope(workspace):
            await AdminModeMiddleware(connection, provider, base_path)(
                {
                    "type": "http",
                    "path": "/v1/me",
                    "headers": [
                        (b"host", host.encode()),
                        (b"x-forwarded-email", b"admin"),
                        (b"cookie", cookies.encode()),
                    ],
                    "expected": expected,
                },
                None,
                None,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "root_path", "cookie", "initializes"),
    [
        ("GET", "/v1/me", "", None, True),
        ("GET", "/omni/v1/me", "/omni", None, True),
        ("GET", "/v1/me", "", _enabled_cookie("admin"), False),
        ("POST", "/v1/me", "", None, False),
        ("GET", "/v1/sharing", "", None, False),
    ],
)
async def test_browser_bootstrap_only_initializes_missing_state_on_identity_get(
    method: str, path: str, root_path: str, cookie: str | None, initializes: bool
) -> None:
    messages = []

    async def connection(scope, receive, send):
        assert effective_admin(True) is not initializes
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def capture(message):
        messages.append(message)

    headers = [
        (b"host", b"test"),
        (b"x-forwarded-email", b"admin"),
        (b"x-omnigent-admin-mode", b"browser"),
    ]
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "root_path": root_path,
        "headers": headers,
    }
    name = admin_mode_cookie_name(HTTPConnection(scope), root_path)
    if cookie is not None:
        headers.append((b"cookie", f"{name}={cookie}".encode()))
    await AdminModeMiddleware(
        connection, UnifiedAuthProvider(source="header", local_single_user=False), root_path
    )(scope, None, capture)
    cookies = [value.decode() for key, value in messages[0]["headers"] if key == b"set-cookie"]
    assert cookies == (
        [f"{name}=off; Path={root_path or '/'}; SameSite=lax"] if initializes else []
    )


def test_mode_does_not_change_cached_or_durable_admin_eligibility(db_uri: str) -> None:
    store = SqlAlchemyPermissionStore(db_uri)
    store.ensure_user("admin", is_admin=True)
    session_id = "0" * 32
    cached = store.resolve_access("admin", session_id)
    assert resolved_allows(cached, 4)
    with admin_mode_scope(False):
        assert store.is_admin("admin")
        assert store.resolve_access("admin", session_id) == cached
        assert not resolved_allows(cached, 1)
        assert store.get_permission_level("admin", session_id) is None
    assert store.get_permission_level("admin", session_id) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cookie", ["ap_session=new-login", "ap_session=; Max-Age=0", "other=value"]
)
@pytest.mark.parametrize("browser", [False, True])
async def test_login_cookie_changes_reset_admin_mode_on_the_deployment_path(
    cookie: str, browser: bool
) -> None:
    provider = UnifiedAuthProvider(
        source="accounts",
        accounts_config=AccountsConfig(b"a" * 32, 8, "http://test", None, 3600, 600),
    )
    sent = []

    async def response(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"set-cookie", cookie.encode())],
            }
        )

    async def capture(message):
        sent.append(message)

    scope = {
        "type": "http",
        "headers": [],
        "scheme": "https",
        "path": "/auth/login",
        "query_string": b"",
        "server": ("test", 443),
    }
    name = admin_mode_cookie_name(HTTPConnection(scope), "/omni")
    if browser:
        scope["headers"].append((b"cookie", f"{name}=off".encode()))
    await AdminModeMiddleware(response, provider, base_path="/omni")(
        scope,
        None,
        capture,
    )
    cookies = [value.decode() for name, value in sent[0]["headers"] if name == b"set-cookie"]
    assert cookies[0] == cookie
    if browser and cookie.startswith("ap_session="):
        assert cookies[1] == f"{name}=off; Path=/omni; SameSite=lax; Secure"
    else:
        assert len(cookies) == 1
