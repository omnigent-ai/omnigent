"""Revisioned JSON documents on a thingy's create-only files.

Documents live as create-only revision files in a :class:`ThingyFiles` scope.
The module is pure storage: it has no access control, schema validation,
request context or network dependencies, so callers authorize and validate
before calling in.

Layout under ``root_path``::

    <collection>/<id>/00000001.json   # create
    <collection>/<id>/00000002.json   # replace
    <collection>/<id>/00000003.json   # delete (tombstone)
    <collection>/<id>/00000004.json   # re-create after a tombstone

There is no mutable head file and no overwrite or delete primitive in
:class:`ThingyFiles`. A writer reads the record, derives the single successor
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
  older ones fail closed with :class:`Conflict`. Re-creating a tombstoned record
  scans the full chain for the same reason.
* Tombstones are revisions, so creation can never reset a record's history, a
  stale ``if_version`` from a deleted generation cannot touch a re-created
  record, and a stale ``create`` retry cannot resurrect a deleted record.
* ``get`` verifies the newest revision, its predecessor link and the revision
  numbering of the record directory. ``verify_record`` verifies the whole chain.
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
* At most 1000 revisions per record and 10000 reserved record IDs per
  collection, enforced by create-only slots under ``.capacity`` even with
  concurrent creates. A page limit of 1..200 with at most 500 records examined
  per ``list`` call and at most 48 KiB per page; when a budget runs out ``list``
  returns the items found so far with a ``next_cursor``, so an empty page with a
  cursor is possible and a silently cut page is not.

The digests protect against accidental damage and partial edits; they are not
authentication. Someone with write access to the storage who recomputes digests
consistently can forge history.
"""

from __future__ import annotations

