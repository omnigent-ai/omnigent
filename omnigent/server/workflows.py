"""Saved workflows API: named plans their owners can run.

Routes, under ``/v1``, each scoped to the caller:

* ``GET /workflows`` lists the caller's workflows by name, one page at a time.
* ``POST /workflows`` saves one.
* ``GET /workflows/{workflow_id}`` returns one, with its steps.
* ``PATCH /workflows/{workflow_id}`` changes its metadata, steps or planning session.
* ``DELETE /workflows/{workflow_id}`` deletes it.
* ``GET /workflows/{workflow_id}/runs`` lists the sessions that ran it, newest
  first, with where each run's steps stand.

Another user's workflow answers 404. A write from an agent tool whose session
is shared with edit access is refused, as project writes are.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from omnigent.entities import Conversation
from omnigent.entities.session_journal import JournalEntry
from omnigent.entities.workflow import InvalidWorkflowError, Workflow
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server.auth import LEVEL_READ, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.permissions import check_session_access
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._sessions.helpers import _session_status_from_cache
from omnigent.server.schemas import (
    CreateWorkflowRequest,
    UpdateWorkflowRequest,
    WorkflowDeleted,
    WorkflowList,
    WorkflowObject,
    WorkflowRun,
    WorkflowRunList,
    WorkflowRunProgress,
    WorkflowRunStarter,
    WorkflowStep,
    WorkflowSummary,
)
from omnigent.server.session_live_state import has_active_child
from omnigent.server.session_orchestration import (
    require_named_source_private,
    require_owned_session,
)
from omnigent.server.workflow_telemetry import log_workflow_saved
from omnigent.stores import ConversationStore
from omnigent.stores.conversation_store import (
    CREATED_BY_SESSION_LABEL_KEY,
    MAX_SESSION_FAMILY_MEMBERS,
    SCHEDULED_TASK_RUN_LABEL_KEY,
)
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.session_journal_store import SessionJournalStore
from omnigent.stores.workflow_store import (
    WORKFLOW_PAGE_MAX,
    WorkflowNameTakenError,
    WorkflowStore,
    workflow_page_cursor,
)
from omnigent.util.uuid import InvalidUuidError, normalize_uuid, uuid_to_bytes

_logger = logging.getLogger(__name__)

_DEFAULT_PAGE = 50
# Each run on a page costs one status lookup per step, so pages stay small.
_DEFAULT_RUNS_PAGE = 20
_RUNS_PAGE_MAX = 50

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
        is_draft=not workflow.steps,
    )


def _object(workflow: Workflow) -> WorkflowObject:
    return WorkflowObject(
        **_summary(workflow).model_dump(),
        steps=[WorkflowStep(**step) for step in workflow.steps],
        planning_session_id=workflow.planning_session_id,
        description_source=workflow.description_source,
    )


def _steps(steps: list[WorkflowStep] | None) -> list[dict[str, Any]] | None:
    return None if steps is None else [step.model_dump() for step in steps]


def _not_found() -> OmnigentError:
    return OmnigentError("Workflow not found", code=ErrorCode.NOT_FOUND)


def _progress(plan: JournalEntry, statuses: dict[str, JournalEntry]) -> WorkflowRunProgress:
    """Count the run's latest steps by their current status."""
    steps = plan.content.get("steps", [])
    counts = Counter(
        statuses[step["id"]].content.get("status") if step["id"] in statuses else None
        for step in steps
    )
    return WorkflowRunProgress(
        total=len(steps),
        started=counts["started"],
        done=counts["done"],
        needs_input=counts["needs_input"],
        failed=counts["failed"],
    )


def _sessions_below(
    direct: Sequence[Conversation], started: Mapping[str, Sequence[Conversation]]
) -> list[str]:
    """The sessions below a run: each one it started, then the ones that one started.

    :param direct: The sessions the run's session started while the run was current.
    :param started: Each session's own sessions, oldest first, by its ID.
    :returns: Their IDs, each right after the session that started it.
    """
    ordered: list[str] = []
    stack = list(reversed(direct))
    while stack and len(ordered) < MAX_SESSION_FAMILY_MEMBERS:
        session = stack.pop()
        if session.id in ordered:
            continue
        ordered.append(session.id)
        stack.extend(reversed(started.get(session.id, ())))
    return ordered


