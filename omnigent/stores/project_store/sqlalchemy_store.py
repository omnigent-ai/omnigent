"""SQLAlchemy-backed project store (projects + project_permissions)."""

from __future__ import annotations

import time

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from omnigent.db.db_models import SqlProject, SqlProjectPermission
from omnigent.db.utils import (
    generate_project_id,
    get_or_create_engine,
    make_managed_session_maker,
)
from omnigent.entities import Project, ProjectPermission
from omnigent.server.auth import (
    LEVEL_OWNER,
    RESERVED_USER_MEMBERS,
    RESERVED_USER_PUBLIC,
)
from omnigent.stores.project_store import ProjectStore


def _to_project(row: SqlProject) -> Project:
    return Project(
        id=row.id,
        name=row.name,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_permission(row: SqlProjectPermission) -> ProjectPermission:
    return ProjectPermission(user_id=row.user_id, project_id=row.project_id, level=row.level)


class SqlAlchemyProjectStore(ProjectStore):
    """SQLAlchemy-backed :class:`ProjectStore`.

    Shares the engine/pool with the other stores via
    :func:`get_or_create_engine`, and mirrors
    :class:`~omnigent.stores.permission_store.sqlalchemy_store.SqlAlchemyPermissionStore`'s
    dialect-aware upsert for grants.
    """

    def __init__(self, storage_location: str) -> None:
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    # ── Project records ───────────────────────────────────────────

    def create_project(self, name: str, owner: str | None) -> Project:
        """Create a project (+ owner grant when *owner* is set)."""
        now = int(time.time())
        project_id = generate_project_id()
        with self._session() as session:
            row = SqlProject(
                id=project_id,
                name=name,
                created_by=owner,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError as exc:
                # (created_by, name) unique — surface a clean domain error.
                raise ValueError(f"project {name!r} already exists for {owner!r}") from exc
            if owner is not None:
                session.add(
                    SqlProjectPermission(user_id=owner, project_id=project_id, level=LEVEL_OWNER)
                )
                session.flush()
            return _to_project(row)

    def get_project(self, project_id: str) -> Project | None:
        with self._session() as session:
            row = session.get(SqlProject, project_id)
            return _to_project(row) if row is not None else None

    def rename_project(self, project_id: str, name: str) -> Project | None:
        with self._session() as session:
            row = session.get(SqlProject, project_id)
            if row is None:
                return None
            row.name = name
            row.updated_at = int(time.time())
            try:
                session.flush()
            except IntegrityError as exc:
                raise ValueError(
                    f"project {name!r} already exists for {row.created_by!r}"
                ) from exc
            return _to_project(row)

    def delete_project(self, project_id: str) -> bool:
        with self._session() as session:
            # Grants are also FK ON DELETE CASCADE, but drop them explicitly
            # so the row is removed even on backends where the FK isn't
            # enforced.
            session.execute(
                delete(SqlProjectPermission).where(SqlProjectPermission.project_id == project_id)
            )
            result = session.execute(delete(SqlProject).where(SqlProject.id == project_id))
            return result.rowcount > 0

    def list_projects_for_user(self, user_id: str | None) -> list[tuple[Project, int]]:
        if user_id is None:
            return []
        with self._session() as session:
            # The caller's own grants + the all-members sentinel. (Public
            # projects are link-reachable but, like public sessions, do not
            # populate a member's project list.)
            grant_rows = (
                session.execute(
                    select(SqlProjectPermission).where(
                        SqlProjectPermission.user_id.in_((user_id, RESERVED_USER_MEMBERS))
                    )
                )
                .scalars()
                .all()
            )
            # Effective level per project = max across the applicable grants.
            levels: dict[str, int] = {}
            for g in grant_rows:
                levels[g.project_id] = max(levels.get(g.project_id, 0), g.level)
            if not levels:
                return []
            projects = (
                session.execute(
                    select(SqlProject)
                    .where(SqlProject.id.in_(list(levels)))
                    .order_by(SqlProject.name)
                )
                .scalars()
                .all()
            )
            return [(_to_project(p), levels[p.id]) for p in projects]

    # ── Grants ────────────────────────────────────────────────────

    def grant(self, user_id: str, project_id: str, level: int) -> ProjectPermission:
        with self._session() as session:
            is_sqlite = self._engine.dialect.name == "sqlite"
            values = {"user_id": user_id, "project_id": project_id, "level": level}
            insert = sqlite_insert if is_sqlite else pg_insert
            stmt = (
                insert(SqlProjectPermission)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["user_id", "project_id"],
                    set_={"level": level},
                )
            )
            session.execute(stmt)
            session.flush()
            return ProjectPermission(user_id=user_id, project_id=project_id, level=level)

    def revoke(self, user_id: str, project_id: str) -> bool:
        with self._session() as session:
            result = session.execute(
                delete(SqlProjectPermission).where(
                    SqlProjectPermission.user_id == user_id,
                    SqlProjectPermission.project_id == project_id,
                )
            )
            return result.rowcount > 0

    def get(self, user_id: str, project_id: str) -> ProjectPermission | None:
        with self._session() as session:
            row = session.get(SqlProjectPermission, (user_id, project_id))
            return _to_permission(row) if row is not None else None

    def list_for_project(self, project_id: str) -> list[ProjectPermission]:
        with self._session() as session:
            rows = (
                session.execute(
                    select(SqlProjectPermission).where(
                        SqlProjectPermission.project_id == project_id
                    )
                )
                .scalars()
                .all()
            )
            return [_to_permission(r) for r in rows]

    def check_access(self, user_id: str | None, project_id: str, required_level: int) -> bool:
        level = self.get_permission_level(user_id, project_id)
        return level is not None and level >= required_level

    def get_permission_level(self, user_id: str | None, project_id: str) -> int | None:
        with self._session() as session:
            candidates: list[str] = [RESERVED_USER_PUBLIC]
            if user_id is not None:
                candidates.extend((user_id, RESERVED_USER_MEMBERS))
            rows = (
                session.execute(
                    select(SqlProjectPermission.level).where(
                        SqlProjectPermission.project_id == project_id,
                        SqlProjectPermission.user_id.in_(candidates),
                    )
                )
                .scalars()
                .all()
            )
            return max(rows) if rows else None
