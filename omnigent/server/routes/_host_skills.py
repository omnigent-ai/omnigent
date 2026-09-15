"""Shared host skill discovery transport for both composers."""

from __future__ import annotations

import asyncio
import secrets

from fastapi import HTTPException

from omnigent.host.frames import HostSkillsFrame, HostSkillsResultFrame, encode_host_frame
from omnigent.server.host_registry import HostConnection, HostRegistry

_SKILLS_TIMEOUT_S = 15.0


async def request_host_skills(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    harness: str,
    path: str,
    session_id: str | None = None,
    agent_id: str | None = None,
    agent_version: str | None = None,
    sub_agent_name: str | None = None,
) -> HostSkillsResultFrame:
    """Request skill metadata over the host tunnel, with bounded waiting and cleanup."""
    request_id = secrets.token_hex(8)
    future: asyncio.Future[HostSkillsResultFrame] = asyncio.get_running_loop().create_future()
    host_conn.pending_skills[request_id] = future
    try:
        host_registry.send_text(
            host_conn,
            encode_host_frame(
                HostSkillsFrame(
                    request_id=request_id,
                    harness=harness,
                    path=path,
                    session_id=session_id,
                    agent_id=agent_id,
                    agent_version=agent_version,
                    sub_agent_name=sub_agent_name,
                )
            ),
        )
        return await asyncio.wait_for(future, timeout=_SKILLS_TIMEOUT_S)
    except ConnectionError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"host '{host_conn.host_id}' connection lost",
        ) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                f"host '{host_conn.host_id}' did not discover skills "
                f"within {_SKILLS_TIMEOUT_S:.0f}s"
            ),
        ) from exc
    finally:
        host_conn.pending_skills.pop(request_id, None)
