"""A uvicorn server that runs inside a process owning its own signals."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import uvicorn


class NoSignalServer(uvicorn.Server):
    """uvicorn Server that never installs process signal handlers.

    ``uvicorn.Server.serve()`` replaces the process's SIGINT/SIGTERM handlers
    for its whole lifetime when run on the main thread, which would steal them
    from the process that embeds it. Stop it explicitly (``should_exit``).
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        """No-op replacement for uvicorn's signal-handler swap."""
        yield
