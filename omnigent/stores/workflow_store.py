"""Persistent store of saved workflows.

Workflows are rows of ``workflows`` in the database that holds the session
metadata. Every read and write is scoped to the caller: another owner's
workflow reads as missing. A unique owner-and-name index arbitrates competing saves.
Every workflow query is served by, and on MySQL pinned to, the primary key or
``uq_workflows_user_id_name``. Description and plan codecs run in batches in the store.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Sequence
from typing import Any, TypeVar

from sqlalchemy import ColumnElement, Select, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, undefer

from omnigent.db import compression
from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import (
    MAX_STORED_VALUE_BYTES,
    WORKFLOW_DESCRIPTION_MAX_STORED_BYTES,
    SqlWorkflow,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities.workflow import (
    WORKFLOW_NAME_MAX_CHARS,
    InvalidWorkflowError,
    Workflow,
    normalize_workflow,
)
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.util.uuid import InvalidUuidError, uuid_to_bytes

WORKFLOW_PAGE_MAX = 200
_OWNER_NAME_INDEX = "uq_workflows_user_id_name"

_Statement = TypeVar("_Statement", bound=Select[Any])


class WorkflowNameTakenError(Exception):
    """The owner already has a workflow with this name."""


def workflow_page_cursor(workflow: Workflow) -> str:
    """Retain the page boundary when its workflow is renamed or deleted."""
    return "name:" + base64.urlsafe_b64encode(workflow.name.encode()).decode("ascii")


def _owned(workflow_id: str, user_id: str | None) -> tuple[ColumnElement[bool], ...]:
    """Match one workflow by its primary key, and only if ``user_id`` owns it."""
    return (
        SqlWorkflow.workspace_id == current_workspace_id(),
        SqlWorkflow.id == workflow_id,
        SqlWorkflow.user_id == (RESERVED_USER_LOCAL if user_id is None else user_id),
    )


def _using(statement: _Statement, index: str) -> _Statement:
    """Pin a workflows read to *index* on MySQL, the primary key or the owner-and-name index."""
    return statement.with_hint(SqlWorkflow, f"FORCE INDEX ({index})", "mysql")


def _is_name_conflict(error: IntegrityError) -> bool:
    """Identify this unique index without masking other integrity errors."""
    constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
    if constraint is not None:  # PostgreSQL and CockroachDB.
        return constraint == _OWNER_NAME_INDEX
    args = getattr(error.orig, "args", ())
    if len(args) > 1 and args[0] == 1062:  # MySQL names the key, sometimes table-qualified.
        key = str(args[1]).rsplit("for key ", 1)[-1].strip("'`").rsplit(".", 1)[-1]
        return key == _OWNER_NAME_INDEX
    return str(error.orig) == (
        "UNIQUE constraint failed: workflows.workspace_id, workflows.user_id, workflows.name"
    )


def _plan_text(steps: list[dict[str, Any]]) -> str:
    """Serialize the plan before applying the deployment's storage codec."""
    return json.dumps({"steps": steps}, ensure_ascii=False, separators=(",", ":"))


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

    def _encode_texts(self, texts: Sequence[str]) -> list[bytes]:
        """Encode a whole batch; deployments override this and :meth:`_decode_texts`.

        Encrypted values must use :func:`compression.encode_encrypted` so a reader
        without the deployment's key fails closed. Return one value per text, in order.
        """
        return [compression.encode(text) or b"" for text in texts]

    def _decode_texts(self, stored: Sequence[bytes]) -> list[bytes | str]:
        """Decode a batch in order; encrypted frames require the deployment's override."""
        return [compression.decode(value) or "" for value in stored]

    def _stored_texts(self, description: str, plan: str | None) -> list[bytes]:
        texts = [description] if plan is None else [description, plan]
        stored = self._encode_texts(texts)
        limits = [
            ("description", WORKFLOW_DESCRIPTION_MAX_STORED_BYTES),
            ("steps", MAX_STORED_VALUE_BYTES),
        ][: len(texts)]
        for value, (field, limit) in zip(stored, limits, strict=True):
            if len(value) > limit:
                raise InvalidWorkflowError(
                    f"{field}: {len(value)} stored bytes; the limit is {limit}"
                )
        return stored

    def check_description(self, description: str) -> None:
        """Refuse a description too large to store, such as one a planning chat proposes.

        :param description: e.g. ``"Triage flaky tests."``; checked as it would be stored.
        :raises InvalidWorkflowError: If its stored form is over the limit.
        """
        self._stored_texts(description.strip(), None)

    def _workflows(self, rows: Sequence[SqlWorkflow]) -> list[Workflow]:
        """Decode a page's descriptions and plans in one call."""
        texts = (
            self._decode_texts([value for row in rows for value in (row.description, row.plan)])
            if rows
            else []
        )
        pairs = zip(texts[::2], texts[1::2], strict=True)
        return [
            Workflow(
                id=row.id,
                user_id=row.user_id,
                name=row.name,
                description=description.decode("utf-8")
                if isinstance(description, bytes)
                else description,
                created_at=row.created_at,
                updated_at=row.updated_at,
                steps=json.loads(plan)["steps"],
                planning_session_id=row.planning_session_id,
            )
            for row, (description, plan) in zip(rows, pairs, strict=True)
        ]

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
        stored_description, stored_plan = self._stored_texts(text, _plan_text(workflow_steps))
        now = now_epoch()
        workflow = Workflow(
            id=uuid.uuid4().hex,
            user_id=RESERVED_USER_LOCAL if user_id is None else user_id,
            name=workflow_name,
            description=text,
            created_at=now,
            updated_at=now,
            steps=workflow_steps,
        )

        def write(session: Session) -> Workflow:
            require_active_account(session, user_id)
            session.add(
                SqlWorkflow(
                    workspace_id=current_workspace_id(),
                    id=workflow.id,
                    user_id=workflow.user_id,
                    name=workflow_name,
                    description=stored_description,
                    plan=stored_plan,
                    created_at=now,
                    updated_at=now,
                )
            )
            return workflow

        try:
            return run_write_transaction(self._session_immediate, "create_workflow", write)
        except IntegrityError as exc:
            if _is_name_conflict(exc):
                raise WorkflowNameTakenError("a workflow with this name already exists") from exc
            raise

    def get(self, workflow_id: str, *, user_id: str | None) -> Workflow | None:
        """Return one of the owner's workflows with its steps, or ``None``."""
        with self._session("get_workflow") as session:
            row = session.execute(
                _using(
                    select(SqlWorkflow)
                    .where(*_owned(workflow_id, user_id))
                    .options(undefer(SqlWorkflow.plan)),
                    "PRIMARY",
                )
            ).scalar_one_or_none()
            return None if row is None else self._workflows([row])[0]

    def list_page(
        self,
        *,
        user_id: str | None,
        limit: int,
        after: str | None = None,
        name: str | None = None,
    ) -> tuple[list[Workflow], bool]:
        """Return one page of the owner's workflows by name, including their saved plans.

        :param user_id: The owner, or ``None`` in single-user mode.
        :param limit: Page size, 1 to :data:`WORKFLOW_PAGE_MAX`.
        :param after: The previous page's cursor, or a legacy workflow ID.
        :param name: Only the workflow with this exact name.
        :returns: ``(workflows, has_more)``.
        :raises InvalidWorkflowError: If ``after`` is invalid or a foreign workflow ID.
        """
        if not 1 <= limit <= WORKFLOW_PAGE_MAX:
            raise InvalidWorkflowError(f"limit must be 1 to {WORKFLOW_PAGE_MAX}")
        with self._session("list_workflows") as session:
            stmt = (
                select(SqlWorkflow)
                .where(
                    SqlWorkflow.workspace_id == current_workspace_id(),
                    SqlWorkflow.user_id == (RESERVED_USER_LOCAL if user_id is None else user_id),
                )
                .options(undefer(SqlWorkflow.plan))
            )
            if name is not None:
                stmt = stmt.where(SqlWorkflow.name == name)
            if after is not None:
                if after.startswith("name:"):
                    try:
                        if len(after) > 4096:
                            raise ValueError("cursor is too long")
                        after_name = base64.b64decode(
                            after[5:], altchars=b"-_", validate=True
                        ).decode("utf-8")
                        if not 1 <= len(after_name) <= WORKFLOW_NAME_MAX_CHARS:
                            raise ValueError("cursor name has an invalid length")
                    except ValueError as exc:
                        raise InvalidWorkflowError("after must be a workflow cursor") from exc
                else:
                    try:
                        uuid_to_bytes(after)
                    except InvalidUuidError as exc:
                        raise InvalidWorkflowError("after must be a workflow ID") from exc
                    after_name = session.execute(
                        _using(select(SqlWorkflow.name).where(*_owned(after, user_id)), "PRIMARY")
                    ).scalar_one_or_none()
                    if after_name is None:
                        raise InvalidWorkflowError("after must be the ID of one of your workflows")
                stmt = stmt.where(SqlWorkflow.name > after_name)
            rows = session.scalars(
                _using(
                    stmt.order_by(SqlWorkflow.name).limit(limit + 1),
                    _OWNER_NAME_INDEX,
                )
            ).all()
            return self._workflows(rows[:limit]), len(rows) > limit

    def update(
        self,
        workflow_id: str,
        *,
        user_id: str | None,
        name: object = None,
        description: object = None,
        expected_description: str | None = None,
        steps: object = None,
        planning_session_id: str | None = None,
    ) -> Workflow | None:
        """Change an owned workflow's metadata, steps or planning session.

        :param workflow_id: The workflow, e.g. ``"0123…cdef"``.
        :param user_id: The owner, or ``None`` in single-user mode.
        :param name: A new name, or ``None`` to keep it.
        :param description: A new description, or ``None`` to keep it.
        :param expected_description: Apply *description* only while the stored
            description is still this text, so a newer one written meanwhile stays.
        :param steps: New steps replacing the old ones, or ``None`` to keep them.
        :param planning_session_id: An owner-checked planning session, or ``None`` to keep it.
        :returns: The updated workflow, or ``None`` if the owner has no such workflow.
        :raises InvalidWorkflowError: If a field is invalid or the steps are too large.
        :raises WorkflowNameTakenError: If another of the owner's workflows has the name.
        """

        def write(session: Session) -> Workflow | None:
            require_active_account(session, user_id)
            row = session.execute(
                _using(
                    select(SqlWorkflow)
                    .where(*_owned(workflow_id, user_id))
                    .options(undefer(SqlWorkflow.plan))
                    .with_for_update(),
                    "PRIMARY",
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            current = self._workflows([row])[0]
            stale = (
                expected_description is not None and expected_description != current.description
            )
            workflow_name, text, workflow_steps = normalize_workflow(
                name=row.name if name is None else name,
                description=current.description if description is None or stale else description,
                steps=current.steps if steps is None else steps,
            )
            stored = self._stored_texts(
                text, None if steps is None else _plan_text(workflow_steps)
            )
            values: dict[str, Any] = {
                "name": workflow_name,
                "description": stored[0],
                "updated_at": now_epoch(),
            }
            if steps is not None:
                values["plan"] = stored[1]
            if planning_session_id is not None:
                values["planning_session_id"] = planning_session_id
            session.execute(
                update(SqlWorkflow)
                .where(
                    SqlWorkflow.workspace_id == current_workspace_id(),
                    SqlWorkflow.id == workflow_id,
                )
                .values(**values)
                .with_hint("FORCE INDEX (PRIMARY)", dialect_name="mysql")
                .execution_options(synchronize_session=False)
            )
            return Workflow(
                id=row.id,
                user_id=row.user_id,
                name=workflow_name,
                description=text,
                created_at=row.created_at,
                updated_at=values["updated_at"],
                steps=workflow_steps,
                planning_session_id=values.get("planning_session_id", row.planning_session_id),
            )

        try:
            return run_write_transaction(self._session_immediate, "update_workflow", write)
        except IntegrityError as exc:
            if _is_name_conflict(exc):
                raise WorkflowNameTakenError("a workflow with this name already exists") from exc
            raise

    def delete(self, workflow_id: str, *, user_id: str | None) -> bool:
        """Delete an owned workflow; ``False`` if the owner has no such workflow."""

        def write(session: Session) -> bool:
            require_active_account(session, user_id)
            row = session.execute(
                _using(select(SqlWorkflow).where(*_owned(workflow_id, user_id)), "PRIMARY")
            ).scalar_one_or_none()
            if row is None:
                return False
            session.delete(row)
            return True

        return run_write_transaction(self._session_immediate, "delete_workflow", write)
