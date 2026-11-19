"""PermissionStoreThingyAccess: owners, grant levels, groups and deleted owners."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from omnigent.server.auth import LEVEL_EDIT, LEVEL_MANAGE, LEVEL_READ
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.thingy_store.local import LocalThingyStore
from omnigent.thingies.access import (
    FULL_ACCESS,
    NO_ACCESS,
    PermissionStoreThingyAccess,
    ThingyPermissions,
)

ALICE, BOB, CAROL = "alice@example.com", "bob@example.com", "carol@example.com"


class _GroupStore(SqlAlchemyPermissionStore):
    """Carol belongs to ``team``; a deployment's directory would answer this."""

    deleted: frozenset[str] = frozenset()

    def caller_principals(self, user_id: str) -> list[str]:
        return [user_id, "team"] if user_id == CAROL else [user_id]

    def deleted_accounts(self, user_ids: Sequence[str]) -> set[str]:
        return {user_id for user_id in user_ids if user_id in self.deleted}


@pytest.fixture()
def setup(db_uri: str, tmp_path: Path) -> tuple[LocalThingyStore, _GroupStore]:
    grants = _GroupStore(db_uri)
    for user in (BOB, CAROL, "team"):
        grants.ensure_user(user)
    return LocalThingyStore(str(tmp_path)), grants


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (LEVEL_READ, ThingyPermissions(read=True)),
        (LEVEL_EDIT, ThingyPermissions(read=True, write_data=True, publish=True)),
        (LEVEL_MANAGE, FULL_ACCESS),
    ],
)
def test_grant_levels_map_to_thingy_permissions(
    setup: tuple[LocalThingyStore, _GroupStore], level: int, expected: ThingyPermissions
) -> None:
    store, grants = setup
    access = PermissionStoreThingyAccess(store, grants)
    thingy = store.create(ALICE, "Board")
    assert access.permissions(ALICE, thingy) == FULL_ACCESS
    assert access.permissions(BOB, thingy) == NO_ACCESS
    grants.grant_for_thingy(BOB, thingy.id, level)
    assert access.permissions(BOB, thingy) == expected


def test_shared_thingies_come_through_a_user_or_a_group_grant(
    setup: tuple[LocalThingyStore, _GroupStore],
) -> None:
    store, grants = setup
    access = PermissionStoreThingyAccess(store, grants)
    direct, grouped, gone = (store.create(ALICE, name) for name in ("Direct", "Grouped", "Gone"))
    own = store.create(CAROL, "Own")
    grants.grant_for_thingy(CAROL, direct.id, LEVEL_READ)
    grants.grant_for_thingy("team", grouped.id, LEVEL_EDIT)
    grants.grant_for_thingy(CAROL, gone.id, LEVEL_READ)
    grants.grant_for_thingy(CAROL, own.id, LEVEL_READ)
    store.delete(gone.id)

    shared = {item.thingy.id: item.shared_via for item in access.shared_with(CAROL)}
    assert shared == {direct.id: "user", grouped.id: "group"}
    assert access.permissions(CAROL, grouped).publish is True
    assert access.shared_with(BOB) == []

    # A deleted owner's thingies stop being shared at once.
    grants.deleted = frozenset({ALICE})
    assert access.shared_with(CAROL) == []
    assert access.permissions(CAROL, direct) == NO_ACCESS

    access.forget(direct.id)
    assert grants.list_for_thingy(direct.id) == ([], None)


def test_without_a_permission_store_thingies_stay_private(tmp_path: Path) -> None:
    store = LocalThingyStore(str(tmp_path))
    access = PermissionStoreThingyAccess(store, None)
    thingy = store.create(ALICE, "Board")
    assert access.permissions(ALICE, thingy) == FULL_ACCESS
    assert access.permissions(BOB, thingy) == NO_ACCESS
    assert access.shared_with(BOB) == []
    access.forget(thingy.id)


def test_grants_left_by_a_deleted_thingy_do_not_count_against_the_shared_list(
    setup: tuple[LocalThingyStore, _GroupStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.thingies import access as access_module

    store, grants = setup
    access = PermissionStoreThingyAccess(store, grants)
    monkeypatch.setattr(access_module, "MAX_SHARED_THINGIES", 2)
    # Thingy IDs are random, so whether the dead grants sort first varies; make enough of them.
    dead = [store.create(ALICE, f"Gone {n}") for n in range(4)]
    live = [store.create(ALICE, f"Live {n}") for n in range(2)]
    for thingy in [*dead, *live]:
        grants.grant_for_thingy(CAROL, thingy.id, LEVEL_READ)
    for thingy in dead:
        # As if a grant raced the deletion and landed after it.
        store.delete(thingy.id)

    shared = {item.thingy.id for item in access.shared_with(CAROL)}
    assert shared == {thingy.id for thingy in live}
    # The dead grants are dropped on the way.
    for thingy in dead:
        assert grants.list_for_thingy(thingy.id) == ([], None)


def test_permissions_for_several_thingies_match_one_at_a_time(
    setup: tuple[LocalThingyStore, _GroupStore],
) -> None:
    store, grants = setup
    access = PermissionStoreThingyAccess(store, grants)
    own, shared, private = (store.create(owner, "x") for owner in (CAROL, ALICE, ALICE))
    grants.grant_for_thingy(CAROL, shared.id, LEVEL_EDIT)
    thingies = [own, shared, private]
    expected = {thingy.id: access.permissions(CAROL, thingy) for thingy in thingies}
    assert access.permissions_many(CAROL, thingies) == expected
    assert expected[own.id] == FULL_ACCESS and expected[private.id] == NO_ACCESS
    grants.deleted = frozenset({ALICE})
    assert access.permissions_many(CAROL, thingies)[shared.id] == NO_ACCESS
