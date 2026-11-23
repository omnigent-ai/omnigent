"""Gadget store: gadget metadata and the create-only JSON files gadgets keep.

A gadget's metadata (owner and name) is one small record. Everything else a
gadget holds (revisions, publication, records, comments, the action ledger and
rule state) lives in create-only JSON files under its own scope, which
:mod:`omnigent.gadgets.documents` builds revisioned records on. Each user also
has a file scope for their shelf (pins and the gadgets shown beside each of
their sessions).

The default implementation keeps everything under ``<data_dir>/gadgets``
(:class:`~omnigent.stores.gadget_store.local.LocalGadgetStore`). A deployment
can pass another implementation to ``create_app(gadget_store=...)``, for example
one backed by a database table of ``(scope, path, value)`` rows: ``create_json``
then maps to an insert that fails on a duplicate key, ``list_paths`` to a
prefix query, :meth:`GadgetFiles.delete_tree` to a delete of the rows under a
path, and ``delete`` to a delete of every row under the gadget's scope.

Paths given to :class:`GadgetFiles` are relative POSIX paths such as
``"data/cards/c1/00000001.json"``; implementations reject absolute paths,
``.``/``..`` segments, empty segments and backslashes.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "GADGET_ID_PATTERN",
    "MAX_FILE_BYTES",
    "MAX_GADGET_NAME_CHARS",
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
    """A bounded resource (revisions, records, listing size) is full."""

    code = "CAPACITY_EXCEEDED"


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
    """

    id: str
    owner_id: str
    name: str
    created_at: float


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

        Readers see the whole tree disappear at once, never part of it.
        """
        raise NotImplementedError

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
    def create(self, owner_id: str, name: str) -> Gadget:
        """Create a gadget with a new ID (see :data:`GADGET_ID_PATTERN`), owned by *owner_id*."""

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
    def list_owned(self, owner_id: str) -> list[Gadget]:
        """Every gadget *owner_id* owns, oldest first."""

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
    def user_files(self, user_id: str) -> GadgetFiles:
        """One user's own file scope, for their shelf."""

    @abstractmethod
    def delete_user_files(self, user_id: str) -> None:
        """Delete every file in one user's scope, as when their account is deleted."""