def require_workflows(request: Request) -> None:
    """Reject workflow and journal requests unless the server enables workflows."""
    flags = getattr(request.app.state, "feature_flags", FeatureFlags())
    if not flags.enabled(Feature.WORKFLOWS):
        raise OmnigentError("Workflows are disabled on this server", code=ErrorCode.NOT_FOUND)


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
    router = APIRouter(dependencies=[Depends(require_workflows)])

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
        :param after: The previous page's ``next_cursor``; legacy workflow IDs also work.
        :param name: Only the workflow with this exact name, for ``/workflow <name>``.
        :raises OmnigentError: 401 if unauthenticated in multi-user mode, 400 for
            an invalid cursor or a foreign workflow ID.
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
            next_cursor=workflow_page_cursor(page[-1]) if has_more and page else None,
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
        log_workflow_saved(
            workflow,
            created=True,
            plan_saved=True,
            source_session_id=request.headers.get(ORCHESTRATION_SOURCE_HEADER),
        )
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
        planning_session_id = body.planning_session_id
        if planning_session_id is not None:
            if await asyncio.to_thread(store.get, workflow_id, user_id=user_id) is None:
                raise _not_found()
            planning_session_id = normalize_uuid(planning_session_id)
            if planning_session_id is None:
                raise OmnigentError("Invalid planning session ID", code=ErrorCode.INVALID_INPUT)
            if user_id not in (None, RESERVED_USER_LOCAL) and permission_store is not None:
                assert user_id is not None
                await asyncio.to_thread(
                    require_owned_session,
                    planning_session_id,
                    user_id,
                    conversation_store,
                    permission_store,
                    private=True,
                )
            else:
                session = await asyncio.to_thread(
                    conversation_store.get_conversation, planning_session_id
                )
                if session is None or session.archived:
                    raise OmnigentError("Planning session not found", code=ErrorCode.NOT_FOUND)
        description_source = body.description_source
        if description_source is not None:
            try:
                uuid_to_bytes(description_source)
            except InvalidUuidError as exc:
                raise OmnigentError(
                    "description_source must be a journal entry ID", code=ErrorCode.INVALID_INPUT
                ) from exc
            description_source = normalize_uuid(description_source)
        try:
            workflow = await asyncio.to_thread(
                store.update,
                workflow_id,
                user_id=user_id,
                name=body.name,
                description=body.description,
                expected_description=body.expected_description,
                description_source=description_source,
                steps=_steps(body.steps),
                planning_session_id=planning_session_id,
            )
        except InvalidWorkflowError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        except WorkflowNameTakenError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.ALREADY_EXISTS) from exc
        if workflow is None:
            raise _not_found()
        log_workflow_saved(
            workflow,
            created=False,
            plan_saved=body.steps is not None,
            source_session_id=request.headers.get(ORCHESTRATION_SOURCE_HEADER),
        )
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

    def _readable(user_id: str | None, session: Conversation) -> bool:
        if permission_store is None:
            return True
        return check_session_access(
            user_id, session.id, LEVEL_READ, permission_store, conversation_store, session
        )

    def _runs(
        journal: SessionJournalStore, plans: Sequence[JournalEntry], user_id: str | None
    ) -> list[WorkflowRun]:
        """The page's runs in sessions the caller can read, with their progress."""
        from omnigent.server.session_journal import entry_to_response

        session_ids = [plan.conversation_id for plan in plans]
        sessions = conversation_store.get_conversations(session_ids)
        readable = [
            plan
            for plan in plans
            if plan.conversation_id in sessions
            and _readable(user_id, sessions[plan.conversation_id])
        ]
        latest_plans = journal.run_latest_plans(readable)
        revisions = journal.plan_graph_revisions(latest_plans)
        statuses = journal.plan_step_statuses(latest_plans)
        current_plans = journal.current_run_plan_ids(readable)
        runs = []
        for plan, latest_plan, revision, step_statuses in zip(
            readable, latest_plans, revisions, statuses, strict=True
        ):
            session = sessions[plan.conversation_id]
            is_current = (
                plan.id in current_plans
                and plan.scheduled_task_run_id == session.labels.get(SCHEDULED_TASK_RUN_LABEL_KEY)
            )
            runs.append(
                WorkflowRun(
                    id=plan.id,
                    workflow_id=plan.workflow_id or "",
                    plan_graph_revision=revision,
                    session_id=plan.conversation_id,
                    title=session.title or "",
                    started_at=plan.created_at,
                    is_current=is_current,
                    session_status=(
                        "running"
                        if has_active_child(session.id)
                        else _session_status_from_cache(session.id, session.live_status)
                    )
                    if is_current
                    else "idle",
                    background_work_running=is_current and session.background_work_running is True,
                    archived=session.archived,
                    progress=_progress(latest_plan, step_statuses),
                    steps=[WorkflowStep(**step) for step in latest_plan.content.get("steps", [])],
                    statuses=[entry_to_response(entry) for entry in step_statuses.values()],
                )
            )
        return runs

    def _link_runs(
        journal: SessionJournalStore,
        runs: list[WorkflowRun],
        plans: Sequence[JournalEntry],
        user_id: str | None,
    ) -> list[WorkflowRun]:
        """Add who started each run and the workflow runs below it.

        The sessions a run's session starts join its family, so one family read
        finds them, and a creator label records which session started each one.
        Each of those sessions adds the workflow run it began with.

        :param plans: The plans that started *runs*, among others.
        """
        sessions = conversation_store.get_conversations([run.session_id for run in runs])
        families = conversation_store.get_session_families(
            list(dict.fromkeys(s.family_id for s in sessions.values() if s.family_id))
        )
        known = {member.id: member for family in families.values() for member in family}
        known.update(sessions)
        started: dict[str, list[Conversation]] = {}
        for member in known.values():
            if starter := member.labels.get(CREATED_BY_SESSION_LABEL_KEY):
                started.setdefault(starter, []).append(member)
        for members in started.values():
            members.sort(key=lambda member: (member.created_at, member.id))
        # A run ends when its session begins its next run, so an automation that
        # reuses its session lists each run's sessions under that run only.
        by_id = {plan.id: plan for plan in plans}
        ends = journal.next_run_starts([by_id[run.id] for run in runs])
        below: dict[str, list[str]] = {}
        for run, end in zip(runs, ends, strict=True):
            direct = [
                member
                for member in started.get(run.session_id, ())
                if member.created_at >= run.started_at and (end is None or member.created_at < end)
            ]
            below[run.id] = _sessions_below(direct, started)
        first_runs = journal.first_workflow_runs(
            list(dict.fromkeys(session_id for ids in below.values() for session_id in ids))
        )
        descendants = {
            descendant.session_id: descendant
            for descendant in (
                _runs(journal, list(first_runs.values()), user_id) if first_runs else ()
            )
        }
        starter_ids = {
            starter
            for member in known.values()
            if (starter := member.labels.get(CREATED_BY_SESSION_LABEL_KEY))
            and starter not in known
        }
        if starter_ids:
            known.update(conversation_store.get_conversations(list(starter_ids)))

        def started_by(session_id: str) -> WorkflowRunStarter | None:
            session = known.get(session_id)
            starter = (
                known.get(session.labels.get(CREATED_BY_SESSION_LABEL_KEY, ""))
                if session
                else None
            )
            if starter is None or not _readable(user_id, starter):
                return None
            return WorkflowRunStarter(session_id=starter.id, title=starter.title or "")

        for run in runs:
            run.started_by = started_by(run.session_id)
            run.descendant_runs = [
                descendants[session_id]
                for session_id in below[run.id]
                if session_id in descendants
            ]
            for descendant in run.descendant_runs:
                descendant.started_by = started_by(descendant.session_id)
        return runs

    def _readable_session(user_id: str | None, session_id: str) -> Conversation | None:
        try:
            session = conversation_store.get_conversation(uuid_to_bytes(session_id).hex())
        except InvalidUuidError:
            return None
        return session if session is not None and _readable(user_id, session) else None

    @router.get("/workflows/{workflow_id}/runs", response_model=WorkflowRunList)
    async def list_workflow_runs(
        request: Request,
        workflow_id: str,
        limit: int = Query(default=_DEFAULT_RUNS_PAGE, ge=1, le=_RUNS_PAGE_MAX),
        before: str | None = None,
        session_id: str | None = None,
    ) -> WorkflowRunList:
        """List one of the caller's workflows' runs, newest first.

        A run is the plan that named the workflow, so a session that ran it
        twice is listed twice. Sessions the caller can't read are left out.
        Each run names the session that started it and the workflow runs below it.

        :param limit: Page size, e.g. ``20``.
        :param before: The previous page's ``next_cursor``.
        :param session_id: Only that session's current run, when it follows this
            workflow, e.g. for its Tasks tab. Read access to the session stands
            in for owning the workflow, so people it is shared with see it too.
        :raises OmnigentError: 401 if unauthenticated in multi-user mode, 404 if
            the caller has no such workflow or can't read the session, 400 for
            an invalid cursor.
        """
        # Imported here: the journal router checks workflows through this module.
        from omnigent.server.session_journal import open_store as open_journal_store

        user_id = require_user(request, auth_provider)
        journal = open_journal_store(conversation_store)
        next_cursor = None
        if session_id is not None:
            session = await asyncio.to_thread(_readable_session, user_id, session_id)
            if session is None:
                raise _not_found()
            if journal is None:
                return WorkflowRunList()
            plan = await asyncio.to_thread(journal.current_workflow_run, session.id)
            wanted = normalize_uuid(workflow_id)
            plans = [plan] if plan is not None and plan.workflow_id == wanted else []
        else:
            workflow = await asyncio.to_thread(_store().get, workflow_id, user_id=user_id)
            if workflow is None:
                raise _not_found()
            if journal is None:
                return WorkflowRunList()
            try:
                page = await asyncio.to_thread(
                    journal.list_workflow_runs, workflow.id, limit=limit, before=before
                )
            except ValueError as exc:
                raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
            plans, next_cursor = page.plans, page.next_cursor

        def runs(store: SessionJournalStore) -> list[WorkflowRun]:
            listed = _runs(store, plans, user_id)
            return _link_runs(store, listed, plans, user_id) if listed else listed

        data = await asyncio.to_thread(runs, journal)
        return WorkflowRunList(data=data, next_cursor=next_cursor)

    return router
