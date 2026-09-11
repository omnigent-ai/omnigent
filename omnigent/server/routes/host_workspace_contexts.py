"""Owner-authorized, leased workspace terminals served by a connected host."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import secrets
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect, WebSocketState

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import (
    HostWorkspaceContextRequestFrame,
    HostWorkspaceContextStreamFrame,
    encode_host_frame,
)
from omnigent.server.auth import LEVEL_OWNER, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._host_launch import host_absent_error, resolve_host_owner
from omnigent.stores import ConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.terminals.ws_common import WS_CLOSE_TERMINAL_NOT_FOUND, WS_CLOSE_WRONG_REPLICA

_CONTEXT_TIMEOUT_S = 30.0


class CreateWorkspaceContext(BaseModel):
    """Workspace resolved and canonicalized on the host machine."""

    workspace: str = Field(min_length=1)


class HandoffWorkspaceContext(BaseModel):
    """Existing session that will own the workspace context."""

    session_id: str = Field(min_length=1)


class CreateWorkspaceTerminal(BaseModel):
    """Draft contexts only launch a plain shell, never an agent harness."""

    terminal: Literal["bash"] = "bash"
    session_key: str = Field(default="main", pattern=r"^[a-zA-Z0-9_-]{1,80}$")


async def _request_context(
    registry: HostRegistry,
    conn: HostConnection,
    user_id: str,
    op: str,
    context_id: str = "",
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Round-trip a context operation, cleaning up on cancellation or disconnect."""
    request_id = secrets.token_hex(16)
    future = asyncio.get_running_loop().create_future()
    conn.pending_workspace_contexts[request_id] = future
    try:
        registry.send_text(
            conn,
            encode_host_frame(
                HostWorkspaceContextRequestFrame(
                    request_id=request_id,
                    op=op,
                    user_id=user_id,
                    context_id=context_id,
                    params=params or {},
                )
            ),
        )
        result = await asyncio.wait_for(future, timeout=_CONTEXT_TIMEOUT_S)
    except ConnectionError as exc:
        raise HTTPException(502, "host connection lost") from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(504, "host workspace context request timed out") from exc
    finally:
        conn.pending_workspace_contexts.pop(request_id, None)
    if result.get("status") != "ok":
        error_status = result.get("error_status")
        if not isinstance(error_status, int) or not 400 <= error_status <= 599:
            error_status = 502
        raise HTTPException(
            error_status, result.get("error") or "workspace context request failed"
        )
    payload = result.get("payload")
    if not isinstance(payload, dict):
        raise HTTPException(502, "invalid workspace context response")
    return payload


