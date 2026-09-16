"""SQLAlchemy-backed file store."""

from __future__ import annotations

import builtins

from sqlalchemy import and_, asc, desc, func, or_, select
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlFile, Uuid16, current_workspace_id, normalize_uuid
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
    )


def _effective_blob_key(row: SqlFile) -> str:
    """Artifact-store key for *row*'s bytes: ``blob_key`` or ``id`` if NULL."""
    return row.blob_key if row.blob_key is not None else row.id


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
        :returns: The newly created :class:`StoredFile`.
        """
        file_id = file_id if file_id is not None else generate_file_id()
        # New rows always store an explicit blob_key so the shared-blob
        # reference count is a plain equality (no COALESCE needed for them);
        # only pre-migration rows carry NULL and fall back to id on read.
        blob_key = blob_key if blob_key is not None else file_id
        created_at = now_epoch()

        def write(session: Session) -> StoredFile:
            row = SqlFile(
                id=file_id,
                created_at=created_at,
                filename=filename,
                bytes=bytes,
                content_type=content_type,
                session_id=session_id,
                blob_key=blob_key,
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
            row = session.execute(
                select(SqlFile.id)
                .where(
                    SqlFile.workspace_id == current_workspace_id(),
                    # id/blob_key are Uuid16 (binary); type the COALESCE so the
                    # bound value is encoded the same way, not compared as text.
                    func.coalesce(SqlFile.blob_key, SqlFile.id, type_=Uuid16())
                    == normalize_uuid(blob_key),
                )
                .limit(1)
            ).first()
            return row is None

    def get(
        self,
        file_id: str,
        session_id: str | None = None,
    ) -> StoredFile | None:
        """
        Fetch file metadata by ID.

        When ``session_id`` is set, only returns the file if it
        belongs to that session.

        :param file_id: Unique file identifier.
        :param session_id: If set, verify ownership.
        :returns: The :class:`StoredFile` if found, otherwise
            ``None``.
        """
        with self._session("select_file_by_id") as session:
            row = session.get(SqlFile, (current_workspace_id(), file_id))
            if row is None:
                return None
            if session_id is not None and row.session_id != normalize_uuid(session_id):
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
            files (``session_id IS NULL``).
        :returns: A :class:`PagedList` of :class:`StoredFile`.
        """
        with self._session("list_files") as session:
            is_desc = order == "desc"
            sort_fn = desc if is_desc else asc
            stmt = select(SqlFile).where(SqlFile.workspace_id == current_workspace_id())
            if include_unscoped:
                stmt = stmt.where(
                    or_(SqlFile.session_id == session_id, SqlFile.session_id.is_(None))
                )
            else:
                stmt = stmt.where(SqlFile.session_id == session_id)
            if after:
                sub = (
                    select(SqlFile.created_at)
                    .where(
                        SqlFile.workspace_id == current_workspace_id(),
                        SqlFile.id == after,
                    )
                    .scalar_subquery()
                )
                ts_cmp = SqlFile.created_at < sub if is_desc else SqlFile.created_at > sub
                id_cmp = SqlFile.id < after if is_desc else SqlFile.id > after
                stmt = stmt.where(or_(ts_cmp, and_(SqlFile.created_at == sub, id_cmp)))
            if before:
                sub = (
                    select(SqlFile.created_at)
                    .where(
                        SqlFile.workspace_id == current_workspace_id(),
                        SqlFile.id == before,
                    )
                    .scalar_subquery()
                )
                ts_cmp = SqlFile.created_at > sub if is_desc else SqlFile.created_at < sub
                id_cmp = SqlFile.id > before if is_desc else SqlFile.id < before
                stmt = stmt.where(or_(ts_cmp, and_(SqlFile.created_at == sub, id_cmp)))
            stmt = stmt.order_by(
                sort_fn(SqlFile.created_at),
                sort_fn(SqlFile.id),
            ).limit(limit + 1)
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

    def delete(
        self,
        file_id: str,
        session_id: str | None = None,
    ) -> bool:
        """
        Delete file metadata by ID.

        When ``session_id`` is set, only deletes if the file
        belongs to that session.

        :param file_id: Unique file identifier.
        :param session_id: If set, verify ownership.
        :returns: ``True`` if deleted, ``False`` otherwise.
        """

        def write(session: Session) -> bool:
            with query_name_scope("omnigent.file_store.select_file_by_id"):
                row = session.get(SqlFile, (current_workspace_id(), file_id))
            if not row:
                return False
            if session_id is not None and row.session_id != normalize_uuid(session_id):
                return False
            session.delete(row)
            return True

        return run_write_transaction(self._session_immediate, "delete_file", write)

    def delete_all_for_session(self, session_id: str) -> builtins.list[str]:
        """
        Delete all file metadata for a session.

        Because a fork copy can share a blob (its ``blob_key`` points at
        another session's row), the deleted rows' blobs may still be
        referenced elsewhere. This returns only the blob keys that became
        **orphaned** — the caller deletes exactly those artifact bytes, so a
        fork's shared blob survives its source session's deletion.

        :param session_id: Owning session/conversation id.
        :returns: The now-orphaned artifact-store keys to clean up.
        """

        def write(session: Session) -> builtins.list[str]:
            stmt = select(SqlFile).where(
                SqlFile.workspace_id == current_workspace_id(),
                SqlFile.session_id == session_id,
            )
            with query_name_scope("omnigent.file_store.list_session_files_for_delete"):
                rows = list(session.execute(stmt).scalars().all())
            blob_keys = {_effective_blob_key(row) for row in rows}
            for row in rows:
                session.delete(row)
            session.flush()
            # After the rows are gone, a blob is orphaned only if no surviving
            # row (any session) still references it.
            orphaned: builtins.list[str] = []
            for blob_key in blob_keys:
                with query_name_scope("omnigent.file_store.count_blob_key_refs"):
                    still_referenced = session.execute(
                        select(SqlFile.id)
                        .where(
                            SqlFile.workspace_id == current_workspace_id(),
                            func.coalesce(SqlFile.blob_key, SqlFile.id, type_=Uuid16())
                            == normalize_uuid(blob_key),
                        )
                        .limit(1)
                    ).first()
                if still_referenced is None:
                    orphaned.append(blob_key)
            return orphaned

        return run_write_transaction(self._session_immediate, "delete_session_files", write)
