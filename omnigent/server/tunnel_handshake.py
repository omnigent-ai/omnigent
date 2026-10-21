"""Bound the first protocol frame after a tunnel upgrade."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Literal

from starlette.websockets import WebSocket

from omnigent.debug_logging import debug_event

_logger = logging.getLogger(__name__)
# Hosts spend up to ~25 s on owner lookup and capability discovery before hello.
HELLO_TIMEOUT_S = 30.0


async def receive_tunnel_hello(
    ws: WebSocket, *, peer_kind: Literal["host", "runner"], peer_id: str
) -> str | None:
    """Receive the initial frame, or close a connection that cannot introduce itself."""
    started = time.monotonic()
    deadline = asyncio.timeout(HELLO_TIMEOUT_S)
    try:
        async with deadline:
            return await ws.receive_text()
    except TimeoutError:
        if not deadline.expired():
            raise
        _logger.warning(
            "%s tunnel did not send its hello within %.0fs",
            peer_kind,
            HELLO_TIMEOUT_S,
            extra=debug_event(
                "tunnel_hello_timeout",
                peer_kind=peer_kind,
                peer_id=peer_id,
                elapsed_ms=round((time.monotonic() - started) * 1000),
                timeout_s=HELLO_TIMEOUT_S,
            ),
        )
        # The peer may drop at the same moment; a failed close has nothing left to report.
        with contextlib.suppress(Exception):
            await ws.close(code=1013, reason="hello timeout")
        return None
