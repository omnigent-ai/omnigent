"""One-shot transient faults for the ``sys_os_shell`` dispatch journey.

A spawned server or runner imports this module from a ``-c`` preamble before
its real entry point. Each fault arms itself from a marker token embedded in
the ``sys_os_shell`` command, so only the marked call is affected, exactly
once per token; everything else passes through untouched.

- ``install_server_fault``: the first ``tools/call`` for a command carrying
  :data:`PROXY_500_MARKER` gets a genuine HTTP 500 from
  ``POST /v1/sessions/{id}/mcp`` instead of reaching the route.
- ``install_runner_fault``: the first OS-environment helper spawn made for a
  command carrying :data:`FORK_EAGAIN_MARKER` raises ``BlockingIOError``
  (``EAGAIN``) from ``subprocess.Popen``, as a host out of process slots does.
"""

from __future__ import annotations

import errno
import json
import os
import re
import subprocess
import threading
from typing import Any

PROXY_500_MARKER = "shellfault-proxy500-"
FORK_EAGAIN_MARKER = "shellfault-forkeagain-"


def _marker_token(text: object, marker: str) -> str | None:
    if not isinstance(text, str):
        return None
    match = re.search(re.escape(marker) + r"[0-9a-f]+", text)
    return match.group(0) if match else None


def _marked_proxy_call(body: bytes) -> str | None:
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("method") != "tools/call":
        return None
    params = payload.get("params") or {}
    if not isinstance(params, dict) or params.get("name") != "sys_os_shell":
        return None
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return None
    return _marker_token(arguments.get("command"), PROXY_500_MARKER)


class _McpProxy500Once:
    """Pure ASGI middleware: one HTTP 500 per marked ``sys_os_shell`` call."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._fired: set[str] = set()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or not scope["path"].endswith("/mcp")
        ):
            await self._app(scope, receive, send)
            return

        messages: list[dict[str, Any]] = []
        body = b""
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break

        token = _marked_proxy_call(body)
        if token is not None and token not in self._fired:
            self._fired.add(token)
            await send(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                }
            )
            await send({"type": "http.response.body", "body": b"Internal Server Error"})
            return

        async def replay() -> dict[str, Any]:
            if messages:
                return messages.pop(0)
            return await receive()

        await self._app(scope, replay, send)


def install_server_fault() -> None:
    import omnigent.server.app as server_app

    real_create_app = server_app.create_app

    def create_app(*args: Any, **kwargs: Any) -> Any:
        app = real_create_app(*args, **kwargs)
        app.add_middleware(_McpProxy500Once)
        return app

    server_app.create_app = create_app


def _is_helper_argv(args: object) -> bool:
    return isinstance(args, (list, tuple)) and "omnigent.inner.os_env" in args and "helper" in args


def install_runner_fault() -> None:
    from omnigent.inner import os_env

    armed = threading.local()
    fired: set[str] = set()
    real_popen = subprocess.Popen
    real_request = os_env._HelperProcessClient.request

    class EagainOncePopen(real_popen):  # type: ignore[valid-type,misc]
        def __init__(self, args: Any, *popen_args: Any, **popen_kwargs: Any) -> None:
            if getattr(armed, "token", None) is not None and _is_helper_argv(args):
                armed.token = None
                raise BlockingIOError(errno.EAGAIN, os.strerror(errno.EAGAIN))
            super().__init__(args, *popen_args, **popen_kwargs)

    def request(self: Any, payload: dict[str, Any]) -> dict[str, Any]:
        token = None
        if payload.get("op") == "shell":
            token = _marker_token(payload.get("command"), FORK_EAGAIN_MARKER)
        if token is not None and token not in fired:
            fired.add(token)
            armed.token = token
        try:
            return real_request(self, payload)
        finally:
            armed.token = None

    subprocess.Popen = EagainOncePopen  # type: ignore[misc]
    os_env._HelperProcessClient.request = request  # type: ignore[method-assign]
