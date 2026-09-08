"""Per-user settings shared across authentication modes."""

from __future__ import annotations

import asyncio

from omnigent.stores.permission_store import PermissionStore


async def background_session_titles_enabled_for_user(
    permission_store: PermissionStore | None,
    user_id: str | None,
) -> bool:
    """Resolve background-title enablement, defaulting on without a stored opt-out."""
    if permission_store is None or user_id is None:
        return True
    return await asyncio.to_thread(
        permission_store.get_background_session_titles_enabled,
        user_id,
    )
