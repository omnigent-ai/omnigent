"""What a deleted account's gadgets leave behind, for the accounts store.

Gadgets live in a :class:`~omnigent.stores.gadget_store.GadgetStore`, which the
accounts database does not reach, so deleting an account calls
:class:`DeletedAccountGadgets` once the deletion has committed, the way it removes
the account's workflows. They belong to the deleted registration, so a username
registered again, even before the cleanup finishes, never inherits them or who they
were shared with: they are detached the moment the account is deleted, and removing
them is bookkeeping that can take several calls.

Each call does bounded work (:data:`GADGETS_PER_CLEANUP` gadgets). A registration with
more left behind keeps a note in the gadget store, and later calls (another account
deletion, a registration of that or another name) go on with it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from omnigent.gadgets.access import GadgetAccess
from omnigent.stores.gadget_store import Conflict, GadgetStore, NotFound

GADGETS_PER_CLEANUP = 50
"""The most gadgets one cleanup step deletes, whatever the account owned."""

PENDING_PER_RESUME = 4
"""The most left-over registrations one call goes on with, after its own."""

# The notes of unfinished cleanups live in a scope of their own, which no account has.
_CLEANUP_SCOPE = "omnigent.deleted-account-gadgets"


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

    def delete_owned(self, user_id: str, registration: str | None) -> bool:
        """Delete up to :data:`GADGETS_PER_CLEANUP` gadgets the deleted registration
        *registration* of *user_id* owns, with their grants, and its shelf once none are
        left; then go on with a few other unfinished cleanups.

        Only that registration's: a cleanup that runs late, after the username was registered
        again, never touches the new account's gadgets or shelf. Grants go first, so a gadget
        whose deletion fails half-way is shared with no one; a later call finishes it.

        :returns: Whether nothing of *registration* is left.
        """
        if not self._enabled():
            return False
        self._note(user_id, registration)
        done = self._step(user_id, registration)
        self.resume(skip=(user_id, registration))
        return done

    def resume(self, skip: tuple[str, str | None] | None = None) -> None:
        """Go on with up to :data:`PENDING_PER_RESUME` cleanups earlier calls left."""
        if not self._enabled():
            return
        files = self._store.user_files(_CLEANUP_SCOPE)
        try:
            names, _ = files.list_page("pending", after=None, limit=PENDING_PER_RESUME + 1)
        except NotFound:
            return
        resumed = 0
        for name in names:
            if resumed >= PENDING_PER_RESUME:
                break
            try:
                note = files.read_json(f"pending/{name}")
            except NotFound:
                continue
            user_id, registration = note.get("user_id"), note.get("registration")
            if not isinstance(user_id, str) or not (
                registration is None or isinstance(registration, str)
            ):
                continue
            if skip == (user_id, registration):
                continue
            resumed += 1
            self._step(user_id, registration)

    @staticmethod
    def _note_name(user_id: str, registration: str | None) -> str:
        key = hashlib.sha256(repr((user_id, registration)).encode()).hexdigest()[:32]
        return f"pending/{key}.json"

    def _note(self, user_id: str, registration: str | None) -> None:
        files = self._store.user_files(_CLEANUP_SCOPE)
        files.mkdirs("pending")
        try:
            files.create_json(
                self._note_name(user_id, registration),
                {"user_id": user_id, "registration": registration},
            )
        except Conflict:
            pass  # Already noted.

    def _step(self, user_id: str, registration: str | None) -> bool:
        """Delete one page of the registration's gadgets; its shelf and note after the last."""
        page, cursor = self._store.list_owned_page(
            user_id, limit=GADGETS_PER_CLEANUP, after=None, owner_registration=registration
        )
        for gadget in page:
            self._access.forget(gadget.id)
            self._store.delete(gadget.id)
        if cursor is not None:
            return False
        self._store.delete_user_files(user_id, owner_registration=registration)
        files = self._store.user_files(_CLEANUP_SCOPE)
        if files.supports_delete:
            files.delete_tree(self._note_name(user_id, registration))
        return True
