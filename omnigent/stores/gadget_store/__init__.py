"""Gadget store: gadget metadata and the create-only JSON files gadgets keep.

A gadget's metadata (owner and name) is one small record. Everything else a
gadget holds (revisions, publication, records, comments, the action ledger and
rule state) lives in create-only JSON files under its own scope, which
:mod:`omnigent.gadgets.documents` builds revisioned records on. Each user also
has a file scope for their shelf (pins and the gadgets shown beside each of
their sessions).

The default implementation keeps everything under ``<data_dir>/gadgets``
(:class:`~omnigent.stores.gadget_store.local.LocalGadgetStore`). A deployment
can pass another implementation to ``create_app(gadget_store=...)``; ``designs/GADGETS.md``
describes what a database-backed one maps each operation to.

Paths given to :class:`GadgetFiles` are relative POSIX paths such as
``"data/cards/c1/00000001.json"``; implementations reject absolute paths,
``.``/``..`` segments, empty segments and backslashes.
"""

from __future__ import annotations

import bisect
import contextlib
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

__all__ = [
    "GADGET_ID_PATTERN",
    "MAX_FILE_BYTES",
    "MAX_GADGET_NAME_CHARS",
    "MAX_OWNED_PAGE_SIZE",
    "OWNS_ANY_PAGES",
    "CapacityExceeded",
    "Conflict",
    "CorruptData",
    "Gadget",
    "GadgetFiles",
    "GadgetStore",
    "InvalidInput",
    "NotFound",
    "StoreError",
    "is_gadget_id",
    "validate_relative_path",
]

# The largest JSON file a gadget keeps; a revision's HTML is the largest one.
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_GADGET_NAME_CHARS = 120
# The most gadgets one page of a user's own gadgets holds.
MAX_OWNED_PAGE_SIZE = 200
# Pages of a user's gadgets one owns_any call reads before it answers that some may remain.
OWNS_ANY_PAGES = 64


# ---------------------------------------------------------------------------
# Errors shared by the store and the document store built on it.


class StoreError(Exception):
    """Base class of every typed gadget storage error."""

    code = "STORE_ERROR"


class NotFound(StoreError):
    """The gadget, record or file does not exist."""

    code = "NOT_FOUND"


