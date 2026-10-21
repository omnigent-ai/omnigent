"""Read a session's native terminal pane over the server's read-only attach WebSocket.

The runner's tmux socket is not reachable from a test, so the pane is read the
way a viewer would: attach read-only and replay the stream through ``pyte``.
Callers keep their own polling, deadlines and assertions.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pyte
import websockets

from tests._helpers.async_thread import run_in_fresh_loop


def terminal_resource_id(base_url: str, session_id: str) -> str | None:
    """Return the session's first terminal resource id, or ``None`` before launch."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources", timeout=10.0)
    resp.raise_for_status()
    for resource in resp.json().get("data", []):
        if resource.get("type") == "terminal" and resource.get("id") is not None:
            return str(resource["id"])
    return None


def render_terminal_screen(
    base_url: str,
    session_id: str,
    terminal_id: str,
    *,
    cols: int,
    rows: int,
    settle_s: float = 3.0,
) -> str:
    """Attach read-only for *settle_s* seconds and return the rendered screen text.

    A read-only attach ignores the resize, so pass a screen wider than the real
    pane to keep its rows from wrapping a second time.
    """
    ws_url = (
        base_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        + f"/v1/sessions/{session_id}/resources/terminals/{terminal_id}/attach?read_only=true"
    )
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)

    async def _collect() -> None:
        async with websockets.connect(ws_url, max_size=None, open_timeout=20) as ws:
            await ws.send(json.dumps({"type": "resize", "cols": cols, "rows": rows}))
            deadline = time.monotonic() + settle_s
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    frame = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except TimeoutError:
                    break
                if isinstance(frame, (bytes, bytearray)):
                    stream.feed(bytes(frame))

    run_in_fresh_loop(_collect())
    return "\n".join(screen.display)
