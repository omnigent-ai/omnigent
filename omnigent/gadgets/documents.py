"""Revisioned JSON documents on a gadget's create-only files.

Documents live as create-only revision files in a :class:`GadgetFiles` scope.
The module is pure storage: it has no access control, schema validation,
request context or network dependencies, so callers authorize and validate
before calling in.

Layout under ``root_path``::

    <collection>/<id>/00000001.json   # create
    <collection>/<id>/00000002.json   # replace
    <collection>/<id>/00000003.json   # delete (tombstone)
    <collection>/<id>/00000004.json   # re-create after a tombstone
    <collection>/<id>/s00000002/00001001.json   # later revisions, one directory per segment
    .order/<collection>/s00000001/00000001.json  # the collection's order: {"id", "first"}

A record's revisions are numbered across segments of
``StoreLimits.revisions_per_segment`` files: the first segment sits in the
record directory itself and each later one in an ``sNNNNNNNN`` subdirectory, so
no directory grows past a segment's size and any revision's path follows from
its number.

There is no mutable head file and no overwrite or delete primitive in
:class:`GadgetFiles`. A writer reads the record, derives the single successor
slot (``latest + 1``) and creates that file atomically. Concurrent writers for
the same slot collide in ``create_json``; exactly one wins and the loser
re-reads the winner. Every revision is a self-describing envelope that carries
its own SHA-256 digest and the digest of its predecessor, so readers detect
altered revisions, gaps, foreign envelopes and broken chain links and fail
closed with :class:`CorruptData`.

Guarantees
----------
* Per-record linearizable replace/delete through ``if_version``. No promise
  across records: no multi-record transactions, and ``list`` is not a snapshot.
* Retried operations are idempotent. An operation is identified by
  ``operation_id`` plus a digest of the whole request (method, collection, id,
  ``if_version``, value, actor). The same operation with the same digest returns
  the original result; the same ``operation_id`` with a different request is a
  :class:`Conflict`. A mutation is only written when its precondition (record
  absent, or the head still equals ``if_version``) holds, so a retry is never
  applied twice. Retries that arrive after other writers advanced the record are
  answered from the ``StoreLimits.idempotency_window`` most recent revisions;
  older ones fail closed with :class:`Conflict`. Re-creating a tombstoned record also
  checks an index of the record's creates (``.creates``), so a late retry of a create
  cannot bring a deleted record back, without reading its history. A create notes itself
  in that index after its revision commits; a writer that stopped in between leaves only
  the newest revision unindexed, and the next write to the record (or a retry of that
  create) notes it first, so no create is ever unindexed once older revisions follow it.
* Tombstones are revisions, so creation can never reset a record's history, a
  stale ``if_version`` from a deleted generation cannot touch a re-created
  record, and a stale ``create`` retry cannot resurrect a deleted record.
* Deleted records give their capacity back. When a create finds the collection
  full, the store removes records whose tombstone is older than
  ``StoreLimits.tombstone_retention_s`` when its :class:`GadgetFiles` can delete
  (``supports_delete``). Removal first appends a ``purge``
  revision, which makes writers of that record wait, then releases the record's
  capacity claim and deletes its directory. One remover at a time holds a
  create-only removal ticket for that purge revision and deletes only while the
  record's newest revision is still it, checked right before each delete, so a
  remover that resumes late never deletes the record created again after it.
  The guarantees above hold for the retention period; a retry arriving after a
  record was removed is a new request.
* ``get`` verifies the newest revision and its predecessor link; it finds the newest by
  reading revisions by number (a doubling probe, then bisection), so a read costs about
  ``2 * log2(revisions)`` file reads and no directory listing. ``verify_record`` verifies
  the whole chain and the numbering of the record's directories.
* ``list`` pages records in the order they were first created. Each new record (or
  one created again after it was removed) first gets the next numbered entry in the
  collection's order, naming its ID and the digest of its first revision; a page reads
  entries by number and skips those whose record is deleted, was removed and created
  again, or never got that first revision. Entries are never removed, so a page whose
  scan budget runs out on old entries is short and continues with its cursor.
* Version tokens and list cursors are opaque, versioned and scoped to the
  store root, collection and record, so a token from another record or store
  fails with :class:`InvalidInput` instead of matching by accident.

Limits (see :class:`StoreLimits`; nothing is silently truncated)
----------------------------------------------------------------
* A document value is JSON, at most 32 KiB as compact UTF-8, nested at most 16
  containers deep, at most 16384 nodes, integers within +-(2**53 - 1).
* Collection and record ids: ``[a-z0-9][a-z0-9_-]{0,63}`` (lowercase only so
  case-insensitive stores cannot alias records). Operation ids:
  ``[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}``.
* At most 10 million revisions per record (in segments of 1000) and 10000
  record IDs per collection, enforced by create-only slots under ``.capacity``
  even with concurrent creates; a new record probes a short run of slots, so a
  collection can report full a little before every slot is taken, and removed
  records free their slots. A page
  limit of 1..200 with at most 500 records examined per ``list`` call and at
  most 48 KiB per page; when a budget runs out ``list`` returns the items found
  so far with a ``next_cursor``, so an empty page with a cursor is possible and a
  silently cut page is not.

The digests protect against accidental damage and partial edits; they are not
authentication. Someone with write access to the storage who recomputes digests
consistently can forge history.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import json
import math
import posixpath
import re
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from omnigent.stores.gadget_store import (
    CapacityExceeded,
    Conflict,
    CorruptData,
    GadgetFiles,
    InvalidInput,
    NotFound,
    StoreError,
)

__all__ = [
    "DEFAULT_LIMITS",
    "CapacityExceeded",
    "Conflict",
    "CorruptData",
    "DeleteResult",
    "Document",
    "DocumentPage",
    "ImmutableDocumentStore",
    "InvalidInput",
    "NotFound",
    "RecordSummary",
    "StoreError",
    "StoreLimits",
    "compute_envelope_digest",
    "generate_document_id",
]

FORMAT_VERSION: Final = 1

_CREATE: Final = "create"
_REPLACE: Final = "replace"
_DELETE: Final = "delete"
# Appended to a tombstone before the record is removed; writers wait for the removal.
_PURGE: Final = "purge"
_KINDS: Final = frozenset({_CREATE, _REPLACE, _DELETE, _PURGE})
_GONE: Final = frozenset({_DELETE, _PURGE})

_IDENTIFIER_RE: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_OPERATION_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_REVISION_NAME_RE: Final = re.compile(r"([0-9]{8})\.json")
_SEGMENT_NAME_RE: Final = re.compile(r"s([0-9]{8})")
_HEX64_RE: Final = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z"
)
_VERSION_BODY_RE: Final = re.compile(r"([0-9a-f]{16})\.([0-9]{1,9})\.([0-9]{1,9})\.([0-9a-f]{32})")
_TIMESTAMP_FORMAT: Final = "%Y-%m-%dT%H:%M:%S.%fZ"

_VERSION_PREFIX: Final = "v1."
_CURSOR_PREFIX: Final = "c1."
_MAX_TOKEN_LENGTH: Final = 512
_MAX_ACTOR_LENGTH: Final = 256
_MAX_ROOT_PATH_LENGTH: Final = 1024
_MAX_JSON_INTEGER: Final = 2**53 - 1
# Bytes reserved for the page wrapper and a cursor when sizing a page.
_PAGE_OVERHEAD_BYTES: Final = 320
# Capacity slots one new record probes; enough that a collection fills nearly to its limit.
_CAPACITY_PROBES: Final = 64
# The characters a record ID starts with, in sort order; reclaim starts its search at one.
_ID_ALPHABET: Final = "0123456789abcdefghijklmnopqrstuvwxyz"
_REVISION_DIGITS: Final = 8
# Entries per directory of a collection's order (``.order``).
_ORDER_PER_SEGMENT: Final = 1000
_ORDER_APPEND_ATTEMPTS: Final = 16
_MAX_REVISION_NUMBER: Final = 10**_REVISION_DIGITS - 1

_ENVELOPE_KEYS: Final = frozenset(
    {
        "format",
        "collection",
        "id",
        "revision",
        "generation",
        "kind",
        "prev_digest",
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
        "operation_id",
        "request_digest",
        "digest",
    }
)


# ---------------------------------------------------------------------------
# Public data types


# A listing position no collection reaches, but whose order file name stays short: a cursor
# naming a later one is refused before any file is read.
_MAX_LISTING_ORDINAL = 10**18


@dataclass(frozen=True)
class StoreLimits:
    """Bounds enforced by the store. Defaults are the production limits."""

    max_document_bytes: int = 32 * 1024
    max_json_depth: int = 16
    max_json_nodes: int = 16_384
    max_revisions_per_record: int = 10_000_000
    revisions_per_segment: int = 1000
    max_records_per_collection: int = 10_000
    # Not enforced: a sizing check against max_records_per_collection only. A collection's
    # listing order gains one entry per create (a record created again adds another) and
    # entries aren't removed with their records, so pages skip the dead ones.
    max_list_entries: int = 20_000
    max_list_limit: int = 200
    max_scan_per_list: int = 500
    max_page_bytes: int = 48 * 1024
    idempotency_window: int = 16
    # A deleted record keeps its history (and answers retries) this long before a create
    # that needs its capacity may remove it.
    tombstone_retention_s: int = 7 * 24 * 60 * 60
    # A removal that stopped this long ago is finished by the next writer of the record.
    purge_abandon_s: int = 10 * 60
    max_reclaim_per_create: int = 16

    @property
    def max_segments_per_record(self) -> int:
        return -(-self.max_revisions_per_record // self.revisions_per_segment)

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_revisions_per_record > _MAX_REVISION_NUMBER:
            raise ValueError("max_revisions_per_record exceeds the revision file name width")
        if self.revisions_per_segment > self.max_revisions_per_record:
            raise ValueError("revisions_per_segment exceeds max_revisions_per_record")
        if self.max_list_entries < self.max_records_per_collection:
            raise ValueError("max_list_entries must be at least max_records_per_collection")


DEFAULT_LIMITS: Final = StoreLimits()


@dataclass(frozen=True)
class Document:
    id: str
    value: Any
    version: str
    created_at: str
    updated_at: str
    created_by: str
    updated_by: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "value": copy.deepcopy(self.value),
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "created_by": self.created_by,
            "updated_by": self.updated_by,
        }


@dataclass(frozen=True)
class DeleteResult:
    id: str
    version: str
    deleted: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "version": self.version, "deleted": self.deleted}


@dataclass(frozen=True)
class DocumentPage:
    items: tuple[Document, ...]
    next_cursor: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"items": [item.to_dict() for item in self.items], "next_cursor": self.next_cursor}


@dataclass(frozen=True)
class RecordSummary:
    """Result of a full-chain verification."""

    revisions: int
    generation: int
    deleted: bool
    version: str


def generate_document_id(operation_id: str) -> str:
    """Deterministic id for a create that carries no client id.

    A retried create then targets the same record, which lets the store answer
    the retry idempotently instead of creating a duplicate.
    """
    _validate_operation_id(operation_id)
    return "d" + hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:32]


def compute_envelope_digest(envelope: Mapping[str, Any]) -> str:
    """SHA-256 over the canonical JSON of an envelope without its ``digest``."""
    body = {key: value for key, value in envelope.items() if key != "digest"}
    return _sha256(_canonical_json(body))


# ---------------------------------------------------------------------------
# Internal helpers


@dataclass(frozen=True)
class _Revision:
    revision: int
    generation: int
    kind: str
    prev_digest: str | None
    value: Any
    created_at: str
    updated_at: str
    created_by: str
    updated_by: str
    operation_id: str
    request_digest: str
    digest: str


@dataclass(frozen=True)
class _Ref:
    collection: str
    doc_id: str
    directory: str


@dataclass(frozen=True)
class _Head:
    count: int
    latest: _Revision
    previous: _Revision | None
    # Revision 1's digest: names this incarnation of the record in the collection's order.
    first_digest: str


# The store defines a ``list`` method, which would shadow the builtin in class-level annotations.
_IntList = list[int]
_StrList = list[str]


def _expect_document(result: Document | DeleteResult) -> Document:
    if not isinstance(result, Document):
        raise CorruptData(f"{result.id}: operation resolved to a delete revision")
    return result


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _encoded_size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _b64encode(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _b64decode(text: str) -> str:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")


def _validate_identifier(value: Any, what: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise InvalidInput(f"{what} must match [a-z0-9][a-z0-9_-]{{0,63}}")
    return value


def _validate_operation_id(value: Any) -> str:
    if not isinstance(value, str) or _OPERATION_ID_RE.fullmatch(value) is None:
        raise InvalidInput("operation_id must match [A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
    return value


def _validate_actor(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_ACTOR_LENGTH
        or not value.isprintable()
    ):
        raise InvalidInput(
            f"actor must be a printable string of 1..{_MAX_ACTOR_LENGTH} characters"
        )
    return value


def _is_timestamp(value: Any) -> bool:
    return isinstance(value, str) and _TIMESTAMP_RE.fullmatch(value) is not None


def _normalize_root(root_path: Any) -> str:
    if not isinstance(root_path, str) or not root_path or len(root_path) > _MAX_ROOT_PATH_LENGTH:
        raise InvalidInput("root_path must be a non-empty path")
    if "\\" in root_path or not root_path.isprintable():
        raise InvalidInput("root_path contains forbidden characters")
    trimmed = root_path.rstrip("/")
    if not trimmed:
        raise InvalidInput("root_path must not be the file system root")
    for index, segment in enumerate(trimmed.split("/")):
        if index == 0 and segment == "":
            continue  # absolute path
        if segment in ("", ".", ".."):
            raise InvalidInput("root_path must be canonical (no empty, '.' or '..' segments)")
    return trimmed


def _request_digest(
    *,
    method: str,
    collection: str,
    doc_id: str,
    if_version: str | None,
    actor: str,
    value: Any,
    has_value: bool,
) -> str:
    payload: dict[str, Any] = {
        "method": method,
        "collection": collection,
        "id": doc_id,
        "if_version": if_version,
        "actor": actor,
    }
    if has_value:
        payload["value"] = value
    return _sha256(_canonical_json(payload))


# ---------------------------------------------------------------------------
# Store


# Record locks the current thread holds; see ImmutableDocumentStore._load_head.
_HELD_RECORD_LOCKS = threading.local()


class ImmutableDocumentStore:
    """Create-only revision chains for JSON documents (see module docstring)."""

    def __init__(
        self,
        files: GadgetFiles,
        root_path: str,
        *,
        limits: StoreLimits = DEFAULT_LIMITS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._files = files
        self._root = _normalize_root(root_path)
        self._limits = limits
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def limits(self) -> StoreLimits:
        return self._limits

    # -- reads --------------------------------------------------------------

    def revision_count(self, collection: str, id: str) -> int:
        """How many revisions the record has, deletes included; 0 when it has none."""
        head = self._load_head(self._ref(collection, id))
        return 0 if head is None else head.count

    def get(self, collection: str, id: str) -> Document | None:
        """Newest live version of a document, or ``None`` if absent or deleted."""
        ref = self._ref(collection, id)
        head = self._load_head(ref)
        if head is None or head.latest.kind in _GONE:
            return None
        return self._document(ref, head.latest)

    def get_version(self, collection: str, id: str, version: str) -> Document | None:
        """Read an immutable version, including after later writes or a tombstone."""
        ref = self._ref(collection, id)
        number = self._validate_version(version, ref)
        head = self._load_head(ref)
        if head is None or not 1 <= number <= head.count:
            raise InvalidInput("The requested version does not exist")
        revision = self._read_revision(ref, number)
        if self._version_of(ref, revision) != version:
            raise InvalidInput("The requested version does not match its stored revision")
        if number > 1:
            self._check_link(ref, self._read_revision(ref, number - 1), revision)
        return None if revision.kind in _GONE else self._document(ref, revision)

    def incarnation(
        self, collection: str, id: str, version: str | None = None
    ) -> tuple[str, int] | None:
        """Which life of the record the newest version (or *version*) belongs to.

        A record created again after a delete starts a new generation, and one created
        again after its history was removed starts a new chain: two versions of one record
        with the same incarnation are edits of the same record. ``None`` when the record is
        absent or deleted now; a *version* that is gone or never existed raises
        :class:`InvalidInput`.
        """
        ref = self._ref(collection, id)
        head = self._load_head(ref)
        if version is None:
            if head is None or head.latest.kind in _GONE:
                return None
            return head.first_digest, head.latest.generation
        number = self._validate_version(version, ref)
        if head is None or not 1 <= number <= head.count:
            raise InvalidInput("The requested version does not exist")
        revision = self._read_revision(ref, number)
        if self._version_of(ref, revision) != version:
            raise InvalidInput("The requested version does not match its stored revision")
        return head.first_digest, revision.generation

    def list(self, collection: str, limit: int = 100, cursor: str | None = None) -> DocumentPage:
        """One page of live documents in the order they were first created; not a snapshot.

        Walks the collection's order (``.order``), whose entries are addressed by number,
        so a page reads at most ``max_scan_per_list`` entries and their records however
        many records, or deleted ones, the collection holds, and never lists the
        collection's directory.
        """
        _validate_identifier(collection, "collection")
        if type(limit) is not int or not 1 <= limit <= self._limits.max_list_limit:
            raise InvalidInput(f"limit must be an integer in 1..{self._limits.max_list_limit}")
        last = self._decode_cursor(cursor, collection) if cursor is not None else 0
        items: list[Document] = []
        page_bytes = _PAGE_OVERHEAD_BYTES
        more = True
        scanned = 0
        while len(items) < limit:
            if scanned >= self._limits.max_scan_per_list:
                break
            entry = self._order_entry(collection, last + 1)
            if entry is None:
                more = False
                break
            scanned += 1
            ref = self._ref(collection, entry[0])
            head = self._load_head(ref)
            if (
                head is not None
                and head.first_digest == entry[1]
                and head.latest.kind not in _GONE
            ):
                document = self._document(ref, head.latest)
                item_bytes = _encoded_size(document.to_dict()) + 1  # +1 for the comma
                if page_bytes + item_bytes > self._limits.max_page_bytes:
                    if not items:
                        raise CapacityExceeded("a single document does not fit in max_page_bytes")
                    break  # Leave it for the next page; the cursor stops before it.
                page_bytes += item_bytes
                items.append(document)
            last += 1
        return DocumentPage(
            items=tuple(items),
            next_cursor=self._encode_cursor(collection, last) if more else None,
        )

    # -- the collection's order ------------------------------------------------

    def _order_path(self, collection: str, number: int) -> str:
        segment = (number - 1) // _ORDER_PER_SEGMENT + 1
        return (
            f"{self._root}/.order/{collection}/s{segment:0{_REVISION_DIGITS}d}"
            f"/{number:0{_REVISION_DIGITS}d}.json"
        )

    def _order_entry(self, collection: str, number: int) -> tuple[str, str] | None:
        """Entry *number*: the record ID and its first revision's digest, or ``None``."""
        try:
            raw = self._files.read_json(self._order_path(collection, number))
        except NotFound:
            return None
        doc_id, first = raw.get("id"), raw.get("first")
        if (
            not isinstance(doc_id, str)
            or _IDENTIFIER_RE.fullmatch(doc_id) is None
            or not isinstance(first, str)
            or _HEX64_RE.fullmatch(first) is None
        ):
            raise CorruptData(f"{collection}: damaged order entry {number}", collection=collection)
        return doc_id, first

    def _order_end(self, collection: str) -> int:
        """How many entries the collection's order has. Entries are numbered without gaps,
        so the last is found by reading entries 1, 2, 4, ... and bisecting: about
        ``2 * log2(entries)`` reads and no listing."""

        def present(number: int) -> bool:
            try:
                self._files.read_json(self._order_path(collection, number))
            except NotFound:
                return False
            return True

        if not present(1):
            return 0
        low, high = 1, 2
        while present(high):
            low, high = high, high * 2
        while high - low > 1:
            middle = (low + high) // 2
            if present(middle):
                low = middle
            else:
                high = middle
        return low

    def _order_append(self, collection: str, doc_id: str, first: str) -> None:
        """Add an entry after the order's last; a writer that loses a slot takes the next."""
        end = self._order_end(collection)
        for _attempt in range(_ORDER_APPEND_ATTEMPTS):
            path = self._order_path(collection, end + 1)
            try:
                self._files.mkdirs(posixpath.dirname(path))
                self._files.create_json(path, {"id": doc_id, "first": first})
                return
            except Conflict:
                end = max(end + 1, self._order_end(collection))
        raise Conflict("collection is busy; retry shortly", reason="concurrent_write")

    def _record_name(self, collection: str, name: str) -> str:
        name = posixpath.basename(str(name).rstrip("/"))
        if _IDENTIFIER_RE.fullmatch(name) is None:
            raise CorruptData(
                f"{collection}: unexpected entry in collection directory", collection=collection
            )
        return name

    def verify_record(self, collection: str, id: str) -> RecordSummary | None:
        """Verify every revision and link of one record; ``None`` if it has no revisions."""
        ref = self._ref(collection, id)
        head = self._load_head(ref)
        if head is None:
            return None
        if self._count_revisions(ref) != head.count:
            raise self._corrupt(ref, "revision numbering has a gap or duplicate")
        successor = head.latest
        for number in range(head.count - 1, 0, -1):
            revision = self._read_revision(ref, number)
            self._check_link(ref, revision, successor)
            successor = revision
        return RecordSummary(
            revisions=head.count,
            generation=head.latest.generation,
            deleted=head.latest.kind in _GONE,
            version=self._version_of(ref, head.latest),
        )

    # -- writes -------------------------------------------------------------

    def create(
        self, collection: str, id: str, value: Any, operation_id: str, actor: str
    ) -> Document:
        ref = self._ref(collection, id)
        clean = self._validate_value(value)
        _validate_operation_id(operation_id)
        _validate_actor(actor)
        digest = _request_digest(
            method=_CREATE,
            collection=collection,
            doc_id=id,
            if_version=None,
            actor=actor,
            value=clean,
            has_value=True,
        )
        head = self._load_head(ref)
        if head is not None and head.latest.kind == _PURGE:
            if not self._older_than(head.latest, self._limits.purge_abandon_s):
                raise Conflict(
                    "document is being removed; retry shortly", reason="concurrent_write"
                )
            self._finish_purge(ref, head.latest)
            head = self._load_head(ref)
            if head is not None:
                raise Conflict("document changed while it was removed", reason="concurrent_write")
        claim = None
        if head is None:
            claim = self._ensure_record_capacity(collection, id)
        else:
            replay = self._find_operation(ref, head, operation_id, 2)
            if replay is not None:
                return _expect_document(self._replay_create(ref, head, replay, digest))
            if head.latest.kind != _DELETE:
                replay = self._find_operation(
                    ref, head, operation_id, self._limits.idempotency_window
                )
                if replay is not None:
                    return _expect_document(self._replay_create(ref, head, replay, digest))
                raise Conflict(
                    "document already exists",
                    reason="already_exists",
                    current_version=self._version_of(ref, head.latest),
                )
            # Re-creating over a tombstone: the same operation may have created an earlier
            # generation, which the record's index of creates finds in one read.
            replay = self._find_operation(
                ref, head, operation_id, self._limits.idempotency_window
            ) or self._indexed_create(ref, head, operation_id)
            if replay is not None:
                return _expect_document(self._replay_create(ref, head, replay, digest))
        return _expect_document(
            self._append(ref, head, _CREATE, clean, operation_id, actor, digest, claim=claim)
        )

    def replace(
        self,
        collection: str,
        id: str,
        value: Any,
        if_version: str,
        operation_id: str,
        actor: str,
    ) -> Document:
        ref = self._ref(collection, id)
        clean = self._validate_value(value)
        _validate_operation_id(operation_id)
        _validate_actor(actor)
        self._validate_version(if_version, ref)
        digest = _request_digest(
            method=_REPLACE,
            collection=collection,
            doc_id=id,
            if_version=if_version,
            actor=actor,
            value=clean,
            has_value=True,
        )
        return _expect_document(
            self._mutate_existing(ref, _REPLACE, clean, if_version, operation_id, actor, digest)
        )

    def delete(
        self, collection: str, id: str, if_version: str, operation_id: str, actor: str
    ) -> DeleteResult:
        ref = self._ref(collection, id)
        _validate_operation_id(operation_id)
        _validate_actor(actor)
        self._validate_version(if_version, ref)
        digest = _request_digest(
            method=_DELETE,
            collection=collection,
            doc_id=id,
            if_version=if_version,
            actor=actor,
            value=None,
            has_value=False,
        )
        result = self._mutate_existing(ref, _DELETE, None, if_version, operation_id, actor, digest)
        if not isinstance(result, DeleteResult):
            raise CorruptData(
                "delete resolved to a non-delete revision", collection=collection, doc_id=id
            )
        return result

    def answer(
        self,
        kind: str,
        collection: str,
        id: str,
        value: Any,
        if_version: str | None,
        operation_id: str,
        actor: str,
    ) -> Document | DeleteResult | None:
        """Answer a write without writing it: ``None`` when it would change the record.

        A retry of an earlier identical write gets that write's receipt, and a write that
        can't land gets the error it would get (``Conflict``, ``NotFound``). A caller that
        keeps derived state about records (rules) uses this to tell those apart from a
        write that changes something, which it must plan for first.
        """
        if kind not in (_CREATE, _REPLACE, _DELETE):
            raise InvalidInput(f"unknown write kind {kind!r}")
        ref = self._ref(collection, id)
        clean = None if kind == _DELETE else self._validate_value(value)
        _validate_operation_id(operation_id)
        _validate_actor(actor)
        if kind != _CREATE:
            self._validate_version(if_version, ref)
        digest = _request_digest(
            method=kind,
            collection=collection,
            doc_id=id,
            if_version=if_version if kind != _CREATE else None,
            actor=actor,
            value=clean,
            has_value=kind != _DELETE,
        )
        head = self._load_head(ref)
        if kind == _CREATE:
            if head is None:
                return None
            if head.latest.kind == _PURGE:
                if not self._older_than(head.latest, self._limits.purge_abandon_s):
                    raise Conflict(
                        "document is being removed; retry shortly", reason="concurrent_write"
                    )
                return None
            replay = self._find_operation(ref, head, operation_id, self._limits.idempotency_window)
            if replay is None and head.latest.kind == _DELETE:
                replay = self._indexed_create(ref, head, operation_id)
            if replay is not None:
                return self._replay_create(ref, head, replay, digest)
            if head.latest.kind != _DELETE:
                raise Conflict(
                    "document already exists",
                    reason="already_exists",
                    current_version=self._version_of(ref, head.latest),
                )
            return None
        if head is None:
            raise NotFound("document does not exist")
        replay = self._find_operation(ref, head, operation_id, self._limits.idempotency_window)
        if replay is not None:
            return self._replay(ref, replay, digest)
        if head.latest.kind in _GONE:
            raise NotFound("document was deleted")
        current = self._version_of(ref, head.latest)
        if current != if_version:
            raise Conflict(
                "document changed since if_version",
                reason="version_mismatch",
                current_version=current,
            )
        return None

    def retire(self, collection: str, id: str, actor: str) -> bool:
        """Delete a record and remove it at once, giving its capacity back.

        For bookkeeping records nobody writes any more (a sealed rule epoch), which need no
        retention for retries. Uses the same fenced removal as reclaiming, so a writer racing
        it collides on the purge revision. Returns whether the record is gone; ``False``
        when the files cannot be deleted or another writer changed the record meanwhile.
        """
        if not self._files.supports_delete:
            return False
        ref = self._ref(collection, id)
        head = self._load_head(ref)
        if head is None:
            return True
        try:
            if head.latest.kind not in _GONE:
                version = self._version_of(ref, head.latest)
                self.delete(collection, id, version, f"retire.{head.latest.digest[:32]}", actor)
                head = self._load_head(ref)
                if head is None:
                    return True
            if head.latest.kind == _DELETE:
                self._purge(ref, head)
            elif head.latest.kind == _PURGE:
                self._finish_purge(ref, head.latest)
        except (Conflict, NotFound):
            return False
        return self._load_head(ref) is None

    # -- mutation core ------------------------------------------------------

    def _mutate_existing(
        self,
        ref: _Ref,
        kind: str,
        value: Any,
        if_version: str,
        operation_id: str,
        actor: str,
        digest: str,
    ) -> Document | DeleteResult:
        head = self._load_head(ref)
        if head is None:
            raise NotFound("document does not exist")
        # The whole replay window, even when the caller names the current version: an
        # operation ID used a few revisions ago for something else is reuse, refused, and
        # its first request keeps its replay.
        replay = self._find_operation(ref, head, operation_id, self._limits.idempotency_window)
        if replay is not None:
            return self._replay(ref, replay, digest)
        current = self._version_of(ref, head.latest)
        deleted = head.latest.kind in _GONE
        if deleted or current != if_version:
            if deleted:
                raise NotFound("document was deleted")
            raise Conflict(
                "document changed since if_version",
                reason="version_mismatch",
                current_version=current,
            )
        return self._append(ref, head, kind, value, operation_id, actor, digest)

    def _append(
        self,
        ref: _Ref,
        head: _Head | None,
        kind: str,
        value: Any,
        operation_id: str,
        actor: str,
        digest: str,
        *,
        claim: str | None = None,
    ) -> Document | DeleteResult:
        """Write the revision after *head* (the first one when ``None``).

        Under the record's lock (:meth:`_record_lock`), which removing a record and
        releasing an abandoned capacity claim also hold, it checks that *head* is still the
        record's newest revision, and for a first revision that its capacity *claim* is
        still held, before it writes anything: a writer that read the record before it was
        removed and created again never adds a revision to the new record, even when it
        stops right after writing.
        """
        with self._record_lock(ref) as held:
            if not held:
                raise Conflict("document is busy; retry shortly", reason="concurrent_write")
            return self._append_locked(ref, head, kind, value, operation_id, actor, digest, claim)

    def _append_locked(
        self,
        ref: _Ref,
        head: _Head | None,
        kind: str,
        value: Any,
        operation_id: str,
        actor: str,
        digest: str,
        claim: str | None,
    ) -> Document | DeleteResult:
        now = self._now()
        if head is None:
            number, generation, prev_digest = 1, 1, None
            created_at, created_by = now, actor
            if claim is not None and not self._holds_claim(claim, ref):
                raise Conflict(
                    "the record's capacity reservation expired; retry",
                    reason="concurrent_write",
                )
        else:
            number = head.count + 1
            if number > self._limits.max_revisions_per_record:
                raise CapacityExceeded(
                    "record already has the maximum of "
                    f"{self._limits.max_revisions_per_record} revisions"
                )
            previous = head.latest
            prev_digest = previous.digest
            # A removal can't run while this lock is held, so the record that is chained
            # now is the one this revision is written into.
            if not self._still_chained(ref, number, prev_digest):
                raise Conflict(
                    "document was removed while it was written", reason="concurrent_write"
                )
            if previous.kind == _CREATE:
                # A create notes itself in the index after its revision commits; a writer
                # that stopped in between left the newest revision unindexed. Every later
                # revision is written here, under the lock, so noting it now leaves no create
                # unindexed once it is no longer the newest. Only after the check above, so
                # a writer holding an old incarnation's head never notes it in a new one.
                self._index_create(ref, previous.operation_id, head.count)
            if (
                self._segment_of(number) > 1
                and (number - 1) % self._limits.revisions_per_segment == 0
            ):
                self._files.mkdirs(self._segment_directory(ref, self._segment_of(number)))
            if kind == _CREATE:
                generation, created_at, created_by = previous.generation + 1, now, actor
            else:
                generation, created_at, created_by = (
                    previous.generation,
                    previous.created_at,
                    previous.created_by,
                )
        body: dict[str, Any] = {
            "format": FORMAT_VERSION,
            "collection": ref.collection,
            "id": ref.doc_id,
            "revision": number,
            "generation": generation,
            "kind": kind,
            "prev_digest": prev_digest,
            "created_at": created_at,
            "updated_at": now,
            "created_by": created_by,
            "updated_by": actor,
            "operation_id": operation_id,
            "request_digest": digest,
        }
        if kind not in _GONE:
            body["value"] = copy.deepcopy(value)
        body["digest"] = compute_envelope_digest(body)
        if head is None:
            # Listed in the collection's order before it exists: an entry whose record never
            # got this first revision names no record and pages skip it.
            self._order_append(ref.collection, ref.doc_id, body["digest"])
            self._files.mkdirs(ref.directory)
        path = self._revision_path(ref, number)
        try:
            self._files.create_json(path, copy.deepcopy(body))
        except Conflict:
            return self._resolve_collision(ref, operation_id, digest)
        except NotFound:
            raise Conflict(
                "document was removed while it was written", reason="concurrent_write"
            ) from None
        if kind == _CREATE:
            self._index_create(ref, operation_id, number)
        return self._result(ref, self._parse_revision(ref, body, number))

    @contextlib.contextmanager
    def record_lock(self, collection: str, id: str) -> Iterator[bool]:
        """The lock every write of the record holds; yields whether it was taken."""
        with self._record_lock(self._ref(collection, id)) as held:
            yield held

    @contextlib.contextmanager
    def _record_lock(self, ref: _Ref) -> Iterator[bool]:
        """The lock every writer, remover and claim release of the record holds."""
        with self._files.exclusive(
            f"{self._root}/.removing/{ref.collection}/{ref.doc_id}"
        ) as held:
            _HELD_RECORD_LOCKS.depth = getattr(_HELD_RECORD_LOCKS, "depth", 0) + 1
            try:
                yield held
            finally:
                _HELD_RECORD_LOCKS.depth -= 1

    def _still_chained(self, ref: _Ref, number: int, prev_digest: str) -> bool:
        """Whether revision ``number - 1`` is still the one with *prev_digest*."""
        try:
            raw = self._files.read_json(self._revision_path(ref, number - 1))
        except (NotFound, CorruptData):
            return False
        return isinstance(raw, dict) and raw.get("digest") == prev_digest

    def _create_index_path(self, ref: _Ref, operation_id: str) -> str:
        key = hashlib.sha256(operation_id.encode()).hexdigest()[:32]
        return f"{self._root}/.creates/{ref.collection}/{ref.doc_id}/{key}.json"

    def _index_create(self, ref: _Ref, operation_id: str, number: int) -> None:
        """Note which revision *operation_id* created, so a late retry of that create finds
        it after a delete without reading the record's history."""
        path = self._create_index_path(ref, operation_id)
        try:
            self._files.mkdirs(posixpath.dirname(path))
            self._files.create_json(path, {"revision": number})
        except (Conflict, NotFound):
            pass  # Already noted, or the record is being removed.

    def _indexed_create(self, ref: _Ref, head: _Head, operation_id: str) -> _Revision | None:
        try:
            noted = self._files.read_json(self._create_index_path(ref, operation_id))
        except NotFound:
            return None
        number = noted.get("revision")
        if type(number) is not int or not 1 <= number <= head.count:
            return None
        revision = self._read_revision(ref, number)
        return revision if revision.operation_id == operation_id else None

    def _resolve_collision(
        self, ref: _Ref, operation_id: str, digest: str
    ) -> Document | DeleteResult:
        """Another writer took our slot: answer from the winner or conflict."""
        winner = self._load_head(ref)
        if winner is None:
            raise Conflict("revision slot was taken by another writer", reason="concurrent_write")
        replay = self._find_operation(ref, winner, operation_id, self._limits.idempotency_window)
        if replay is not None:
            return self._replay(ref, replay, digest)
        raise Conflict(
            "document was changed by a concurrent writer",
            reason="concurrent_write",
            current_version=self._version_of(ref, winner.latest),
        )

    def _find_operation(
        self, ref: _Ref, head: _Head, operation_id: str, depth: int
    ) -> _Revision | None:
        """Newest revision written by ``operation_id`` among the last ``depth`` revisions."""
        known = {head.count: head.latest}
        if head.previous is not None:
            known[head.count - 1] = head.previous
        successor: _Revision | None = None
        for number in range(head.count, max(head.count - depth, 0), -1):
            revision = known.get(number) or self._read_revision(ref, number)
            if successor is not None:
                self._check_link(ref, revision, successor)
            if revision.operation_id == operation_id:
                return revision
            successor = revision
        return None

    def _replay_create(
        self, ref: _Ref, head: _Head, revision: _Revision, digest: str
    ) -> Document | DeleteResult:
        """Answer a create retry, first noting the create in the index if a stop left it out."""
        result = self._replay(ref, revision, digest)
        # Only the newest revision can be an unindexed create: every append notes the one
        # before it (see _append_locked).
        if revision.kind == _CREATE and revision is head.latest:
            with self._record_lock(ref) as held:
                # Still this incarnation's newest revision: a record removed and created
                # again since must not get this old create's entry.
                if held and self._still_chained(ref, head.count + 1, revision.digest):
                    self._index_create(ref, revision.operation_id, head.count)
        return result

    def _replay(self, ref: _Ref, revision: _Revision, digest: str) -> Document | DeleteResult:
        if revision.request_digest != digest:
            raise Conflict(
                "operation_id was already used for a different request",
                reason="operation_reuse",
            )
        return self._result(ref, revision)

    def _result(self, ref: _Ref, revision: _Revision) -> Document | DeleteResult:
        if revision.kind in _GONE:
            return DeleteResult(id=ref.doc_id, version=self._version_of(ref, revision))
        return self._document(ref, revision)

    def _document(self, ref: _Ref, revision: _Revision) -> Document:
        return Document(
            id=ref.doc_id,
            value=copy.deepcopy(revision.value),
            version=self._version_of(ref, revision),
            created_at=revision.created_at,
            updated_at=revision.updated_at,
            created_by=revision.created_by,
            updated_by=revision.updated_by,
        )

    # -- reading and validating state --------------------------------------

    def _ref(self, collection: str, doc_id: str) -> _Ref:
        _validate_identifier(collection, "collection")
        _validate_identifier(doc_id, "id")
        return _Ref(collection, doc_id, f"{self._root}/{collection}/{doc_id}")

    def _segment_of(self, number: int) -> int:
        return (number - 1) // self._limits.revisions_per_segment + 1

    def _segment_directory(self, ref: _Ref, segment: int) -> str:
        if segment == 1:
            return ref.directory
        return f"{ref.directory}/s{segment:0{_REVISION_DIGITS}d}"

    def _revision_path(self, ref: _Ref, number: int) -> str:
        directory = self._segment_directory(ref, self._segment_of(number))
        return f"{directory}/{number:0{_REVISION_DIGITS}d}.json"

    def _corrupt(self, ref: _Ref, message: str) -> CorruptData:
        return CorruptData(
            f"{ref.collection}/{ref.doc_id}: {message}",
            collection=ref.collection,
            doc_id=ref.doc_id,
        )

    def _load_head(self, ref: _Ref) -> _Head | None:
        """The record's newest revision and the one before it, verified against each other.

        Read without the record's lock, a writer adding a revision between the probes and
        the listing can make a sound record look damaged. A read that finds damage is
        therefore taken once more under the lock writers hold, and only damage seen there
        is reported. A thread already holding a record lock reads again without it instead,
        because the lock files are shared by several records.
        """
        try:
            return self._read_head(ref)
        except CorruptData:
            if getattr(_HELD_RECORD_LOCKS, "depth", 0):
                return self._read_head(ref)
            with self._record_lock(ref) as held:
                if not held:
                    raise Conflict(
                        "document is busy; retry shortly", reason="concurrent_write"
                    ) from None
                return self._read_head(ref)

    def _read_head(self, ref: _Ref) -> _Head | None:
        """The record's newest revision and the one before it, verified against each other.

        Finds the newest segment by reading each segment's first revision at 1, 2, 4, 8,
        ... segments and bisecting, then lists only that segment's directory, so a read
        costs about ``2 * log2(segments)`` file reads and one listing of at most a segment
        however long the history grows. Checking every revision is :meth:`verify_record`'s
        job.
        """
        seen: dict[int, Any] = {}
        count = self._find_count(ref, seen)
        if count == 0:
            return None

        def revision(number: int) -> _Revision:
            if number in seen:
                return self._parse_revision(ref, seen[number], number)
            return self._read_revision(ref, number)

        latest = revision(count)
        previous = None
        if count > 1:
            previous = revision(count - 1)
            self._check_link(ref, previous, latest)
        first = latest if count == 1 else revision(1)
        return _Head(count=count, latest=latest, previous=previous, first_digest=first.digest)

    def _revision_exists(self, ref: _Ref, number: int, seen: dict[int, Any] | None = None) -> bool:
        try:
            raw = self._files.read_json(self._revision_path(ref, number))
        except NotFound:
            return False
        if seen is not None:
            seen[number] = raw
        return True

    def _find_count(self, ref: _Ref, seen: dict[int, Any]) -> int:
        per_segment = self._limits.revisions_per_segment
        most = self._limits.max_segments_per_record

        def started(segment: int) -> bool:
            return self._revision_exists(ref, (segment - 1) * per_segment + 1, seen)

        if not started(1):
            # Never written, or removed; anything else under the record is damage.
            try:
                leftover = self._files.list_paths(ref.directory)
            except NotFound:
                return 0
            if leftover:
                raise self._corrupt(ref, "record has revisions but not its first one")
            return 0
        low, high = 1, 2
        while high <= most and started(high):
            low, high = high, high * 2
        if high > most and started(most + 1):
            raise self._corrupt(ref, "record has more revisions than the store allows")
        high = min(high, most + 1)
        while high - low > 1:
            middle = (low + high) // 2
            if started(middle):
                low = middle
            else:
                high = middle
        return self._count_in_segment(ref, low, seen)

    def _count_in_segment(self, ref: _Ref, last: int, seen: dict[int, Any]) -> int:
        """Revisions up to the end of segment *last*, the newest one with a revision,
        checking that segment's numbering and that no later segment holds revisions."""
        per_segment = self._limits.revisions_per_segment
        if last == 1:
            # The first segment shares the record directory with the segment directories,
            # of which only an empty second one (a writer that stopped after creating it)
            # can be there now.
            try:
                entries = self._files.list_paths(ref.directory)
            except NotFound:
                return 0
            if len(entries) > per_segment + 1:
                raise self._corrupt(ref, "unexpected entries in record directory")
            numbers: set[int] = set()
            for entry in entries:
                name = posixpath.basename(str(entry).rstrip("/"))
                if (match := _REVISION_NAME_RE.fullmatch(name)) is not None:
                    numbers.add(int(match.group(1)))
                elif _SEGMENT_NAME_RE.fullmatch(name) is not None:
                    if int(name[1:]) != 2 or not self._segment_empty(ref, 2):
                        raise self._corrupt(ref, "revision segments have a gap")
                else:
                    raise self._corrupt(ref, "unexpected entry in record directory")
            if numbers != set(range(1, len(numbers) + 1)):
                raise self._corrupt(ref, "revision numbering has a gap or duplicate")
            return len(numbers)
        base = (last - 1) * per_segment
        try:
            names = self._files.list_paths(self._segment_directory(ref, last))
        except NotFound:
            raise self._corrupt(ref, "revision segment cannot be read") from None
        later: set[int] = set()
        for entry in names:
            match = _REVISION_NAME_RE.fullmatch(posixpath.basename(str(entry).rstrip("/")))
            if match is None:
                raise self._corrupt(ref, "unexpected entry in revision segment")
            later.add(int(match.group(1)))
        if later != set(range(base + 1, base + len(later) + 1)) or len(later) > per_segment:
            raise self._corrupt(ref, "revision numbering has a gap or duplicate")
        if not self._segment_empty(ref, last + 1):
            raise self._corrupt(ref, "revision segments have a gap")
        # The segment before must be full: its last revision is there.
        if not self._revision_exists(ref, base, seen):
            raise self._corrupt(ref, "revision segments have a gap")
        return base + len(later)

    def _segment_empty(self, ref: _Ref, segment: int) -> bool:
        try:
            return not self._files.list_paths(self._segment_directory(ref, segment))
        except NotFound:
            return True

    def _count_revisions(self, ref: _Ref) -> int:
        """The record's revision count from its directories, checking their numbering and
        layout; only :meth:`verify_record` pays for this full check."""
        try:
            entries = self._files.list_paths(ref.directory)
        except NotFound:
            return 0
        per_segment = self._limits.revisions_per_segment
        if len(entries) > per_segment + self._limits.max_segments_per_record:
            raise self._corrupt(ref, "record has more revisions than the store allows")
        numbers: set[int] = set()
        segments: set[int] = set()
        for entry in entries:
            name = posixpath.basename(str(entry).rstrip("/"))
            if (match := _REVISION_NAME_RE.fullmatch(name)) is not None:
                numbers.add(int(match.group(1)))
            elif (match := _SEGMENT_NAME_RE.fullmatch(name)) is not None:
                segments.add(int(match.group(1)))
            else:
                raise self._corrupt(ref, "unexpected entry in record directory")
        if numbers != set(range(1, len(numbers) + 1)) or len(numbers) > per_segment:
            raise self._corrupt(ref, "revision numbering has a gap or duplicate")
        if not segments:
            return len(numbers)
        last = max(segments)
        if (
            len(numbers) != per_segment
            or segments != set(range(2, last + 1))
            or last > self._limits.max_segments_per_record
        ):
            raise self._corrupt(ref, "revision segments have a gap")
        # Earlier segments are full by construction; reading a revision in one fails closed.
        base = (last - 1) * per_segment
        try:
            names = self._files.list_paths(self._segment_directory(ref, last))
        except NotFound:
            raise self._corrupt(ref, "revision segment is listed but cannot be read") from None
        later: set[int] = set()
        for entry in names:
            match = _REVISION_NAME_RE.fullmatch(posixpath.basename(str(entry).rstrip("/")))
            if match is None:
                raise self._corrupt(ref, "unexpected entry in revision segment")
            later.add(int(match.group(1)))
        # An empty newest segment is a writer that stopped after creating it.
        if later != set(range(base + 1, base + len(later) + 1)) or len(later) > per_segment:
            raise self._corrupt(ref, "revision numbering has a gap or duplicate")
        return base + len(later)

    def _read_revision(self, ref: _Ref, number: int) -> _Revision:
        try:
            raw = self._files.read_json(self._revision_path(ref, number))
        except NotFound:
            raise self._corrupt(ref, f"revision {number} is listed but cannot be read") from None
        return self._parse_revision(ref, raw, number)

    def _parse_revision(self, ref: _Ref, raw: Any, number: int) -> _Revision:
        def bad(message: str) -> CorruptData:
            return self._corrupt(ref, f"revision {number}: {message}")

        if not isinstance(raw, dict):
            raise bad("not a JSON object")
        raw = copy.deepcopy(raw)
        kind = raw.get("kind")
        if not isinstance(kind, str) or kind not in _KINDS:
            raise bad("unknown kind")
        expected_keys = _ENVELOPE_KEYS if kind in _GONE else _ENVELOPE_KEYS | {"value"}
        if set(raw) != expected_keys:
            raise bad("unexpected or missing fields")

        def text(key: str) -> str:
            found = raw[key]
            if not isinstance(found, str):
                raise bad(f"{key} must be a string")
            return found

        def integer(key: str) -> int:
            found = raw[key]
            if type(found) is not int:
                raise bad(f"{key} must be an integer")
            return found

        if integer("format") != FORMAT_VERSION:
            raise bad("unsupported format")
        if text("collection") != ref.collection or text("id") != ref.doc_id:
            raise bad("envelope belongs to a different record")
        if integer("revision") != number:
            raise bad("revision number does not match its file name")
        generation = integer("generation")
        if generation < 1:
            raise bad("invalid generation")
        prev_digest = raw["prev_digest"]
        if number == 1:
            if prev_digest is not None or kind != _CREATE or generation != 1:
                raise bad("first revision must be a generation 1 create without predecessor")
        elif not isinstance(prev_digest, str) or _HEX64_RE.fullmatch(prev_digest) is None:
            raise bad("invalid predecessor digest")
        for key in ("created_at", "updated_at"):
            if _TIMESTAMP_RE.fullmatch(text(key)) is None:
                raise bad(f"{key} is not a valid timestamp")
        try:
            _validate_actor(text("created_by"))
            _validate_actor(text("updated_by"))
            _validate_operation_id(text("operation_id"))
        except InvalidInput as error:
            raise bad(str(error)) from None
        if _HEX64_RE.fullmatch(text("request_digest")) is None:
            raise bad("invalid request digest")
        if kind == _CREATE and (
            raw["created_at"] != raw["updated_at"] or raw["created_by"] != raw["updated_by"]
        ):
            raise bad("create revision must have equal created and updated fields")
        value = None
        if kind not in _GONE:
            try:
                value = self._validate_value(raw["value"])
            except InvalidInput as error:
                raise bad(f"stored value is invalid: {error}") from None
        if (
            _HEX64_RE.fullmatch(text("digest")) is None
            or compute_envelope_digest(raw) != raw["digest"]
        ):
            raise bad("digest mismatch")
        return _Revision(
            revision=number,
            generation=generation,
            kind=kind,
            prev_digest=prev_digest,
            value=value,
            created_at=raw["created_at"],
            updated_at=raw["updated_at"],
            created_by=raw["created_by"],
            updated_by=raw["updated_by"],
            operation_id=raw["operation_id"],
            request_digest=raw["request_digest"],
            digest=raw["digest"],
        )

    def _check_link(self, ref: _Ref, previous: _Revision, current: _Revision) -> None:
        if current.revision != previous.revision + 1 or current.prev_digest != previous.digest:
            raise self._corrupt(
                ref, f"revision {current.revision} does not link to its predecessor"
            )
        if current.kind == _CREATE:
            if previous.kind != _DELETE or current.generation != previous.generation + 1:
                raise self._corrupt(ref, f"revision {current.revision} re-creates a live record")
        elif current.kind == _PURGE:
            if (
                previous.kind != _DELETE
                or current.generation != previous.generation
                or current.created_at != previous.created_at
                or current.created_by != previous.created_by
            ):
                raise self._corrupt(ref, f"revision {current.revision} removes a live record")
        elif (
            previous.kind in _GONE
            or current.generation != previous.generation
            or current.created_at != previous.created_at
            or current.created_by != previous.created_by
        ):
            raise self._corrupt(
                ref, f"revision {current.revision} is inconsistent with its predecessor"
            )

    def _ensure_record_capacity(self, collection: str, doc_id: str) -> str:
        """Claim a capacity slot for *doc_id*, or find its claim; the slot's path."""
        try:
            return self._claim_capacity(collection, doc_id)
        except CapacityExceeded:
            if not self._reclaim(collection, doc_id) and not self._release_abandoned_claims(
                collection, doc_id
            ):
                raise
            return self._claim_capacity(collection, doc_id)

    def _probe_slots(self, collection: str, doc_id: str) -> Sequence[str]:
        """The capacity slots *doc_id* may claim: a short run from its hash, in order."""
        capacity = self._limits.max_records_per_collection
        directory = f"{self._root}/.capacity/{collection}"
        start = int(hashlib.sha256(doc_id.encode()).hexdigest(), 16) % capacity
        probes = min(capacity, _CAPACITY_PROBES)
        return [f"{directory}/{(start + offset) % capacity:08d}.json" for offset in range(probes)]

    def _read_claim(self, path: str, collection: str) -> dict[str, Any] | None:
        """The claim in slot *path*, or ``None`` when the slot is free."""
        try:
            owner = self._files.read_json(path)
        except NotFound:
            return None
        if (
            not isinstance(owner, dict)
            or set(owner) - {"at"} != {"collection", "id"}
            or owner["collection"] != collection
            or not isinstance(owner["id"], str)
            or _IDENTIFIER_RE.fullmatch(owner["id"]) is None
            or ("at" in owner and not _is_timestamp(owner["at"]))
        ):
            raise CorruptData("Invalid record capacity claim", collection=collection)
        return owner

    def _holds_claim(self, path: str, ref: _Ref) -> bool:
        try:
            owner = self._read_claim(path, ref.collection)
        except CorruptData:
            return False
        return owner is not None and owner["id"] == ref.doc_id

    def _claim_capacity(self, collection: str, doc_id: str) -> str:
        """Claim one of the collection's record slots for *doc_id*, or find its claim.

        Slots are create-only files in a table of ``max_records_per_collection``, so admitted
        records never exceed it, and a record probes only a short run of slots from its
        hash: a create costs bounded work, and a collection may fill up a little before
        every slot is taken. Claims survive failed appends and tombstones, and a retry of
        that ID reuses its claim; a claim whose record was never written is released after
        ``purge_abandon_s`` (:meth:`_release_abandoned_claims`). Returns the slot's path.
        """
        directory = f"{self._root}/.capacity/{collection}"
        self._files.mkdirs(directory)
        free: list[str] = []
        for path in self._probe_slots(collection, doc_id):
            owner = self._read_claim(path, collection)
            if owner is None:
                free.append(path)
            elif owner["id"] == doc_id:
                return path
        for path in free:
            try:
                self._files.create_json(
                    path, {"collection": collection, "id": doc_id, "at": self._now()}
                )
                return path
            except Conflict:
                owner = self._read_claim(path, collection)
                if owner is not None and owner["id"] == doc_id:
                    return path
        raise CapacityExceeded(
            f"collection is full: it holds at most {self._limits.max_records_per_collection} "
            "records"
        )

    def _release_abandoned_claims(self, collection: str, doc_id: str) -> bool:
        """Free the slots in *doc_id*'s probe run whose record was never written.

        A create claims its slot before it writes the record's first revision, so one that
        stopped in between leaves a claim with no record. A claim older than
        ``purge_abandon_s`` whose record has no revision is released under that record's
        lock, which its writer also takes before the first revision and which makes the
        writer check its claim is still held: a slow writer is refused rather than adding a
        record without a slot. Looks only at this create's probe run, so it costs bounded
        work, and stops at the first slot it frees. Returns whether it freed one.
        """
        if not self._files.supports_delete:
            return False
        for path in self._probe_slots(collection, doc_id):
            try:
                owner = self._read_claim(path, collection)
            except CorruptData:
                continue
            if (
                owner is None
                or owner["id"] == doc_id
                or "at" not in owner
                or self._seconds_since(owner["at"]) < self._limits.purge_abandon_s
            ):
                continue
            ref = self._ref(collection, owner["id"])
            if self._has_revisions(ref):
                continue
            with self._record_lock(ref) as held:
                if not held or self._has_revisions(ref):
                    continue
                try:
                    if self._read_claim(path, collection) != owner:
                        continue
                except CorruptData:
                    continue
                self._files.delete_tree(ref.directory)
                self._files.delete_tree(path)
                return True
        return False

    def _has_revisions(self, ref: _Ref) -> bool:
        """Whether the record was ever written: a record's first entry is its first
        revision, so it is probed by name, however long the record's history (never by
        listing its directory). An unreadable first revision still counts as written."""
        try:
            self._files.read_json(self._revision_path(ref, 1))
        except NotFound:
            return False
        except CorruptData:
            return True
        return True

    def _claim_path(self, collection: str, doc_id: str) -> str | None:
        """The capacity slot *doc_id* holds, or ``None`` if it holds none."""
        for path in self._probe_slots(collection, doc_id):
            # Released slots leave holes, so keep probing; claims never move.
            try:
                owner = self._read_claim(path, collection)
            except CorruptData:
                continue
            if owner is not None and owner["id"] == doc_id:
                return path
        return None

    # -- removing deleted records -------------------------------------------

    def _seconds_since(self, timestamp: str) -> float:
        written = datetime.strptime(timestamp, _TIMESTAMP_FORMAT).replace(tzinfo=UTC)
        moment = self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return (moment - written).total_seconds()

    def _older_than(self, revision: _Revision, seconds: int) -> bool:
        return self._seconds_since(revision.updated_at) >= seconds

    def _reclaim(self, collection: str, doc_id: str) -> bool:
        """Remove some records deleted longer than the retention ago; whether any went.

        Looks only at the records holding the capacity slots *doc_id* may claim (its probe
        run), so each create costs bounded work and frees a slot it can actually take.
        """
        if not self._files.supports_delete:
            return False
        # A new record can only take a slot in its own probe run, so the records worth
        # removing are those holding those slots: a bounded, exact set of candidates.
        candidates: list[str] = []
        for path in self._probe_slots(collection, doc_id):
            try:
                owner = self._read_claim(path, collection)
            except CorruptData:
                continue
            if owner is not None and owner["id"] not in candidates:
                candidates.append(owner["id"])
        removed = 0
        for candidate in candidates:
            if candidate == doc_id:
                continue
            ref = self._ref(collection, candidate)
            try:
                head = self._load_head(ref)
                if head is None:
                    continue
                if head.latest.kind == _DELETE and self._older_than(
                    head.latest, self._limits.tombstone_retention_s
                ):
                    self._purge(ref, head)
                elif head.latest.kind == _PURGE and self._older_than(
                    head.latest, self._limits.purge_abandon_s
                ):
                    self._finish_purge(ref, head.latest)
                else:
                    continue
            except (Conflict, CorruptData, NotFound):
                continue  # written to meanwhile, or not ours to repair: leave it
            removed += 1
            if removed >= self._limits.max_reclaim_per_create:
                break
        return removed > 0

    def _purge(self, ref: _Ref, head: _Head) -> None:
        marker = head.latest
        operation_id = f"purge.{marker.digest[:32]}"
        digest = _request_digest(
            method=_PURGE,
            collection=ref.collection,
            doc_id=ref.doc_id,
            if_version=self._version_of(ref, marker),
            actor=marker.updated_by,
            value=None,
            has_value=False,
        )
        # The purge revision fences the record: a writer racing it collides on this slot,
        # and later writers see it and wait until the directory is gone.
        result = self._append(ref, head, _PURGE, None, operation_id, marker.updated_by, digest)
        if not isinstance(result, DeleteResult):
            raise Conflict("record changed before it could be removed", reason="concurrent_write")
        purged = self._load_head(ref)
        if purged is None or purged.latest.kind != _PURGE:
            raise Conflict("record changed before it could be removed", reason="concurrent_write")
        self._finish_purge(ref, purged.latest)

    def _finish_purge(self, ref: _Ref, marker: _Revision) -> None:
        """Release a purged record's capacity slot and delete its directory.

        Fenced by *marker*, the purge revision: only the holder of its newest removal
        ticket deletes, and only while *marker* is still the record's newest revision.
        The check and the deletions it allows run under the record's lock
        (:meth:`GadgetFiles.exclusive`), which every remover takes, so a remover that
        stalls after its check holds off the one that takes over: the record can't be
        removed and created again in between, and its deletion never reaches a new one.
        A remover that finds another holding the lock or the ticket, or the record already
        gone or created again, leaves it alone.
        """
        tickets = f"{self._root}/.removals/{ref.collection}/{ref.doc_id}/{marker.digest[:32]}"
        with self._record_lock(ref) as held:
            if not held:
                return
            ticket = self._take_removal_ticket(tickets)
            if ticket is None:
                return
            if not self._still_removing(ref, marker, tickets, ticket):
                return
            claim = self._claim_path(ref.collection, ref.doc_id)
            creates = f"{self._root}/.creates/{ref.collection}/{ref.doc_id}"
            for path in (claim, creates, ref.directory):
                if path is not None:
                    self._files.delete_tree(path)
            self._files.delete_tree(tickets)

    def _ticket_exists(self, tickets: str, number: int) -> bool:
        try:
            self._files.read_json(f"{tickets}/{number:0{_REVISION_DIGITS}d}.json")
        except (NotFound, CorruptData):
            return False
        return True

    def _newest_ticket(self, tickets: str) -> int:
        """The newest removal ticket's number, or 0, in O(log tickets) probes.

        Tickets are numbered from 1 without gaps (each takes the next number) and none is
        removed until the purge removes them all, so this never lists them.
        """
        if not self._ticket_exists(tickets, 1):
            return 0
        low, high = 1, 2
        while self._ticket_exists(tickets, high):
            low, high = high, high * 2
        while high - low > 1:
            middle = (low + high) // 2
            if self._ticket_exists(tickets, middle):
                low = middle
            else:
                high = middle
        return low

    def _take_removal_ticket(self, tickets: str) -> int | None:
        """A new removal ticket's number, or ``None`` while another remover holds one.

        A ticket older than ``purge_abandon_s`` belongs to a remover that stopped; the
        next number takes over, and the stopped remover then finds it is not the newest.
        """
        newest = self._newest_ticket(tickets)
        if newest:
            try:
                taken = self._files.read_json(f"{tickets}/{newest:0{_REVISION_DIGITS}d}.json")
            except NotFound:
                return None
            at = taken.get("at") if isinstance(taken, dict) else None
            if not isinstance(at, str) or _TIMESTAMP_RE.fullmatch(at) is None:
                return None
            if self._seconds_since(at) < self._limits.purge_abandon_s:
                return None
        number = newest + 1
        self._files.mkdirs(tickets)
        try:
            self._files.create_json(
                f"{tickets}/{number:0{_REVISION_DIGITS}d}.json", {"at": self._now()}
            )
        except Conflict:
            return None
        return number

    def _still_removing(self, ref: _Ref, marker: _Revision, tickets: str, ticket: int) -> bool:
        """Whether this remover may delete: *marker* is still the newest revision and
        *ticket* the newest ticket."""
        head = self._load_head(ref)
        if head is None or head.latest.digest != marker.digest:
            return False
        return self._newest_ticket(tickets) == ticket

    # -- values, tokens and cursors ----------------------------------------

    def _now(self) -> str:
        moment = self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(UTC).strftime(_TIMESTAMP_FORMAT)

    def _validate_value(self, value: Any) -> Any:
        """Return a private, validated copy of a JSON value or raise InvalidInput."""
        nodes = [0]
        clone = self._clone_json(value, 0, nodes)
        try:
            encoded = json.dumps(
                clone, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            ).encode("utf-8")
        except (UnicodeEncodeError, ValueError) as error:
            raise InvalidInput(f"value is not valid JSON text: {error}") from None
        if len(encoded) > self._limits.max_document_bytes:
            raise InvalidInput(f"value exceeds {self._limits.max_document_bytes} bytes")
        return clone

    def _clone_json(self, value: Any, depth: int, nodes: _IntList) -> Any:
        nodes[0] += 1
        if nodes[0] > self._limits.max_json_nodes:
            raise InvalidInput(f"value has more than {self._limits.max_json_nodes} nodes")
        if value is None or isinstance(value, (bool, str)):
            return value
        if isinstance(value, int):
            if abs(value) > _MAX_JSON_INTEGER:
                raise InvalidInput("integers must be within +-(2**53 - 1)")
            return int(value)
        if isinstance(value, float):
            if not math.isfinite(value):
                raise InvalidInput("numbers must be finite")
            return value
        if isinstance(value, (list, dict)):
            if depth + 1 > self._limits.max_json_depth:
                raise InvalidInput(f"value is nested deeper than {self._limits.max_json_depth}")
            if isinstance(value, list):
                return [self._clone_json(item, depth + 1, nodes) for item in value]
            result: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise InvalidInput("object keys must be strings")
                result[key] = self._clone_json(item, depth + 1, nodes)
            return result
        raise InvalidInput(f"unsupported value type {type(value).__name__}")

    def _scope(self, *parts: str) -> str:
        return hashlib.sha256("\0".join((self._root, *parts)).encode("utf-8")).hexdigest()[:16]

    def _version_of(self, ref: _Ref, revision: _Revision) -> str:
        scope = self._scope(ref.collection, ref.doc_id)
        body = f"{scope}.{revision.generation}.{revision.revision}.{revision.digest[:32]}"
        return _VERSION_PREFIX + _b64encode(body)

    def _validate_version(self, token: Any, ref: _Ref) -> int:
        """Reject malformed tokens and tokens issued for another record or store."""
        if (
            not isinstance(token, str)
            or len(token) > _MAX_TOKEN_LENGTH
            or not token.startswith(_VERSION_PREFIX)
        ):
            raise InvalidInput("if_version is not a valid version token")
        try:
            body = _b64decode(token[len(_VERSION_PREFIX) :])
        except ValueError:
            raise InvalidInput("if_version is not a valid version token") from None
        match = _VERSION_BODY_RE.fullmatch(body)
        if match is None or _VERSION_PREFIX + _b64encode(body) != token:
            raise InvalidInput("if_version is not a valid version token")
        if match.group(1) != self._scope(ref.collection, ref.doc_id):
            raise InvalidInput("if_version belongs to a different document")
        return int(match.group(3))

    def _encode_cursor(self, collection: str, last: int) -> str:
        payload = {"v": 2, "s": self._scope(collection), "after": last}
        return _CURSOR_PREFIX + _b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
        )

    def _decode_cursor(self, cursor: Any, collection: str) -> int:
        invalid = InvalidInput("cursor is not valid")
        if (
            not isinstance(cursor, str)
            or len(cursor) > _MAX_TOKEN_LENGTH
            or not cursor.startswith(_CURSOR_PREFIX)
        ):
            raise invalid
        try:
            payload = json.loads(_b64decode(cursor[len(_CURSOR_PREFIX) :]))
        except ValueError:
            raise invalid from None
        if (
            not isinstance(payload, dict)
            or set(payload) != {"v", "s", "after"}
            or payload["v"] != 2
        ):
            raise invalid
        after = payload["after"]
        if (
            not isinstance(payload["s"], str)
            or type(after) is not int
            or not 0 <= after <= _MAX_LISTING_ORDINAL
        ):
            raise invalid
        if self._encode_cursor(collection, after) != cursor:
            if payload["s"] != self._scope(collection):
                raise InvalidInput("cursor belongs to a different collection")
            raise invalid
        return after
