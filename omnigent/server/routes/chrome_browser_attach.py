"""WebSocket relay from a web viewer to the session runner's Chrome browser."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Final

from fastapi import APIRouter, Query, WebSocket

from omnigent.debug_logging import set_current_user_id
from omnigent.runtime import get_runner_ws_factory
from omnigent.server._runner_ws_tunnel import (
    RunnerRuntimeSupportWSError,
    RunnerUnavailableWSError,
    WrongReplicaWSError,
)
from omnigent.server.auth import AuthProvider
from omnigent.server.routes.terminal_attach import (
    _authorize_terminal_attach,
    _RunnerWSClosed,
    _shuttle_ws_frames,
)
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.terminals.ws_common import WS_CLOSE_INTERNAL_ERROR, WS_CLOSE_WRONG_REPLICA

_logger = logging.getLogger(__name__)

_WS_CLOSE_TRY_AGAIN_LATER: Final[int] = 1013


def create_chrome_browser_attach_router(
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    conversation_store: ConversationStore | None = None,
) -> APIRouter:
    """
    Build the router exposing the Chrome browser attach WebSocket route.

    Access follows terminal attach: a viewer that can type into the page drives
    a browser on the owner's host, so it needs owner access, and a read-only
    viewer needs read access.

    :param auth_provider: Authenticates the WebSocket handshake.
    :param permission_store: Session permission store; ``None`` disables checks.
    :param conversation_store: Conversation store used by permission checks.
    :returns: An :class:`APIRouter` carrying the attach route.
    """
    router = APIRouter()

    @router.websocket("/sessions/{session_id}/resources/chrome_browser/attach")
    async def attach_chrome_browser(
        websocket: WebSocket,
        session_id: str,
        read_only: bool = Query(default=False),
    ) -> None:
        """
        Relay a viewer to the Chrome browser on the session's runner.

        :param websocket: The viewer's WebSocket.
        :param session_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
        :param read_only: Show the page without accepting input.
        """
        user_id = await _authorize_terminal_attach(
            websocket,
            session_id=session_id,
            read_only=read_only,
            auth_provider=auth_provider,
            permission_store=permission_store,
            conversation_store=conversation_store,
        )
        set_current_user_id(user_id)
        await websocket.accept()

        ws_factory = get_runner_ws_factory()
        if ws_factory is None:
            # No tunnel: the runner shares this process, so bridge directly.
            from omnigent.runner.chrome_browser import bridge_chrome_browser_to_websocket

            await bridge_chrome_browser_to_websocket(websocket, read_only=read_only)
            return

        runner_path = (
            f"/v1/sessions/{session_id}/resources/chrome_browser/attach"
            f"?read_only={'true' if read_only else 'false'}"
        )
        try:
            runner_cm = await asyncio.to_thread(ws_factory, runner_path)
        except WrongReplicaWSError:
            await websocket.close(
                code=WS_CLOSE_WRONG_REPLICA,
                reason="host on another replica; retry without slice key",
            )
            return
        except RunnerUnavailableWSError:
            await websocket.close(
                code=_WS_CLOSE_TRY_AGAIN_LATER, reason="session runner is not connected"
            )
            return
        except RunnerRuntimeSupportWSError as exc:
            await websocket.close(code=WS_CLOSE_INTERNAL_ERROR, reason=exc.code)
            return
        except Exception:  # noqa: BLE001
            await websocket.close(
                code=WS_CLOSE_INTERNAL_ERROR, reason="runner attach factory failed"
            )
            return
        try:
            async with runner_cm as runner_ws:
                await _shuttle_ws_frames(websocket, runner_ws)
        except _RunnerWSClosed as closed:
            code = closed.code if closed.code and closed.code >= 1000 else WS_CLOSE_INTERNAL_ERROR
            with contextlib.suppress(RuntimeError):
                await websocket.close(code=code, reason=closed.reason or "")
        except Exception:
            _logger.exception("Chrome browser relay failed for session %s", session_id)
            with contextlib.suppress(RuntimeError):
                await websocket.close(
                    code=WS_CLOSE_INTERNAL_ERROR, reason="runner attach proxy failed"
                )

    return router
