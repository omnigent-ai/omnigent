"""Owner-only, transient access to a host's SKILL.md bodies."""

from __future__ import annotations

import asyncio
import secrets

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from omnigent.harness_aliases import canonicalize_harness
from omnigent.host.frames import (
    CAP_SKILL_CONTENT,
    HostSkillContentFrame,
    HostSkillContentResultFrame,
    encode_host_frame,
)
from omnigent.host.skill_content import MAX_SKILL_CONTENT_BYTES
from omnigent.server.auth import AuthProvider
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._host_launch import host_absent_error, resolve_host_owner
from omnigent.stores.host_store import HostStore

_SKILL_CONTENT_TIMEOUT_S = 15.0


class SkillContentResponse(BaseModel):
    model_config = ConfigDict(strict=True)

    name: str = Field(max_length=1024)
    description: str = Field(
        max_length=8192, description="Skill description, capped at 8192 characters."
    )
    content: str = Field(max_length=MAX_SKILL_CONTENT_BYTES)
    truncated: bool = Field(
        description="Whether the SKILL.md body exceeded the 256 KiB UTF-8 limit."
    )

    @field_validator("content")
    @classmethod
    def bounded_content(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_SKILL_CONTENT_BYTES:
            raise ValueError("skill content exceeds byte limit")
        return value


def create_skill_content_router(
    host_registry: HostRegistry,
    host_store: HostStore,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/hosts/{host_id}/harnesses/{harness}/skills/{name:path}")
    async def get_skill_content(
        request: Request,
        response: Response,
        host_id: str,
        harness: str,
        name: str,
    ) -> SkillContentResponse:
        user_id = require_user(request, auth_provider)
        host = await asyncio.to_thread(
            resolve_host_owner,
            user_id=user_id,
            host_id=host_id,
            host_store=host_store,
        )
        harness = canonicalize_harness(harness) or harness
        if harness not in {"claude-native", "codex-native", "cursor-native"}:
            raise HTTPException(
                status_code=404, detail="skill contents unavailable for this harness"
            )
        conn = host_registry.get(host_id)
        if conn is None:
            raise host_absent_error(host)
        if CAP_SKILL_CONTENT not in conn.hello.capabilities:
            raise HTTPException(status_code=501, detail="update the host to see skill contents")
        result = await request_host_skill_content(
            host_registry=host_registry,
            host_conn=conn,
            harness=harness,
            name=name,
        )
        if result.status != "ok" or result.skill is None:
            raise HTTPException(status_code=502, detail="host skill content lookup failed")
        try:
            skill = SkillContentResponse.model_validate(result.skill)
            if skill.name != name:
                raise ValueError("wrong skill")
        except (ValidationError, ValueError):
            raise HTTPException(
                status_code=502, detail="host sent a malformed skill content reply"
            ) from None
        response.headers["Cache-Control"] = "no-store"
        return skill

    return router


async def request_host_skill_content(
    *, host_registry: HostRegistry, host_conn: HostConnection, harness: str, name: str
) -> HostSkillContentResultFrame:
    """Request one skill's contents over the host tunnel, with bounded waiting and cleanup."""
    request_id = secrets.token_hex(8)
    future: asyncio.Future[HostSkillContentResultFrame] = (
        asyncio.get_running_loop().create_future()
    )
    host_conn.pending_skill_content[request_id] = future
    try:
        host_registry.send_text(
            host_conn,
            encode_host_frame(
                HostSkillContentFrame(request_id=request_id, harness=harness, name=name)
            ),
        )
        return await asyncio.wait_for(future, timeout=_SKILL_CONTENT_TIMEOUT_S)
    except ConnectionError as exc:
        raise HTTPException(
            status_code=502, detail=f"host '{host_conn.host_id}' connection lost"
        ) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                f"host '{host_conn.host_id}' did not report skill contents "
                f"within {_SKILL_CONTENT_TIMEOUT_S:.0f}s"
            ),
        ) from exc
    finally:
        host_conn.pending_skill_content.pop(request_id, None)
