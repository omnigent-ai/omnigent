"""One round-trip helper for ``host.provider_op``.

Only caller today is the ``/v1/hosts/{id}/providers*`` and
``/v1/hosts/{id}/agents/{name}/pin`` route group, which turns a failure
into an HTTP error. The request-id / future / frame / timeout / cleanup
shape lives here once, mirroring
:mod:`omnigent.server.routes._host_model_options`.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

from omnigent.host.frames import (
    HostProviderOpFrame,
    encode_host_frame,
)
from omnigent.server.host_registry import HostConnection, HostRegistry


async def request_host_provider_op(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    op: str,
    params: dict[str, Any],
    timeout_s: float,
) -> dict[str, Any]:
    """
    Send a ``host.provider_op`` frame and await the host's result.

    :param host_registry: Registry used to enqueue the outbound frame.
    :param host_conn: Live host connection to run the operation on.
    :param op: One of the provider-op names the host's frame handler
        dispatches (``providers_list``, ``provider_upsert``,
        ``provider_delete``, ``provider_test``, ``agents_list``,
        ``agent_pin_set``, ``agent_pin_clear``).
    :param params: Op-specific JSON arguments.
    :param timeout_s: Seconds to wait for the result frame.
    :returns: The result payload (``status``/``payload``/``error`` shape).
    :raises ConnectionError: The host connection dropped before the frame
        could be enqueued.
    :raises asyncio.TimeoutError: The host did not answer within
        *timeout_s*.
    """
    request_id = secrets.token_hex(8)
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    host_conn.pending_provider_ops[request_id] = future
    frame = encode_host_frame(
        HostProviderOpFrame(request_id=request_id, op=op, params=params)
    )
    try:
        host_registry.send_text(host_conn, frame)
        return await asyncio.wait_for(future, timeout=timeout_s)
    finally:
        host_conn.pending_provider_ops.pop(request_id, None)
