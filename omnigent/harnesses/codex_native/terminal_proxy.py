"""Register native TUI submissions before forwarding them to the app-server."""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http import HTTPStatus

from websockets.asyncio.client import connect
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response


@dataclass(frozen=True)
class RegisteredTerminalInput:
    client_id: str
    refuse: Callable[[], Awaitable[None]]


RegisterInput = Callable[[str, str | None], Awaitable[RegisteredTerminalInput]]

TOKEN_ENV = "OMNIGENT_CODEX_TERMINAL_PROXY_TOKEN"


class CodexTerminalProxy:
    """A launch-scoped TUI transport; model-facing tools cannot register input.

    Registration callbacks obtain identity from the authenticated terminal writer.
    The provider retains the registered client ID; only the TUI sees its original
    display ID. A lost acknowledgement never releases pending input evidence.
    Display aliases are a bounded FIFO cache, independent of requester evidence:
    old history falls back to its provider ID instead of another input's alias.
    """

    _MAX_DISPLAY_IDS = 8192

    def __init__(self, upstream: str, register: RegisterInput) -> None:
        self._upstream = upstream
        self._register = register
        self._token = secrets.token_urlsafe(32)
        self._server: Server | None = None
        self._display_ids: dict[str, str | None] = {}

    @property
    def environment(self) -> dict[str, str]:
        return {TOKEN_ENV: self._token}

    async def start(self) -> str:
        self._server = await serve(
            self._connection,
            "127.0.0.1",
            0,
            process_request=self._authorize,
            max_size=None,
        )
        port = next(iter(self._server.sockets)).getsockname()[1]
        return f"ws://127.0.0.1:{port}"

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._display_ids.clear()

    def _authorize(self, connection: ServerConnection, request: Request) -> Response | None:
        values = request.headers.get_all("Authorization")
        if len(values) != 1 or not secrets.compare_digest(
            values[0].encode(), f"Bearer {self._token}".encode()
        ):
            return connection.respond(
                HTTPStatus.FORBIDDEN, "Terminal transport authentication required"
            )
        return None

    def _display_payload(self, value: object) -> object:
        if isinstance(value, list):
            return [self._display_payload(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: self._display_payload(item) for key, item in value.items()}
        client_id = result.get("clientId")
        if result.get("type") == "userMessage" and isinstance(client_id, str):
            if client_id in self._display_ids:
                result["clientId"] = self._display_ids[client_id]
        return result

    async def _connection(self, terminal: ServerConnection) -> None:
        pending: dict[tuple[type, object], RegisteredTerminalInput] = {}
        requests: set[tuple[type, object]] = set()
        async with connect(self._upstream, max_size=None) as provider:

            async def submit() -> None:
                async for raw in terminal:
                    try:
                        message = json.loads(raw)
                    except (ValueError, UnicodeError):
                        await provider.send(raw)
                        continue
                    if isinstance(message, dict) and "method" in message:
                        rpc_id = message.get("id")
                        if type(rpc_id) in (int, str):
                            request_key = (type(rpc_id), rpc_id)
                            if request_key in requests:
                                await terminal.close(code=1008, reason="Duplicate request ID")
                                return
                            requests.add(request_key)
                    if isinstance(message, dict) and message.get("method") in (
                        "turn/start",
                        "turn/steer",
                    ):
                        params = message.get("params")
                        rpc_id = message.get("id")
                        if type(rpc_id) not in (int, str):
                            await terminal.close(code=1008, reason="Input requires a request ID")
                            return
                        if isinstance(params, dict) and isinstance(params.get("threadId"), str):
                            expected = (
                                params.get("expectedTurnId")
                                if message["method"] == "turn/steer"
                                else None
                            )
                            try:
                                registration = await self._register(
                                    params["threadId"],
                                    expected if isinstance(expected, str) else None,
                                )
                            except (OSError, RuntimeError, ValueError):
                                requests.discard((type(rpc_id), rpc_id))
                                await terminal.send(
                                    json.dumps(
                                        {
                                            "id": rpc_id,
                                            "error": {
                                                "code": -32000,
                                                "message": (
                                                    "Input not submitted: "
                                                    "requester registration unavailable"
                                                ),
                                            },
                                        }
                                    )
                                )
                                continue
                            client_id = registration.client_id
                            display_id = params.get("clientUserMessageId")
                            self._display_ids[client_id] = (
                                display_id if isinstance(display_id, str) else None
                            )
                            if len(self._display_ids) > self._MAX_DISPLAY_IDS:
                                del self._display_ids[next(iter(self._display_ids))]
                            params["clientUserMessageId"] = client_id
                            if type(rpc_id) in (int, str):
                                pending[(type(rpc_id), rpc_id)] = registration
                            raw = json.dumps(message)
                    await provider.send(raw)

            async def receive() -> None:
                async for raw in provider:
                    try:
                        message = json.loads(raw)
                    except (ValueError, UnicodeError):
                        await terminal.send(raw)
                        continue
                    if isinstance(message, dict) and "method" not in message:
                        rpc_id = message.get("id")
                        if type(rpc_id) in (int, str):
                            requests.discard((type(rpc_id), rpc_id))
                        registration = (
                            pending.pop((type(rpc_id), rpc_id), None)
                            if type(rpc_id) in (int, str)
                            else None
                        )
                        if registration is not None and isinstance(message.get("error"), dict):
                            self._display_ids.pop(registration.client_id, None)
                            # Pinned start/steer error responses precede input enqueue.
                            with contextlib.suppress(OSError, RuntimeError, ValueError):
                                await registration.refuse()
                    await terminal.send(json.dumps(self._display_payload(message)))

            tasks = [asyncio.create_task(submit()), asyncio.create_task(receive())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    with contextlib.suppress(ConnectionClosed):
                        task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
