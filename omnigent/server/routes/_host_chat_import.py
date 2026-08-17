"""One round-trip helper for native chat discovery and loading."""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

from omnigent.host.frames import HostChatImportFrame, encode_host_frame
from omnigent.server.host_registry import HostConnection, HostRegistry


async def request_host_chat_import(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    source: str,
    session_id: str | None,
    limit: int,
    timeout_s: float,
) -> dict[str, Any]:
    """Ask a host to discover or normalize a local native chat."""
    request_id = secrets.token_hex(8)
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    host_conn.pending_chat_imports[request_id] = future
    frame = encode_host_frame(
        HostChatImportFrame(
            request_id=request_id,
            source=source,
            session_id=session_id,
            limit=limit,
        )
    )
    try:
        host_registry.send_text(host_conn, frame)
        return await asyncio.wait_for(future, timeout=timeout_s)
    finally:
        host_conn.pending_chat_imports.pop(request_id, None)
