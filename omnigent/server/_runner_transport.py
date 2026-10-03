"""Server-side helpers for connecting to a local UDS runner."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from omnigent.util.socket_teardown_guard import install_socket_teardown_guard

# Type alias: callable returning an async context manager around a
# connected websockets client. Kept ``Any`` because the actual return
# type lives in :mod:`websockets.asyncio.client` and varies between
# library versions.
RunnerWSFactory = Callable[[str], Any]


def build_uds_runner(uds_path: str) -> tuple[httpx.AsyncClient, RunnerWSFactory]:
    """Build the HTTP client + WS factory for a UDS-attached runner.

    :param uds_path: Filesystem path to the runner's Unix socket.
    :returns: ``(client, ws_factory)``. The client uses httpx's UDS
        transport; the WS factory uses ``websockets.unix_connect``
        against the same path. Both target ``http://runner`` /
        ``ws://runner`` as cosmetic base hosts because the UDS
        transport ignores the host portion.
    """
    install_socket_teardown_guard()
    client = httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=uds_path),
        base_url="http://runner",
        timeout=httpx.Timeout(5.0, read=None),
    )

    # Imported lazily so ``import omnigent.server`` doesn't pay the
    # websockets-library cost when the runner attach feature isn't used.
    from websockets.asyncio.client import unix_connect as _ws_unix_connect

    def ws_factory(path: str) -> Any:
        return _ws_unix_connect(
            path=uds_path,
            uri=f"ws://runner{path}",
            open_timeout=10,
        )

    return client, ws_factory
