"""Saved workflows API: named plans their owners can run.

Routes, under ``/v1``, each scoped to the caller:

* ``GET /workflows`` lists the caller's workflows by name, one page at a time.
* ``POST /workflows`` saves one.
* ``GET /workflows/{workflow_id}`` returns one, with its steps.
* ``PATCH /workflows/{workflow_id}`` changes its name, description or steps.
* ``DELETE /workflows/{workflow_id}`` deletes it.

Another user's workflow answers 404. A write from an agent tool whose session
is shared with edit access is refused, as project writes are.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from fastapi import APIRouter, Query, Request

from omnigent.entities.workflow import InvalidWorkflowError, Workflow
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.schemas import (
    CreateWorkflowRequest,
    UpdateWorkflowRequest,
    WorkflowDeleted,
    WorkflowList,
    WorkflowObject,
    WorkflowStep,
    WorkflowSummary,
)
from omnigent.server.session_orchestration import require_named_source_private
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.workflow_store import (
    WORKFLOW_PAGE_MAX,
    WorkflowNameTakenError,
    WorkflowStore,
)

_logger = logging.getLogger(__name__)

_DEFAULT_PAGE = 50

# custom-lint: disable-next=workspace-scoped-cache -- keyed by database URL; queries scope rows
_stores: dict[str, WorkflowStore] = {}
_stores_lock = threading.Lock()


def open_store(conversation_store: object) -> WorkflowStore | None:
    """Return the workflow store beside the session metadata, or ``None`` without SQL.

    :param conversation_store: The server's conversation store; workflows live in
        the database at its ``storage_location``.
    """
    location = getattr(conversation_store, "storage_location", None)
    if not isinstance(location, str) or not location:
        return None
    with _stores_lock:
        store = _stores.get(location)
        if store is None:
            try:
                store = WorkflowStore(location)
            except Exception:
                _logger.warning("Workflow storage is unavailable", exc_info=True)
                return None
            _stores[location] = store
        return store


def _summary(workflow: Workflow) -> WorkflowSummary:
    return WorkflowSummary(
        id=workflow.id,
        name=workflow.name,
        description=workflow.description,
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
    )


def _object(workflow: Workflow) -> WorkflowObject:
    return WorkflowObject(
        **_summary(workflow).model_dump(),
        steps=[WorkflowStep(**step) for step in workflow.steps or []],
    )


def _steps(steps: list[WorkflowStep] | None) -> list[dict[str, Any]] | None:
    return None if steps is None else [step.model_dump() for step in steps]


def _not_found() -> OmnigentError:
    return OmnigentError("Workflow not found", code=ErrorCode.NOT_FOUND)


def create_workflows_router(
    conversation_store: ConversationStore,
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the workflows router, mounted under ``/v1``.

    :param conversation_store: The server's conversation store. Workflows are
        stored in its database; without a SQL backend every route answers 404.
        It also resolves the calling session an agent tool names.
    :param auth_provider: Identifies the caller; ``None`` in single-user mode.
    :param permission_store: Checks whether that calling session is shared
        with edit access; ``None`` disables the check.
    """
    router = APIRouter()

    def _store() -> WorkflowStore:
        store = open_store(conversation_store)
        if store is None:
            raise OmnigentError(
                "Workflows are not available on this server", code=ErrorCode.NOT_FOUND
            )
        return store

    async def _writer(request: Request) -> str | None:
        """The caller, once an agent tool's write from a shared session is refused."""
        user_id = require_user(request, auth_provider)
        await asyncio.to_thread(
            require_named_source_private, request, user_id, conversation_store, permission_store
        )
        return user_id

    @router.get("/workflows", response_model=WorkflowList)
    async def list_workflows(
        request: Request,
        limit: int = Query(default=_DEFAULT_PAGE, ge=1, le=WORKFLOW_PAGE_MAX),
        after: str | None = None,
        name: str | None = None,
    ) -> WorkflowList:
        """List the caller's workflows by name, without their steps.

        :param limit: Page size, e.g. ``50``.
        :param after: The previous page's ``last_id``.
        :param name: Only the workflow with this exact name, for ``/workflow <name>``.
        :raises OmnigentError: 401 if unauthenticated in multi-user mode, 400 for
            an ``after`` that isn't one of the caller's workflows.
        """
        user_id = require_user(request, auth_provider)
        store = _store()
        try:
            page, has_more = await asyncio.to_thread(
                store.list_page,
                user_id=user_id,
                limit=limit,
                after=after,
                name=name,
            )
        except InvalidWorkflowError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        return WorkflowList(
            data=[_summary(workflow) for workflow in page],
            first_id=page[0].id if page else None,
            last_id=page[-1].id if page else None,
            has_more=has_more,
        )

    @router.post("/workflows", response_model=WorkflowObject)
    async def create_workflow(request: Request, body: CreateWorkflowRequest) -> WorkflowObject:
        """Save a workflow owned by the caller.

        :raises OmnigentError: 401 if unauthenticated in multi-user mode, 400 for
            an invalid field or steps too large to store, 409 if the caller
            already has a workflow with this name.
        """
        user_id = await _writer(request)
        store = _store()
        try:
            workflow = await asyncio.to_thread(
                store.create,
                user_id=user_id,
                name=body.name,
                description=body.description,
                steps=_steps(body.steps),
            )
        except InvalidWorkflowError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        except WorkflowNameTakenError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.ALREADY_EXISTS) from exc
        return _object(workflow)

    @router.get("/workflows/{workflow_id}", response_model=WorkflowObject)
    async def get_workflow(request: Request, workflow_id: str) -> WorkflowObject:
        """Return one of the caller's workflows, with its steps.

        :raises OmnigentError: 401 if unauthenticated in multi-user mode, 404 if
            the caller has no such workflow.
        """
        user_id = require_user(request, auth_provider)
        workflow = await asyncio.to_thread(_store().get, workflow_id, user_id=user_id)
        if workflow is None:
            raise _not_found()
        return _object(workflow)

    @router.patch("/workflows/{workflow_id}", response_model=WorkflowObject)
    async def update_workflow(
        request: Request, workflow_id: str, body: UpdateWorkflowRequest
    ) -> WorkflowObject:
        """Change one of the caller's workflows; fields left out keep their values.

        :raises OmnigentError: 401 if unauthenticated in multi-user mode, 400 for
            an invalid field, 404 if the caller has no such workflow, 409 if
            another of the caller's workflows has the new name.
        """
        user_id = await _writer(request)
        store = _store()
        try:
            workflow = await asyncio.to_thread(
                store.update,
                workflow_id,
                user_id=user_id,
                name=body.name,
                description=body.description,
                steps=_steps(body.steps),
            )
        except InvalidWorkflowError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        except WorkflowNameTakenError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.ALREADY_EXISTS) from exc
        if workflow is None:
            raise _not_found()
        return _object(workflow)

    @router.delete("/workflows/{workflow_id}", response_model=WorkflowDeleted)
    async def delete_workflow(request: Request, workflow_id: str) -> WorkflowDeleted:
        """Delete one of the caller's workflows.

        :raises OmnigentError: 401 if unauthenticated in multi-user mode, 404 if
            the caller has no such workflow.
        """
        user_id = await _writer(request)
        deleted = await asyncio.to_thread(_store().delete, workflow_id, user_id=user_id)
        if not deleted:
            raise _not_found()
        return WorkflowDeleted(id=workflow_id)

    return router
