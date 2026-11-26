"""The gadget grant defaults for stores that keep none, such as the SQL store."""

from __future__ import annotations

import uuid

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_READ
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.stores.test_permission_store import _minimal_store


def _gadget() -> str:
    return uuid.uuid4().hex


def test_store_without_gadget_grants_keeps_gadgets_private() -> None:
    methods = {
        "grant_for_gadget",
        "revoke_for_gadget",
        "list_for_gadget",
        "resolve_gadget_access",
        "list_gadgets_shared_with",
        "delete_for_gadget",
    }
    assert not methods & PermissionStore.__abstractmethods__
    minimal = _minimal_store()
    assert minimal.supports_gadget_grants is False
    with pytest.raises(OmnigentError) as caught:
        minimal.grant_for_gadget("alice@example.com", _gadget(), LEVEL_READ)
    assert caught.value.code == ErrorCode.FORBIDDEN
    assert minimal.resolve_gadget_access(["alice@example.com"], _gadget()) is None
    assert minimal.list_gadgets_shared_with(["alice@example.com"]) == ([], None)
    assert minimal.delete_for_gadget(_gadget()) == 0


def test_the_sql_store_keeps_no_gadget_grants(db_uri: str) -> None:
    store = SqlAlchemyPermissionStore(db_uri)
    assert store.supports_gadget_grants is False
    with pytest.raises(OmnigentError) as caught:
        store.grant_for_gadget("alice@example.com", _gadget(), LEVEL_READ)
    assert caught.value.code == ErrorCode.FORBIDDEN
