"""SQLAlchemy-backed file store."""

from __future__ import annotations

from sqlalchemy import and_, asc, desc, or_, select

from omnigent.db.db_models import SqlFile, current_workspace_id
from omnigent.db.utils import (
    generate_file_id,
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
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
    )


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
        self._session = make_managed_session_maker(self._engine)

    def create(
        self,
        filename: str,
        bytes: int,
        content_type: str | None = None,
        session_id: str | None = None,
    ) -> StoredFile:
        """
        Record a new file in the database.

        :param filename: Original filename.
        :param bytes: File size in bytes.
        :param content_type: MIME type.
        :param session_id: Owning session id, or ``None`` for
            global files.
        :returns: The newly created :class:`StoredFile`.
        """
        row = SqlFile(
            id=generate_file_id(),
            created_at=now_epoch(),
            filename=filename,
            bytes=bytes,
            content_type=content_type,
            session_id=session_id,
        )
        with self._session() as session:
            session.add(row)
            return _to_entity(row)

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
        with self._session() as session:
            row = session.get(SqlFile, (current_workspace_id(), file_id))
            if row is None:
                return None
            if session_id is not None and row.session_id != session_id:
                return None
            return _to_entity(row)

    def list(
        self,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
        session_id: str | None = None,
        include_unscoped: bool = False,
    ) -> PagedList[StoredFile]:
        """
        List files with cursor-based pagination.

        :param limit: Maximum number of files to return.
        :param after: Cursor file ID for forward pagination.
        :param before: Cursor file ID for backward pagination.
        :param order: Sort direction, ``"desc"`` or ``"asc"``.
        :param session_id: Filter to this session's files.
            ``None`` lists all files.
        :param include_unscoped: When ``True`` and ``session_id``
            is set, also return global files (``session_id IS NULL``).
        :returns: A :class:`PagedList` of :class:`StoredFile`.
        """
        with self._session() as session:
            is_desc = order == "desc"
            sort_fn = desc if is_desc else asc
            stmt = select(SqlFile).where(SqlFile.workspace_id == current_workspace_id())
            if session_id is not None:
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
        with self._session() as session:
            row = session.get(SqlFile, (current_workspace_id(), file_id))
            if not row:
                return False
            if session_id is not None and row.session_id != session_id:
                return False
            session.delete(row)
            return True

    def delete_all_for_session(self, session_id: str) -> list[str]:
        """
        Delete all file metadata for a session.

        :param session_id: Owning session/conversation id.
        :returns: List of deleted file ids for artifact cleanup.
        """
        with self._session() as session:
            stmt = select(SqlFile).where(
                SqlFile.workspace_id == current_workspace_id(),
                SqlFile.session_id == session_id,
            )
            rows = list(session.execute(stmt).scalars().all())
            ids = [row.id for row in rows]
            for row in rows:
                session.delete(row)
            return ids
