"""The host's workspace helper: started on demand, stopped when idle.

One :class:`WorkspaceHelperProxy` per host daemon starts the helper on the
first call that needs it, and serves every route's calls through it. Each
helper process is a *generation*: its connection, capabilities and the
workspaces it opened belong to it alone, because the helper numbers
workspaces from scratch each time it starts. When a generation ends (crash,
idle stop, shutdown) it is retired at once and every later call starts a new
one, so an id from an old helper can never name a workspace in a new one.

Calls hold a lease on their workspace until they finish, streams included,
and only unleased workspaces are closed to stay near the limit. Starting
fails over to the Python reader: three failed starts in a row, or a helper
this host can't speak to, turn the helper off for the daemon's life.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from connectrpc.protocol import ProtocolType
from pyqwest import Client

from omnigent.api.workspace.v1alpha1.workspace_connect import WorkspaceServiceClient
from omnigent.api.workspace.v1alpha1.workspace_helper_connect import (
    WorkspaceHelperServiceClient,
)
from omnigent.api.workspace.v1alpha1.workspace_helper_pb2 import (
    CloseWorkspaceRequest,
    OpenWorkspaceRequest,
)
from omnigent.host.workspace_helper.process import (
    HelperIncompatible,
    HelperProcess,
    HelperUnavailable,
    find_helper_binary,
    start_helper,
)

_logger = logging.getLogger(__name__)

# The request header that names the workspace a WorkspaceService call reads.
WORKSPACE_HEADER = "omnigent-workspace-id"
# Workspaces kept open per helper. A soft limit: open calls keep theirs.
MAX_OPEN_WORKSPACES = 16
IDLE_STOP_S = 10 * 60.0
MAX_START_FAILURES = 3
_OPEN_TIMEOUT_MS = 10_000
_CLOSE_TIMEOUT_MS = 1_000
# Only the address's path reaches the helper; the transport ignores the host.
_HELPER_ADDRESS = "http://workspace-helper"

StartHelper = Callable[[str], Awaitable[HelperProcess]]


@dataclass
class _Workspace:
    """A workspace one generation opened, or is opening, and its open calls."""

    root: Path
    opened: asyncio.Task[str]
    leases: int = 0


@dataclass(frozen=True)
class HelperCall:
    """What one call needs to reach its workspace in the helper.

    :param client: The helper's WorkspaceService.
    :param headers: Name the workspace; send them with the call.
    """

    client: WorkspaceServiceClient
    headers: dict[str, str]


@dataclass
class _Generation:
    """One helper process and everything that is only valid while it runs."""

    helper: HelperProcess
    number: int
    workspace_client: WorkspaceServiceClient
    control_client: WorkspaceHelperServiceClient
    workspaces: OrderedDict[Path, _Workspace] = field(default_factory=OrderedDict)
    active_calls: int = 0
    last_active: float = field(default_factory=time.monotonic)
    retired: bool = False
    tasks: set[asyncio.Task[object]] = field(default_factory=set)

    @classmethod
    def connect(cls, helper: HelperProcess, number: int) -> _Generation:
        http = Client(helper.transport)
        return cls(
            helper=helper,
            number=number,
            workspace_client=WorkspaceServiceClient(
                _HELPER_ADDRESS, protocol=ProtocolType.GRPC, http_client=http
            ),
            control_client=WorkspaceHelperServiceClient(
                _HELPER_ADDRESS, protocol=ProtocolType.GRPC, http_client=http
            ),
        )

    def spawn(self, coroutine: Awaitable[object], name: str) -> asyncio.Task[object]:
        task: asyncio.Task[object] = asyncio.ensure_future(coroutine)
        task.set_name(name)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def acquire(self, root: Path) -> _Workspace:
        """Lease ``root``'s workspace, opening it once for concurrent callers."""
        workspace = self.workspaces.get(root)
        if workspace is None:
            opened = asyncio.ensure_future(self._open(root))
            workspace = _Workspace(root=root, opened=opened)
            self.workspaces[root] = workspace
        self.workspaces.move_to_end(root)
        workspace.leases += 1
        try:
            # One caller giving up must not cancel the open the others wait for.
            await asyncio.shield(workspace.opened)
        except BaseException as exc:
            self.release(workspace)
            if workspace.opened.done() and self.workspaces.get(root) is workspace:
                # A failed open is forgotten, so the next call tries again.
                if workspace.opened.cancelled() or workspace.opened.exception() is not None:
                    del self.workspaces[root]
            if not isinstance(exc, Exception):
                raise
            if self.retired:
                raise HelperUnavailable("The workspace helper stopped") from exc
            # The host checked the root already, so a failed open is the helper's fault.
            raise HelperUnavailable(f"The workspace helper couldn't open {root}: {exc}") from exc
        if self.retired:
            self.release(workspace)
            raise HelperUnavailable("The workspace helper stopped")
        return workspace

    def release(self, workspace: _Workspace) -> None:
        workspace.leases -= 1
        self.last_active = time.monotonic()
        self._evict()

    def _evict(self) -> None:
        """Close the least recently used unleased workspaces beyond the limit."""
        excess = len(self.workspaces) - MAX_OPEN_WORKSPACES
        for root, workspace in list(self.workspaces.items()):
            if excess <= 0 or self.retired:
                return
            opened = workspace.opened
            if workspace.leases or not opened.done() or opened.cancelled():
                continue
            del self.workspaces[root]
            excess -= 1
            if opened.exception() is None:
                self.spawn(self._close(opened.result()), name="workspace-helper-close")

    async def _open(self, root: Path) -> str:
        response = await self.control_client.open_workspace(
            OpenWorkspaceRequest(root=str(root)), timeout_ms=_OPEN_TIMEOUT_MS
        )
        return response.workspace_id

    async def _close(self, workspace_id: str) -> None:
        with contextlib.suppress(Exception):
            await self.control_client.close_workspace(
                CloseWorkspaceRequest(workspace_id=workspace_id), timeout_ms=_CLOSE_TIMEOUT_MS
            )

    async def close_all_workspaces(self) -> None:
        opened = [w.opened for w in self.workspaces.values()]
        self.workspaces.clear()
        ids = [
            task.result()
            for task in opened
            if task.done() and not task.cancelled() and task.exception() is None
        ]
        await asyncio.gather(*(self._close(workspace_id) for workspace_id in ids))

    async def stop(self) -> None:
        """Release everything this generation holds, each step bounded."""
        for task in list(self.tasks):
            task.cancel()
        opens = [workspace.opened for workspace in self.workspaces.values()]
        await self.helper.close(self.close_all_workspaces)
        # Opens still in flight fail once the connection closes; their callers see that.
        await asyncio.gather(*self.tasks, *opens, return_exceptions=True)


