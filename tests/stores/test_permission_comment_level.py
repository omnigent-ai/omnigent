"""Stored comment grants (level 5) never pass edit-level checks or read as owner."""

from __future__ import annotations

import pytest

from omnigent.server.auth import (
    LEVEL_COMMENT,
    LEVEL_EDIT,
    LEVEL_MANAGE,
    LEVEL_OWNER,
    LEVEL_READ,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_ALLOWED = (LEVEL_READ, LEVEL_COMMENT)
_DENIED = (LEVEL_EDIT, LEVEL_MANAGE, LEVEL_OWNER)


@pytest.mark.parametrize(
    "required,expected", [(lvl, True) for lvl in _ALLOWED] + [(lvl, False) for lvl in _DENIED]
)
def test_store_check_access_ranks_comment(db_uri: str, required: int, expected: bool) -> None:
    store = SqlAlchemyPermissionStore(db_uri)
    conv_id = SqlAlchemyConversationStore(db_uri).create_conversation().id
    store.ensure_user("commenter@example.com")
    store.grant("commenter@example.com", conv_id, LEVEL_COMMENT)
    assert store.check_access("commenter@example.com", conv_id, required) is expected


def _session_with(db_uri: str, grants: dict[str, int]) -> tuple[SqlAlchemyConversationStore, str]:
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    conv_id = conversations.create_conversation().id
    for user_id, level in grants.items():
        permissions.ensure_user(user_id)
        permissions.grant(user_id, conv_id, level)
    return conversations, conv_id


def test_commenter_is_never_reported_as_owner(db_uri: str) -> None:
    conversations, conv_id = _session_with(
        db_uri, {"owner@example.com": LEVEL_OWNER, "commenter@example.com": LEVEL_COMMENT}
    )
    assert conversations.get_session_owner(conv_id) == "owner@example.com"
    assert conversations.get_session_owner(conv_id, owner_only=True) == "owner@example.com"


def test_ownerless_fallback_prefers_editor_over_commenter(db_uri: str) -> None:
    conversations, conv_id = _session_with(
        db_uri, {"editor@example.com": LEVEL_EDIT, "commenter@example.com": LEVEL_COMMENT}
    )
    assert conversations.get_session_owner(conv_id) == "editor@example.com"
    assert conversations.get_session_owner(conv_id, owner_only=True) is None


def test_ownerless_fallback_prefers_commenter_over_reader(db_uri: str) -> None:
    conversations, conv_id = _session_with(
        db_uri, {"commenter@example.com": LEVEL_COMMENT, "reader@example.com": LEVEL_READ}
    )
    assert conversations.get_session_owner(conv_id) == "commenter@example.com"
