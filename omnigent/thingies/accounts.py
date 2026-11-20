"""What a deleted account's thingies leave behind, for the accounts store.

Thingies live in a :class:`~omnigent.stores.thingy_store.ThingyStore`, which the
accounts database does not reach, so deleting an account calls
:class:`DeletedAccountThingies` once the deletion has committed, the way it removes
the account's workflows. Re-registering the username waits until none remain, so
a new account under an old name never inherits its thingies or who they were shared
with.
"""

from __future__ import annotations

from omnigent.stores.thingy_store import ThingyStore
from omnigent.thingies.access import ThingyAccess


class DeletedAccountThingies:
    """Deletes a deleted account's thingies, their grants and the account's shelf."""

    def __init__(self, store: ThingyStore, access: ThingyAccess) -> None:
        """
        :param store: Where the thingies are kept.
        :param access: Forgets each deleted thingy's grants.
        """
        self._store = store
        self._access = access

    def delete_owned(self, user_id: str) -> None:
        """Delete every thingy *user_id* owns, with its grants, then their shelf.

        Grants go first, so a thingy whose deletion fails half-way is shared with no
        one; a retry finishes it.
        """
        for thingy in self._store.list_owned(user_id):
            self._access.forget(thingy.id)
            self._store.delete(thingy.id)
        self._store.delete_user_files(user_id)

    def owns_any(self, user_id: str) -> bool:
        """Whether *user_id* still owns a thingy."""
        return bool(self._store.list_owned(user_id))
