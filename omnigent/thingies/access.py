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
from dataclasses import dataclass
from typing import Literal

from omnigent.server.auth import LEVEL_EDIT, LEVEL_MANAGE, LEVEL_READ, RESERVED_USER_LOCAL
from omnigent.stores.permission_store import MAX_SHARED_THINGIES_PAGE_SIZE, PermissionStore
from omnigent.stores.thingy_store import Thingy, ThingyStore

SharedVia = Literal["user", "group"]
# The most shared thingies a viewer's list reads.
MAX_SHARED_THINGIES = 200


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
        if viewer_id == thingy.owner_id:
            return FULL_ACCESS
        grants = self._grants()
        if grants is None or viewer_id == RESERVED_USER_LOCAL:
            return NO_ACCESS
        # A deleted owner's thingies stop being shared at once.
        if grants.deleted_accounts([thingy.owner_id]):
            return NO_ACCESS
        level = grants.resolve_thingy_access(grants.caller_principals(viewer_id), thingy.id)
        return permissions_for_level(level)

    def shared_with(self, viewer_id: str) -> list[SharedThingy]:
        grants = self._grants()
        if grants is None or viewer_id == RESERVED_USER_LOCAL:
            return []
        principals = grants.caller_principals(viewer_id)
        ids: list[str] = []
        cursor: str | None = None
        while len(ids) < MAX_SHARED_THINGIES:
            page, cursor = grants.list_thingies_shared_with(
                principals, limit=MAX_SHARED_THINGIES_PAGE_SIZE, after_thingy_id=cursor
            )
            ids.extend(page)
            if cursor is None:
                break
        found = self._store.get_many(ids[:MAX_SHARED_THINGIES])
        owners = sorted({thingy.owner_id for thingy in found.values()})
        deleted = grants.deleted_accounts(owners) if owners else set()
        shared = []
        for thingy_id in ids[:MAX_SHARED_THINGIES]:
            thingy = found.get(thingy_id)
            if thingy is None or thingy.owner_id == viewer_id or thingy.owner_id in deleted:
                continue
            direct = grants.resolve_thingy_access([viewer_id], thingy_id) is not None
            shared.append(SharedThingy(thingy, "user" if direct else "group"))
        return shared

    def forget(self, thingy_id: str) -> None:
        grants = self._grants()
        if grants is not None:
            grants.delete_for_thingy(thingy_id)
