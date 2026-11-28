"""Live public ceilings apply outside cached raw grants and across task boundaries."""

from __future__ import annotations

import asyncio

import pytest

from omnigent.entities import ResolvedAccess
from omnigent.server.permissions import resolved_allows, resolved_level
from omnigent.server.sharing_settings import (
    PublicSharingMaxLevel,
    PublicSharingPolicyMiddleware,
    effective_public_level,
)


@pytest.mark.asyncio
async def test_open_connection_rechecks_ceiling_and_restores_scope() -> None:
    ceiling = PublicSharingMaxLevel.EDIT
    raw = ResolvedAccess(is_admin=False, user_grant_level=1, public_grant_level=4)

    async def connection(scope, receive, send):
        nonlocal ceiling
        assert resolved_level(raw) == 2
        assert resolved_allows(raw, 2)
        assert not resolved_allows(raw, 3)
        assert await asyncio.to_thread(effective_public_level, 4) == 2
        ceiling = PublicSharingMaxLevel.READ
        assert resolved_level(raw) == 1
        assert not resolved_allows(raw, 2)
        assert await asyncio.to_thread(effective_public_level, 4) == 1

    middleware = PublicSharingPolicyMiddleware(connection, max_level=lambda: ceiling)
    await middleware({"type": "websocket"}, None, None)
    assert effective_public_level(4) == 1


@pytest.mark.asyncio
async def test_concurrent_applications_keep_independent_public_ceilings() -> None:
    async def connection(scope, receive, send):
        await asyncio.sleep(0)
        assert effective_public_level(4) == scope["expected"]
        assert await asyncio.to_thread(effective_public_level, 4) == scope["expected"]

    read = PublicSharingPolicyMiddleware(connection, lambda: PublicSharingMaxLevel.READ)
    edit = PublicSharingPolicyMiddleware(connection, lambda: PublicSharingMaxLevel.EDIT)
    await asyncio.gather(
        read({"type": "http", "expected": 1}, None, None),
        edit({"type": "http", "expected": 2}, None, None),
    )
