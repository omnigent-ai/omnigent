"""Who may do what with a thingy.

:class:`ThingyAccess` answers two questions for the thingy routes: a viewer's
permissions on one thingy, and which other people's thingies reach a viewer.
The default, :class:`PermissionStoreThingyAccess`, gives the owner everything
and everyone else what their grants in the server's
:class:`~omnigent.stores.permission_store.PermissionStore` give them, the way
sessions and workflows are shared. A deployment with its own access model passes
another implementation to ``create_app(thingy_access=...)``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from omnigent.server.auth import LEVEL_EDIT, LEVEL_MANAGE, LEVEL_READ, RESERVED_USER_LOCAL
from omnigent.stores.permission_store import MAX_SHARED_THINGIES_PAGE_SIZE, PermissionStore
from omnigent.stores.thingy_store import Thingy, ThingyStore

SharedVia = Literal["user", "group"]
# The most shared thingies a viewer's list reads.
MAX_SHARED_THINGIES = 200
# Grant pages read to fill that list, bounding the work when many grants are dead.
MAX_SHARED_THINGY_PAGES = 10


@dataclass(frozen=True)
class ThingyPermissions:
    """What a viewer may do with one thingy.

    :param read: Open it, read its records, comments and action ledger.
    :param write_data: Change its records and comments.
    :param publish: Upload and publish revisions, rename it, request actions and
        register a thingy session.
    :param manage: Delete it, change who it is shared with, and approve new
        capabilities on publish.
    """

    read: bool = False
    write_data: bool = False
    publish: bool = False
    manage: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {
            "read": self.read,
            "write_data": self.write_data,
            "publish": self.publish,
            "manage": self.manage,
        }


NO_ACCESS = ThingyPermissions()
FULL_ACCESS = ThingyPermissions(read=True, write_data=True, publish=True, manage=True)


def permissions_for_level(level: int | None) -> ThingyPermissions:
    """The permissions a grant level gives; owners get :data:`FULL_ACCESS`."""
    if level is None or level < LEVEL_READ:
        return NO_ACCESS
    if level >= LEVEL_MANAGE:
        return FULL_ACCESS
    if level >= LEVEL_EDIT:
        return ThingyPermissions(read=True, write_data=True, publish=True)
    return ThingyPermissions(read=True)


@dataclass(frozen=True)
class SharedThingy:
    """Someone else's thingy a viewer can read, and how it reaches them."""

    thingy: Thingy
    shared_via: SharedVia


class ThingyAccess(ABC):
    """A deployment's access rules for thingies. Blocking; called off the event loop."""

    @abstractmethod
    def permissions(self, viewer_id: str, thingy: Thingy) -> ThingyPermissions:
        """What *viewer_id* may do with *thingy*; :data:`NO_ACCESS` hides it."""

    def permissions_many(
        self, viewer_id: str, thingies: Sequence[Thingy]
    ) -> dict[str, ThingyPermissions]:
        """:meth:`permissions` for several thingies, keyed by ID.

        The default asks one at a time; an implementation can batch its lookups.
        """
        return {thingy.id: self.permissions(viewer_id, thingy) for thingy in thingies}

    @abstractmethod
    def shared_with(self, viewer_id: str) -> list[SharedThingy]:
        """Other people's thingies *viewer_id* can read, at most :data:`MAX_SHARED_THINGIES`.

        Leaves out the viewer's own thingies and those they reach only through a
        role that reads everything, such as an administrator's.
        """

    def forget(self, thingy_id: str) -> None:  # noqa: B027 - optional hook
        """Drop any sharing state kept for a deleted thingy. The default keeps none."""


class PermissionStoreThingyAccess(ThingyAccess):
    """Owner plus thingy grants in the :class:`PermissionStore` (see module docstring)."""

    def __init__(self, store: ThingyStore, permission_store: PermissionStore | None) -> None:
        """
        :param store: Resolves the thingies the grants name.
        :param permission_store: Keeps thingy grants; ``None``, or a store that
            does not support them, leaves every thingy private to its owner.
        """
        self._store = store
        self._permission_store = permission_store

    def _grants(self) -> PermissionStore | None:
        store = self._permission_store
        return store if store is not None and store.supports_thingy_grants else None

    def permissions(self, viewer_id: str, thingy: Thingy) -> ThingyPermissions:
        return self.permissions_many(viewer_id, [thingy])[thingy.id]

    def permissions_many(
        self, viewer_id: str, thingies: Sequence[Thingy]
    ) -> dict[str, ThingyPermissions]:
        result = {thingy.id: FULL_ACCESS for thingy in thingies if thingy.owner_id == viewer_id}
        others = [thingy for thingy in thingies if thingy.id not in result]
        grants = self._grants()
        if not others or grants is None or viewer_id == RESERVED_USER_LOCAL:
            return {**result, **{thingy.id: NO_ACCESS for thingy in others}}
        # A deleted owner's thingies stop being shared at once.
        deleted = grants.deleted_accounts(sorted({thingy.owner_id for thingy in others}))
        principals = grants.caller_principals(viewer_id)
        for thingy in others:
            if thingy.owner_id in deleted:
                result[thingy.id] = NO_ACCESS
            else:
                level = grants.resolve_thingy_access(principals, thingy.id)
                result[thingy.id] = permissions_for_level(level)
        return result

    def shared_with(self, viewer_id: str) -> list[SharedThingy]:
        grants = self._grants()
        if grants is None or viewer_id == RESERVED_USER_LOCAL:
            return []
        principals = grants.caller_principals(viewer_id)
        shared: list[SharedThingy] = []
        cursor: str | None = None
        # Grants whose thingy is gone (a grant that raced its deletion) are skipped and
        # dropped, so they never take a live thingy's place under the limit.
        for _ in range(MAX_SHARED_THINGY_PAGES):
            ids, cursor = grants.list_thingies_shared_with(
                principals, limit=MAX_SHARED_THINGIES_PAGE_SIZE, after_thingy_id=cursor
            )
            found = self._store.get_many(ids)
            for thingy_id in ids:
                if thingy_id not in found:
                    grants.delete_for_thingy(thingy_id)
            owners = sorted({thingy.owner_id for thingy in found.values()})
            deleted = grants.deleted_accounts(owners) if owners else set()
            for thingy_id in ids:
                thingy = found.get(thingy_id)
                if thingy is None or thingy.owner_id == viewer_id or thingy.owner_id in deleted:
                    continue
                direct = grants.resolve_thingy_access([viewer_id], thingy_id) is not None
                shared.append(SharedThingy(thingy, "user" if direct else "group"))
                if len(shared) >= MAX_SHARED_THINGIES:
                    return shared
            if cursor is None:
                break
        return shared

    def forget(self, thingy_id: str) -> None:
        grants = self._grants()
        if grants is not None:
            grants.delete_for_thingy(thingy_id)