class Conflict(StoreError):
    """Create-only collision, stale ``if_version`` or operation id reuse.

    ``reason`` is one of ``already_exists``, ``version_mismatch``,
    ``concurrent_write``, ``operation_reuse`` or the generic ``conflict``.
    ``current_version`` is the record's newest version token when known.
    """

    code = "CONFLICT"

    def __init__(
        self, message: str, *, reason: str = "conflict", current_version: str | None = None
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.current_version = current_version


class InvalidInput(StoreError):
    """The request is malformed, out of bounds, or names another scope.

    ``details`` is machine-readable context the API returns with the error, e.g.
    which field of a record failed its schema.
    """

    code = "INVALID_INPUT"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details


class CapacityExceeded(StoreError):
    """A bounded resource (revisions, records, listing size) is full.

    ``details`` is machine-readable context the API returns with the error.
    """

    code = "CAPACITY_EXCEEDED"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details


class CorruptData(StoreError):
    """Stored state fails validation (digest, chain, identity, bounds)."""

    code = "CORRUPT_DATA"

    def __init__(
        self, message: str, *, collection: str | None = None, doc_id: str | None = None
    ) -> None:
        super().__init__(message)
        self.collection = collection
        self.doc_id = doc_id


def validate_relative_path(path: str) -> tuple[str, ...]:
    """Split a :class:`GadgetFiles` path into its segments, refusing unsafe ones.

    :param path: e.g. ``"data/cards/c1/00000001.json"``.
    :returns: The segments, e.g. ``("data", "cards", "c1", "00000001.json")``.
    :raises InvalidInput: For an empty or absolute path, ``.``/``..`` or empty
        segments, backslashes or non-printable characters.
    """
    if not isinstance(path, str) or not path or not path.isprintable() or "\\" in path:
        raise InvalidInput("Invalid gadget file path")
    parts = tuple(path.split("/"))
    if path.startswith("/") or any(part in ("", ".", "..") for part in parts):
        raise InvalidInput("Invalid gadget file path")
    return parts


# ---------------------------------------------------------------------------
# Interfaces


@dataclass(frozen=True)
class Gadget:
    """One gadget's metadata.

    :param id: 32 lowercase hex characters, e.g. ``"3f2a…"`` (a UUID without dashes).
    :param owner_id: The owner's user ID, e.g. ``"alice@example.com"``.
    :param name: Display name, 1 to :data:`MAX_GADGET_NAME_CHARS` characters.
    :param created_at: Epoch seconds.
    :param owner_registration: The owner's account registration, for a server with built-in
        accounts, or ``None``. A password change keeps it; a username registered again gets
        a new one, so the new account never owns the old one's gadgets.
    """

    id: str
    owner_id: str
    name: str
    created_at: float
    owner_registration: str | None = None


class GadgetFiles(ABC):
    """Create-only JSON files in one scope (a gadget's, or one user's shelf).

    There is no overwrite: the document store derives every change as a new
    file, so implementations only need an atomic create. Deleting is optional
    (:meth:`delete_tree`); it lets the document store give a deleted record's
    capacity back, and without it a collection's record IDs are used up for good.
    """

    #: Whether :meth:`delete_tree` is implemented.
    supports_delete: bool = False

    def delete_tree(self, path: str) -> None:
        """Delete a file, or a directory with everything under it; nothing if it is missing.

        Readers see the whole tree disappear at once, never part of it. A store that
        deletes must also implement :meth:`exclusive`.
        """
        raise NotImplementedError

    @contextlib.contextmanager
    def exclusive(self, name: str) -> Iterator[bool]:  # noqa: ARG002
        """Hold the lock *name* in this scope against every process using the same storage,
        for a check and the deletion it allows; yields whether it is held. A store that
        cannot delete has nothing to guard and yields ``True``.
        """
        yield True

    @abstractmethod
    def read_json(self, path: str) -> dict:
        """Read one JSON object.

        :raises NotFound: When the file does not exist.
        :raises CorruptData: When its content is not a JSON object within
            :data:`MAX_FILE_BYTES`.
        """

    @abstractmethod
    def create_json(self, path: str, value: dict) -> None:
        """Create a file atomically, never replacing one.

        Readers never see a partly written file.

        :raises Conflict: When the file already exists.
        :raises NotFound: When its parent directory does not exist, or the gadget
            whose scope this is was deleted.
        :raises CapacityExceeded: When the encoded value exceeds :data:`MAX_FILE_BYTES`.
        """

    @abstractmethod
    def list_paths(self, path: str) -> list[str]:
        """Names of a directory's direct children, sorted.

        Read-after-write consistent with :meth:`create_json` and :meth:`mkdirs`.

        :raises NotFound: When the directory does not exist.
        :raises CapacityExceeded: Rather than return a truncated listing.
        """

    def list_page(self, path: str, *, after: str | None, limit: int) -> tuple[list[str], bool]:
        """Up to *limit* of a directory's child names that sort after *after*, in order.

        :returns: The names and whether more follow. Callers page through a directory with
            this instead of :meth:`list_paths`, so a database store answers each page with a
            bounded index range scan. The default reads the whole listing; a store that can
            seek should override it.
        :raises NotFound: When the directory does not exist.
        """
        names = self.list_paths(path)
        start = 0 if after is None else bisect.bisect_right(names, after)
        page = names[start : start + limit]
        return page, start + limit < len(names)

    @abstractmethod
    def mkdirs(self, path: str) -> None:
        """Create a directory and its missing ancestors within the scope; idempotent.

        :raises NotFound: When the gadget whose scope this is was deleted; a
            gadget's scope is never created again once it is gone.
        """


# A gadget's ID is its store's: 1 to 64 lowercase letters, digits, "_" or "-", starting with a
# letter or digit (the file store uses 32 hex digits; another store may use, say, decimal IDs).
GADGET_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def is_gadget_id(value: object) -> bool:
    """Whether *value* has the form of a gadget ID (any store's)."""
    return isinstance(value, str) and GADGET_ID_PATTERN.fullmatch(value) is not None


class GadgetStore(ABC):
    """Gadget metadata plus each gadget's and each user's :class:`GadgetFiles`.

    Implementations are synchronous and thread-safe; the routes call them from
    worker threads. Access control is not the store's: callers authorize first.
    """

    def __init__(self, storage_location: str) -> None:
        """
        :param storage_location: Backend-specific location, e.g. the directory
            ``"/home/alice/.omnigent/gadgets"`` or a database URI.
        """
        self.storage_location = storage_location

    @abstractmethod
    def create(self, owner_id: str, name: str, *, owner_registration: str | None = None) -> Gadget:
        """Create a gadget with a new ID (see :data:`GADGET_ID_PATTERN`), owned by *owner_id*.

        IDs are never reused, even after a delete: grants and cleanup rely on that.

        :param owner_registration: See :attr:`Gadget.owner_registration`.
        """

    @abstractmethod
    def get(self, gadget_id: str) -> Gadget | None:
        """The gadget, or ``None`` when it does not exist or *gadget_id* is malformed."""

    def get_many(self, gadget_ids: Iterable[str]) -> dict[str, Gadget]:
        """The existing gadgets among *gadget_ids*, keyed by ID.

        The default calls :meth:`get` per ID; a database store can batch it.
        """
        found: dict[str, Gadget] = {}
        for gadget_id in dict.fromkeys(gadget_ids):
            gadget = self.get(gadget_id)
            if gadget is not None:
                found[gadget_id] = gadget
        return found

    @abstractmethod
    def list_owned_page(
        self,
        owner_id: str,
        *,
        limit: int,
        after: str | None = None,
        owner_registration: str | None = None,
    ) -> tuple[list[Gadget], str | None]:
        """One page of the gadgets *owner_id* owns, in an order the store keeps stable.

        Only gadgets created with *owner_registration* count as that owner's.

        Each page must cost bounded work, whatever the owner's count (an ordered index,
        not a scan of every gadget they own).

        :param limit: The most gadgets to return, 1 to :data:`MAX_OWNED_PAGE_SIZE`.
        :param after: The previous page's cursor (opaque to callers), or ``None`` to start.
        :returns: The page and the cursor of the next one, or ``None`` after the last.
            A page can hold fewer than *limit* gadgets, or none, and still continue.
            Deleting gadgets already returned does not move the cursor.
        """

    def iter_owned(
        self, owner_id: str, *, owner_registration: str | None = None
    ) -> Iterator[Gadget]:
        """Every gadget *owner_id* owns, read one page at a time as the caller goes.

        For work that visits them all, such as deleting an account, without holding the
        whole list; requests page instead.
        """
        cursor: str | None = None
        while True:
            page, cursor = self.list_owned_page(
                owner_id,
                limit=MAX_OWNED_PAGE_SIZE,
                after=cursor,
                owner_registration=owner_registration,
            )
            yield from page
            if cursor is None:
                return

    def owns_any(self, owner_id: str, *, owner_registration: str | None = None) -> bool:
        """Whether *owner_id* still owns a gadget, for registering a deleted username again.

        Bounded per call: pages that hold no live gadget (stale index entries) are passed
        over up to :data:`OWNS_ANY_PAGES`; past that it answers ``True`` and a later call
        goes on, as an implementation that repairs its index as it reads (the local store
        does) gets closer each time.
        """
        cursor: str | None = None
        for _ in range(OWNS_ANY_PAGES):
            page, cursor = self.list_owned_page(
                owner_id, limit=1, after=cursor, owner_registration=owner_registration
            )
            if page:
                return True
            if cursor is None:
                return False
        return True

    @abstractmethod
    def rename(self, gadget_id: str, name: str) -> Gadget | None:
        """Rename a gadget; ``None`` when it does not exist."""

    @abstractmethod
    def delete(self, gadget_id: str) -> bool:
        """Delete a gadget with every file in its scope.

        Writes and renames racing it fail (``NotFound``, or ``None`` from
        :meth:`rename`) rather than leave files of the deleted gadget behind.

        :returns: ``True`` when it existed.
        """

    @abstractmethod
    def gadget_files(self, gadget_id: str) -> GadgetFiles:
        """The file scope of an existing gadget."""

    @abstractmethod
    def user_files(self, user_id: str, *, owner_registration: str | None = None) -> GadgetFiles:
        """One user's own file scope, for their shelf; one per account registration."""

    @abstractmethod
    def delete_user_files(self, user_id: str, *, owner_registration: str | None = None) -> None:
        """Delete every file in one user's scope, as when their account is deleted."""
