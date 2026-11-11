"""Tests for the grant writes shared by the Share dialog and automation run sharing."""

from __future__ import annotations

import pytest

from omnigent.server.auth import LEVEL_EDIT, LEVEL_MANAGE, LEVEL_READ
from omnigent.server.session_sharing import write_session_grant
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


def test_kept_higher_grant_survives_an_upgrade_after_the_read(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An automation share never lowers a grant the owner raises while it is writing."""
    store = SqlAlchemyPermissionStore(db_uri)
    store.ensure_user("bob@example.com")
    session_id = SqlAlchemyConversationStore(db_uri).create_conversation().id
    store.grant("bob@example.com", session_id, LEVEL_READ)
    read = store.get

    def read_then_owner_upgrades(user_id: str, conversation_id: str):
        seen = read(user_id, conversation_id)
        store.grant(user_id, conversation_id, LEVEL_MANAGE)
        return seen

    monkeypatch.setattr(store, "get", read_then_owner_upgrades)
    kept = write_session_grant(store, "bob@example.com", session_id, LEVEL_EDIT, keep_higher=True)

    assert kept is None
    assert read("bob@example.com", session_id).level == LEVEL_MANAGE  # type: ignore[union-attr]
