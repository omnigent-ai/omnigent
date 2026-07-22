"""SQLAlchemy-backed project store."""

from __future__ import annotations

from sqlalchemy import asc, select
from sqlalchemy.exc import IntegrityError

from omnigent.db.db_models import SqlProject, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)
from omnigent.entities import Project
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.project_store import ProjectStore


def _to_entity(row: SqlProject) -> Project:
    """
    Convert a :class:`SqlProject` ORM row to a :class:`Project`.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`Project` dataclass instance.
    """
    return Project(
        id=row.id,
        name=row.name,
        owner_user_id=row.owner_user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyProjectStore(ProjectStore):
    """
    SQLAlchemy-backed implementation of :class:`ProjectStore`.

    Persists projects in a relational database via the SQLAlchemy ORM. Every
    query is scoped by ``workspace_id`` (tenant partition) and ``owner_user_id``
    (projects are owner-private).
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy project store.

        Creates or reuses a SQLAlchemy engine and session factory for the given
        database URI.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///chat.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    def _name_taken(
        self,
        session,  # type: ignore[no-untyped-def]
        *,
        owner_user_id: str | None,
        name: str,
        exclude_id: str | None,
    ) -> bool:
        """Return whether ``owner_user_id`` already has a project named ``name``.

        Enforces per-owner name uniqueness in the store because a DB unique
        index cannot: ``owner_user_id`` is NULL in single-user mode and SQL
        treats NULLs as distinct, so null-owner rows would never collide.

        :param session: The active SQLAlchemy session.
        :param owner_user_id: The owner scope.
        :param name: The candidate name.
        :param exclude_id: A project id to exclude (the row being renamed).
        :returns: ``True`` if another of the owner's projects has this name.
        """
        stmt = select(SqlProject.id).where(
            SqlProject.workspace_id == current_workspace_id(),
            SqlProject.owner_user_id == owner_user_id,
            SqlProject.name == name,
        )
        if exclude_id is not None:
            stmt = stmt.where(SqlProject.id != exclude_id)
        return session.execute(stmt).first() is not None

    def create(
        self,
        project_id: str,
        name: str,
        owner_user_id: str | None,
    ) -> Project:
        """Insert a new, empty project.

        Name uniqueness has two layers: the ``_name_taken`` pre-check gives a
        friendly error (and is the only guard for NULL owners, which SQL treats
        as distinct), while the ``ix_projects_name`` UNIQUE index enforces it at
        the DB layer for non-NULL owners — catching a concurrent create that
        slips past the check. An index violation surfaces as ``IntegrityError``,
        which we map to the same ``ALREADY_EXISTS``.
        """
        with self._session() as session:
            if self._name_taken(session, owner_user_id=owner_user_id, name=name, exclude_id=None):
                raise OmnigentError(
                    f"A project named {name!r} already exists",
                    code=ErrorCode.ALREADY_EXISTS,
                )
            row = SqlProject(
                id=project_id,
                name=name,
                owner_user_id=owner_user_id,
                created_at=now_epoch(),
                updated_at=None,
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError as exc:
                raise OmnigentError(
                    f"A project named {name!r} already exists",
                    code=ErrorCode.ALREADY_EXISTS,
                ) from exc
            return _to_entity(row)

    def get(self, project_id: str, *, owner_user_id: str | None) -> Project | None:
        """Return an owned project by id, or ``None`` if not found."""
        with self._session() as session:
            row = session.get(SqlProject, (current_workspace_id(), project_id))
            if row is None or row.owner_user_id != owner_user_id:
                return None
            return _to_entity(row)

    def list(self, *, owner_user_id: str | None) -> list[Project]:
        """List the owner's projects ordered by ``created_at ASC, id ASC``."""
        with self._session() as session:
            stmt = (
                select(SqlProject)
                .where(SqlProject.workspace_id == current_workspace_id())
                .where(SqlProject.owner_user_id == owner_user_id)
                .order_by(asc(SqlProject.created_at), asc(SqlProject.id))
            )
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(r) for r in rows]

    def update(
        self,
        project_id: str,
        *,
        owner_user_id: str | None,
        name: str | None = None,
    ) -> Project | None:
        """Update mutable fields of an owned project.

        ``None`` leaves a field unchanged. Returns ``None`` if the project does
        not exist or is not owned by ``owner_user_id``.
        """
        with self._session() as session:
            row = session.get(SqlProject, (current_workspace_id(), project_id))
            if row is None or row.owner_user_id != owner_user_id:
                return None
            changed = False
            if name is not None and row.name != name:
                if self._name_taken(
                    session, owner_user_id=owner_user_id, name=name, exclude_id=project_id
                ):
                    raise OmnigentError(
                        f"A project named {name!r} already exists",
                        code=ErrorCode.ALREADY_EXISTS,
                    )
                row.name = name
                changed = True
            if changed:
                row.updated_at = now_epoch()
            try:
                session.flush()
            except IntegrityError as exc:
                # A concurrent rename raced past _name_taken and hit the UNIQUE
                # index (non-NULL owners).
                raise OmnigentError(
                    f"A project named {name!r} already exists",
                    code=ErrorCode.ALREADY_EXISTS,
                ) from exc
            return _to_entity(row)

    def delete(self, project_id: str, *, owner_user_id: str | None) -> bool:
        """Delete an owned project. Idempotent; returns ``False`` if not found."""
        with self._session() as session:
            row = session.get(SqlProject, (current_workspace_id(), project_id))
            if row is None or row.owner_user_id != owner_user_id:
                return False
            session.delete(row)
            return True
