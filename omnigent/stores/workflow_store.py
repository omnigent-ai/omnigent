"""Persistent store of saved workflows.

Workflows are rows of ``workflows`` in the database that holds the session
metadata. Every read and write is scoped to the caller: another owner's
workflow reads as missing. Names are unique per owner through a check here
rather than a unique key, so two concurrent saves of one name can both land,
as with projects. Every query is served by the primary key or by
``ix_workflows_user_id_name``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import ColumnElement, select, tuple_
from sqlalchemy.orm import Session, undefer

from omnigent.db import compression
from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import MAX_STORED_VALUE_BYTES, SqlWorkflow, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities.workflow import InvalidWorkflowError, Workflow, normalize_workflow
from omnigent.util.uuid import InvalidUuidError, uuid_to_bytes

WORKFLOW_PAGE_MAX = 200

# What a list page reads: everything but the plan.
_SUMMARY_COLUMNS = (
    SqlWorkflow.id,
    SqlWorkflow.user_id,
    SqlWorkflow.name,
    SqlWorkflow.description,
    SqlWorkflow.created_at,
    SqlWorkflow.updated_at,
)


class WorkflowNameTakenError(Exception):
    """The owner already has a workflow with this name."""


def _owned(workflow_id: str, user_id: str | None) -> tuple[ColumnElement[bool], ...]:
    """Match one workflow by its primary key, and only if ``user_id`` owns it."""
    return (
        SqlWorkflow.workspace_id == current_workspace_id(),
        SqlWorkflow.id == workflow_id,
        SqlWorkflow.user_id == user_id,
    )


def _plan_text(steps: list[dict[str, Any]]) -> str:
    """The stored plan, refused when it doesn't fit the column compressed."""
    text = json.dumps({"steps": steps}, ensure_ascii=False, separators=(",", ":"))
    size = len(compression.encode(text) or b"")
    if size > MAX_STORED_VALUE_BYTES:
        raise InvalidWorkflowError(
            f"the steps take {size} bytes compressed; the limit is {MAX_STORED_VALUE_BYTES}"
        )
    return text


def _to_workflow(row: Any, *, with_steps: bool) -> Workflow:
    return Workflow(
        id=row.id,
        user_id=row.user_id,
        name=row.name,
        description=row.description,
        created_at=row.created_at,
        updated_at=row.updated_at,
        steps=json.loads(row.plan)["steps"] if with_steps else None,
    )


