"""Shared fakes for the native sub-agent forwarder/server/router leak tests.

A native session carries a restart-forever transcript forwarder keyed by session
id in ``orchestration._AUTO_FORWARDER_TASKS``, and may own a native app-server
and a loopback subagent router. These helpers stand in for those live resources
so a reap can be asserted without launching real harnesses or sockets.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from pathlib import Path

from omnigent.runner import subagent_routing as routing
from omnigent.runner.native import orchestration as orch


class FakeAppServer:
    """Stand-in for a native app-server whose ``close()`` the reap must await."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeRouter:
    """Stand-in for a running subagent router whose ``close()`` the reap must call.

    Mirrors only the attributes teardown touches: ``close`` and the directory
    fields the dir-prune step reads. ``bridge_dir`` sits outside the router root
    so pruning is a no-op.
    """

    def __init__(self) -> None:
        self.closed = False
        self.advertised_dirs: set[Path] = set()
        self.bridge_dir = Path("/nonexistent") / f"router-{uuid.uuid4().hex}"

    def close(self) -> None:
        self.closed = True


async def _forever() -> None:
    await asyncio.sleep(3600)


def register_forwarder(session_id: str) -> asyncio.Task[object]:
    """Register a never-completing transcript forwarder for *session_id*.

    :param session_id: Native session id the forwarder is keyed under.
    :returns: The registered task so a test can assert on its cancellation.
    """
    task: asyncio.Task[object] = asyncio.ensure_future(_forever())
    task.set_name(f"claude-forwarder-{session_id}")
    orch._register_auto_forwarder_task(session_id, task)
    return task


def register_router(session_id: str) -> FakeRouter:
    """Install a :class:`FakeRouter` for *session_id* in the routing registry.

    :param session_id: Session id whose router a reap must shut down.
    :returns: The installed fake so a test can assert it was closed.
    """
    router = FakeRouter()
    routing._session_routers[session_id] = router  # type: ignore[assignment]
    return router


async def drain_forwarder(session_id: str, task: asyncio.Task[object]) -> None:
    """Drop and cancel a forwarder registered by :func:`register_forwarder`.

    :param session_id: Session id the forwarder is keyed under.
    :param task: The forwarder task to cancel and await.
    """
    orch._AUTO_FORWARDER_TASKS.pop(session_id, None)
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
