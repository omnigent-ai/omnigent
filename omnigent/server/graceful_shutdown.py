"""Graceful ASGI-server shutdown shared by every launcher.

``omnigent server`` and the deploy entrypoints must shut down the same way on
``SIGTERM``: drain in-flight SSE session streams, then bound uvicorn's graceful
wait so one held stream can't keep the process alive until the orchestrator
``SIGKILL``s it. This shared home sits next to
:func:`omnigent.util.tunnel_limits.uvicorn_tunnel_kwargs` so the launchers can't
drift from ``omnigent server``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import uvicorn.server

if TYPE_CHECKING:
    import socket

# Seconds uvicorn waits for active connections after SIGTERM before
# force-closing them. SSE streams drain themselves in
# ShutdownSignalingServer.shutdown(), so this window mainly covers WebSocket
# tunnels. Overridable via OMNIGENT_SERVER_SHUTDOWN_TIMEOUT_S.
SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_S_DEFAULT = 5
SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_S = int(
    os.environ.get(
        "OMNIGENT_SERVER_SHUTDOWN_TIMEOUT_S",
        str(SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_S_DEFAULT),
    )
)


class ShutdownSignalingServer(uvicorn.server.Server):
    """uvicorn.Server that drains SSE subscribers before the graceful wait.

    ``Server.shutdown()`` runs in order: (1) close sockets, (2)
    ``asyncio.wait_for(_wait_tasks_to_complete(), timeout=…)``, (3) force-cancel
    remaining tasks on timeout, (4) run the ASGI lifespan shutdown. The lifespan
    ``finally`` (step 4) is too late: SSE generators waiting on a heartbeat are
    already force-cancelled at step 3 (spurious ``CancelledError``). Draining
    before step 2 lets them exit cleanly within the graceful window.
    """

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        import asyncio

        from omnigent.runtime import session_stream
        from omnigent.server import shutdown_state

        # The runner tunnels close next; their disconnect handlers must
        # read that loss as ours, not as the runners dying.
        shutdown_state.mark_server_shutting_down()
        session_stream.shutdown_all()
        # Yield so streams consume the sentinel and exit before transports
        # close; without this pause the generators write to an already-closing
        # transport and connections linger past the graceful window.
        await asyncio.sleep(0)
        await super().shutdown(sockets)
