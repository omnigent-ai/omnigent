"""Attribute runner requests without changing ASGI task or streaming lifetimes."""

from starlette.types import ASGIApp, Receive, Scope, Send

from omnigent.debug_logging import current_session_id_scope


def request_path_session_id(path: str) -> str | None:
    """Return the session a ``/v1/sessions/<id>/…`` request path names, else ``None``."""
    parts = path.split("/")
    if len(parts) > 3 and parts[1:3] == ["v1", "sessions"]:
        return parts[3] or None
    return None


class RunnerLogContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        with current_session_id_scope(request_path_session_id(scope.get("path", ""))):
            await self.app(scope, receive, send)
