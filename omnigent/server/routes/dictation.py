"""Streaming dictation route: the transcription WebSocket.

This module hosts the server-side speech-to-text surface behind the
composer mic button (``designs/server-dictation.md``):

- ``WS /v1/dictation/stream`` — one connection per dictation take.

Availability is advertised as ``dictation_available`` on ``GET /v1/info``
(the web UI's boot-time capability probe); there is no separate probe
endpoint.

Wire protocol on the WebSocket
------------------------------

- **Client → server, binary frames**: raw 16 kHz mono s16le PCM. The
  browser worklet downsamples from the capture rate before sending.
- **Client → server, text frames**: JSON control messages.
  ``{"type": "stop"}`` asks the server to flush trailing audio and
  finish the take. Unknown shapes are ignored so future control
  messages don't break older servers.
- **Server → client, text frames**: JSON events.
    - ``{"type": "ready"}`` — sent once after the engine is ready;
      the client may start streaming audio.
    - ``{"type": "partial", "text": ...}`` — revisable in-progress
      utterance, throttled server-side.
    - ``{"type": "final", "text": ...}`` — an utterance completed by
      endpoint detection (a pause). The client appends it and clears
      its partial region.
    - ``{"type": "stopped", "text": ...}`` — reply to ``stop``: the
      flushed tail utterance (possibly empty). The server closes the
      socket after sending it.
    - ``{"type": "error", "message": ...}`` — fatal; the server closes.

Auth
----

Dictation is not session-scoped — the new-chat composer dictates before
any session exists — so the check is identity-level only, matching
``GET /v1/harnesses``: when an auth provider is configured the caller
must be authenticated (the WebSocket handshake carries identity via the
ingress/dev proxy exactly like the terminal-attach socket); in
single-user/dev mode the route is open.

Capacity
--------

Decoding is CPU-bound, so concurrent takes are capped (default 2,
``OMNIGENT_DICTATION_MAX_STREAMS``). Over-cap connections are accepted
and immediately closed with code 1013 (try again later) so the client
can distinguish "busy" from "broken".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from typing import Final

import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, WebSocketException
from starlette import status

from omnigent.server.auth import AuthProvider
from omnigent.server.dictation import (
    DictationEngine,
    DictationStreamHandle,
    get_engine,
    max_streams,
)

_logger = logging.getLogger(__name__)

_WS_CLOSE_TRY_AGAIN_LATER: Final[int] = 1013
_WS_CLOSE_INTERNAL_ERROR: Final[int] = 1011

#: Minimum interval between partial-transcript pushes. Keeps the socket
#: chatty enough for live text without a frame per audio chunk.
_PARTIAL_INTERVAL_S: Final[float] = 0.15


def create_dictation_router(
    *,
    auth_provider: AuthProvider | None = None,
    engine_provider: Callable[[], DictationEngine] | None = None,
) -> APIRouter:
    """Build the router carrying the dictation stream route.

    Wired into the FastAPI app under the ``/v1`` prefix in
    :func:`omnigent.server.app.create_app`.

    :param auth_provider: Optional provider used to authenticate the
        WebSocket handshake. ``None`` preserves single-user/dev
        behavior (open).
    :param engine_provider: Engine factory override for tests. Defaults
        to :func:`omnigent.server.dictation.get_engine`, which resolves
        the configured engine and loads models on first use.
    :returns: An :class:`APIRouter` carrying the stream route.
    """
    router = APIRouter()
    resolve_engine = engine_provider or get_engine
    # Router-scoped so each app (and each test app) gets its own cap.
    slots = asyncio.Semaphore(max_streams())

    @router.websocket("/dictation/stream")
    async def dictation_stream(websocket: WebSocket) -> None:
        """Transcribe one dictation take (see module docstring)."""
        if auth_provider is not None and auth_provider.get_user_id(websocket) is None:
            raise WebSocketException(
                code=status.WS_1008_POLICY_VIOLATION,
                reason="authentication required",
            )
        await websocket.accept()

        if slots.locked():
            await websocket.close(
                code=_WS_CLOSE_TRY_AGAIN_LATER,
                reason="dictation is at capacity; try again shortly",
            )
            return

        async with slots:
            # Engine construction loads model weights — seconds on first
            # use. Run it off-loop; later takes reuse the shared engine.
            try:
                engine = await asyncio.to_thread(resolve_engine)
                handle: DictationStreamHandle = await asyncio.to_thread(engine.create_stream)
            except Exception:
                _logger.exception("dictation engine failed to initialize")
                with contextlib.suppress(RuntimeError):
                    await websocket.send_text(
                        json.dumps({"type": "error", "message": "dictation engine unavailable"})
                    )
                    await websocket.close(code=_WS_CLOSE_INTERNAL_ERROR)
                return
            # Release the take on every exit — normal stop, abrupt browser
            # disconnect, or a crash mid-send. For the in-process engines
            # close() just frees the recognizer stream, so a best-effort
            # close on the way out is enough.
            try:
                await websocket.send_text(json.dumps({"type": "ready"}))
                await _pump_dictation(websocket, handle)
            finally:
                # An abrupt disconnect tears the ASGI task down via
                # cancellation, which would cancel this close mid-await and
                # leak the take (the remote engine holds a worker slot until
                # close). Shield it so cleanup always completes.
                with anyio.CancelScope(shield=True):
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(handle.close)

    return router


async def _pump_dictation(websocket: WebSocket, handle: DictationStreamHandle) -> None:
    """Shuttle audio in and transcript events out until stop/disconnect.

    :param websocket: The accepted browser-facing WebSocket.
    :param handle: The per-connection recognizer stream.
    """
    last_partial_sent = ""
    last_partial_at = 0.0
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return

            data = message.get("bytes")
            if data is not None:
                update = await asyncio.to_thread(handle.feed_pcm16, data)
                if update.finalized:
                    await websocket.send_text(
                        json.dumps({"type": "final", "text": update.finalized})
                    )
                    last_partial_sent = ""
                    last_partial_at = 0.0
                now = time.monotonic()
                if (
                    update.partial != last_partial_sent
                    and now - last_partial_at >= _PARTIAL_INTERVAL_S
                ):
                    await websocket.send_text(
                        json.dumps({"type": "partial", "text": update.partial})
                    )
                    last_partial_sent = update.partial
                    last_partial_at = now
                continue

            text_frame = message.get("text")
            if text_frame is None:
                continue
            try:
                control = json.loads(text_frame)
            except ValueError:
                continue
            if isinstance(control, dict) and control.get("type") == "stop":
                tail = await asyncio.to_thread(handle.finish)
                await websocket.send_text(json.dumps({"type": "stopped", "text": tail}))
                await websocket.close()
                return
            # Unknown control messages are ignored for forward compat.
    except WebSocketDisconnect:
        return
    except Exception:
        _logger.exception("dictation stream failed")
        with contextlib.suppress(RuntimeError):
            await websocket.send_text(json.dumps({"type": "error", "message": "dictation failed"}))
            await websocket.close(code=_WS_CLOSE_INTERNAL_ERROR)
