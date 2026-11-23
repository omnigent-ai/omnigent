"""What a deleted account's gadgets leave behind, for the accounts store.

Gadgets live in a :class:`~omnigent.stores.gadget_store.GadgetStore`, which the
accounts database does not reach, so deleting an account calls
:class:`DeletedAccountGadgets` once the deletion has committed, the way it removes
the account's workflows. Re-registering the username waits until none remain, so
a new account under an old name never inherits its gadgets or who they were shared
with.
"""

from __future__ import annotations

import contextlib

from omnigent.gadgets.access import GadgetAccess
from omnigent.gadgets.registration import (
    ALIASES,
    REGISTRATION_DIR,
    REGISTRATION_FILE,
    gadget_registration,
)
from omnigent.stores.gadget_store import Conflict, GadgetStore, NotFound


class DeletedAccountGadgets:
    """Deletes a deleted account's gadgets, their grants and the account's shelf."""

    def __init__(self, store: GadgetStore, access: GadgetAccess) -> None:
        """
        :param store: Where the gadgets are kept.
        :param access: Forgets each deleted gadget's grants.
        """
        self._store = store
        self._access = access

    def carry(self, user_id: str, old: str, new: str) -> None:
        """Keep the account's gadgets when its generation rotates from *old* to *new*.

        Called before the rotation commits, so a crash leaves at most a link for a generation
        no one ever held. Writes two create-only files: *new*'s link to the registration, and
        the registration's note of *new*, which deletion follows to remove the link.
        """
        registration = gadget_registration(self._store, user_id, old)
        if registration is None or registration == new:
            return
        link = self._store.user_files(user_id, owner_generation=new)
        link.mkdirs(REGISTRATION_DIR)
        with contextlib.suppress(Conflict):  # A retried rotation linked it already.
            link.create_json(REGISTRATION_FILE, {"registration": registration})
        notes = self._store.user_files(user_id, owner_generation=registration)
        notes.mkdirs(ALIASES)
        with contextlib.suppress(Conflict):
            notes.create_json(f"{ALIASES}/{new}.json", {"generation": new})

    def delete_owned(self, user_id: str, generation: str | None = None) -> None:
        """Delete every gadget the deleted account *user_id* (*generation*) owns, with its
        grants, then its shelf.

        Only that generation's: a cleanup that runs late, after the username was registered
        again, never touches the new account's gadgets or shelf. Grants go first, so a gadget
        whose deletion fails half-way is shared with no one; a retry finishes it. Works
        through one page of gadgets at a time, so a large account costs bounded memory and
        starts deleting at once.
        """
        registration = gadget_registration(self._store, user_id, generation)
        for gadget in self._store.iter_owned(user_id, owner_generation=registration):
            self._access.forget(gadget.id)
            self._store.delete(gadget.id)
        if registration is not None:
            # The links of the generations the account rotated through, a page at a time.
            notes = self._store.user_files(user_id, owner_generation=registration)
            after: str | None = None
            while True:
                try:
                    names, more = notes.list_page(ALIASES, after=after, limit=100)
                except NotFound:
                    break
                for name in names:
                    if name.endswith(".json"):
                        self._store.delete_user_files(
                            user_id, owner_generation=name.removesuffix(".json")
                        )
                if not more or not names:
                    break
                after = names[-1]
        self._store.delete_user_files(user_id, owner_generation=registration)
        if generation != registration:
            self._store.delete_user_files(user_id, owner_generation=generation)

    def owns_any(self, user_id: str, generation: str | None = None) -> bool:
        """Whether the deleted account *user_id* (*generation*) still owns a live gadget.

        Stale entries of its index (an emptied segment, a marker a deletion left) don't
        count; see :meth:`GadgetStore.owns_any`.
        """
        registration = gadget_registration(self._store, user_id, generation)
        return self._store.owns_any(user_id, owner_generation=registration)
