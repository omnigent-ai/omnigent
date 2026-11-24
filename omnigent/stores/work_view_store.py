"""Owner-scoped persistence for tasks and saved views."""

from __future__ import annotations

import uuid
from typing import Any, TypeVar

from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import (
    SqlProject,
    SqlWorkTask,
    SqlWorkTaskSession,
    SqlWorkView,
    current_workspace_id,
)
from omnigent.db.keyset import keyset_after
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities.work_view import (
    TASK_STATUS_CODES,
    LinkWorkSession,
    SaveView,
    SaveWorkTask,
    ViewQuery,
)
from omnigent.errors import ErrorCode, OmnigentError


def _missing() -> OmnigentError:
    return OmnigentError("Item not found", code=ErrorCode.NOT_FOUND)


OwnedRow = TypeVar("OwnedRow", bound=SqlProject | SqlWorkTask | SqlWorkView)


def _owned(
    session: Session,
    model: type[OwnedRow],
    record_id: str,
    user_id: str | None,
    *,
    lock: bool = False,
) -> OwnedRow:
    row = session.get(model, (current_workspace_id(), record_id), with_for_update=lock)
    if row is None or row.user_id != user_id:
        raise _missing()
    return row


def _view(row: SqlWorkView) -> dict[str, Any]:
    return {
        **row.definition,
        "id": row.id,
        "name": row.name,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _task(row: SqlWorkTask, project: SqlProject | None = None) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "description": row.description,
        "status": next(name for name, code in TASK_STATUS_CODES.items() if code == row.status),
        "priority": row.priority,
        "project_id": project.id if project is not None else None,
        "project_name": project.name if project is not None else None,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


class WorkViewStore:
    def __init__(self, storage_location: str) -> None:
        engine = get_or_create_engine(storage_location)
        self._dialect = engine.dialect.name
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.work_views"
        )
        self._write = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.work_views", immediate=True
        )

    def list_views(self, user_id: str | None, after: str | None = None) -> dict[str, Any]:
        with self._session("list_saved_views") as session:
            query = select(SqlWorkView).where(
                SqlWorkView.workspace_id == current_workspace_id(), SqlWorkView.user_id == user_id
            )
            if after:
                _owned(session, SqlWorkView, after, user_id)
                query = query.where(SqlWorkView.id > after)
            rows = session.scalars(query.order_by(SqlWorkView.id).limit(101)).all()
            return {
                "data": [_view(row) for row in rows[:100]],
                "has_more": len(rows) > 100,
                "next_cursor": rows[99].id if len(rows) > 100 else None,
            }

    def get_view(self, record_id: str, user_id: str | None) -> dict[str, Any]:
        with self._session("read_saved_view") as session:
            return _view(_owned(session, SqlWorkView, record_id, user_id))

    def save_view(
        self, body: SaveView, user_id: str | None, record_id: str | None = None
    ) -> dict[str, Any]:
        def write(session: Session) -> dict[str, Any]:
            require_active_account(session, user_id)
            now = now_epoch()
            if record_id:
                row = _owned(session, SqlWorkView, record_id, user_id)
            else:
                row = SqlWorkView(
                    workspace_id=current_workspace_id(),
                    id=uuid.uuid4().hex,
                    user_id=user_id,
                    created_at=now,
                )
                session.add(row)
            row.name = body.name
            row.definition = body.model_dump(exclude={"name"})
            row.updated_at = now
            return _view(row)

        return run_write_transaction(self._write, "save_view", write)

    def delete_view(self, record_id: str, user_id: str | None) -> None:
        def write(session: Session) -> None:
            require_active_account(session, user_id)
            session.delete(_owned(session, SqlWorkView, record_id, user_id))

        run_write_transaction(self._write, "delete_view", write)

    def _project(
        self, session: Session, project_id: str | None, user_id: str | None
    ) -> SqlProject | None:
        if not project_id:
            return None
        project = session.get(SqlProject, (current_workspace_id(), project_id))
        return project if project is not None and project.user_id == user_id else None

    def save_task(
        self, body: SaveWorkTask, user_id: str | None, record_id: str | None = None
    ) -> dict[str, Any]:
        def write(session: Session) -> dict[str, Any]:
            require_active_account(session, user_id)
            project = self._project(session, body.project_id, user_id)
            if body.project_id and project is None:
                raise _missing()
            now = now_epoch()
            if record_id:
                row = _owned(session, SqlWorkTask, record_id, user_id)
            else:
                row = SqlWorkTask(
                    workspace_id=current_workspace_id(),
                    id=uuid.uuid4().hex,
                    user_id=user_id,
                    created_at=now,
                )
                session.add(row)
            row.title, row.description = body.title, body.description
            row.status, row.priority = TASK_STATUS_CODES[body.status], body.priority
            row.project_id, row.updated_at = body.project_id, now
            return _task(row, project)

        return run_write_transaction(self._write, "save_task", write)

    def get_task(self, record_id: str, user_id: str | None) -> dict[str, Any]:
        with self._session("read_task") as session:
            row = _owned(session, SqlWorkTask, record_id, user_id)
            result = _task(row, self._project(session, row.project_id, user_id))
            links = session.scalars(
                select(SqlWorkTaskSession)
                .where(
                    SqlWorkTaskSession.workspace_id == current_workspace_id(),
                    SqlWorkTaskSession.task_id == record_id,
                )
                .order_by(SqlWorkTaskSession.created_at, SqlWorkTaskSession.session_id)
                .limit(100)
            ).all()
            result["sessions"] = [
                {"session_id": link.session_id, "involvement": link.involvement} for link in links
            ]
            return result

    def delete_task(self, record_id: str, user_id: str | None) -> None:
        def write(session: Session) -> None:
            require_active_account(session, user_id)
            task = _owned(session, SqlWorkTask, record_id, user_id, lock=True)
            session.execute(
                delete(SqlWorkTaskSession).where(
                    SqlWorkTaskSession.workspace_id == current_workspace_id(),
                    SqlWorkTaskSession.task_id == record_id,
                )
            )
            session.delete(task)

        run_write_transaction(self._write, "delete_task", write)

    def link_session(self, record_id: str, body: LinkWorkSession, user_id: str | None) -> None:
        def write(session: Session) -> None:
            require_active_account(session, user_id)
            task = _owned(session, SqlWorkTask, record_id, user_id, lock=True)
            key = (current_workspace_id(), body.session_id)
            row = session.get(SqlWorkTaskSession, key)
            if row is not None and row.task_id != record_id:
                raise OmnigentError(
                    "Session is already linked to a task; unlink it first",
                    code=ErrorCode.ALREADY_EXISTS,
                )
            if row is None:
                count = (
                    session.scalar(
                        select(func.count())
                        .select_from(SqlWorkTaskSession)
                        .where(
                            SqlWorkTaskSession.workspace_id == key[0],
                            SqlWorkTaskSession.task_id == record_id,
                        )
                    )
                    or 0
                )
                if count >= 100:
                    raise OmnigentError(
                        "A task can link up to 100 sessions", code=ErrorCode.INVALID_INPUT
                    )
                row = SqlWorkTaskSession(
                    workspace_id=key[0],
                    task_id=record_id,
                    session_id=body.session_id,
                    created_at=now_epoch(),
                )
                session.add(row)
            row.involvement = body.involvement
            task.updated_at = now_epoch()

        try:
            run_write_transaction(self._write, "link_task_session", write)
        except IntegrityError as exc:
            raise OmnigentError(
                "Session is already linked to a task", code=ErrorCode.ALREADY_EXISTS
            ) from exc

    def unlink_session(self, record_id: str, session_id: str, user_id: str | None) -> None:
        def write(session: Session) -> None:
            require_active_account(session, user_id)
            task = _owned(session, SqlWorkTask, record_id, user_id)
            session.execute(
                delete(SqlWorkTaskSession).where(
                    SqlWorkTaskSession.workspace_id == current_workspace_id(),
                    SqlWorkTaskSession.task_id == record_id,
                    SqlWorkTaskSession.session_id == session_id,
                )
            )
            task.updated_at = now_epoch()

        run_write_transaction(self._write, "unlink_task_session", write)

    def query(self, body: ViewQuery, user_id: str | None) -> dict[str, Any]:
        """Apply filters before keyset pagination; views never cache membership."""
        with self._session("query_view_results") as session:
            is_task = body.entity_type == "tasks"
            model = SqlWorkTask if is_task else SqlProject
            title = SqlWorkTask.title if is_task else SqlProject.name
            query = select(model).where(
                model.workspace_id == current_workspace_id(), model.user_id == user_id
            )
            filters = body.filters
            if filters.search.strip():
                query = query.where(title.icontains(filters.search.strip(), autoescape=True))
            if filters.updated_after is not None:
                query = query.where(
                    func.coalesce(model.updated_at, model.created_at) >= filters.updated_after
                )
            if is_task:
                query = query.outerjoin(
                    SqlProject,
                    (SqlProject.workspace_id == SqlWorkTask.workspace_id)
                    & (SqlProject.id == SqlWorkTask.project_id)
                    & (SqlProject.user_id == user_id),
                )
                if filters.statuses:
                    query = query.where(
                        SqlWorkTask.status.in_(
                            [TASK_STATUS_CODES[status] for status in filters.statuses]
                        )
                    )
                if filters.priorities:
                    query = query.where(SqlWorkTask.priority.in_(filters.priorities))
                project_filters = []
                if filters.project_ids:
                    project_filters.append(SqlProject.id.in_(filters.project_ids))
                if filters.no_project:
                    project_filters.append(SqlProject.id.is_(None))
                if project_filters:
                    query = query.where(or_(*project_filters))
            elif filters.project_ids:
                query = query.where(SqlProject.id.in_(filters.project_ids))
            order = body.display.order_by
            column = (
                title
                if order == "title"
                else func.coalesce(model.updated_at, model.created_at)
                if order == "updated_at"
                else getattr(model, order)
            )
            # Unset priority sorts after explicitly prioritized work.
            if order == "priority":
                column = case((SqlWorkTask.priority == 0, 5), else_=SqlWorkTask.priority)
            ascending = body.display.direction == "asc"
            if body.cursor:
                cursor = _owned(session, model, body.cursor, user_id)
                value = (
                    (cursor.title if isinstance(cursor, SqlWorkTask) else cursor.name)
                    if order == "title"
                    else (cursor.updated_at or cursor.created_at)
                    if order == "updated_at"
                    else getattr(cursor, order)
                )
                if order == "priority" and value == 0:
                    value = 5
                query = query.where(
                    keyset_after(
                        self._dialect,
                        (column, model.id),
                        (value, body.cursor),
                        descending=not ascending,
                    )
                )
            rows = session.scalars(
                query.order_by(
                    column.asc() if ascending else column.desc(),
                    model.id.asc() if ascending else model.id.desc(),
                ).limit(body.limit + 1)
            ).all()
            more = len(rows) > body.limit
            rows = rows[: body.limit]
            project_ids = {
                row.project_id for row in rows if isinstance(row, SqlWorkTask) and row.project_id
            }
            projects = (
                {
                    project.id: project
                    for project in session.scalars(
                        select(SqlProject).where(
                            SqlProject.workspace_id == current_workspace_id(),
                            SqlProject.user_id == user_id,
                            SqlProject.id.in_(project_ids),
                        )
                    ).all()
                }
                if project_ids
                else {}
            )
            data = [
                _task(row, projects.get(row.project_id) if row.project_id else None)
                if isinstance(row, SqlWorkTask)
                else {
                    "id": row.id,
                    "title": row.name,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at or row.created_at,
                }
                for row in rows
            ]
            return {"data": data, "has_more": more, "next_cursor": rows[-1].id if more else None}
