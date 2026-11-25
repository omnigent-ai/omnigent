"""Append-only storage for project KPI sets."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import (
    SqlProject,
    SqlProjectKpiRevision,
    SqlProjectRoleKpiRevision,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities.project_kpi import ProjectKpi, ProjectKpiRevision
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.project_store.access import project_role

_Revision = SqlProjectKpiRevision | SqlProjectRoleKpiRevision


def _revision(row: _Revision) -> ProjectKpiRevision:
    return ProjectKpiRevision(
        revision=row.revision,
        kpis=[ProjectKpi.model_validate(kpi) for kpi in row.kpis or []],
        created_by_user_id=row.created_by_user_id,
        created_by_session_id=row.created_by_session_id,
        created_at=row.created_at,
    )


class ProjectKpiStore:
    """Reads and appends KPI revisions; any member reads, only the owner writes.

    Every method serves the project's own set, or a role's set when given its
    ``identity_id``; the two are versioned separately.
    """

    def __init__(self, storage_location: str) -> None:
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.project_kpi_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.project_kpi_store", immediate=True
        )

    @staticmethod
    def _project(session: Session, project_id: str, user_id: str | None) -> tuple[SqlProject, str]:
        row = session.get(SqlProject, (current_workspace_id(), project_id))
        role = project_role(session, row, user_id)
        if row is None or role is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return row, role

    @staticmethod
    def _table(identity_id: str | None) -> type[_Revision]:
        return SqlProjectKpiRevision if identity_id is None else SqlProjectRoleKpiRevision

    @staticmethod
    def _scope(project_id: str, identity_id: str | None = None) -> tuple[Any, ...]:
        if identity_id is None:
            return (
                SqlProjectKpiRevision.workspace_id == current_workspace_id(),
                SqlProjectKpiRevision.project_id == project_id,
            )
        return (
            SqlProjectRoleKpiRevision.workspace_id == current_workspace_id(),
            SqlProjectRoleKpiRevision.project_id == project_id,
            SqlProjectRoleKpiRevision.identity_id == identity_id,
        )

    def current(
        self, project_id: str, *, user_id: str | None, identity_id: str | None = None
    ) -> ProjectKpiRevision | None:
        """The newest revision, or ``None`` when KPIs were never set.

        :raises OmnigentError: ``NOT_FOUND`` without access to the project.
        """
        with self._session("get_current_project_kpis") as session:
            self._project(session, project_id, user_id)
            table = self._table(identity_id)
            row = session.scalar(
                select(table)
                .where(*self._scope(project_id, identity_id))
                .order_by(table.revision.desc())
                .limit(1)
            )
            return None if row is None else _revision(row)

    def revisions(
        self,
        project_id: str,
        *,
        user_id: str | None,
        limit: int,
        before: int | None = None,
        identity_id: str | None = None,
    ) -> list[ProjectKpiRevision]:
        """Revisions newest first, optionally only those older than *before*."""
        with self._session("list_project_kpi_revisions") as session:
            self._project(session, project_id, user_id)
            table = self._table(identity_id)
            stmt = select(table).where(*self._scope(project_id, identity_id))
            if before is not None:
                stmt = stmt.where(table.revision < before)
            rows = session.scalars(stmt.order_by(table.revision.desc()).limit(limit)).all()
            return [_revision(row) for row in rows]

    def append(
        self,
        project_id: str,
        kpis: list[ProjectKpi],
        *,
        user_id: str | None,
        session_id: str | None = None,
        base_revision: int | None = None,
        identity_id: str | None = None,
    ) -> ProjectKpiRevision:
        """Store *kpis* as the next revision and return it.

        :param base_revision: When given, refuse unless it is the current revision
            (0 for none), so a concurrent edit isn't silently replaced.
        :raises OmnigentError: ``NOT_FOUND`` without access, ``FORBIDDEN`` for a
            shared member, ``CONFLICT`` when *base_revision* is stale.
        """
        stored = [kpi.model_dump(exclude_none=True) for kpi in kpis]
        created_at = now_epoch()

        def write(session: Session) -> ProjectKpiRevision:
            _, role = self._project(session, project_id, user_id)
            if role != "owner":
                raise OmnigentError(
                    "Only the project owner or its Lead can change the project's KPIs",
                    code=ErrorCode.FORBIDDEN,
                )
            require_active_account(session, user_id)
            table = self._table(identity_id)
            latest = session.scalar(
                select(func.max(table.revision)).where(*self._scope(project_id, identity_id))
            )
            latest = latest or 0
            if base_revision is not None and base_revision != latest:
                raise OmnigentError(
                    f"The KPIs changed since revision {base_revision}; the current revision "
                    f"is {latest}. Re-read them and apply your change again.",
                    code=ErrorCode.CONFLICT,
                )
            scope_columns = {} if identity_id is None else {"identity_id": identity_id}
            row = table(
                workspace_id=current_workspace_id(),
                project_id=project_id,
                **scope_columns,
                revision=latest + 1,
                kpis=stored,
                created_by_user_id=user_id,
                created_by_session_id=session_id,
                created_at=created_at,
            )
            session.add(row)
            session.flush()
            return _revision(row)

        try:
            return run_write_transaction(self._session_immediate, "append_project_kpis", write)
        except IntegrityError as exc:
            raise OmnigentError(
                "Another edit to the project's KPIs landed at the same time; re-read and retry.",
                code=ErrorCode.CONFLICT,
            ) from exc