class WorkflowStore:
    """SQLAlchemy-backed store of saved workflows."""

    def __init__(self, storage_location: str) -> None:
        """Open the store.

        :param storage_location: SQLAlchemy URI of the database holding
            ``workflows``, e.g. ``"sqlite:///omnigent.db"``.
        """
        self.storage_location = storage_location
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.workflows"
        )
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.workflows", immediate=True
        )

    def _name_taken(
        self, session: Session, *, user_id: str | None, name: str, exclude_id: str | None
    ) -> bool:
        stmt = select(SqlWorkflow.id).where(
            SqlWorkflow.workspace_id == current_workspace_id(),
            SqlWorkflow.user_id == user_id,
            SqlWorkflow.name == name,
        )
        if exclude_id is not None:
            stmt = stmt.where(SqlWorkflow.id != exclude_id)
        return session.execute(stmt.limit(1)).first() is not None

    def create(
        self, *, user_id: str | None, name: object, description: object, steps: object
    ) -> Workflow:
        """Save a new workflow.

        :param user_id: The owner, or ``None`` in single-user mode.
        :param name: One line, e.g. ``"nightly-build"``.
        :param description: What the workflow is for.
        :param steps: The graph, in the journal plan's step format.
        :returns: The saved workflow, with its steps.
        :raises InvalidWorkflowError: If a field is invalid or the steps are too large.
        :raises WorkflowNameTakenError: If the owner already uses the name.
        """
        workflow_name, text, workflow_steps = normalize_workflow(
            name=name, description=description, steps=steps
        )
        plan = _plan_text(workflow_steps)
        now = now_epoch()
        workflow = Workflow(
            id=uuid.uuid4().hex,
            user_id=user_id,
            name=workflow_name,
            description=text,
            created_at=now,
            updated_at=now,
            steps=workflow_steps,
        )

        def write(session: Session) -> Workflow:
            require_active_account(session, user_id)
            if self._name_taken(session, user_id=user_id, name=workflow_name, exclude_id=None):
                raise WorkflowNameTakenError(f"a workflow named {workflow_name!r} already exists")
            session.add(
                SqlWorkflow(
                    workspace_id=current_workspace_id(),
                    id=workflow.id,
                    user_id=user_id,
                    name=workflow_name,
                    description=text,
                    plan=plan,
                    created_at=now,
                    updated_at=now,
                )
            )
            return workflow

        return run_write_transaction(self._session_immediate, "create_workflow", write)

    def get(self, workflow_id: str, *, user_id: str | None) -> Workflow | None:
        """Return one of the owner's workflows with its steps, or ``None``."""
        with self._session("get_workflow") as session:
            row = session.execute(
                select(*_SUMMARY_COLUMNS, SqlWorkflow.plan).where(*_owned(workflow_id, user_id))
            ).first()
        return None if row is None else _to_workflow(row, with_steps=True)

    def list_page(
        self,
        *,
        user_id: str | None,
        limit: int,
        after: str | None = None,
        name: str | None = None,
    ) -> tuple[list[Workflow], bool]:
        """Return one page of the owner's workflows by name, without their steps.

        :param user_id: The owner, or ``None`` in single-user mode.
        :param limit: Page size, 1 to :data:`WORKFLOW_PAGE_MAX`.
        :param after: The previous page's last workflow ID.
        :param name: Only the workflow with this exact name.
        :returns: ``(workflows, has_more)``.
        :raises InvalidWorkflowError: If ``after`` isn't one of the owner's workflows.
        """
        if not 1 <= limit <= WORKFLOW_PAGE_MAX:
            raise InvalidWorkflowError(f"limit must be 1 to {WORKFLOW_PAGE_MAX}")
        with self._session("list_workflows") as session:
            stmt = select(*_SUMMARY_COLUMNS).where(
                SqlWorkflow.workspace_id == current_workspace_id(),
                SqlWorkflow.user_id == user_id,
            )
            if name is not None:
                stmt = stmt.where(SqlWorkflow.name == name)
            if after is not None:
                try:
                    uuid_to_bytes(after)
                except InvalidUuidError as exc:
                    raise InvalidWorkflowError("after must be a workflow ID") from exc
                after_name = session.execute(
                    select(SqlWorkflow.name).where(*_owned(after, user_id))
                ).scalar_one_or_none()
                if after_name is None:
                    raise InvalidWorkflowError("after must be the ID of one of your workflows")
                stmt = stmt.where(tuple_(SqlWorkflow.name, SqlWorkflow.id) > (after_name, after))
            rows = session.execute(
                stmt.order_by(SqlWorkflow.name, SqlWorkflow.id).limit(limit + 1)
            ).all()
        return [_to_workflow(row, with_steps=False) for row in rows[:limit]], len(rows) > limit

    def update(
        self,
        workflow_id: str,
        *,
        user_id: str | None,
        name: object = None,
        description: object = None,
        steps: object = None,
    ) -> Workflow | None:
        """Change an owned workflow's name, description or steps.

        :param workflow_id: The workflow, e.g. ``"0123…cdef"``.
        :param user_id: The owner, or ``None`` in single-user mode.
        :param name: A new name, or ``None`` to keep it.
        :param description: A new description, or ``None`` to keep it.
        :param steps: New steps replacing the old ones, or ``None`` to keep them.
        :returns: The updated workflow, or ``None`` if the owner has no such workflow.
        :raises InvalidWorkflowError: If a field is invalid or the steps are too large.
        :raises WorkflowNameTakenError: If another of the owner's workflows has the name.
        """

        def write(session: Session) -> Workflow | None:
            row = session.execute(
                select(SqlWorkflow)
                .where(*_owned(workflow_id, user_id))
                .options(undefer(SqlWorkflow.plan))
            ).scalar_one_or_none()
            if row is None:
                return None
            workflow_name, text, workflow_steps = normalize_workflow(
                name=row.name if name is None else name,
                description=row.description if description is None else description,
                steps=json.loads(row.plan)["steps"] if steps is None else steps,
            )
            require_active_account(session, user_id)
            if workflow_name != row.name and self._name_taken(
                session, user_id=user_id, name=workflow_name, exclude_id=workflow_id
            ):
                raise WorkflowNameTakenError(f"a workflow named {workflow_name!r} already exists")
            row.name = workflow_name
            row.description = text
            if steps is not None:
                row.plan = _plan_text(workflow_steps)
            row.updated_at = now_epoch()
            return _to_workflow(row, with_steps=True)

        return run_write_transaction(self._session_immediate, "update_workflow", write)

    def delete(self, workflow_id: str, *, user_id: str | None) -> bool:
        """Delete an owned workflow; ``False`` if the owner has no such workflow."""

        def write(session: Session) -> bool:
            row = session.execute(
                select(SqlWorkflow).where(*_owned(workflow_id, user_id))
            ).scalar_one_or_none()
            if row is None:
                return False
            session.delete(row)
            return True

        return run_write_transaction(self._session_immediate, "delete_workflow", write)
