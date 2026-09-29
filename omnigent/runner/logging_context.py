"""Attribute runner requests without changing ASGI task or streaming lifetimes."""

from starlette.types import ASGIApp, Receive, Scope, Send

from omnigent.debug_logging import current_session_id_scope


class RunnerLogContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        parts = scope.get("path", "").split("/")
        session_id = parts[3] if len(parts) > 3 and parts[1:3] == ["v1", "sessions"] else None
        with current_session_id_scope(session_id):
            await self.app(scope, receive, send)
