"""Thingy store: thingy metadata and the create-only JSON files thingies keep.

A thingy's metadata (owner and name) is one small record. Everything else a
thingy holds (revisions, publication, records, comments, the action ledger and
rule state) lives in create-only JSON files under its own scope, which
:mod:`omnigent.thingies.documents` builds revisioned records on. Each user also
has a file scope for their shelf (pins and the thingies shown beside each of
their sessions).

The default implementation keeps everything under ``<data_dir>/thingies``
(:class:`~omnigent.stores.thingy_store.local.LocalThingyStore`). A deployment
can pass another implementation to ``create_app(thingy_store=...)``, for example
one backed by a database table of ``(scope, path, value)`` rows: ``create_json``
then maps to an insert that fails on a duplicate key, ``list_paths`` to a
prefix query and ``delete`` to a delete of every row under the thingy's scope.

Paths given to :class:`ThingyFiles` are relative POSIX paths such as
``"data/cards/c1/00000001.json"``; implementations reject absolute paths,
``.``/``..`` segments, empty segments and backslashes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "MAX_FILE_BYTES",
    "MAX_THINGY_NAME_CHARS",
    "CapacityExceeded",
    "Conflict",
    "CorruptData",
    "InvalidInput",
    "NotFound",
    "StoreError",
    "Thingy",
    "ThingyFiles",
    "ThingyStore",
    "validate_relative_path",
]

# The largest JSON file a thingy keeps; a revision's HTML is the largest one.
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_THINGY_NAME_CHARS = 120


# ---------------------------------------------------------------------------
# Errors shared by the store and the document store built on it.


class StoreError(Exception):
    """Base class of every typed thingy storage error."""

    code = "STORE_ERROR"


class NotFound(StoreError):
    """The thingy, record or file does not exist."""

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
    """Split a :class:`ThingyFiles` path into its segments, refusing unsafe ones.

    :param path: e.g. ``"data/cards/c1/00000001.json"``.
    :returns: The segments, e.g. ``("data", "cards", "c1", "00000001.json")``.
    :raises InvalidInput: For an empty or absolute path, ``.``/``..`` or empty
        segments, backslashes or non-printable characters.
    """
    if not isinstance(path, str) or not path or not path.isprintable() or "\\" in path:
        raise InvalidInput("Invalid thingy file path")
    parts = tuple(path.split("/"))
    if path.startswith("/") or any(part in ("", ".", "..") for part in parts):
        raise InvalidInput("Invalid thingy file path")
    return parts


# ---------------------------------------------------------------------------
# Interfaces


@dataclass(frozen=True)
class Thingy:
    """One thingy's metadata.

    :param id: 32 lowercase hex characters, e.g. ``"3f2a…"`` (a UUID without dashes).
    :param owner_id: The owner's user ID, e.g. ``"alice@example.com"``.
    :param name: Display name, 1 to :data:`MAX_THINGY_NAME_CHARS` characters.
    :param created_at: Epoch seconds.
    """

    id: str
    owner_id: str
    name: str
    created_at: float


class ThingyFiles(ABC):
    """Create-only JSON files in one scope (a thingy's, or one user's shelf).

    There is no overwrite or delete: the document store derives every change as
    a new file, so implementations only need an atomic create.
    """

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
        :raises NotFound: When its parent directory does not exist, or the thingy
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

        :raises NotFound: When the thingy whose scope this is was deleted; a
            thingy's scope is never created again once it is gone.
        """


class ThingyStore(ABC):
    """Thingy metadata plus each thingy's and each user's :class:`ThingyFiles`.

    Implementations are synchronous and thread-safe; the routes call them from
    worker threads. Access control is not the store's: callers authorize first.
    """

    def __init__(self, storage_location: str) -> None:
        """
        :param storage_location: Backend-specific location, e.g. the directory
            ``"/home/alice/.omnigent/thingies"`` or a database URI.
        """
        self.storage_location = storage_location

    @abstractmethod
    def create(self, owner_id: str, name: str) -> Thingy:
        """Create a thingy with a new random ID, owned by *owner_id*."""

    @abstractmethod
    def get(self, thingy_id: str) -> Thingy | None:
        """The thingy, or ``None`` when it does not exist or *thingy_id* is malformed."""

    def get_many(self, thingy_ids: Iterable[str]) -> dict[str, Thingy]:
        """The existing thingies among *thingy_ids*, keyed by ID.

        The default calls :meth:`get` per ID; a database store can batch it.
        """
        found: dict[str, Thingy] = {}
        for thingy_id in dict.fromkeys(thingy_ids):
            thingy = self.get(thingy_id)
            if thingy is not None:
                found[thingy_id] = thingy
        return found

    @abstractmethod
    def list_owned(self, owner_id: str) -> list[Thingy]:
        """Every thingy *owner_id* owns, oldest first."""

    @abstractmethod
    def rename(self, thingy_id: str, name: str) -> Thingy | None:
        """Rename a thingy; ``None`` when it does not exist."""

    @abstractmethod
    def delete(self, thingy_id: str) -> bool:
        """Delete a thingy with every file in its scope.

        Writes and renames racing it fail (``NotFound``, or ``None`` from
        :meth:`rename`) rather than leave files of the deleted thingy behind.

        :returns: ``True`` when it existed.
        """

    @abstractmethod
    def thingy_files(self, thingy_id: str) -> ThingyFiles:
        """The file scope of an existing thingy."""

    @abstractmethod
    def user_files(self, user_id: str) -> ThingyFiles:
        """One user's own file scope, for their shelf."""

    @abstractmethod
    def delete_user_files(self, user_id: str) -> None:
        """Delete every file in one user's scope, as when their account is deleted."""
