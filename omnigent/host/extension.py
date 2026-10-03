"""Optional host-owned background work supplied by an installed package.

An extension is selected by ``OMNIGENT_HOST_EXTENSION`` and registered in the
``omnigent.host_extension`` entry-point group. The host starts it once, keeps
its child processes out of orphan cleanup, and stops it after a failed start or
on graceful in-process shutdown. SIGTERM, including that sent by
``omnigent host stop``, terminates the process without invoking ``stop()``.
Without a selected extension the host follows its normal lifecycle.

Entry-point discovery, import, and construction run synchronously before the
host event loop starts. Extension imports and constructors must be lightweight;
the startup budget applies to ``start()``, not package loading.
"""

from __future__ import annotations

import abc
import importlib.metadata
import logging
import os
from pathlib import Path

HOST_EXTENSION_ENV_VAR = "OMNIGENT_HOST_EXTENSION"
HOST_EXTENSION_ENTRY_POINT_GROUP = "omnigent.host_extension"

_logger = logging.getLogger(__name__)


class HostExtension(abc.ABC):
    """Lifecycle and child ownership contract for host-local services.

    ``start`` has a one-second host-connection budget; ``stop`` has a
    five-second shutdown budget. Both callbacks must keep the event loop
    responsive and honor cancellation at await points. On timeout, the host
    cancels the callback and continues; an in-process callback that blocks the
    event loop cannot be interrupted by an asyncio deadline.
    """

    @property
    @abc.abstractmethod
    def owned_pids(self) -> set[int]:
        """Return direct children whose exit status belongs to this extension."""

    @abc.abstractmethod
    async def start(self) -> None:
        """Start host-local work without blocking the host connection loop."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Release resources after a failed start or graceful host shutdown."""

    def before_runner_spawn(
        self,
        *,
        session_id: str | None,
        harness: str | None,
        workspace: Path,
        runner_id: str,
        server_url: str,
    ) -> None:
        """Optionally prepare a validated launch before its runner starts.

        Called on the runner-spawn worker thread. Implementations must return
        promptly and avoid network work. Exceptions are logged and ignored so
        optional preparation cannot prevent a normal runner launch.
        """
        del session_id, harness, workspace, runner_id, server_url


def load_host_extension() -> HostExtension | None:
    """Load the explicitly selected extension; failures leave the host usable."""
    name = os.environ.get(HOST_EXTENSION_ENV_VAR, "").strip()
    if not name:
        return None
    try:
        matches = [
            entry
            for entry in importlib.metadata.entry_points(group=HOST_EXTENSION_ENTRY_POINT_GROUP)
            if entry.name == name
        ]
        if len(matches) != 1:
            _logger.warning("Expected one host extension named %r; found %d", name, len(matches))
            return None
        extension = matches[0].load()()
        if not isinstance(extension, HostExtension):
            _logger.warning("Host extension %r does not implement HostExtension", name)
            return None
        return extension
    except Exception:
        _logger.exception("Could not load host extension %r", name)
        return None
