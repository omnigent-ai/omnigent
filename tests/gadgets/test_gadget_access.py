"""PermissionStoreGadgetAccess: owners, grant levels, groups and deleted owners."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from omnigent.gadgets.access import (
    FULL_ACCESS,
    NO_ACCESS,
    GadgetPermissions,
    PermissionStoreGadgetAccess,
)
from omnigent.server.auth import LEVEL_EDIT, LEVEL_MANAGE, LEVEL_READ
from omnigent.stores.gadget_store.local import LocalGadgetStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

ALICE, BOB, CAROL = "alice@example.com", "bob@example.com", "carol@example.com"


class _GroupStore(SqlAlchemyPermissionStore):
    """Carol belongs to ``team``; a deployment's directory would answer this."""

    deleted: frozenset[str] = frozenset()

    def caller_principals(self, user_id: str) -> list[str]:
        return [user_id, "team"] if user_id == CAROL else [user_id]

    def deleted_accounts(self, user_ids: Sequence[str]) -> set[str]:
        return {user_id for user_id in user_ids if user_id in self.deleted}


@pytest.fixture()
def setup(db_uri: str, tmp_path: Path) -> tuple[LocalGadgetStore, _GroupStore]:
    grants = _GroupStore(db_uri)
    for user in (BOB, CAROL, "team"):
        grants.ensure_user(user)
    return LocalGadgetStore(str(tmp_path)), grants


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (LEVEL_READ, GadgetPermissions(read=True)),
        (LEVEL_EDIT, GadgetPermissions(read=True, write_data=True, publish=True)),
        (LEVEL_MANAGE, FULL_ACCESS),
    ],
)
def test_grant_levels_map_to_gadget_permissions(
    setup: tuple[LocalGadgetStore, _GroupStore], level: int, expected: GadgetPermissions
) -> None:
    store, grants = setup
    access = PermissionStoreGadgetAccess(store, grants)
    gadget = store.create(ALICE, "Board")
    assert access.permissions(ALICE, gadget) == FULL_ACCESS
    assert access.permissions(BOB, gadget) == NO_ACCESS
    grants.grant_for_gadget(BOB, gadget.id, level)
    assert access.permissions(BOB, gadget) == expected


def test_shared_gadgets_come_through_a_user_or_a_group_grant(
    setup: tuple[LocalGadgetStore, _GroupStore],
) -> None:
    store, grants = setup
    access = PermissionStoreGadgetAccess(store, grants)
    direct, grouped, gone = (store.create(ALICE, name) for name in ("Direct", "Grouped", "Gone"))
    own = store.create(CAROL, "Own")
    grants.grant_for_gadget(CAROL, direct.id, LEVEL_READ)
    grants.grant_for_gadget("team", grouped.id, LEVEL_EDIT)
    grants.grant_for_gadget(CAROL, gone.id, LEVEL_READ)
    grants.grant_for_gadget(CAROL, own.id, LEVEL_READ)
    store.delete(gone.id)

    shared = {item.gadget.id: item.shared_via for item in access.shared_with(CAROL)}
    assert shared == {direct.id: "user", grouped.id: "group"}
    assert access.permissions(CAROL, grouped).publish is True
    assert access.shared_with(BOB) == []

    # A deleted owner's gadgets stop being shared at once.
    grants.deleted = frozenset({ALICE})
    assert access.shared_with(CAROL) == []
    assert access.permissions(CAROL, direct) == NO_ACCESS

    access.forget(direct.id)
    assert grants.list_for_gadget(direct.id) == ([], None)


def test_without_a_permission_store_gadgets_stay_private(tmp_path: Path) -> None:
    store = LocalGadgetStore(str(tmp_path))
    access = PermissionStoreGadgetAccess(store, None)
    gadget = store.create(ALICE, "Board")
    assert access.permissions(ALICE, gadget) == FULL_ACCESS
    assert access.permissions(BOB, gadget) == NO_ACCESS
    assert access.shared_with(BOB) == []
    access.forget(gadget.id)


def test_grants_left_by_a_deleted_gadget_do_not_count_against_the_shared_list(
    setup: tuple[LocalGadgetStore, _GroupStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.gadgets import access as access_module

    store, grants = setup
    access = PermissionStoreGadgetAccess(store, grants)
    monkeypatch.setattr(access_module, "MAX_SHARED_GADGETS", 2)
    # Gadget IDs are random, so whether the dead grants sort first varies; make enough of them.
    dead = [store.create(ALICE, f"Gone {n}") for n in range(4)]
    live = [store.create(ALICE, f"Live {n}") for n in range(2)]
    for gadget in [*dead, *live]:
        grants.grant_for_gadget(CAROL, gadget.id, LEVEL_READ)
    for gadget in dead:
        # As if a grant raced the deletion and landed after it.
        store.delete(gadget.id)

    shared = {item.gadget.id for item in access.shared_with(CAROL)}
    assert shared == {gadget.id for gadget in live}
    # The dead grants are dropped on the way.
    for gadget in dead:
        assert grants.list_for_gadget(gadget.id) == ([], None)


def test_permissions_for_several_gadgets_match_one_at_a_time(
    setup: tuple[LocalGadgetStore, _GroupStore],
) -> None:
    store, grants = setup
    access = PermissionStoreGadgetAccess(store, grants)
    own, shared, private = (store.create(owner, "x") for owner in (CAROL, ALICE, ALICE))
    grants.grant_for_gadget(CAROL, shared.id, LEVEL_EDIT)
    gadgets = [own, shared, private]
    expected = {gadget.id: access.permissions(CAROL, gadget) for gadget in gadgets}
    assert access.permissions_many(CAROL, gadgets) == expected
    assert expected[own.id] == FULL_ACCESS and expected[private.id] == NO_ACCESS
    grants.deleted = frozenset({ALICE})
    assert access.permissions_many(CAROL, gadgets)[shared.id] == NO_ACCESS
