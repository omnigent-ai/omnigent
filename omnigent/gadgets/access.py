"""Who may do what with a gadget.

:class:`GadgetAccess` answers two questions for the gadget routes: a viewer's
permissions on one gadget, and which other people's gadgets reach a viewer.
The default, :class:`PermissionStoreGadgetAccess`, gives the owner everything
and everyone else what their grants in the server's
:class:`~omnigent.stores.permission_store.PermissionStore` give them, the way
sessions and workflows are shared. A deployment with its own access model passes
another implementation to ``create_app(gadget_access=...)``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from omnigent.server.auth import LEVEL_EDIT, LEVEL_MANAGE, LEVEL_READ, RESERVED_USER_LOCAL
from omnigent.stores.gadget_store import Gadget, GadgetStore
from omnigent.stores.permission_store import MAX_SHARED_GADGETS_PAGE_SIZE, PermissionStore

SharedVia = Literal["user", "group"]
# The most shared gadgets one page of a viewer's list holds.
MAX_SHARED_GADGETS = MAX_SHARED_GADGETS_PAGE_SIZE


@dataclass(frozen=True)
class GadgetPermissions:
    """What a viewer may do with one gadget.

    :param read: Open it, read its records, comments and action ledger.
    :param write_data: Change its records and comments.
    :param publish: Upload and publish revisions, rename it, request actions and
        register a gadget session.
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


NO_ACCESS = GadgetPermissions()
FULL_ACCESS = GadgetPermissions(read=True, write_data=True, publish=True, manage=True)


def permissions_for_level(level: int | None) -> GadgetPermissions:
    """The permissions a grant level gives; owners get :data:`FULL_ACCESS`."""
    if level is None or level < LEVEL_READ:
        return NO_ACCESS
    if level >= LEVEL_MANAGE:
        return FULL_ACCESS
    if level >= LEVEL_EDIT:
        return GadgetPermissions(read=True, write_data=True, publish=True)
    return GadgetPermissions(read=True)


@dataclass(frozen=True)
class SharedGadget:
    """Someone else's gadget a viewer can read, and how it reaches them."""

    gadget: Gadget
    shared_via: SharedVia


@dataclass(frozen=True)
class SharedPage:
    """One page of the gadgets shared with a viewer.

    :param next_cursor: Where the next page starts, or ``None`` after the last. A page
        can be short, or empty, and still continue.
    """

    items: list[SharedGadget]
    next_cursor: str | None


class GadgetAccess(ABC):
    """A deployment's access rules for gadgets. Blocking; called off the event loop."""

    @abstractmethod
    def permissions(self, viewer_id: str, gadget: Gadget) -> GadgetPermissions:
        """What *viewer_id* may do with *gadget*; :data:`NO_ACCESS` hides it."""

    def permissions_many(
        self, viewer_id: str, gadgets: Sequence[Gadget]
    ) -> dict[str, GadgetPermissions]:
        """:meth:`permissions` for several gadgets, keyed by ID.

        The default asks one at a time; an implementation can batch its lookups.
        """
        return {gadget.id: self.permissions(viewer_id, gadget) for gadget in gadgets}

    @abstractmethod
    def shared_with(
        self, viewer_id: str, *, limit: int = MAX_SHARED_GADGETS, cursor: str | None = None
    ) -> SharedPage:
        """One page of other people's gadgets *viewer_id* can read.

        Leaves out the viewer's own gadgets and those they reach only through a
        role that reads everything, such as an administrator's.

        :param limit: The most gadgets on the page, 1 to :data:`MAX_SHARED_GADGETS`.
        :param cursor: The previous page's ``next_cursor``, or ``None`` to start.
        """

    def forget(self, gadget_id: str) -> None:  # noqa: B027 - optional hook
        """Drop any sharing state kept for a deleted gadget. The default keeps none."""


class PermissionStoreGadgetAccess(GadgetAccess):
    """Owner plus gadget grants in the :class:`PermissionStore` (see module docstring)."""

    def __init__(self, store: GadgetStore, permission_store: PermissionStore | None) -> None:
        """
        :param store: Resolves the gadgets the grants name.
        :param permission_store: Keeps gadget grants; ``None``, or a store that
            does not support them, leaves every gadget private to its owner.
        """
        self._store = store
        self._permission_store = permission_store

    def _grants(self) -> PermissionStore | None:
        store = self._permission_store
        return store if store is not None and store.supports_gadget_grants else None

    def permissions(self, viewer_id: str, gadget: Gadget) -> GadgetPermissions:
        return self.permissions_many(viewer_id, [gadget])[gadget.id]

    def permissions_many(
        self, viewer_id: str, gadgets: Sequence[Gadget]
    ) -> dict[str, GadgetPermissions]:
        result = {gadget.id: FULL_ACCESS for gadget in gadgets if gadget.owner_id == viewer_id}
        others = [gadget for gadget in gadgets if gadget.id not in result]
        grants = self._grants()
        if not others or grants is None or viewer_id == RESERVED_USER_LOCAL:
            return {**result, **{gadget.id: NO_ACCESS for gadget in others}}
        # A deleted owner's gadgets stop being shared at once.
        deleted = grants.deleted_accounts(sorted({gadget.owner_id for gadget in others}))
        principals = grants.caller_principals(viewer_id)
        for gadget in others:
            if gadget.owner_id in deleted:
                result[gadget.id] = NO_ACCESS
            else:
                level = grants.resolve_gadget_access(principals, gadget.id)
                result[gadget.id] = permissions_for_level(level)
        return result

    def shared_with(
        self, viewer_id: str, *, limit: int = MAX_SHARED_GADGETS, cursor: str | None = None
    ) -> SharedPage:
        grants = self._grants()
        if grants is None or viewer_id == RESERVED_USER_LOCAL:
            return SharedPage([], None)
        principals = grants.caller_principals(viewer_id)
        # One page of grants per call; grants whose gadget is gone (one that raced its
        # deletion) are dropped, so a page can come back short and still continue.
        ids, next_cursor = grants.list_gadgets_shared_with(
            principals, limit=limit, after_gadget_id=cursor
        )
        found = self._store.get_many(ids)
        for gadget_id in ids:
            if gadget_id not in found:
                grants.delete_for_gadget(gadget_id)
        owners = sorted({gadget.owner_id for gadget in found.values()})
        deleted = grants.deleted_accounts(owners) if owners else set()
        shared: list[SharedGadget] = []
        for gadget_id in ids:
            gadget = found.get(gadget_id)
            if gadget is None or gadget.owner_id == viewer_id or gadget.owner_id in deleted:
                continue
            direct = grants.resolve_gadget_access([viewer_id], gadget_id) is not None
            shared.append(SharedGadget(gadget, "user" if direct else "group"))
        return SharedPage(shared, next_cursor)

    def forget(self, gadget_id: str) -> None:
        grants = self._grants()
        if grants is not None:
            grants.delete_for_gadget(gadget_id)