class WorkspaceHelperProxy:
    """Starts the workspace helper on demand and hands out calls to it.

    :param binary: The helper to run, or ``None`` for none; see
        :func:`~omnigent.host.workspace_helper.process.find_helper_binary`.
    :param idle_stop_s: Stop the helper after this long without calls.
    :param start: Starts one helper process; tests replace it.
    """

    def __init__(
        self,
        binary: str | None,
        *,
        idle_stop_s: float = IDLE_STOP_S,
        start: StartHelper = start_helper,
    ) -> None:
        self._binary = binary
        self._idle_stop_s = idle_stop_s
        self._start_helper = start
        self._generation: _Generation | None = None
        self._starting: asyncio.Task[_Generation | None] | None = None
        self._generations = 0
        self._failures = 0
        self._disabled: str | None = None if binary else "no workspace helper is installed"
        self._closed = False
        self._stopping: set[asyncio.Task[None]] = set()

    @classmethod
    def from_environment(cls) -> WorkspaceHelperProxy:
        """The proxy for this host's installed or configured helper, if any."""
        return cls(find_helper_binary())

    @property
    def enabled(self) -> bool:
        """Whether a helper may still be started."""
        return self._disabled is None and not self._closed

    async def capabilities(self) -> frozenset[str]:
        """What the helper serves, starting it if needed; empty without one."""
        generation = await self._current()
        return generation.helper.capabilities if generation else frozenset()

    @asynccontextmanager
    async def call(self, root: Path) -> AsyncIterator[HelperCall]:
        """Lease ``root``'s workspace in the helper for one call or stream.

        :raises HelperUnavailable: When no helper runs now. A call that fails
            because the helper stopped retires its generation.
        """
        generation = await self._current()
        if generation is None:
            raise HelperUnavailable("No workspace helper is running")
        generation.active_calls += 1
        try:
            workspace = await generation.acquire(root)
            try:
                yield HelperCall(
                    client=generation.workspace_client,
                    headers={WORKSPACE_HEADER: workspace.opened.result()},
                )
            finally:
                generation.release(workspace)
        except BaseException:
            if not generation.helper.running:
                self._retire(generation, "stopped")
            raise
        finally:
            generation.active_calls -= 1
            generation.last_active = time.monotonic()

    async def close(self) -> None:
        """Stop the helper for good, for the daemon's shutdown."""
        self._closed = True
        if self._starting is not None:
            self._starting.cancel()
            with contextlib.suppress(BaseException):
                await self._starting
        if self._generation is not None:
            self._retire(self._generation, "shutdown")
        while self._stopping:
            await asyncio.gather(*self._stopping, return_exceptions=True)

    async def _current(self) -> _Generation | None:
        generation = self._generation
        if generation is not None and generation.helper.running:
            return generation
        if generation is not None:
            self._retire(generation, "stopped")
        if not self.enabled:
            return None
        if self._starting is None:
            self._starting = asyncio.ensure_future(self._start())
        # Concurrent callers share one start; one caller cancelling can't stop it.
        return await asyncio.shield(self._starting)

    async def _start(self) -> _Generation | None:
        assert self._binary is not None
        try:
            helper = await self._start_helper(self._binary)
        except HelperIncompatible as exc:
            self._disable(str(exc))
            return None
        except HelperUnavailable as exc:
            self._failures += 1
            if self._failures >= MAX_START_FAILURES:
                self._disable(f"{exc} ({self._failures} times)")
            else:
                _logger.info("Workspace helper start failed: %s", exc)
            return None
        finally:
            self._starting = None
        if self._closed:
            await helper.close()
            return None
        self._failures = 0
        self._generations += 1
        generation = _Generation.connect(helper, self._generations)
        self._generation = generation
        generation.spawn(self._watch(generation), name="workspace-helper-watch")
        _logger.info(
            "Workspace helper %s started (generation %d)", helper.version, generation.number
        )
        return generation

    async def _watch(self, generation: _Generation) -> None:
        """Retire ``generation`` when its helper stops, or after it idles."""
        stopped = asyncio.ensure_future(generation.helper.wait_stopped())
        try:
            while not stopped.done():
                idle_for = time.monotonic() - generation.last_active
                if generation.active_calls == 0 and idle_for >= self._idle_stop_s:
                    self._retire(generation, "idle")
                    return
                remaining = max(self._idle_stop_s - idle_for, 0.05)
                await asyncio.wait({stopped}, timeout=remaining)
            self._retire(generation, "stopped")
        finally:
            stopped.cancel()

    def _retire(self, generation: _Generation, reason: str) -> None:
        """End ``generation`` now; its cleanup runs in the background, bounded."""
        if generation.retired:
            return
        generation.retired = True
        if self._generation is generation:
            self._generation = None
        _logger.info("Workspace helper generation %d ended: %s", generation.number, reason)
        # Its own call or watcher may be what retires it; cleanup must outlive both.
        stopping = asyncio.get_running_loop().create_task(
            generation.stop(), name="workspace-helper-stop"
        )
        self._stopping.add(stopping)
        stopping.add_done_callback(self._stopping.discard)

    def _disable(self, reason: str) -> None:
        self._disabled = reason
        _logger.warning("Workspace helper turned off for this host's run: %s", reason)
