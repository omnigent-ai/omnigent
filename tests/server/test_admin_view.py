from __future__ import annotations

import asyncio

import pytest

from omnigent.server.auth import AdminViewMiddleware, effective_admin
from omnigent.server.permissions import resolved_allows, resolved_level
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "websocket"])
async def test_admin_view_is_scoped_to_each_connection(transport: str, db_uri: str) -> None:
    store = SqlAlchemyPermissionStore(db_uri)
    store.ensure_user("admin", is_admin=True)
    access = store.resolve_access("admin", "0" * 32)

    async def connection(scope, receive, send):
        await asyncio.sleep(0)
        enabled = scope["expected"]
        assert store.is_admin("admin")
        assert store.resolve_access("admin", "0" * 32) == access
        assert resolved_allows(access, 1) is enabled
        assert resolved_level(access) == (4 if enabled else None)
        assert await asyncio.to_thread(effective_admin, True) is enabled
        assert not effective_admin(False)

    middleware = AdminViewMiddleware(connection)
    await asyncio.gather(
        *(
            middleware({"type": transport, "headers": headers, "expected": enabled}, None, None)
            for headers, enabled in [
                ([], True),
                ([(b"cookie", b"omnigent_admin_view=off")], False),
                ([(b"cookie", b"omnigent_admin_view=on")], True),
            ]
        )
    )
    assert effective_admin(True)
