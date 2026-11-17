"""Attribute runner requests without changing ASGI task or streaming lifetimes."""

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from omnigent.scheduled_context import (
    SCHEDULED_RUN_HEADER,
    parse_scheduled_run,
    scheduled_run_scope,
)


class RunnerLogContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        parts = scope.get("path", "").split("/")
        session_id = parts[3] if len(parts) > 3 and parts[1:3] == ["v1", "sessions"] else None
        run = parse_scheduled_run(Headers(scope=scope).get(SCHEDULED_RUN_HEADER))
        with scheduled_run_scope(run, session_id=session_id):
            await self.app(scope, receive, send)
