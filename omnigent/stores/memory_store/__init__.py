"""Long-term memory persistence contract.

One interface sits behind the ``memory_*`` tools and the ``/v1/memory`` routes.
Backends (SQLAlchemy by default, Hindsight on request, and deployment-specific
stores) implement it, so a tool behaves the same on every backend.

The contract is deliberately the common subset of what backends can do:

* entries are keyed by ``(workspace, actor_id, path)``; the workspace comes from
  the ambient :func:`omnigent.db.db_models.current_workspace_id` scope;
* a path cannot be renamed;
* listing is paginated, in an order the backend chooses;
* search is optional (:attr:`MemoryStore.supports_search`); where offered it
  covers a single actor and returns at most :data:`MAX_SEARCH_LIMIT` hits,
  ranked, without pagination;
* there are no versions or etags; a write replaces the entry at its path.

``actor_id`` is whose memory it is. Callers derive it from the authenticated
user. It is never accepted from an agent or a request body.
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from dataclasses import dataclass

from omnigent.errors import ErrorCode, OmnigentError

# A user id of up to 128 characters plus an owner-kind prefix such as ``user:``.
MAX_ACTOR_ID_LENGTH = 160
MAX_PATH_LENGTH = 512
MAX_DESCRIPTION_LENGTH = 512
# UTF-8 bytes before compression. Leaves headroom for zstd framing under the
# SQLAlchemy backend's 16,383-byte stored column (``MEMORY_CONTENT_MAX_STORED_BYTES``).
MAX_CONTENT_BYTES = 16_000
MAX_SEARCH_LIMIT = 100
DEFAULT_SEARCH_LIMIT = 10
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 100
_PATH_PUNCTUATION = frozenset("/-_. ")
MEMORY_SEARCH_UNSUPPORTED = "Memory search is not available on this memory backend"


@dataclass(frozen=True)
class MemoryEntry:
    """
    One stored memory.

    :param actor_id: Owner of the memory, e.g. ``"user:alice"``.
    :param path: Absolute, case-sensitive path, e.g. ``"/projects/api/gotchas"``.
    :param content: Markdown or plain text body.
    :param description: One-line summary returned with listings and search hits, or
        ``None``.
    :param created_at: Unix epoch seconds when the entry was first written.
    :param updated_at: Unix epoch seconds of the last write.
    """

    actor_id: str
    path: str
    content: str
    description: str | None
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class MemoryHit:
    """
    One ranked search result.

    :param entry: The matching entry.
    :param score: Backend-specific relevance; higher is better.
    """

    entry: MemoryEntry
    score: float


@dataclass(frozen=True)
class MemoryPage:
    """
    One page of :meth:`MemoryStore.list`.

    :param entries: Entries in the backend's listing order. ``content`` is empty:
        listing returns paths and descriptions only.
    :param next_page_token: Opaque token for the next page, or ``None``.
    """

    entries: list[MemoryEntry]
    next_page_token: str | None = None


def validate_actor_id(actor_id: str) -> str:
    """
    Reject an empty or oversized actor id.

    :param actor_id: Candidate actor id.
    :returns: *actor_id* unchanged.
    :raises OmnigentError: ``INVALID_INPUT`` when unusable. Backends call this
        on every operation so a missing actor fails closed instead of falling
        back to a shared default.
    """
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise OmnigentError("actor_id is required", code=ErrorCode.INVALID_INPUT)
    if len(actor_id) > MAX_ACTOR_ID_LENGTH:
        raise OmnigentError(
            f"actor_id exceeds {MAX_ACTOR_ID_LENGTH} characters", code=ErrorCode.INVALID_INPUT
        )
    return actor_id


def validate_path(path: str) -> str:
    """
    Require an absolute path of plain names.

    Each segment is letters, digits, spaces, ``-``, ``_`` and ``.``, does not start
    or end with a space, and is not ``.`` or ``..``. Paths are listed in later
    sessions' startup instructions, so they may not carry markup, quotes or line
    breaks.

    :param path: Candidate path, e.g. ``"/user/preferences/style"``.
    :returns: *path* unchanged.
    :raises OmnigentError: ``INVALID_INPUT`` when the path is malformed.
    """
    if not isinstance(path, str) or not path.startswith("/") or path == "/":
        raise OmnigentError(
            "path must be absolute, e.g. '/user/preferences'", code=ErrorCode.INVALID_INPUT
        )
    if len(path) > MAX_PATH_LENGTH:
        raise OmnigentError(
            f"path exceeds {MAX_PATH_LENGTH} characters", code=ErrorCode.INVALID_INPUT
        )
    if any(segment in ("", ".", "..") for segment in path[1:].split("/")):
        raise OmnigentError(
            "path may not contain empty, '.' or '..' segments", code=ErrorCode.INVALID_INPUT
        )
    if any(not (char.isalnum() or char in _PATH_PUNCTUATION) for char in path):
        raise OmnigentError(
            "path may contain only letters, digits, spaces, '-', '_', '.' and '/'",
            code=ErrorCode.INVALID_INPUT,
        )
    if any(segment != segment.strip(" ") for segment in path[1:].split("/")):
        raise OmnigentError(
            "path segments may not start or end with a space", code=ErrorCode.INVALID_INPUT
        )
    return path


def validate_path_prefix(prefix: str | None) -> str | None:
    """
    Normalize an optional listing/search prefix.

    A prefix matches whole path segments: ``"/projects"`` matches
    ``"/projects/api"`` but not ``"/projects-old/x"``.

    :param prefix: ``None``/empty for no filter, else an absolute path.
    :returns: The prefix without a trailing slash, or ``None``.
    :raises OmnigentError: ``INVALID_INPUT`` when malformed.
    """
    if prefix is None or prefix in ("", "/"):
        return None
    return validate_path(prefix.rstrip("/"))


def validate_content(content: str, description: str | None) -> None:
    """
    Enforce entry size limits.

    :param content: Entry body.
    :param description: Optional one-line summary.
    :raises OmnigentError: ``INVALID_INPUT`` when empty or over a limit.
    """
    if not isinstance(content, str) or not content.strip():
        raise OmnigentError("content is required", code=ErrorCode.INVALID_INPUT)
    if len(content.encode()) > MAX_CONTENT_BYTES:
        raise OmnigentError(
            f"content exceeds {MAX_CONTENT_BYTES} bytes", code=ErrorCode.INVALID_INPUT
        )
    if description is not None and len(description) > MAX_DESCRIPTION_LENGTH:
        raise OmnigentError(
            f"description exceeds {MAX_DESCRIPTION_LENGTH} characters",
            code=ErrorCode.INVALID_INPUT,
        )


def validate_limit(limit: int, maximum: int) -> int:
    """
    Require ``1 <= limit <= maximum``.

    :param limit: Requested page size or result count.
    :param maximum: Inclusive upper bound.
    :returns: *limit* unchanged.
    :raises OmnigentError: ``INVALID_INPUT`` when out of range.
    """
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise OmnigentError(f"limit must be between 1 and {maximum}", code=ErrorCode.INVALID_INPUT)
    return limit


def path_in_prefix(path: str, prefix: str | None) -> bool:
    """
    Whether *path* is at or under *prefix* on a segment boundary.

    :param path: A stored path, e.g. ``"/projects/api/notes"``.
    :param prefix: A normalized prefix from :func:`validate_path_prefix`.
    :returns: ``True`` when there is no prefix or *path* is under it.
    """
    return prefix is None or path == prefix or path.startswith(prefix + "/")


class MemoryStore(ABC):
    """
    Abstract memory backend.

    Every method takes the ``actor_id`` explicitly and must call
    :func:`validate_actor_id` first; the workspace comes from the ambient scope.

    :cvar supports_search: Whether :meth:`search` is implemented. The server
        offers ``memory_search`` only for a backend that sets it.
    """

    supports_search: bool = False

    @abstractmethod
    def write(
        self, actor_id: str, path: str, content: str, description: str | None = None
    ) -> MemoryEntry:
        """
        Create the entry at *path*, or replace it.

        :param actor_id: Owner of the memory.
        :param path: Absolute path, see :func:`validate_path`.
        :param content: Entry body.
        :param description: Optional one-line summary, returned with listings and
            search hits.
        :returns: The stored entry.
        """

    @abstractmethod
    def get(self, actor_id: str, path: str) -> MemoryEntry | None:
        """
        Read one entry.

        :param actor_id: Owner of the memory.
        :param path: Absolute path.
        :returns: The entry, or ``None`` when absent.
        """

    @abstractmethod
    def list(
        self,
        actor_id: str,
        path_prefix: str | None = None,
        page_token: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> MemoryPage:
        """
        List entries without content, one bounded page per call.

        Paging from the first page to the last returns every entry once, in a
        stable order the backend chooses.

        :param actor_id: Owner of the memory.
        :param path_prefix: Only paths at or under this prefix.
        :param page_token: ``next_page_token`` of the previous page; opaque.
        :param page_size: Page size, at most :data:`MAX_PAGE_SIZE`.
        :returns: One page of entries with empty ``content``. A page may hold
            fewer than *page_size* entries while more remain.
        """

    def search(
        self,
        actor_id: str,
        query: str,
        path_prefix: str | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> builtins.list[MemoryHit]:
        """
        Rank the actor's entries against *query*.

        Backends that set :attr:`supports_search` override this.

        :param actor_id: Owner of the memory.
        :param query: Free-text query.
        :param path_prefix: Only paths at or under this prefix.
        :param limit: Result count, at most :data:`MAX_SEARCH_LIMIT`.
        :returns: Hits, best first, without pagination.
        :raises OmnigentError: ``NOT_FOUND`` on a backend without search.
        """
        del actor_id, query, path_prefix, limit
        raise OmnigentError(MEMORY_SEARCH_UNSUPPORTED, code=ErrorCode.NOT_FOUND)

    @abstractmethod
    def delete(self, actor_id: str, path: str) -> bool:
        """
        Delete one entry.

        :param actor_id: Owner of the memory.
        :param path: Absolute path.
        :returns: ``True`` when an entry was deleted.
        """
