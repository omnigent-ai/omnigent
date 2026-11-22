"""SQLAlchemy-backed file store."""

from __future__ import annotations

import builtins
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, asc, desc, or_, select
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlFile, current_workspace_id, normalize_uuid
from omnigent.db.keyset import keyset_after
from omnigent.db.query_context import query_name_scope
from omnigent.db.utils import (
    generate_file_id,
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities import PagedList, StoredFile
from omnigent.stores.file_store import FileStore

# Rows deleted per transaction when deleting all of one session's or project's files.
_DELETE_BATCH_SIZE = 500


@dataclass(frozen=True)
class _DeletedBatch:
    """
    One batch of an owner's deleted file rows.

    :param deleted: How many rows the batch deleted, at most ``_DELETE_BATCH_SIZE``.
    :param orphaned: Blob keys no surviving row references any more.
    """

    deleted: int
    orphaned: list[str]


def _to_entity(row: SqlFile) -> StoredFile:
    """
    Convert a :class:`SqlFile` ORM row to a :class:`StoredFile` entity.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`StoredFile` dataclass instance.
    """
    return StoredFile(
        id=row.id,
        created_at=row.created_at,
        filename=row.filename,
        bytes=row.bytes,
        content_type=row.content_type,
        session_id=row.session_id,
        blob_key=row.blob_key,
        source_metadata=(
            json.loads(row.source_metadata) if row.source_metadata is not None else None
        ),
        project_id=row.project_id,
    )


def _effective_blob_key(row: SqlFile) -> str:
    """Artifact-store key for *row*'s bytes: ``blob_key`` or ``id`` if NULL."""
    return row.blob_key if row.blob_key is not None else row.id


def _blob_is_referenced(session: Session, blob_key: str) -> bool:
    """
    Whether any of the workspace's rows still keeps its bytes under *blob_key*.

    Matches ``COALESCE(blob_key, id) = :key`` with two lookups that each read at
    most one row: the ``blob_key`` index, then the primary key for a row from
    before ``blob_key`` existed, whose bytes live under its own id.

    :param session: The calling transaction's session.
    :param blob_key: The artifact-store key, e.g. ``"a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"``.
    :returns: ``True`` while some row still references the blob.
    """
    key = normalize_uuid(blob_key)
    in_workspace = SqlFile.workspace_id == current_workspace_id()
    by_key = select(SqlFile.id).where(in_workspace, SqlFile.blob_key == key).limit(1)
    if session.execute(by_key).first() is not None:
        return True
    legacy = select(SqlFile.id).where(in_workspace, SqlFile.id == key, SqlFile.blob_key.is_(None))
    return session.execute(legacy).first() is not None


def _owned_by(row: SqlFile, session_id: str | None, project_id: str | None) -> bool:
    """
    Whether *row* belongs to the given session or project, when one is given.

    :param row: The file row.
    :param session_id: Required owning session, or ``None`` to skip the check.
    :param project_id: Required owning project, or ``None`` to skip the check.
    :returns: ``False`` when a given owner doesn't match.
    """
    if session_id is not None and row.session_id != normalize_uuid(session_id):
        return False
    return project_id is None or row.project_id == normalize_uuid(project_id)


class SqlAlchemyFileStore(FileStore):
    """
    SQLAlchemy-backed implementation of :class:`FileStore`.

    Persists file metadata in a relational database via
    SQLAlchemy ORM.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy file store.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///files.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.file_store",
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.file_store",
            immediate=True,
        )

    def create(
        self,
        filename: str,
        bytes: int,
        content_type: str | None = None,
        session_id: str | None = None,
        file_id: str | None = None,
        blob_key: str | None = None,
        source_metadata: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> StoredFile:
        """
        Record a new file in the database.

        :param filename: Original filename.
        :param bytes: File size in bytes.
        :param content_type: MIME type.
        :param session_id: Owning session id, or ``None`` for
            global files.
        :param file_id: Caller-chosen id (a fork pre-allocates ids for
            the file copies it creates), or ``None`` to generate one.
        :param blob_key: Artifact-store key for the row's bytes. A fork
            passes the source row's blob so the copy shares the bytes;
            ``None`` (default) points the row at its own ``file_id`` — an
            independent blob the uploader is expected to ``put`` there.
        :param source_metadata: Opaque JSON-able dict of metadata about
            the original upload before any server-side transform, or
            ``None`` when there is nothing to record.
        :param project_id: Owning project for a project file, or ``None``.
        :returns: The newly created :class:`StoredFile`.
        :raises ValueError: If both ``session_id`` and ``project_id`` are set.
        """
        if session_id is not None and project_id is not None:
            raise ValueError("a file belongs to a session or a project, not both")
        file_id = file_id if file_id is not None else generate_file_id()
        # New rows always store an explicit blob_key so the shared-blob
        # reference count is a plain equality (no COALESCE needed for them);
        # only pre-migration rows carry NULL and fall back to id on read.
        blob_key = blob_key if blob_key is not None else file_id
        created_at = now_epoch()
        encoded_metadata = json.dumps(source_metadata) if source_metadata is not None else None

        def write(session: Session) -> StoredFile:
            row = SqlFile(
                id=file_id,
                created_at=created_at,
                filename=filename,
                bytes=bytes,
                content_type=content_type,
                session_id=session_id,
                project_id=project_id,
                blob_key=blob_key,
                source_metadata=encoded_metadata,
            )
            session.add(row)
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "insert_file", write)

    def is_blob_key_orphaned(self, blob_key: str) -> bool:
        """
        Whether no file row references *blob_key* any more.

        A blob is shared when a fork copies a row without copying the bytes,
        so the artifact-store blob may only be deleted once the last file row
        pointing at it is gone. Callers delete the row first, then check this
        before removing the blob.

        :param blob_key: The artifact-store key to test, e.g. an
            :func:`_effective_blob_key` value.
        :returns: ``True`` when the blob is safe to delete (no referrers).
        """
        with self._session("count_blob_key_refs") as session:
            return not _blob_is_referenced(session, blob_key)

    def get(
        self,
        file_id: str,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> StoredFile | None:
        """
        Fetch file metadata by ID.

        When ``session_id`` or ``project_id`` is set, only returns the
        file if it belongs to that session or project.

        :param file_id: Unique file identifier.
        :param session_id: If set, verify session ownership.
        :param project_id: If set, verify project ownership.
        :returns: The :class:`StoredFile` if found, otherwise
            ``None``.
        """
        with self._session("select_file_by_id") as session:
            row = session.get(SqlFile, (current_workspace_id(), file_id))
            if row is None or not _owned_by(row, session_id, project_id):
                return None
            return _to_entity(row)

    def list(
        self,
        session_id: str,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
        include_unscoped: bool = False,
    ) -> PagedList[StoredFile]:
        """
        List a session's files with cursor-based pagination.

        Always scoped to ``session_id`` — the query filters on it, so
        it is served by ``ix_files_session_id_created_at``.

        :param session_id: Owning session whose files to list.
        :param limit: Maximum number of files to return.
        :param after: Cursor file ID for forward pagination.
        :param before: Cursor file ID for backward pagination.
        :param order: Sort direction, ``"desc"`` or ``"asc"``.
        :param include_unscoped: When ``True``, also return global
            files, which belong to no session and no project.
        :returns: A :class:`PagedList` of :class:`StoredFile`.
        """
        owner = SqlFile.session_id == session_id
        if include_unscoped:
            unscoped = and_(SqlFile.session_id.is_(None), SqlFile.project_id.is_(None))
            owner = or_(owner, unscoped)
        with self._session("list_files") as session:
            return _page(session, owner, limit=limit, after=after, before=before, order=order)

    def list_for_project(
        self,
        project_id: str,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
    ) -> PagedList[StoredFile]:
        """
        List a project's files with cursor-based pagination.

        Served by ``ix_files_project_id_created_at``.

        :param project_id: Owning project whose files to list.
        :param limit: Maximum number of files to return.
        :param after: Cursor file ID for forward pagination.
        :param before: Cursor file ID for backward pagination.
        :param order: Sort direction, ``"desc"`` or ``"asc"``.
        :returns: A :class:`PagedList` of :class:`StoredFile`.
        """
        owner = SqlFile.project_id == normalize_uuid(project_id)
        with self._session("list_project_files") as session:
            return _page(session, owner, limit=limit, after=after, before=before, order=order)

    def delete(
        self,
        file_id: str,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> bool:
        """
        Delete file metadata by ID.

        When ``session_id`` or ``project_id`` is set, only deletes if
        the file belongs to that session or project.

        :param file_id: Unique file identifier.
        :param session_id: If set, verify session ownership.
        :param project_id: If set, verify project ownership.
        :returns: ``True`` if deleted, ``False`` otherwise.
        """

        def write(session: Session) -> bool:
            with query_name_scope("omnigent.file_store.select_file_by_id"):
                row = session.get(SqlFile, (current_workspace_id(), file_id))
            if not row or not _owned_by(row, session_id, project_id):
                return False
            session.delete(row)
            return True

        return run_write_transaction(self._session_immediate, "delete_file", write)

    def delete_all_for_session(
        self, session_id: str, on_orphaned: Callable[[builtins.list[str]], None]
    ) -> None:
        """
        Delete all file metadata for a session.

        Because a fork copy can share a blob (its ``blob_key`` points at
        another session's row), the deleted rows' blobs may still be
        referenced elsewhere. Only the blob keys that became **orphaned** are
        reported, so the caller deletes exactly those artifact bytes and a
        fork's shared blob survives its source session's deletion.

        :param session_id: Owning session/conversation id.
        :param on_orphaned: Receives each committed batch's orphaned keys.
        """
        self._delete_all(
            SqlFile.session_id == session_id,
            on_orphaned,
            transaction_name="delete_session_files",
            select_name="list_session_files_for_delete",
        )

    def delete_all_for_project(
        self, project_id: str, on_orphaned: Callable[[builtins.list[str]], None]
    ) -> None:
        """
        Delete all file metadata for a project.

        :param project_id: Owning project id.
        :param on_orphaned: Receives each committed batch's orphaned keys.
        """
        self._delete_all(
            SqlFile.project_id == normalize_uuid(project_id),
            on_orphaned,
            transaction_name="delete_project_files",
            select_name="list_project_files_for_delete",
        )

    def _delete_all(
        self,
        owner: ColumnElement[bool],
        on_orphaned: Callable[[builtins.list[str]], None],
        *,
        transaction_name: str,
        select_name: str,
    ) -> None:
        """
        Delete one owner's file rows and report the blobs nothing references any more.

        Runs in bounded batches, one transaction each, over the owner's index.
        Each batch's orphaned keys are reported once it commits, so a failure
        loses none of them and leaves the remaining rows for a retry.

        :param owner: Filter selecting the owner's rows, e.g.
            ``SqlFile.project_id == project_id``.
        :param on_orphaned: Receives each committed batch's orphaned keys.
        :param transaction_name: Semantic name of each batch's write transaction,
            e.g. ``"delete_project_files"``.
        :param select_name: Semantic name of each batch's row lookup, e.g.
            ``"list_project_files_for_delete"``.
        """

        def delete_batch(session: Session) -> _DeletedBatch:
            stmt = (
                select(SqlFile)
                .where(SqlFile.workspace_id == current_workspace_id(), owner)
                .order_by(SqlFile.created_at, SqlFile.id)
                .limit(_DELETE_BATCH_SIZE)
            )
            with query_name_scope(f"omnigent.file_store.{select_name}"):
                rows = list(session.execute(stmt).scalars().all())
            blob_keys = {_effective_blob_key(row) for row in rows}
            for row in rows:
                session.delete(row)
            session.flush()
            # After the rows are gone, a blob is orphaned only if no surviving
            # row (any session or project) still references it.
            orphaned: builtins.list[str] = []
            for blob_key in blob_keys:
                with query_name_scope("omnigent.file_store.count_blob_key_refs"):
                    still_referenced = _blob_is_referenced(session, blob_key)
                if not still_referenced:
                    orphaned.append(blob_key)
            return _DeletedBatch(deleted=len(rows), orphaned=orphaned)

        while True:
            batch = run_write_transaction(self._session_immediate, transaction_name, delete_batch)
            on_orphaned(batch.orphaned)
            if batch.deleted < _DELETE_BATCH_SIZE:
                return


def _page(
    session: Session,
    owner: ColumnElement[bool],
    *,
    limit: int,
    after: str | None,
    before: str | None,
    order: str,
) -> PagedList[StoredFile]:
    """
    Return one page of an owner's files, newest or oldest first.

    :param session: Open database session.
    :param owner: Filter selecting the owner's rows, e.g.
        ``SqlFile.session_id == session_id``.
    :param limit: Maximum number of files to return.
    :param after: Cursor file ID for forward pagination.
    :param before: Cursor file ID for backward pagination.
    :param order: Sort direction, ``"desc"`` or ``"asc"``.
    :returns: A :class:`PagedList` of :class:`StoredFile`.
    """
    is_desc = order == "desc"
    sort_fn = desc if is_desc else asc
    in_workspace = SqlFile.workspace_id == current_workspace_id()
    stmt: Select[tuple[SqlFile]] = select(SqlFile).where(in_workspace, owner)
    dialect_name = session.get_bind().dialect.name
    for cursor_id, older in ((after, is_desc), (before, not is_desc)):
        if not cursor_id:
            continue
        cursor_at = session.execute(
            select(SqlFile.created_at).where(in_workspace, SqlFile.id == cursor_id)
        ).scalar_one_or_none()
        if cursor_at is None:
            return PagedList(data=[], first_id=None, last_id=None, has_more=False)
        # Seek on the whole (created_at, id) key, so ties on created_at cost no extra rows.
        stmt = stmt.where(
            keyset_after(
                dialect_name,
                (SqlFile.created_at, SqlFile.id),
                (cursor_at, cursor_id),
                descending=older,
            )
        )
    stmt = stmt.order_by(sort_fn(SqlFile.created_at), sort_fn(SqlFile.id)).limit(limit + 1)
    rows = list(session.execute(stmt).scalars().all())
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]
    entities = [_to_entity(r) for r in rows]
    return PagedList(
        data=entities,
        first_id=entities[0].id if entities else None,
        last_id=entities[-1].id if entities else None,
        has_more=has_more,
    )
