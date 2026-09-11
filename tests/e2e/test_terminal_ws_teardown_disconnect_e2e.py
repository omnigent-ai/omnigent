"""Regression e2e: unhandled ASGI exception on abrupt terminal detach.

User journey
------------
1. A user has a terminal open in the browser, attached to the runner over the
   terminal-attach WebSocket (``bridge_tmux_control_to_websocket``).
2. The client transport drops abruptly (tab crash / laptop sleep / network
   partition) *without* the runner ever having failed a send first, so the
   bridge's application-side WebSocket is still ``CONNECTED`` when it tears the
   attach down.
3. On teardown the bridge best-effort closes the socket
   (``await websocket.close()``). The dead transport makes uvicorn raise
   ``ClientDisconnected`` (an ``OSError``), which starlette re-raises as
   ``WebSocketDisconnect(1006)``.

The teardown ``close()`` is wrapped only in ``contextlib.suppress(RuntimeError)``
(unlike the seed/clipboard sends, which also suppress ``WebSocketDisconnect``),
so the ``WebSocketDisconnect`` escapes the bridge coroutine unhandled. uvicorn's
``run_asgi`` then logs ``Exception in ASGI application`` and the session is
interrupted -- stack tail:

    control_bridge.py, in bridge_tmux_control_to_websocket
        await websocket.close()
    starlette/websockets.py, in close
        await self.send({"type": "websocket.close", ...})
    starlette/websockets.py, in send
        raise WebSocketDisconnect(code=1006)

This drives the *real* ``bridge_tmux_control_to_websocket`` against a *real*
private tmux server through a *real* starlette ``WebSocket`` whose ASGI
transport reports dead exactly the way uvicorn does when the client is gone.
No product code is stubbed; the failure is a genuine unhandled exception
escaping the bridge.

On the buggy build the bridge raises ``WebSocketDisconnect`` and this test
fails. Once the teardown ``close()`` also suppresses ``WebSocketDisconnect``,
the bridge returns cleanly and this test passes.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
from starlette.websockets import WebSocket, WebSocketState
from uvicorn.protocols.utils import ClientDisconnected

from omnigent.terminals.control_bridge import bridge_tmux_control_to_websocket

_HAS_TMUX = shutil.which("tmux") is not None


async def _new_private_tmux(inner: str) -> tuple[Path, str]:
    """Create a private single-pane tmux server, like ``terminal.py:launch``.

    :param inner: The command the pane runs, e.g. ``"sleep 30"``.
    :returns: ``(socket_path, tmux_target)``.
    """
    tmux = shutil.which("tmux")
    assert tmux is not None
    tmpdir = Path(tempfile.mkdtemp(prefix="terminal-ws-teardown-"))
    sock = tmpdir / "tmux.sock"
    proc = await asyncio.create_subprocess_exec(
        tmux,
        "-S",
        str(sock),
        "-f",
        os.devnull,
        "new-session",
        "-d",
        "-s",
        "main",
        "-x",
        "80",
        "-y",
        "24",
        inner,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()
    return sock, "main"


def _kill_tmux(sock: Path) -> None:
    """Best-effort tear down the private tmux server and its tempdir."""
    tmux = shutil.which("tmux")
    if tmux is not None:
        subprocess.run(
            [tmux, "-S", str(sock), "kill-server"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    shutil.rmtree(sock.parent, ignore_errors=True)


def _dead_transport_websocket() -> WebSocket:
    """A real starlette WebSocket whose client transport dies mid-attach.

    - ``send`` succeeds while the client is connected (so the on-attach seed
      capture is delivered), then raises uvicorn's real ``ClientDisconnected``
      once the client has dropped -- exactly what a dead transport does.
    - ``receive`` delivers a single ``websocket.disconnect`` (the client
      dropping) and blocks forever afterwards. That ends the bridge's
      control->ws reader so teardown runs the bare ``await websocket.close()``
      branch while the application state is still ``CONNECTED``.
    """
    dropped = {"value": False}
    disconnect_sent = asyncio.Event()

    async def receive() -> dict[str, Any]:
        if not disconnect_sent.is_set():
            disconnect_sent.set()
            # The client just dropped: report the disconnect *and* mark the
            # transport dead so every subsequent send fails like the real one.
            dropped["value"] = True
            return {"type": "websocket.disconnect", "code": 1006}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover

    async def send(message: dict[str, Any]) -> None:
        if dropped["value"] and message["type"] in {
            "websocket.send",
            "websocket.close",
        }:
            raise ClientDisconnected

    ws = WebSocket(
        {"type": "websocket", "headers": [], "query_string": b""},
        receive=receive,
        send=send,
    )
    # The attach handshake already completed in production: both sides CONNECTED.
    ws.application_state = WebSocketState.CONNECTED
    ws.client_state = WebSocketState.CONNECTED
    return ws


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
async def test_terminal_attach_teardown_swallows_client_disconnect() -> None:
    """Teardown close() on a dead client must not leak an unhandled exception.

    The terminal-attach bridge's teardown ``await websocket.close()`` raises
    ``WebSocketDisconnect(1006)`` when the client transport is already gone,
    and (on the buggy build) that escapes the bridge coroutine as the
    ``Exception in ASGI application`` that interrupts the session.
    """
    sock, target = await _new_private_tmux("sleep 30")
    # Let the pane paint its prompt so the on-attach seed send is exercised.
    await asyncio.sleep(0.3)
    ws = _dead_transport_websocket()

    leaked: BaseException | None = None
    try:
        await asyncio.wait_for(
            bridge_tmux_control_to_websocket(
                ws,
                socket_path=str(sock),
                tmux_target=target,
                read_only=False,
            ),
            timeout=15,
        )
    except asyncio.TimeoutError:  # pragma: no cover - guards a wedged bridge
        _kill_tmux(sock)
        pytest.fail("bridge_tmux_control_to_websocket did not return within 15s")
    except BaseException as exc:
        leaked = exc
    finally:
        _kill_tmux(sock)

    assert leaked is None, (
        "terminal-attach teardown leaked an unhandled exception when the client "
        f"transport was already gone: {type(leaked).__name__}: {leaked!r}. "
        "Expected the bridge to swallow the close()-time WebSocketDisconnect."
    )
