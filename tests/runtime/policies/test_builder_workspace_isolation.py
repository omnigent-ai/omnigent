"""The policy builder's per-session caches must not leak across workspaces.

Conversation ids can collide across workspaces (imported sessions), so the
session-owner cache — read on every tool-call engine build — must resolve each
workspace's owner independently, or one tenant's budget/owner would be
evaluated for another's session. See OMNI-7361.
"""

from __future__ import annotations

from omnigent.db.db_models import workspace_scope
from omnigent.runtime.policies import builder
from omnigent.runtime.policies.builder import _resolve_session_owner_cached


class _FakeOwnerStore:
    """Minimal stand-in exposing only ``get_session_owner`` and a call count."""

    def __init__(self, owner: str | None) -> None:
        self.owner = owner
        self.calls = 0

    def get_session_owner(self, conversation_id: str) -> str | None:
        self.calls += 1
        return self.owner


def test_session_owner_cache_is_workspace_isolated() -> None:
    """The same conversation_id resolves distinct owners per workspace."""
    builder._SESSION_OWNER_CACHE.clear()
    try:
        ws1 = _FakeOwnerStore("alice")
        ws2 = _FakeOwnerStore("bob")
        with workspace_scope(1):
            assert _resolve_session_owner_cached("conv_shared", ws1) == "alice"  # type: ignore[arg-type]
            # Second lookup is served from cache — the store is not re-queried.
            assert _resolve_session_owner_cached("conv_shared", ws1) == "alice"  # type: ignore[arg-type]
            assert ws1.calls == 1
        with workspace_scope(2):
            # Same id, different workspace: workspace 1's cached "alice" must
            # NOT be returned — the store is queried and yields "bob".
            assert _resolve_session_owner_cached("conv_shared", ws2) == "bob"  # type: ignore[arg-type]
            assert ws2.calls == 1
        with workspace_scope(1):
            # Workspace 1's entry is intact and independent of workspace 2's.
            assert _resolve_session_owner_cached("conv_shared", ws1) == "alice"  # type: ignore[arg-type]
            assert ws1.calls == 1
    finally:
        builder._SESSION_OWNER_CACHE.clear()
