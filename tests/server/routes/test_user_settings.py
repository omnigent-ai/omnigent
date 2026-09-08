from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from omnigent.server.auth import AuthProvider
from omnigent.server.routes.user_settings import create_user_settings_router
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

pytestmark = pytest.mark.asyncio


class _AuthProvider(AuthProvider):
    def get_user_id(self, request: object) -> str | None:
        return "alice@example.com"


async def test_user_settings_default_on_and_round_trip(db_uri: str) -> None:
    store = SqlAlchemyPermissionStore(db_uri)
    store.ensure_user("alice@example.com")
    app = FastAPI()
    app.include_router(
        create_user_settings_router(_AuthProvider(), store),
        prefix="/v1",
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        initial = await client.get("/v1/user-settings")
        disabled = await client.put(
            "/v1/user-settings",
            json={"background_session_titles_enabled": False},
        )
        persisted = await client.get("/v1/user-settings")

    assert initial.status_code == 200
    assert initial.json() == {"background_session_titles_enabled": True}
    assert disabled.status_code == 200
    assert disabled.json() == {"background_session_titles_enabled": False}
    assert persisted.json() == {"background_session_titles_enabled": False}
