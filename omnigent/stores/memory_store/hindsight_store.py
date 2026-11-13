"""Hindsight-backed memory store (opt-in: ``omnigent[hindsight]``).

Maps the :class:`MemoryStore` contract onto a Hindsight memory bank:

* one **bank per (workspace, actor)**, so memory is isolated exactly as the
  other backends isolate it;
* one Hindsight **document per path** (``document_id`` is the percent-encoded
  path), written with ``update_mode="replace"`` so a write replaces the entry;
* ``get``/``list``/``delete`` use the documents API, ``search`` uses ``recall``.

Hindsight extracts facts from what it retains, so ``search`` returns the
documents whose extracted facts best match the query, ranked best first. The
credential belongs to the server process; it is never read from agent specs.
"""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import inspect
import logging
import re
import threading
from collections.abc import Awaitable, Mapping
from datetime import datetime
from typing import Any, TypeVar
from urllib.parse import quote, unquote

from omnigent.db.db_models import current_workspace_id
from omnigent.db.utils import now_epoch
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.memory_store import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_SEARCH_LIMIT,
    MAX_PAGE_SIZE,
    MAX_SEARCH_LIMIT,
    MemoryEntry,
    MemoryHit,
    MemoryPage,
    MemoryStore,
    path_in_prefix,
    validate_actor_id,
    validate_content,
    validate_limit,
    validate_path,
    validate_path_prefix,
)

_logger = logging.getLogger(__name__)
_T = TypeVar("_T")

DEFAULT_API_URL = "https://api.hindsight.vectorize.io"
# First release with ``Hindsight.documents`` and ``aretain(update_mode=...)``.
MIN_CLIENT_VERSION = "0.5.0"
_REQUEST_TIMEOUT_SECONDS = 60.0
_RECALL_MAX_TOKENS = 4096
_BANK_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _document_id(path: str) -> str:
    """Percent-encode *path* so it is one URL segment (no slashes)."""
    return quote(path, safe="")


def _path_from_document_id(document_id: str) -> str:
    """Inverse of :func:`_document_id`."""
    return unquote(document_id)


def _offset_from_page_token(page_token: str | None) -> int:
    """
    Read a listing token: the offset into Hindsight's document list.

    :param page_token: ``next_page_token`` of the previous page, or ``None``.
    :returns: The offset, ``0`` for the first page.
    :raises OmnigentError: ``INVALID_INPUT`` for a token this store did not issue.
    """
    if page_token is None:
        return 0
    if not page_token.isdecimal() or len(page_token) > 12:
        raise OmnigentError("page_token is not from a previous page", code=ErrorCode.INVALID_INPUT)
    return int(page_token)


def bank_id_for(workspace_id: int, actor_id: str) -> str:
    """
    Bank holding one actor's memory in one workspace.

    A hash of the exact ``(workspace, actor)`` pair keeps two actors whose ids
    sanitize alike (``"user:a b"``, ``"user:a_b"``) in different banks.

    :param workspace_id: Tenant scope.
    :param actor_id: Memory owner.
    :returns: A bank id, e.g. ``"omnigent-w0-user_alice-1f3c9a07b2"``.
    """
    digest = hashlib.sha256(f"{workspace_id}\0{actor_id}".encode()).hexdigest()[:10]
    readable = _BANK_UNSAFE.sub("_", actor_id)[:48]
    return f"omnigent-w{workspace_id}-{readable}-{digest}"


def _epoch(value: Any, fallback: int) -> int:
    """Convert a Hindsight timestamp (datetime or ISO string) to epoch seconds."""
    if isinstance(value, datetime):
        return int(value.timestamp())
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return fallback
    return fallback


def _field(obj: Any, name: str) -> Any:
    """Read *name* from a Hindsight model or a plain dict.

    ``hindsight-client`` 0.5–0.8 returns document-list items as dicts; later
    releases return ``DocumentListItem`` models.
    """
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


