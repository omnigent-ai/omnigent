"""What a deleted account's gadgets leave behind, for the accounts store.

Gadgets live in a :class:`~omnigent.stores.gadget_store.GadgetStore`, which the
accounts database does not reach, so deleting an account calls
:class:`DeletedAccountGadgets` once the deletion has committed, the way it removes
the account's workflows. Re-registering the username waits until none remain, so
a new account under an old name never inherits its gadgets or who they were shared
with.
"""

from __future__ import annotations

from omnigent.gadgets.access import GadgetAccess
from omnigent.stores.gadget_store import GadgetStore


class DeletedAccountGadgets:
    """Deletes a deleted account's gadgets, their grants and the account's shelf."""

    def __init__(self, store: GadgetStore, access: GadgetAccess) -> None:
        """
        :param store: Where the gadgets are kept.
        :param access: Forgets each deleted gadget's grants.
        """
        self._store = store
        self._access = access

    def delete_owned(self, user_id: str) -> None:
        """Delete every gadget *user_id* owns, with its grants, then their shelf.

        Grants go first, so a gadget whose deletion fails half-way is shared with no
        one; a retry finishes it.
        """
        for gadget in self._store.list_owned(user_id):
            self._access.forget(gadget.id)
            self._store.delete(gadget.id)
        self._store.delete_user_files(user_id)

    def owns_any(self, user_id: str) -> bool:
        """Whether *user_id* still owns a gadget."""
        return bool(self._store.list_owned(user_id))