def create_host_workspace_contexts_router(
    host_registry: HostRegistry,
    host_store: HostStore,
    conversation_store: ConversationStore,
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Mount host-owned contexts; adopted contexts additionally require session ownership."""
    router = APIRouter(prefix="/hosts/{host_id}/workspace-contexts")

    async def resolve(request: Request | WebSocket, host_id: str) -> tuple[HostConnection, str]:
        user_id = require_user(request, auth_provider)  # type: ignore[arg-type]
        host = await asyncio.to_thread(
            resolve_host_owner, user_id=user_id, host_id=host_id, host_store=host_store
        )
        conn = host_registry.get(host_id)
        if conn is None:
            raise host_absent_error(host)
        if not conn.hello.workspace_contexts:
            raise HTTPException(409, "upgrade this host to enable workspace contexts")
        return conn, user_id or RESERVED_USER_LOCAL

    async def authorize_context(conn: HostConnection, user_id: str, context_id: str) -> None:
        context = await _request_context(host_registry, conn, user_id, "describe", context_id)
        session_id = context.get("session_id")
        if session_id is not None:
            await require_access(
                user_id, session_id, LEVEL_OWNER, permission_store, conversation_store
            )

    async def operation(
        request: Request,
        host_id: str,
        context_id: str,
        op: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        conn, user_id = await resolve(request, host_id)
        await authorize_context(conn, user_id, context_id)
        return await _request_context(host_registry, conn, user_id, op, context_id, params)

    @router.post("", dependencies=[Depends(require_json_content_type)])
    async def create_context(
        request: Request, host_id: str, body: CreateWorkspaceContext
    ) -> dict[str, Any]:
        conn, user_id = await resolve(request, host_id)
        return await _request_context(
            host_registry, conn, user_id, "create", params=body.model_dump()
        )

    @router.post("/{context_id}/heartbeat")
    async def heartbeat(request: Request, host_id: str, context_id: str) -> dict[str, Any]:
        return await operation(request, host_id, context_id, "heartbeat")

    @router.delete("/{context_id}")
    async def delete_context(request: Request, host_id: str, context_id: str) -> dict[str, Any]:
        return await operation(request, host_id, context_id, "delete")

    @router.post("/{context_id}/handoff", dependencies=[Depends(require_json_content_type)])
    async def handoff(
        request: Request, host_id: str, context_id: str, body: HandoffWorkspaceContext
    ) -> dict[str, Any]:
        conn, user_id = await resolve(request, host_id)
        await require_access(
            user_id, body.session_id, LEVEL_OWNER, permission_store, conversation_store
        )
        conv = await asyncio.to_thread(conversation_store.get_conversation, body.session_id)
        if conv is None:
            raise HTTPException(404, "session not found")
        if conv.host_id != host_id or not conv.workspace:
            raise HTTPException(409, "session must use the same host and workspace")
        await authorize_context(conn, user_id, context_id)
        # The host compares canonical paths using its own filesystem semantics.
        return await _request_context(
            host_registry,
            conn,
            user_id,
            "handoff",
            context_id,
            {"session_id": body.session_id, "workspace": conv.workspace},
        )

    @router.get("/{context_id}/resources/terminals")
    async def list_terminals(request: Request, host_id: str, context_id: str) -> dict[str, Any]:
        return await operation(request, host_id, context_id, "list_terminals")

    @router.post(
        "/{context_id}/resources/terminals", dependencies=[Depends(require_json_content_type)]
    )
    async def create_terminal(
        request: Request, host_id: str, context_id: str, body: CreateWorkspaceTerminal
    ) -> dict[str, Any]:
        return await operation(request, host_id, context_id, "create_terminal", body.model_dump())

    @router.delete("/{context_id}/resources/terminals/{terminal_id}")
    async def delete_terminal(
        request: Request, host_id: str, context_id: str, terminal_id: str
    ) -> dict[str, Any]:
        return await operation(
            request, host_id, context_id, "delete_terminal", {"terminal_id": terminal_id}
        )

    @router.websocket("/{context_id}/resources/terminals/{terminal_id}/attach")
    async def attach(
        websocket: WebSocket,
        host_id: str,
        context_id: str,
        terminal_id: str,
        read_only: bool = Query(default=False),
    ) -> None:
        conn: HostConnection | None = None
        channel_id = secrets.token_hex(16)
        tasks: list[asyncio.Task[None]] = []
        try:
            conn, user_id = await resolve(websocket, host_id)
            await authorize_context(conn, user_id, context_id)
            queue: asyncio.Queue[HostWorkspaceContextStreamFrame] = asyncio.Queue(maxsize=256)
            conn.workspace_context_streams[channel_id] = queue
            await _request_context(
                host_registry,
                conn,
                user_id,
                "attach",
                context_id,
                {"terminal_id": terminal_id, "channel_id": channel_id, "read_only": read_only},
            )
            await websocket.accept()

            async def receive_browser() -> None:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    binary = message.get("bytes")
                    text = message.get("text")
                    if binary is not None:
                        if read_only:
                            continue
                        if len(binary) > 192 * 1024:
                            await websocket.close(code=1009)
                            return
                        data = base64.b64encode(binary).decode("ascii")
                    elif text is not None:
                        if len(text.encode("utf-8")) > 256 * 1024:
                            await websocket.close(code=1009)
                            return
                        data = text
                    else:
                        continue
                    # Bound terminal traffic without delaying host control RPCs.
                    if conn.outbound_queue.qsize() >= 256:
                        await websocket.close(code=1013)
                        return
                    host_registry.send_text(
                        conn,
                        encode_host_frame(
                            HostWorkspaceContextStreamFrame(
                                channel_id=channel_id, data=data, binary=binary is not None
                            )
                        ),
                    )

            async def receive_host() -> None:
                while True:
                    frame = await queue.get()
                    if frame.close_code is not None:
                        await websocket.close(code=frame.close_code)
                        return
                    if frame.binary:
                        await websocket.send_bytes(base64.b64decode(frame.data, validate=True))
                    else:
                        await websocket.send_text(frame.data)

            tasks = [asyncio.create_task(receive_browser()), asyncio.create_task(receive_host())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except OmnigentError as exc:
            code = WS_CLOSE_WRONG_REPLICA if exc.code == ErrorCode.WRONG_REPLICA else 1008
            if code != 1008 and websocket.application_state == WebSocketState.CONNECTING:
                await websocket.accept()
            await websocket.close(
                code=code, reason="workspace context unavailable or not authorized"
            )
        except HTTPException as exc:
            code = WS_CLOSE_TERMINAL_NOT_FOUND if exc.status_code == 404 else 1008
            if exc.status_code >= 500:
                code = 1011
            if code != 1008 and websocket.application_state == WebSocketState.CONNECTING:
                await websocket.accept()
            await websocket.close(
                code=code, reason="workspace context unavailable or not authorized"
            )
        except (ConnectionError, ValueError):
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(code=1011)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            if conn is not None and channel_id in conn.workspace_context_streams:
                conn.workspace_context_streams.pop(channel_id, None)
                with contextlib.suppress(ConnectionError):
                    host_registry.send_text(
                        conn,
                        encode_host_frame(
                            HostWorkspaceContextStreamFrame(channel_id=channel_id, close_code=1000)
                        ),
                    )
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*tasks, return_exceptions=True)

    return router
