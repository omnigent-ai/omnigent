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

from omnigent.db.db_models import DEFAULT_WORKSPACE_ID, current_workspace_id
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


def workspace_registration(registration: str | None) -> str | None:
    """The registration gadgets are owned under: the account's *registration* within the
    workspace the request is scoped to (:func:`~omnigent.db.db_models.workspace_scope`).

    One user ID can stand for different people in different workspaces of a multi-tenant
    server, and one gadget store can serve them all, so a gadget belongs to its owner only in
    the workspace it was made in. The single-workspace default keeps the bare registration.
    """
    workspace = current_workspace_id()
    if workspace == DEFAULT_WORKSPACE_ID:
        return registration
    return f"w{workspace}" if registration is None else f"w{workspace}:{registration}"


def workspace_registrations(
    store: PermissionStore | None, user_ids: Sequence[str]
) -> dict[str, str | None]:
    """:func:`workspace_registration` for each of *user_ids*, from *store*'s accounts."""
    found = store.account_registrations(user_ids) if store is not None else {}
    return {user_id: workspace_registration(found.get(user_id)) for user_id in user_ids}


def owned_by(gadget: Gadget, user_id: str, registration: str | None) -> bool:
    """Whether *user_id*'s registration *registration* owns *gadget*.

    The registration (:meth:`PermissionStore.account_registrations`) tells a username
    registered again from the deleted account that held it before, so the new account never
    reaches the old one's gadgets, even where a copy of them survived (another server's
    gadget directory), while a password change keeps them.
    """
    return gadget.owner_id == user_id and gadget.owner_registration == registration


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


def _current(gadget: Gadget, registrations: dict[str, str | None]) -> bool:
    """Whether *gadget*'s owner registration is still its owner's: a username deleted and
    registered again leaves the old registration's gadgets shared with no one."""
    return gadget.owner_registration == registrations.get(gadget.owner_id)


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

    def _registrations(self, user_ids: Sequence[str]) -> dict[str, str | None]:
        return workspace_registrations(self._permission_store, user_ids)

    def _grants(self) -> PermissionStore | None:
        store = self._permission_store
        return store if store is not None and store.supports_gadget_grants else None

    def permissions(self, viewer_id: str, gadget: Gadget) -> GadgetPermissions:
        return self.permissions_many(viewer_id, [gadget])[gadget.id]

    def permissions_many(
        self, viewer_id: str, gadgets: Sequence[Gadget]
    ) -> dict[str, GadgetPermissions]:
        registrations = self._registrations([viewer_id, *(gadget.owner_id for gadget in gadgets)])
        mine = registrations.get(viewer_id)
        result = {
            gadget.id: FULL_ACCESS for gadget in gadgets if owned_by(gadget, viewer_id, mine)
        }
        others = [gadget for gadget in gadgets if gadget.id not in result]
        grants = self._grants()
        if not others or grants is None or viewer_id == RESERVED_USER_LOCAL:
            return {**result, **{gadget.id: NO_ACCESS for gadget in others}}
        # A deleted owner's gadgets stop being shared at once.
        deleted = grants.deleted_accounts(sorted({gadget.owner_id for gadget in others}))
        principals = grants.caller_principals(viewer_id)
        for gadget in others:
            if gadget.owner_id in deleted or not _current(gadget, registrations):
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
        # Skip, but do not delete, grants for gadgets absent from this store; a page can come
        # back short and still continue.
        ids, next_cursor = grants.list_gadgets_shared_with(
            principals, limit=limit, after_gadget_id=cursor
        )
        found = self._store.get_many(ids)
        owners = sorted({gadget.owner_id for gadget in found.values()})
        deleted = grants.deleted_accounts(owners) if owners else set()
        registrations = self._registrations(owners)
        shared: list[SharedGadget] = []
        for gadget_id in ids:
            gadget = found.get(gadget_id)
            if (
                gadget is None
                or gadget.owner_id == viewer_id
                or gadget.owner_id in deleted
                or not _current(gadget, registrations)
            ):
                continue
            direct = grants.resolve_gadget_access([viewer_id], gadget_id) is not None
            shared.append(SharedGadget(gadget, "user" if direct else "group"))
        return SharedPage(shared, next_cursor)

    def forget(self, gadget_id: str) -> None:
        grants = self._grants()
        if grants is not None:
            grants.delete_for_gadget(gadget_id)
