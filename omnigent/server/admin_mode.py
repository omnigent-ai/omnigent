"""Browser-scoped admin access without changing the account's admin eligibility."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, StrictBool
from starlette._utils import get_route_path
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from omnigent.db.db_models import current_workspace_id
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes._content_type import require_json_content_type

if TYPE_CHECKING:
    from omnigent.server.admin_list import AdminList
    from omnigent.server.auth import AuthProvider
    from omnigent.stores.permission_store import PermissionStore

ADMIN_MODE_COOKIE = "omnigent_admin_mode"
_admin_mode: ContextVar[bool] = ContextVar("admin_mode", default=True)
_credential_admin: ContextVar[bool] = ContextVar("credential_admin", default=True)
_credential_admin_limit: ContextVar[bool] = ContextVar("credential_admin_limit", default=True)


def credential_admin_allowed() -> bool:
    """Whether the authenticated credential and its enclosing scope allow admin authority."""
    return _credential_admin.get() and _credential_admin_limit.get()


def bind_credential_admin_allowed(enabled: bool) -> None:
    """Replace the authenticated credential's ceiling, preserving enclosing restrictions."""
    _credential_admin.set(enabled)


@contextmanager
def credential_admin_scope(enabled: bool) -> Iterator[None]:
    """Narrow credential authority for this scope and restore authentication changes on exit."""
    limit = _credential_admin_limit.set(credential_admin_allowed() and enabled)
    credential = _credential_admin.set(_credential_admin.get())
    try:
        yield
    finally:
        _credential_admin.reset(credential)
        _credential_admin_limit.reset(limit)


def effective_admin(eligible: bool) -> bool:
    """Apply the current browser's mode to an uncached or cached eligibility flag."""
    return eligible and _admin_mode.get() and credential_admin_allowed()


@contextmanager
def admin_mode_scope(enabled: bool) -> Iterator[None]:
    token = _admin_mode.set(enabled)
    try:
        yield
    finally:
        _admin_mode.reset(token)


def _enabled_cookie(user_id: str) -> str:
    return "on:" + hashlib.sha256(user_id.encode()).hexdigest()


def admin_mode_cookie_name(connection: HTTPConnection, base_path: str) -> str:
    """Keep browser mode separate across hosts, ports, mounts, and workspaces."""
    namespace = (
        f"{connection.url.netloc.lower()}\n"
        f"{base_path.rstrip('/') or '/'}\n{current_workspace_id()}"
    )
    return f"{ADMIN_MODE_COOKIE}_{hashlib.sha256(namespace.encode()).hexdigest()}"


class AdminModeMiddleware:
    """Apply the same mode to HTTP, downloads, and each WebSocket connection."""

    def __init__(self, app: ASGIApp, auth_provider: AuthProvider, base_path: str = "") -> None:
        self._app = app
        self._auth_provider = auth_provider
        self._base_path = base_path or "/"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return
        with credential_admin_scope(True):
            await self._handle_connection(scope, receive, send)

    async def _handle_connection(self, scope: Scope, receive: Receive, send: Send) -> None:
        connection = HTTPConnection(scope)
        state_cookie_name = admin_mode_cookie_name(connection, self._base_path)
        value = connection.cookies.get(state_cookie_name)
        initialize = (
            scope["type"] == "http"
            and scope.get("method") == "GET"
            and get_route_path(scope) == "/v1/me"
            and connection.headers.get("x-omnigent-admin-mode") == "browser"
        )
        browser = initialize or value is not None
        # Clients without the browser cookie retain their existing API behavior.
        enabled = not browser
        if value is not None and value.startswith("on:"):
            user_id = self._auth_provider.get_user_id(connection)
            enabled = user_id is not None and value == _enabled_cookie(user_id)

        async def send_response(message: Message) -> None:
            cookie_name = getattr(self._auth_provider, "session_cookie_name", None)
            if (
                browser
                and message["type"] == "http.response.start"
                and (
                    (initialize and value is None)
                    or (
                        cookie_name
                        and any(
                            name.lower() == b"set-cookie"
                            and header_value.startswith(cookie_name.encode() + b"=")
                            for name, header_value in message.get("headers", [])
                        )
                    )
                )
            ):
                # First browser use and each login/logout start with ordinary access.
                reset = Response()
                reset.set_cookie(
                    state_cookie_name,
                    "off",
                    path=self._base_path,
                    secure=connection.url.scheme == "https",
                    samesite="lax",
                )
                message["headers"] = [
                    *message.get("headers", []),
                    *[(name, value) for name, value in reset.raw_headers if name == b"set-cookie"],
                ]
            await send(message)

        with admin_mode_scope(enabled):
            await self._app(scope, receive, send_response)


class SetAdminModeRequest(BaseModel):
    enabled: StrictBool


def create_admin_mode_router(
    auth_provider: AuthProvider,
    permission_store: PermissionStore,
    admin_list: AdminList,
    base_path: str,
) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/me/admin-mode", dependencies=[Depends(require_json_content_type)])
    async def set_admin_mode(body: SetAdminModeRequest, request: Request) -> JSONResponse:
        user_id = auth_provider.get_user_id(request)
        if user_id is None:
            raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
        eligible = await asyncio.to_thread(permission_store.is_admin, user_id)
        eligible = eligible or admin_list.is_admin(user_id)
        can_enable = eligible and credential_admin_allowed()
        if body.enabled and not can_enable:
            raise OmnigentError("Admin privileges required", code=ErrorCode.FORBIDDEN)
        enabled = body.enabled and can_enable
        cookie_name = admin_mode_cookie_name(request, base_path)
        response = JSONResponse(
            {
                "is_admin": enabled,
                "can_enable_admin_mode": can_enable,
                "admin_mode_enabled": enabled,
                "admin_mode_cookie_name": cookie_name,
            }
        )
        response.set_cookie(
            cookie_name,
            _enabled_cookie(user_id) if enabled else "off",
            path=base_path or "/",
            secure=request.url.scheme == "https",
            samesite="lax",
        )
        return response

    return router