import base64
import bisect
import copy
import hashlib
import json
import math
import posixpath
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from omnigent.stores.thingy_store import (
    CapacityExceeded,
    Conflict,
    CorruptData,
    InvalidInput,
    NotFound,
    StoreError,
    ThingyFiles,
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
_KINDS: Final = frozenset({_CREATE, _REPLACE, _DELETE})

_IDENTIFIER_RE: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_OPERATION_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_REVISION_NAME_RE: Final = re.compile(r"([0-9]{8})\.json")
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
_REVISION_DIGITS: Final = 8
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


@dataclass(frozen=True)
class StoreLimits:
    """Bounds enforced by the store. Defaults are the production limits."""

    max_document_bytes: int = 32 * 1024
    max_json_depth: int = 16
    max_json_nodes: int = 16_384
    max_revisions_per_record: int = 1000
    max_records_per_collection: int = 10_000
    max_list_entries: int = 20_000
    max_list_limit: int = 200
    max_scan_per_list: int = 500
    max_page_bytes: int = 48 * 1024
    idempotency_window: int = 16

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_revisions_per_record > _MAX_REVISION_NUMBER:
            raise ValueError("max_revisions_per_record exceeds the revision file name width")
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


class ImmutableDocumentStore:
    """Create-only revision chains for JSON documents (see module docstring)."""

    def __init__(
        self,
        files: ThingyFiles,
        root_path: str,
        *,
        limits: StoreLimits = DEFAULT_LIMITS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._files = files
        self._root = _normalize_root(root_path)
        self._limits = limits
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- reads --------------------------------------------------------------

    def get(self, collection: str, id: str) -> Document | None:
        """Newest live version of a document, or ``None`` if absent or deleted."""
        ref = self._ref(collection, id)
        head = self._load_head(ref)
        if head is None or head.latest.kind == _DELETE:
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
        return None if revision.kind == _DELETE else self._document(ref, revision)

    def list(self, collection: str, limit: int = 100, cursor: str | None = None) -> DocumentPage:
        """One page of live documents ordered by id; not a snapshot across records."""
        _validate_identifier(collection, "collection")
        if type(limit) is not int or not 1 <= limit <= self._limits.max_list_limit:
            raise InvalidInput(f"limit must be an integer in 1..{self._limits.max_list_limit}")
        after = self._decode_cursor(cursor, collection) if cursor is not None else None
        ids = self._list_ids(collection)
        position = bisect.bisect_right(ids, after) if after is not None else 0
        first = position
        items: list[Document] = []
        scanned = 0
        page_bytes = _PAGE_OVERHEAD_BYTES
        while (
            position < len(ids) and len(items) < limit and scanned < self._limits.max_scan_per_list
        ):
            ref = self._ref(collection, ids[position])
            position += 1
            scanned += 1
            head = self._load_head(ref)
            if head is None or head.latest.kind == _DELETE:
                continue
            document = self._document(ref, head.latest)
            item_bytes = _encoded_size(document.to_dict()) + 1  # +1 for the separating comma
            if page_bytes + item_bytes > self._limits.max_page_bytes:
                if not items:
                    raise CapacityExceeded("a single document does not fit in max_page_bytes")
                position -= 1  # leave it for the next page; the cursor stops before it
                break
            page_bytes += item_bytes
            items.append(document)
        more = position > first and position < len(ids)
        return DocumentPage(
            items=tuple(items),
            next_cursor=self._encode_cursor(collection, ids[position - 1]) if more else None,
        )

    def verify_record(self, collection: str, id: str) -> RecordSummary | None:
        """Verify every revision and link of one record; ``None`` if it has no revisions."""
        ref = self._ref(collection, id)
        head = self._load_head(ref)
        if head is None:
            return None
        successor = head.latest
        for number in range(head.count - 1, 0, -1):
            revision = self._read_revision(ref, number)
            self._check_link(ref, revision, successor)
            successor = revision
        return RecordSummary(
            revisions=head.count,
            generation=head.latest.generation,
            deleted=head.latest.kind == _DELETE,
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
        if head is None:
            self._ensure_record_capacity(collection, id)
        else:
            replay = self._find_operation(ref, head, operation_id, 2)
            if replay is not None:
                return _expect_document(self._replay(ref, replay, digest))
            if head.latest.kind != _DELETE:
                replay = self._find_operation(
                    ref, head, operation_id, self._limits.idempotency_window
                )
                if replay is not None:
                    return _expect_document(self._replay(ref, replay, digest))
                raise Conflict(
                    "document already exists",
                    reason="already_exists",
                    current_version=self._version_of(ref, head.latest),
                )
            # Re-creating over a tombstone: the same operation may have created
            # an earlier generation, so look through the whole chain.
            replay = self._find_operation(ref, head, operation_id, head.count)
            if replay is not None:
                return _expect_document(self._replay(ref, replay, digest))
        return _expect_document(
            self._append(ref, head, _CREATE, clean, operation_id, actor, digest)
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
        # Cheap reuse/replay check on the revisions already in hand. When the
        # precondition below holds the operation cannot have been applied yet.
        replay = self._find_operation(ref, head, operation_id, 2)
        if replay is not None:
            return self._replay(ref, replay, digest)
        current = self._version_of(ref, head.latest)
        deleted = head.latest.kind == _DELETE
        if deleted or current != if_version:
            replay = self._find_operation(ref, head, operation_id, self._limits.idempotency_window)
            if replay is not None:
                return self._replay(ref, replay, digest)
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
    ) -> Document | DeleteResult:
        now = self._now()
        if head is None:
            number, generation, prev_digest = 1, 1, None
            created_at, created_by = now, actor
        else:
            number = head.count + 1
            if number > self._limits.max_revisions_per_record:
                raise CapacityExceeded(
                    "record already has the maximum of "
                    f"{self._limits.max_revisions_per_record} revisions"
                )
            previous = head.latest
            prev_digest = previous.digest
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
        if kind != _DELETE:
            body["value"] = copy.deepcopy(value)
        body["digest"] = compute_envelope_digest(body)
        if head is None:
            self._files.mkdirs(ref.directory)
        try:
            self._files.create_json(self._revision_path(ref, number), copy.deepcopy(body))
        except Conflict:
            return self._resolve_collision(ref, operation_id, digest)
        return self._result(ref, self._parse_revision(ref, body, number))

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

    def _replay(self, ref: _Ref, revision: _Revision, digest: str) -> Document | DeleteResult:
        if revision.request_digest != digest:
            raise Conflict(
                "operation_id was already used for a different request",
                reason="operation_reuse",
            )
        return self._result(ref, revision)

    def _result(self, ref: _Ref, revision: _Revision) -> Document | DeleteResult:
        if revision.kind == _DELETE:
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

    def _revision_path(self, ref: _Ref, number: int) -> str:
        return f"{ref.directory}/{number:0{_REVISION_DIGITS}d}.json"

    def _corrupt(self, ref: _Ref, message: str) -> CorruptData:
        return CorruptData(
            f"{ref.collection}/{ref.doc_id}: {message}",
            collection=ref.collection,
            doc_id=ref.doc_id,
        )

    def _load_head(self, ref: _Ref) -> _Head | None:
        count = self._count_revisions(ref)
        if count == 0:
            return None
        latest = self._read_revision(ref, count)
        previous = None
        if count > 1:
            previous = self._read_revision(ref, count - 1)
            self._check_link(ref, previous, latest)
        return _Head(count=count, latest=latest, previous=previous)

    def _count_revisions(self, ref: _Ref) -> int:
        try:
            entries = self._files.list_paths(ref.directory)
        except NotFound:
            return 0
        if len(entries) > self._limits.max_revisions_per_record:
            raise self._corrupt(ref, "record has more revisions than the store allows")
        numbers: set[int] = set()
        for entry in entries:
            match = _REVISION_NAME_RE.fullmatch(posixpath.basename(str(entry).rstrip("/")))
            if match is None:
                raise self._corrupt(ref, "unexpected entry in record directory")
            numbers.add(int(match.group(1)))
        if len(numbers) != len(entries) or numbers != set(range(1, len(numbers) + 1)):
            raise self._corrupt(ref, "revision numbering has a gap or duplicate")
        return len(numbers)

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
        expected_keys = _ENVELOPE_KEYS if kind == _DELETE else _ENVELOPE_KEYS | {"value"}
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
        if kind != _DELETE:
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
        elif (
            previous.kind == _DELETE
            or current.generation != previous.generation
            or current.created_at != previous.created_at
            or current.created_by != previous.created_by
        ):
            raise self._corrupt(
                ref, f"revision {current.revision} is inconsistent with its predecessor"
            )

    def _list_ids(self, collection: str) -> _StrList:
        directory = f"{self._root}/{collection}"
        try:
            entries = self._files.list_paths(directory)
        except NotFound:
            return []
        if len(entries) > self._limits.max_list_entries:
            raise CapacityExceeded(
                f"collection has more than {self._limits.max_list_entries} entries"
            )
        ids: set[str] = set()
        for entry in entries:
            name = posixpath.basename(str(entry).rstrip("/"))
            if _IDENTIFIER_RE.fullmatch(name) is None:
                raise CorruptData(
                    f"{collection}: unexpected entry in collection directory",
                    collection=collection,
                )
            ids.add(name)
        return sorted(ids)

    def _ensure_record_capacity(self, collection: str, doc_id: str) -> None:
        capacity = self._limits.max_records_per_collection
        try:
            entries = self._files.list_paths(f"{self._root}/{collection}")
        except NotFound:
            entries = []
        if len(entries) >= capacity:
            if any(posixpath.basename(entry.rstrip("/")) == doc_id for entry in entries):
                return
            raise CapacityExceeded(f"collection already has {capacity} records")
        # A count check alone races: an admitted create must never make listing impossible.
        # Claims survive failed appends and tombstones, and a retry of that ID reuses its claim.
        directory = f"{self._root}/.capacity/{collection}"
        self._files.mkdirs(directory)
        claim = {"collection": collection, "id": doc_id}
        start = int(hashlib.sha256(doc_id.encode()).hexdigest(), 16) % capacity
        for offset in range(capacity):
            path = f"{directory}/{(start + offset) % capacity:08d}.json"
            try:
                owner = self._files.read_json(path)
            except NotFound:
                try:
                    self._files.create_json(path, claim)
                    return
                except Conflict:
                    owner = self._files.read_json(path)
            if owner == claim:
                return
            if (
                not isinstance(owner, dict)
                or set(owner) != {"collection", "id"}
                or owner["collection"] != collection
                or not isinstance(owner["id"], str)
                or _IDENTIFIER_RE.fullmatch(owner["id"]) is None
            ):
                raise CorruptData("Invalid record capacity claim", collection=collection)
        raise CapacityExceeded(f"collection has reserved all {capacity} record slots")

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

    def _encode_cursor(self, collection: str, last_id: str) -> str:
        payload = {"v": 1, "s": self._scope(collection), "after": last_id}
        return _CURSOR_PREFIX + _b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
        )

    def _decode_cursor(self, cursor: Any, collection: str) -> str:
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
            or payload["v"] != 1
        ):
            raise invalid
        after = payload["after"]
        if not isinstance(payload["s"], str) or not isinstance(after, str):
            raise invalid
        if (
            _IDENTIFIER_RE.fullmatch(after) is None
            or self._encode_cursor(collection, after) != cursor
        ):
            if payload["s"] != self._scope(collection):
                raise InvalidInput("cursor belongs to a different collection")
            raise invalid
        return after
