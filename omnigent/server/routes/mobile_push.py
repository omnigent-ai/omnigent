"""Authenticated, tenant-scoped FCM registration endpoints."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import Receive, Scope, Send

from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.mobile_push_config import FcmConfig
from omnigent.server.mobile_push_content import Platform
from omnigent.server.mobile_push_store import MobilePushStore
from omnigent.server.routes._auth_helpers import require_user


class DeviceRegistration(BaseModel):
    model_config = ConfigDict(extra="ignore")
    platform: Platform
    fcm_token: str = Field(min_length=1, max_length=4096, pattern=r"^\S+$", repr=False)
    firebase_project_id: str = Field(min_length=1, max_length=64)


InstallationId = Annotated[str, Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]


def create_mobile_push_router(
    store: MobilePushStore | None, config: FcmConfig | None, auth_provider: AuthProvider | None
) -> APIRouter:
    class GatedRoute(APIRoute):
        async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
            if store is None or config is None:
                raise HTTPException(404, "Mobile push is disabled")
            await super().handle(scope, receive, send)

    router = APIRouter(prefix="/v1/mobile-push", tags=["mobile-push"], route_class=GatedRoute)

    def identity(request: Request) -> str:
        user_id = require_user(request, auth_provider)
        if user_id is None:
            raise HTTPException(401, "Authentication required")
        if user_id == RESERVED_USER_LOCAL:
            raise HTTPException(403, "Mobile push requires a distinct authenticated user")
        return user_id

    @router.put("/devices/{installation_id}", status_code=204)
    async def register(
        installation_id: InstallationId, body: DeviceRegistration, request: Request
    ) -> Response:
        user_id = identity(request)
        assert config is not None and store is not None
        if body.firebase_project_id != config.project_id:
            raise HTTPException(409, "Firebase project does not match this server")
        await asyncio.to_thread(
            store.register,
            installation_id,
            user_id=user_id,
            platform=body.platform,
            fcm_token=body.fcm_token,
        )
        return Response(status_code=204)

    @router.delete("/devices/{installation_id}", status_code=204)
    async def unregister(installation_id: InstallationId, request: Request) -> Response:
        user_id = identity(request)
        assert store is not None
        await asyncio.to_thread(store.delete_device, installation_id, user_id)
        return Response(status_code=204)

    return router
