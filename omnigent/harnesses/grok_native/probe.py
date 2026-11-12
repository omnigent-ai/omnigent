"""Bind-readiness probe for ``grok agent serve``.

Kept apart from :mod:`.serve` (which owns the per-session secret) so the
credential handling and the network probe never share a module.
"""

from __future__ import annotations

import asyncio

_BIND_HOST = "127.0.0.1"


async def wait_for_grok_serve(
    port: int,
    *,
    timeout_s: float = 30.0,
    poll_s: float = 0.1,
) -> None:
    """Wait until the serve port accepts a TCP connection.

    The server prints its WS URL once bound; a connectable port is the cheap
    readiness signal (the ACP ``initialize`` handshake happens on the client
    side of the WS connect, not at bind time).

    :raises TimeoutError: when the port never comes up (e.g. crashed spawn).
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        try:
            _reader, writer = await asyncio.open_connection(_BIND_HOST, port)
        except OSError:
            await asyncio.sleep(poll_s)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise TimeoutError(f"grok agent serve did not bind {port} within {timeout_s}s")


__all__ = ["wait_for_grok_serve"]
