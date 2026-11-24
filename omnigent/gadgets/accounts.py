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
deletion, a registration of that or another name, a server start) go on with it. The note
is written even while gadgets are off, before a later registration of the name replaces
the deleted one, so the cleanup only waits until gadgets are on again.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import secrets
from collections.abc import Callable

from omnigent.db.db_models import DEFAULT_WORKSPACE_ID, current_workspace_id, workspace_scope
from omnigent.gadgets.access import GadgetAccess
from omnigent.stores.gadget_store import Conflict, CorruptData, GadgetFiles, GadgetStore, NotFound

_LOG = logging.getLogger(__name__)

GADGETS_PER_CLEANUP = 50
"""The most gadgets one cleanup step deletes, whatever the account owned."""

PENDING_PER_RESUME = 4
"""The most left-over registrations one call goes on with, after its own."""

# The notes of unfinished cleanups live in a scope of their own, which no account has: a user
# ID may be any string, so the scope also takes a registration no account can be given
# (registrations are account timestamps or ``w<workspace>`` forms, never this).
_CLEANUP_SCOPE = "omnigent.deleted-account-gadgets"
_CLEANUP_REGISTRATION = "omnigent:internal"


class DeletedAccountGadgets:
    """Deletes a deleted account's gadgets, their grants and the account's shelf."""

    def __init__(
        self, store: GadgetStore, access: GadgetAccess, enabled: Callable[[], bool] = lambda: True
    ) -> None:
        """
        :param store: Where the gadgets are kept.
        :param access: Forgets each deleted gadget's grants.
        :param enabled: Whether the server has gadgets on; while off, deleting an account
            only notes the cleanup, and deletes nothing until gadgets are on again (what
            the gadget store holds stays shared with no one meanwhile).
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
        # Noted first, even with gadgets off: once the name is registered again nothing else
        # remembers the deleted registration.
        self._note(user_id, registration)
        if not self._enabled():
            return False
        done = self._step(user_id, registration)
        self.resume(skip=(user_id, registration))
        return done

    def resume(self, skip: tuple[str, str | None] | None = None) -> None:
        """Go on with up to :data:`PENDING_PER_RESUME` cleanups earlier calls left.

        Each runs in the workspace its account was deleted in, whatever this call's, so its
        grants go too. One that fails (a corrupt note or gadget) is logged and left for a later
        call, and each call starts at a random note, so a cleanup that keeps failing never
        keeps the others from running.
        """
        if not self._enabled():
            return
        files = self._cleanup_files()
        start = f"{secrets.token_hex(16)}.json"
        try:
            names, _ = files.list_page("pending", after=start, limit=PENDING_PER_RESUME + 1)
            if len(names) < PENDING_PER_RESUME + 1:
                wrapped, _ = files.list_page(
                    "pending", after=None, limit=PENDING_PER_RESUME + 1 - len(names)
                )
                names += [name for name in wrapped if name <= start and name not in names]
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
            except CorruptData:
                _LOG.warning("Skipping an unreadable gadget cleanup note %s", name)
                continue
            user_id, registration = note.get("user_id"), note.get("registration")
            workspace = note.get("workspace", DEFAULT_WORKSPACE_ID)
            if (
                not isinstance(user_id, str)
                or not (registration is None or isinstance(registration, str))
                or type(workspace) is not int
            ):
                continue
            if skip == (user_id, registration):
                continue
            resumed += 1
            try:
                with workspace_scope(workspace):
                    self._step(user_id, registration)
            except (CorruptData, Conflict) as error:
                _LOG.warning("Gadget cleanup of a deleted account stopped: %s", error)

    @staticmethod
    def _note_name(user_id: str, registration: str | None) -> str:
        key = hashlib.sha256(repr((user_id, registration)).encode()).hexdigest()[:32]
        return f"pending/{key}.json"

    def _cleanup_files(self) -> GadgetFiles:
        return self._store.user_files(_CLEANUP_SCOPE, owner_registration=_CLEANUP_REGISTRATION)

    def _note(self, user_id: str, registration: str | None) -> None:
        files = self._cleanup_files()
        files.mkdirs("pending")
        with contextlib.suppress(Conflict):  # Already noted.
            files.create_json(
                self._note_name(user_id, registration),
                {
                    "user_id": user_id,
                    "registration": registration,
                    "workspace": current_workspace_id(),
                },
            )

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
        files = self._cleanup_files()
        if files.supports_delete:
            files.delete_tree(self._note_name(user_id, registration))
        return True
