"""Guard that ``ws_bridge`` keeps fastapi off the client CLI import path.

``omnigent/terminals/ws_bridge.py`` supplies tmux helpers that the client
CLI reaches through ``omnigent.claude_native``, but only the server half of
the bridge talks to fastapi. Importing fastapi at module scope therefore
charged every ``omnigent claude`` launch for a dependency it never uses.

The module-scope checks must run in a subprocess: pytest imports the server
routes, so ``fastapi`` is already in this interpreter's ``sys.modules`` and an
in-process assertion would be vacuous.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from omnigent.terminals.ws_bridge import _forward_pty_to_ws

_PROBE = """
import sys
import {module}
print("LOADED" if "fastapi" in sys.modules else "ABSENT")
"""


def _fastapi_state_after_importing(module: str) -> str:
    """Report whether importing *module* in a fresh interpreter loads fastapi."""
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE.format(module=module)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, f"probe failed for {module}: {proc.stderr}"
    return proc.stdout.strip()


@pytest.mark.parametrize(
    "module",
    ["omnigent.terminals.ws_bridge", "omnigent.claude_native"],
)
def test_import_does_not_pull_in_fastapi(module: str) -> None:
    """Neither the bridge nor the CLI path that reaches it may load fastapi."""
    assert _fastapi_state_after_importing(module) == "ABSENT", (
        f"importing {module} loaded fastapi; the deferred import regressed and "
        f"every 'omnigent claude' launch pays ~190ms for it again"
    )


class _DisconnectingWebSocket:
    """WebSocket fake whose first ``send_bytes`` raises ``WebSocketDisconnect``."""

    def __init__(self) -> None:
        self.calls = 0

    async def send_bytes(self, data: bytes) -> None:
        """Fail the way a client that vanished mid-frame does."""
        from fastapi import WebSocketDisconnect

        self.calls += 1
        raise WebSocketDisconnect(code=1006)


@pytest.mark.asyncio
async def test_forward_pty_to_ws_still_catches_websocket_disconnect() -> None:
    """The deferred import must resolve at runtime, not just at type-check time.

    ``WebSocketDisconnect`` moved from module scope into the function body, so
    a name error there would surface only when a client actually drops — this
    exercises that path.
    """
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    queue.put_nowait(b"payload")

    ws = _DisconnectingWebSocket()
    await asyncio.wait_for(
        _forward_pty_to_ws(ws, queue),  # type: ignore[arg-type]  # structural WS fake
        timeout=5.0,
    )

    assert ws.calls == 1, "the disconnect should end forwarding after one attempt"