class _LoopThread:
    """One event loop on a daemon thread, so the async client lives on a single loop."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="omnigent-hindsight-memory", daemon=True
        )
        self._thread.start()

    def run(self, coroutine: Awaitable[_T]) -> _T:
        """Run *coroutine* on the loop and wait for its result."""
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)  # type: ignore[arg-type]
        return future.result(timeout=_REQUEST_TIMEOUT_SECONDS)


def create_hindsight_client(api_url: str | None, api_key: str) -> Any:
    """
    Build a Hindsight client, importing the optional dependency lazily.

    :param api_url: Base URL, or ``None`` for Hindsight Cloud.
    :param api_key: Server-side API key, e.g. resolved from ``env:HINDSIGHT_API_KEY``.
    :returns: A ``hindsight_client.Hindsight``.
    :raises OmnigentError: ``INVALID_INPUT`` when ``hindsight-client`` is missing
        or older than :data:`MIN_CLIENT_VERSION`.
    """
    try:
        import hindsight_client
    except ImportError as exc:
        raise OmnigentError(
            "The Hindsight memory backend needs the optional dependency: "
            "pip install 'omnigent[hindsight]'",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    # One document per path needs the documents API and replace-on-retain.
    retain_parameters = inspect.signature(hindsight_client.Hindsight.aretain).parameters
    if (
        not hasattr(hindsight_client.Hindsight, "documents")
        or "update_mode" not in retain_parameters
    ):
        raise OmnigentError(
            f"The Hindsight memory backend needs hindsight-client>={MIN_CLIENT_VERSION}: "
            f"pip install 'hindsight-client>={MIN_CLIENT_VERSION}'",
            code=ErrorCode.INVALID_INPUT,
        )
    return hindsight_client.Hindsight(
        base_url=api_url or DEFAULT_API_URL, api_key=api_key, timeout=30.0
    )


class HindsightMemoryStore(MemoryStore):
    """
    :class:`MemoryStore` on Hindsight.

    Hindsight does its own fact extraction, so a ``write`` returns once Hindsight
    has accepted and processed the document, which can take a few seconds.
    Listing follows Hindsight's own order (most recently updated first) and pages
    by offset, so a write or delete while paging can shift entries between pages.
    """

    supports_search = True

    def __init__(self, client: Any) -> None:
        """
        :param client: A ``hindsight_client.Hindsight`` (or a compatible object
            exposing ``acreate_bank``, ``aretain``, ``arecall`` and
            ``documents.{get,list,delete}_document``).
        """
        self._client = client
        self._loop = _LoopThread()
        self._banks_created: set[str] = set()
        self._banks_lock = threading.Lock()

    # ── plumbing ──────────────────────────────────────────────────

    def _call(self, coroutine: Awaitable[_T], what: str) -> _T:
        """Run a client coroutine, mapping backend failures to a clean error."""
        try:
            return self._loop.run(coroutine)
        except OmnigentError:
            raise
        except Exception as exc:
            _logger.warning("Hindsight %s failed: %s", what, type(exc).__name__, exc_info=True)
            raise OmnigentError(
                f"Memory backend error during {what}", code=ErrorCode.INTERNAL_ERROR
            ) from exc

    def _is_missing(self, exc: BaseException) -> bool:
        """Whether *exc* is a Hindsight 404."""
        return getattr(exc, "status", None) == 404 or type(exc).__name__ == "NotFoundException"

    def _ensure_bank(self, bank: str) -> None:
        """Create *bank* once per process; creation of an existing bank is ignored."""
        with self._banks_lock:
            if bank in self._banks_created:
                return
        try:
            self._loop.run(self._client.acreate_bank(bank_id=bank, name=bank))
        except Exception:
            # Most likely the bank already exists. A real outage fails the next call.
            _logger.debug("create_bank(%r) failed; assuming it exists", bank, exc_info=True)
        with self._banks_lock:
            self._banks_created.add(bank)

    async def _fetch(self, bank: str, path: str) -> Any | None:
        """Fetch one document, or ``None`` when absent."""
        try:
            return await self._client.documents.get_document(
                bank_id=bank, document_id=_document_id(path)
            )
        except Exception as exc:
            if self._is_missing(exc):
                return None
            raise

    def _entry(
        self, actor_id: str, path: str, document: Any, *, with_content: bool
    ) -> MemoryEntry:
        """Build an entry from a Hindsight document or document-list item."""
        now = now_epoch()
        metadata = _field(document, "document_metadata") or {}
        description = (
            metadata.get("description") or None if isinstance(metadata, Mapping) else None
        )
        created = _epoch(_field(document, "created_at"), now)
        return MemoryEntry(
            actor_id=actor_id,
            path=path,
            content=(_field(document, "original_text") or "") if with_content else "",
            description=description,
            created_at=created,
            updated_at=_epoch(_field(document, "updated_at"), created),
        )

    # ── MemoryStore ───────────────────────────────────────────────

    def write(
        self, actor_id: str, path: str, content: str, description: str | None = None
    ) -> MemoryEntry:
        validate_actor_id(actor_id)
        validate_path(path)
        validate_content(content, description)
        bank = bank_id_for(current_workspace_id(), actor_id)
        self._ensure_bank(bank)
        self._call(
            self._client.aretain(
                bank_id=bank,
                content=content,
                document_id=_document_id(path),
                update_mode="replace",
                metadata={"path": path, "description": description or ""},
            ),
            "write",
        )
        # Hindsight stamps the timestamps; read them back so a write reports the
        # same values a later get does.
        document = self._call(self._fetch(bank, path), "write")
        if document is None:
            now = now_epoch()
            return MemoryEntry(actor_id, path, content, description, now, now)
        return self._entry(actor_id, path, document, with_content=True)

    def get(self, actor_id: str, path: str) -> MemoryEntry | None:
        validate_actor_id(actor_id)
        validate_path(path)
        bank = bank_id_for(current_workspace_id(), actor_id)
        document = self._call(self._fetch(bank, path), "get")
        return (
            None if document is None else self._entry(actor_id, path, document, with_content=True)
        )

    def list(
        self,
        actor_id: str,
        path_prefix: str | None = None,
        page_token: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> MemoryPage:
        validate_actor_id(actor_id)
        prefix = validate_path_prefix(path_prefix)
        validate_limit(page_size, MAX_PAGE_SIZE)
        offset = _offset_from_page_token(page_token)
        bank = bank_id_for(current_workspace_id(), actor_id)

        async def fetch() -> tuple[list[Any], Any]:
            try:
                page = await self._client.documents.list_documents(
                    bank_id=bank,
                    q=_document_id(prefix) if prefix else None,
                    limit=page_size,
                    offset=offset,
                )
            except Exception as exc:
                if self._is_missing(exc):
                    return [], 0
                raise
            return list(getattr(page, "items", None) or []), getattr(page, "total", None)

        items, total = self._call(fetch(), "list")
        # Advance by what Hindsight returned, which may be fewer than asked for.
        consumed = offset + len(items)
        more = consumed < total if isinstance(total, int) else len(items) == page_size
        next_token = str(consumed) if items and more else None
        # ``q`` matches anywhere in the document id; keep whole-segment matches only,
        # so a page can come back short while more remain.
        entries = [
            entry
            for entry in (
                self._entry(
                    actor_id, _path_from_document_id(_field(item, "id")), item, with_content=False
                )
                for item in items
            )
            if path_in_prefix(entry.path, prefix)
        ]
        return MemoryPage(entries=entries, next_page_token=next_token)

    def search(
        self,
        actor_id: str,
        query: str,
        path_prefix: str | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> builtins.list[MemoryHit]:
        validate_actor_id(actor_id)
        prefix = validate_path_prefix(path_prefix)
        validate_limit(limit, MAX_SEARCH_LIMIT)
        if not isinstance(query, str) or not query.strip():
            raise OmnigentError("query is required", code=ErrorCode.INVALID_INPUT)
        bank = bank_id_for(current_workspace_id(), actor_id)

        async def recall() -> list[tuple[str, Any]]:
            try:
                response = await self._client.arecall(
                    bank_id=bank, query=query, max_tokens=_RECALL_MAX_TOKENS
                )
            except Exception as exc:
                if self._is_missing(exc):
                    return []
                raise
            # Facts come best-first; keep a document's first (best) rank.
            ranked: dict[str, int] = {}
            for result in getattr(response, "results", None) or []:
                document_id = _field(result, "document_id")
                if document_id and document_id not in ranked:
                    ranked[document_id] = len(ranked)
            hits: list[tuple[str, Any]] = []
            for document_id in ranked:
                path = _path_from_document_id(document_id)
                if not path_in_prefix(path, prefix):
                    continue
                document = await self._fetch(bank, path)
                if document is not None:
                    hits.append((path, document))
                if len(hits) >= limit:
                    break
            return hits

        found = self._call(recall(), "search")
        total = len(found)
        return [
            MemoryHit(
                entry=self._entry(actor_id, path, document, with_content=True),
                score=float(total - rank),
            )
            for rank, (path, document) in enumerate(found)
        ]

    def delete(self, actor_id: str, path: str) -> bool:
        validate_actor_id(actor_id)
        validate_path(path)
        bank = bank_id_for(current_workspace_id(), actor_id)

        async def remove() -> bool:
            try:
                await self._client.documents.delete_document(
                    bank_id=bank, document_id=_document_id(path)
                )
            except Exception as exc:
                if self._is_missing(exc):
                    return False
                raise
            return True

        return self._call(remove(), "delete")
