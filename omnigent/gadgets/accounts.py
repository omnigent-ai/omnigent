"""What a deleted account's gadgets leave behind, for the accounts store.

Gadgets live in a :class:`~omnigent.stores.gadget_store.GadgetStore`, which the
accounts database does not reach, so deleting an account calls
:class:`DeletedAccountGadgets` once the deletion has committed, the way it removes
the account's workflows. They belong to the deleted registration, so a username
registered again, even before the cleanup finishes, never inherits them or who they
were shared with: they are detached the moment the account is deleted, and removing
them is bookkeeping that can take several calls.

Each call does bounded work (:data:`GADGETS_PER_CLEANUP` gadgets). A registration with
more left behind keeps a note in a queue in the gadget store, and later calls (another
account deletion, a registration of that or another name, a server start) go on with the
notes at its front, sending each one they worked on to the back. The note is written even
while gadgets are off, before a later registration of the name replaces the deleted one,
so the cleanup only waits until gadgets are on again.

Before anything else, deleting writes the cleanup's intent as a file of its own, which takes
no lock; a deletion whose queueing then fails (the queue busy, say) still leaves it, and the
next call moves it into the queue. Deleting also leaves a permanent mark for the
registration. A write that was already on its way for that registration (a create, a shelf
change) can land after the cleanup finished;
:meth:`DeletedAccountGadgets.after_registration_write` sees the mark and queues the cleanup
again, so nothing of a deleted registration stays behind.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
from collections.abc import Callable, Iterator

from omnigent.db.db_models import DEFAULT_WORKSPACE_ID, current_workspace_id, workspace_scope
from omnigent.gadgets.access import GadgetAccess
from omnigent.stores.gadget_store import Conflict, CorruptData, GadgetFiles, GadgetStore, NotFound

_LOG = logging.getLogger(__name__)

GADGETS_PER_CLEANUP = 50
"""The most gadgets one cleanup step deletes, whatever the account owned."""

PENDING_PER_RESUME = 4
"""The most left-over registrations one call goes on with, after its own."""

QUEUE_SEGMENT_SIZE = 256
"""Notes per queue segment: what one call lists is bounded by this, not by the backlog."""

# The notes of unfinished cleanups live in a scope of their own, which no account has: a user
# ID may be any string, so the scope also takes a registration no account can be given
# (registrations are account timestamps or ``w<workspace>`` forms, never this).
_CLEANUP_SCOPE = "omnigent.deleted-account-gadgets"
_CLEANUP_REGISTRATION = "omnigent:internal"
_QUEUE = "queue"
_HEADS = f"{_QUEUE}/heads"
_INCOMING = "incoming"

HEAD_PAGES = 8
"""Pages of head versions one read goes through to find the newest. Every head write removes
the versions it saw, so normally one or two are there; this bounds a read even if removing
them kept failing."""

INCOMING_PER_RESUME = 8
"""Intents one call moves into the queue."""


def _key(user_id: str, registration: str | None) -> str:
    return hashlib.sha256(repr((user_id, registration)).encode()).hexdigest()[:32]


_HEAD_VERSION = re.compile(r"h[0-9]{12}\.json")


def _segment(number: int) -> str:
    return f"{_QUEUE}/s{number:08d}"


class _CleanupQueue:
    """The notes of unfinished cleanups, in numbered segments with a head naming the first
    and the newest, so each call reads a bounded part of it.

    ``keys/<key>.json`` says which segment holds a registration's note (so it is queued once),
    ``deleted/<key>.json`` marks a deleted registration for good, and ``incoming/<key>.json``
    holds an intent not yet queued. The head is written, and segments are appended to, only
    under the queue's lock.

    The head is a create-only version under ``queue/heads/``: a new version is written before
    the older ones are removed, and reads take the newest, so a write stopped at any point
    leaves a head that names every segment still holding notes.
    """

    def __init__(self, files: GadgetFiles) -> None:
        self._files = files

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with self._files.exclusive(_QUEUE) as held:
            if not held:
                raise Conflict("The gadget cleanup queue is busy", reason="busy")
            yield

    def _head_versions(self) -> list[str]:
        """The head versions there, oldest first (at most :data:`HEAD_PAGES` pages)."""
        names: list[str] = []
        after: str | None = None
        for _ in range(HEAD_PAGES):
            try:
                page, more = self._files.list_page(_HEADS, after=after, limit=QUEUE_SEGMENT_SIZE)
            except NotFound:
                return names
            names += [name for name in page if _HEAD_VERSION.fullmatch(name)]
            if not more or not page:
                break
            after = page[-1]
        return names

    def _read_head(self) -> tuple[int, int, bool]:
        """The first and newest segment numbers, and whether the head exists yet."""
        versions = self._head_versions()
        if not versions:
            return 0, 0, False
        raw = self._files.read_json(f"{_HEADS}/{versions[-1]}")
        first, last = raw.get("first"), raw.get("last")
        if type(first) is not int or type(last) is not int or not 0 <= first <= last:
            raise CorruptData("Invalid gadget cleanup queue head")
        return first, last, True

    def _write_head(self, first: int, last: int) -> None:
        """Write a new head version, then remove the older ones."""
        # A store that cannot delete keeps one segment and works from its start each time.
        if not self._files.supports_delete:
            return
        versions = self._head_versions()
        number = int(versions[-1][1:-5]) + 1 if versions else 1
        self._files.mkdirs(_HEADS)
        self._files.create_json(f"{_HEADS}/h{number:012d}.json", {"first": first, "last": last})
        for old in versions:
            with contextlib.suppress(NotFound):
                self._files.delete_tree(f"{_HEADS}/{old}")

    def _queued_at(self, key: str) -> int | None:
        try:
            raw = self._files.read_json(f"keys/{key}.json")
        except NotFound:
            return None
        segment = raw.get("segment")
        return segment if type(segment) is int else None

    def _append_locked(self, key: str, note: dict, *, after: int | None = None) -> None:
        """Queue *note* in the newest segment, or a new one past it when that is full or is
        segment *after* (a note sent back from the segment it is in must land behind it)."""
        first, last, exists = self._read_head()
        names, _ = self._listed(_segment(last), QUEUE_SEGMENT_SIZE)
        full = (len(names) >= QUEUE_SEGMENT_SIZE or after == last) and self._files.supports_delete
        if full:
            last += 1
        if full or not exists:
            self._write_head(first, last)
        self._files.mkdirs(_segment(last))
        with contextlib.suppress(Conflict):
            self._files.create_json(f"{_segment(last)}/{key}.json", note)
        self._files.mkdirs("keys")
        if self._files.supports_delete:
            with contextlib.suppress(NotFound):
                self._files.delete_tree(f"keys/{key}.json")
        with contextlib.suppress(Conflict):
            self._files.create_json(f"keys/{key}.json", {"segment": last})

    def _listed(self, path: str, limit: int) -> tuple[list[str], bool]:
        try:
            return self._files.list_page(path, after=None, limit=limit)
        except NotFound:
            return [], False

    def intend(self, user_id: str, registration: str | None, workspace: int) -> None:
        """Record the cleanup's intent where :meth:`take_incoming` finds it; takes no lock."""
        self._files.mkdirs(_INCOMING)
        with contextlib.suppress(Conflict):  # Recorded already.
            self._files.create_json(
                f"{_INCOMING}/{_key(user_id, registration)}.json",
                {"user_id": user_id, "registration": registration, "workspace": workspace},
            )

    def add(self, user_id: str, registration: str | None, workspace: int) -> None:
        key = _key(user_id, registration)
        with self._locked():
            if self._queued_at(key) is None:
                self._append_locked(
                    key, {"user_id": user_id, "registration": registration, "workspace": workspace}
                )
            # Queued now, so the intent has done its job.
            if self._files.supports_delete:
                with contextlib.suppress(NotFound):
                    self._files.delete_tree(f"{_INCOMING}/{key}.json")

    def take_incoming(self, limit: int) -> None:
        """Queue up to *limit* intents a deletion left without queueing them."""
        if not self._files.supports_delete:
            return  # Nothing leaves the intents there, so they would hide later ones.
        names, _ = self._listed(_INCOMING, limit)
        for name in names:
            try:
                note = self._files.read_json(f"{_INCOMING}/{name}")
            except NotFound:
                continue
            except CorruptData:
                _LOG.warning("Dropping an unreadable gadget cleanup intent %s", name)
                with contextlib.suppress(NotFound):
                    self._files.delete_tree(f"{_INCOMING}/{name}")
                continue
            user_id, registration = note.get("user_id"), note.get("registration")
            workspace = note.get("workspace", DEFAULT_WORKSPACE_ID)
            if (
                not isinstance(user_id, str)
                or not (registration is None or isinstance(registration, str))
                or type(workspace) is not int
            ):
                with contextlib.suppress(NotFound):
                    self._files.delete_tree(f"{_INCOMING}/{name}")
                continue
            self.add(user_id, registration, workspace)

    def mark_deleted(self, user_id: str, registration: str | None, workspace: int) -> None:
        self._files.mkdirs("deleted")
        with contextlib.suppress(Conflict):  # Marked already.
            self._files.create_json(
                f"deleted/{_key(user_id, registration)}.json",
                {"user_id": user_id, "registration": registration, "workspace": workspace},
            )

    def deleted(self, user_id: str, registration: str | None) -> dict | None:
        try:
            return self._files.read_json(f"deleted/{_key(user_id, registration)}.json")
        except NotFound:
            return None

    def front(self, limit: int) -> list[tuple[int, str]]:
        """Up to *limit* notes at the front, as ``(segment, name)``; emptied front segments
        are retired on the way (at most two segments are looked at)."""
        with self._locked():
            first, last, _ = self._read_head()
        found: list[tuple[int, str]] = []
        for number in range(first, min(first + 2, last + 1)):
            names, _ = self._listed(_segment(number), limit - len(found))
            found += [(number, name) for name in names if name.endswith(".json")]
            if not names and number < last:
                self._retire(number)
            if len(found) >= limit:
                break
        return found

    def _retire(self, number: int) -> None:
        if not self._files.supports_delete:
            return
        with self._locked():
            first, last, _ = self._read_head()
            if number != first or number >= last:
                return
            names, _ = self._listed(_segment(number), 1)
            if names:
                return  # Something was added meanwhile.
            # The head moves on first: a stop between the two leaves only an empty directory.
            self._write_head(first + 1, last)
            with contextlib.suppress(NotFound):
                self._files.delete_tree(_segment(number))

    def read(self, number: int, name: str) -> dict:
        return self._files.read_json(f"{_segment(number)}/{name}")

    def done(self, number: int, name: str) -> None:
        """Drop a finished note, and the key saying it is queued."""
        key = name.removesuffix(".json")
        if not self._files.supports_delete:
            return
        with self._locked():
            with contextlib.suppress(NotFound):
                self._files.delete_tree(f"{_segment(number)}/{name}")
            if self._queued_at(key) == number:
                with contextlib.suppress(NotFound):
                    self._files.delete_tree(f"keys/{key}.json")

    def to_back(self, number: int, name: str, note: dict) -> None:
        """Send a note worked on to the back, so the notes behind it get their turn."""
        key = name.removesuffix(".json")
        if not self._files.supports_delete:
            return
        with self._locked():
            self._append_locked(key, note, after=number)
            with contextlib.suppress(NotFound):
                self._files.delete_tree(f"{_segment(number)}/{name}")


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
        queue = self._queue()
        workspace = current_workspace_id()
        # Recorded, marked and queued first, even with gadgets off: once the name is
        # registered again nothing else remembers the deleted registration. The intent goes
        # first and takes no lock, so a queue too busy to take the note doesn't lose it.
        # Without a registration (a store that tells accounts apart only by name) a mark
        # would catch the next account of the name too, so there is none.
        queue.intend(user_id, registration, workspace)
        if registration is not None:
            queue.mark_deleted(user_id, registration, workspace)
        try:
            queue.add(user_id, registration, workspace)
        except Conflict as error:
            _LOG.warning("Gadget cleanup queued later, the queue is busy: %s", error)
        if not self._enabled():
            return False
        done = self._step(user_id, registration)
        if done:
            self._forget_note(queue, user_id, registration)
        self.resume(skip=(user_id, registration))
        return done

    @staticmethod
    def after_registration_write(
        store: GadgetStore, user_id: str, registration: str | None
    ) -> None:
        """Queue the cleanup again when a write for *registration* landed after it was deleted.

        Called after the write: a write that lands before the deletion's mark is removed by
        that deletion's cleanup, and one that lands after sees the mark here. Costs one read.
        """
        if registration is None:
            return
        queue = _CleanupQueue(DeletedAccountGadgets._files_of(store))
        mark = queue.deleted(user_id, registration)
        if mark is None:
            return
        workspace = mark.get("workspace", DEFAULT_WORKSPACE_ID)
        workspace = workspace if type(workspace) is int else DEFAULT_WORKSPACE_ID
        queue.intend(user_id, registration, workspace)
        try:
            queue.add(user_id, registration, workspace)
        except Conflict as error:
            # The write itself landed; the intent gets it queued on a later call.
            _LOG.warning("Gadget cleanup queued later, the queue is busy: %s", error)

    def resume(self, skip: tuple[str, str | None] | None = None) -> None:
        """Go on with up to :data:`PENDING_PER_RESUME` cleanups at the front of the queue.

        Each runs in the workspace its account was deleted in, whatever this call's, so its
        grants go too. One that is not finished, or fails (a corrupt note or gadget, logged),
        goes to the back, so a cleanup that keeps failing never keeps the others from running.
        """
        if not self._enabled():
            return
        queue = self._queue()
        try:
            queue.take_incoming(INCOMING_PER_RESUME)
            front = queue.front(PENDING_PER_RESUME + 1)
        except (CorruptData, Conflict) as error:
            _LOG.warning("Gadget cleanup queue unavailable: %s", error)
            return
        resumed = 0
        for number, name in front:
            if resumed >= PENDING_PER_RESUME:
                break
            try:
                note = queue.read(number, name)
            except NotFound:
                continue
            except CorruptData:
                _LOG.warning("Skipping an unreadable gadget cleanup note %s", name)
                with contextlib.suppress(NotFound, Conflict):
                    queue.done(number, name)
                continue
            user_id, registration = note.get("user_id"), note.get("registration")
            workspace = note.get("workspace", DEFAULT_WORKSPACE_ID)
            if (
                not isinstance(user_id, str)
                or not (registration is None or isinstance(registration, str))
                or type(workspace) is not int
            ):
                with contextlib.suppress(NotFound, Conflict):
                    queue.done(number, name)
                continue
            if skip == (user_id, registration):
                continue
            resumed += 1
            done = False
            try:
                with workspace_scope(workspace):
                    done = self._step(user_id, registration)
            except (CorruptData, Conflict) as error:
                _LOG.warning("Gadget cleanup of a deleted account stopped: %s", error)
            with contextlib.suppress(NotFound, Conflict):
                if done:
                    queue.done(number, name)
                else:
                    queue.to_back(number, name, note)

    @staticmethod
    def _files_of(store: GadgetStore) -> GadgetFiles:
        return store.user_files(_CLEANUP_SCOPE, owner_registration=_CLEANUP_REGISTRATION)

    def _queue(self) -> _CleanupQueue:
        return _CleanupQueue(self._files_of(self._store))

    def _note(self, user_id: str, registration: str | None) -> None:
        self._queue().add(user_id, registration, current_workspace_id())

    @staticmethod
    def _forget_note(queue: _CleanupQueue, user_id: str, registration: str | None) -> None:
        key = _key(user_id, registration)
        number = queue._queued_at(key)
        if number is not None:
            with contextlib.suppress(NotFound, Conflict):
                queue.done(number, f"{key}.json")

    def _step(self, user_id: str, registration: str | None) -> bool:
        """Delete one page of the registration's gadgets; its shelf after the last."""
        page, cursor = self._store.list_owned_page(
            user_id, limit=GADGETS_PER_CLEANUP, after=None, owner_registration=registration
        )
        for gadget in page:
            self._access.forget(gadget.id)
            self._store.delete(gadget.id)
        if cursor is not None:
            return False
        self._store.delete_user_files(user_id, owner_registration=registration)
        return True
