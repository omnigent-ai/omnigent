"""Personal session communication preference and bounded Canvas activity."""

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.session_messaging import messaging_service


class MessagingSettingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    expected_version: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


def create_session_messaging_router() -> APIRouter:
    router = APIRouter()

    @router.get("/session-communication/settings")
    async def read_setting(request: Request) -> dict[str, Any]:
        service = messaging_service(request)
        return await asyncio.to_thread(service.setting, service.owner_client(request))

    @router.put("/session-communication/settings")
    async def change_setting(request: Request, body: MessagingSettingInput) -> dict[str, Any]:
        service = messaging_service(request)
        return await asyncio.to_thread(
            service.setting,
            service.owner_client(request),
            enabled=body.enabled,
            expected_version=body.expected_version,
        )

    @router.get("/sessions/{session_id}/communication-activity")
    async def activity(request: Request, session_id: str) -> dict[str, Any]:
        service = messaging_service(request)
        user = require_user(request, service.auth)
        if not user:
            return {"data": []}
        return {"data": await asyncio.to_thread(service.activity, session_id, user)}

    return router
