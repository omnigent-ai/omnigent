"""One round-trip helper for ``host.skills``.

Mirrors :mod:`omnigent.server.routes._host_model_options`: the request-id /
future / frame / timeout / cleanup shape lives here once so a caller only has
to decide how to handle failure.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

from omnigent.host.frames import HostSkillsFrame, encode_host_frame
from omnigent.server.host_registry import HostConnection, HostRegistry


async def request_host_skills(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    harness: str,
    path: str | None,
    skills_filter: str | list[str],
    timeout_s: float,
) -> dict[str, Any]:
    """
    Send a ``host.skills`` frame and await the host's result.

    :param host_registry: Registry used to enqueue the outbound frame.
    :param host_conn: Live host connection to query.
    :param harness: Harness id, e.g. ``"claude-sdk"``.
    :param path: Workspace directory to walk, or ``None`` for home scope only.
    :param skills_filter: The agent spec's ``skills:`` filter.
    :param timeout_s: Seconds to wait for the result frame.
    :returns: The result payload, e.g. ``{"status": "ok", "skills": [...]}``.
    :raises ConnectionError: The host connection dropped before the frame
        could be enqueued.
    :raises asyncio.TimeoutError: The host did not answer within *timeout_s*.
    """
    request_id = secrets.token_hex(8)
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    host_conn.pending_skills[request_id] = future
    frame = encode_host_frame(
        HostSkillsFrame(
            request_id=request_id,
            harness=harness,
            path=path,
            skills_filter=skills_filter,
        )
    )
    try:
        host_registry.send_text(host_conn, frame)
        return await asyncio.wait_for(future, timeout=timeout_s)
    finally:
        host_conn.pending_skills.pop(request_id, None)
