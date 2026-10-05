"""Per-request client capabilities that change how responses are shaped.

A client advertises them with the :data:`PERMISSION_LEVELS_HEADER` request
header, or the :data:`PERMISSION_LEVELS_QUERY_PARAM` query parameter where it
can't set headers (browser WebSockets). Responses fall back to the
representation every client understands when a capability is not advertised,
so older clients (including tabs opened before an upgrade) keep working.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import overload
from urllib.parse import parse_qsl

from starlette.types import ASGIApp, Receive, Scope, Send

from omnigent.server.auth import LEVEL_COMMENT, LEVEL_READ

# Comma-separated permission levels beyond 1-4 the client can interpret,
# e.g. ``"comment"``.
PERMISSION_LEVELS_HEADER = "X-Omnigent-Permission-Levels"
_PERMISSION_LEVELS_HEADER_BYTES = PERMISSION_LEVELS_HEADER.lower().encode("latin-1")
PERMISSION_LEVELS_QUERY_PARAM = "omnigent_permission_levels"

_understands_comment_level: ContextVar[bool] = ContextVar(
    "omnigent_client_understands_comment_level", default=False
)


@overload
def client_permission_level(level: int) -> int: ...
@overload
def client_permission_level(level: None) -> None: ...
def client_permission_level(level: int | None) -> int | None:
    """Return *level* as the current client should see it.

    A client that hasn't advertised the comment level gets it as read: its
    permission checks predate it and would otherwise treat 5 as edit or owner.
    The server still enforces the real level either way.

    :param level: The caller's stored level, or ``None``.
    :returns: *level*, or ``LEVEL_READ`` in place of ``LEVEL_COMMENT``.
    """
    if level == LEVEL_COMMENT and not _understands_comment_level.get():
        return LEVEL_READ
    return level


class ClientCapabilitiesMiddleware:
    """Record the request's advertised capabilities for response serialization."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        raw: list[str] = [
            value.decode("latin-1")
            for name, value in scope.get("headers", [])
            if name == _PERMISSION_LEVELS_HEADER_BYTES
        ]
        raw += [
            value
            for key, value in parse_qsl(scope.get("query_string", b"").decode("latin-1"))
            if key == PERMISSION_LEVELS_QUERY_PARAM
        ]
        levels = {part.strip().lower() for value in raw for part in value.split(",")}
        token = _understands_comment_level.set("comment" in levels)
        try:
            await self.app(scope, receive, send)
        finally:
            _understands_comment_level.reset(token)
