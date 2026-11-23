"""What a deleted account's gadgets leave behind, for the accounts store.

Gadgets live in a :class:`~omnigent.stores.gadget_store.GadgetStore`, which the
accounts database does not reach, so deleting an account calls
:class:`DeletedAccountGadgets` once the deletion has committed, the way it removes
the account's workflows. They belong to the deleted registration, so a username
registered again, even before the cleanup finishes, never inherits them or who they
were shared with.
"""

from __future__ import annotations

from collections.abc import Callable

from omnigent.gadgets.access import GadgetAccess
from omnigent.stores.gadget_store import GadgetStore


class DeletedAccountGadgets:
    """Deletes a deleted account's gadgets, their grants and the account's shelf."""

    def __init__(
        self, store: GadgetStore, access: GadgetAccess, enabled: Callable[[], bool] = lambda: True
    ) -> None:
        """
        :param store: Where the gadgets are kept.
        :param access: Forgets each deleted gadget's grants.
        :param enabled: Whether the server has gadgets on; while off, deleting an account
            leaves the gadget store alone (what it holds stays shared with no one).
        """
        self._store = store
        self._access = access
        self._enabled = enabled

    def delete_owned(self, user_id: str, registration: str | None) -> None:
        """Delete every gadget the deleted registration *registration* of *user_id* owns,
        with its grants, then its shelf.

        Only that registration's: a cleanup that runs late, after the username was registered
        again, never touches the new account's gadgets or shelf, and until it runs the old
        ones are shared with no one (:class:`PermissionStoreGadgetAccess`). Grants go first,
        so a gadget whose deletion fails half-way is shared with no one; a retry finishes it.
        Works through one page of gadgets at a time, so a large account costs bounded memory
        and starts deleting at once.
        """
        if not self._enabled():
            return
        for gadget in self._store.iter_owned(user_id, owner_registration=registration):
            self._access.forget(gadget.id)
            self._store.delete(gadget.id)
        self._store.delete_user_files(user_id, owner_registration=registration)
