"""REST API routes for projects (``/v1/projects``).

Projects are first-class, owner-private containers that group sessions (see
``designs/PROJECTS_PRD.md``). These endpoints let the web UI create empty
projects, list them, rename them, and delete them. Session membership
(filing a session into a project) is managed on the sessions API via the
conversation store's ``project_id``.

Because projects are owner-private and carry no ACL of their own, every handler
scopes to the requesting user: a caller only ever sees and mutates their own
projects. In single-user mode (no auth provider) the owner is ``None`` and all
projects share that scope.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

from fastapi import APIRouter, Query, Request
from sqlalchemy.exc import IntegrityError

from omnigent.entities import Project
from omnigent.entities.project import PROJECT_IDENTITY_LABEL_KEY, MandateProposal
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server import session_live_state
from omnigent.server.auth import AuthProvider
from omnigent.server.project_identities import (
    ORCHESTRATOR_IDENTITY_ID,
    identity_response,
    identity_session_state,
    orchestrator_identity_response,
)
from omnigent.server.project_main_session import (
    ProjectSessionCreator,
    create_initial_main_session,
    creation_id,
    creation_key,
    default_project_agent_id,
    expected_main_id,
    main_session_state,
    project_session_payload,
    validate_main_assignment,
)
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes.sessions import announce_projects_changed
from omnigent.server.schemas import (
    CreateProjectRequest,
    ProjectIdentityRequest,
    ProjectIdentitySessionRequest,
    ProjectOrderRequest,
    ProjectOrderResponse,
    ProposeMandateRequest,
    UpdateProjectRequest,
)
from omnigent.server.session_orchestration import require_named_source_private
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.project_store import ProjectOrderPreference, ProjectStore, apply_project_order
from omnigent.util.uuid import InvalidUuidError, normalize_uuid, uuid_to_bytes

_DEFAULT_PROJECT_PAGE = 50
_MAX_PROJECT_PAGE = 200


def _to_response(project: Project) -> dict[str, Any]:
    """Convert a :class:`Project` entity to a ``ProjectObject`` response dict.

    :param project: The entity to convert.
    :returns: Dict matching the :class:`ProjectObject` shape.
    """
    return {
        "id": project.id,
        "object": "project",
        "name": project.name,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "config": project.config,
        "mandate": project.mandate,
        "mandate_proposals": [_proposal_response(entry) for entry in project.mandate_proposals],
        "main_session_id": project.main_session_id,
        # Deprecated response alias; remove in 0.25.0.
        "orchestrator_session_id": project.main_session_id,
    }


def _proposal_response(proposal: MandateProposal) -> dict[str, Any]:
    """Serialize a pending proposal as a ``MandateProposalObject``."""
    return {
        "id": proposal.id,
        "text": proposal.text,
        "created_at": proposal.created_at,
        "session_id": proposal.session_id,
    }


def _refuse_agent_mandate_write(request: Request, mandate: str | None) -> None:
    """Keep the mandate owner-written: agents it bounds may only propose additions.

    :param request: The incoming request; agent tools mark theirs with
        :data:`ORCHESTRATION_SOURCE_HEADER`.
    :param mandate: The requested mandate, or ``None`` when unchanged.
    :raises OmnigentError: 403 for an agent-originated mandate write.
    """
    if mandate is not None and ORCHESTRATION_SOURCE_HEADER in request.headers:
        raise OmnigentError(
            "Only the project owner can change a project's mandate; propose an addition "
            "with sys_project_mandate_propose instead",
            code=ErrorCode.FORBIDDEN,
        )


def create_projects_router(
    project_store: ProjectStore,
    auth_provider: AuthProvider | None = None,
    conversation_store: ConversationStore | None = None,
    permission_store: PermissionStore | None = None,
    create_session: ProjectSessionCreator | None = None,
) -> APIRouter:
    """Build the projects router (``/v1/projects``).

    :param project_store: The store backing project persistence.
    :param auth_provider: Auth provider used to identify the requesting user.
        ``None`` in single-user mode (owner scope is ``None``).
    :param conversation_store: Resolves the calling session an agent tool names.
    :param permission_store: Checks whether that session is shared with edit access.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    async def response(project: Project) -> dict[str, Any]:
        return {
            **_to_response(project),
            "main_session_state": await main_session_state(
                project, conversation_store, permission_store
            ),
        }

    async def _require_private_source(request: Request, user_id: str | None) -> None:
        """Refuse an agent-tool write whose calling session is shared with edit access."""
        if conversation_store is not None:
            await asyncio.to_thread(
                require_named_source_private,
                request,
                user_id,
                conversation_store,
                permission_store,
            )

    @router.post("/projects")
    async def create_project(
        request: Request,
        body: CreateProjectRequest,
    ) -> dict[str, Any]:
        """Create a project and its ordinary main session for the caller.

        :param request: The incoming request, used to identify the user.
        :param body: Project payload (name).
        :returns: The created project as a serialized dict.
        :raises OmnigentError: 401 if unauthenticated in multi-user mode, 409
            if the caller already has a project with this name.
        """
        user_id = require_user(request, auth_provider)
        await _require_private_source(request, user_id)
        _refuse_agent_mandate_write(request, body.mandate)
        key = creation_key(request) if "Idempotency-Key" in request.headers else None
        project_id = creation_id("project", user_id, key) if key else uuid.uuid4().hex
        project = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
        created_here = False
        if project is None:
            try:
                project = await asyncio.to_thread(
                    project_store.create,
                    project_id,
                    body.name,
                    user_id,
                    body.config,
                    body.mandate,
                )
                created_here = True
            except (IntegrityError, OmnigentError) as exc:
                if isinstance(exc, OmnigentError) and exc.code != ErrorCode.ALREADY_EXISTS:
                    raise
                # A concurrent retry may have inserted this exact operation's row.
                project = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
                if project is None:
                    raise
        try:
            project = await create_initial_main_session(
                request,
                project,
                projects=project_store,
                create_session=create_session,
            )
        except Exception as exc:
            if created_here and not (
                isinstance(exc, OmnigentError) and exc.code == ErrorCode.CONFLICT
            ):
                # Roll back validation failures, never a bound chat or a concurrent winner.
                with contextlib.suppress(OmnigentError):
                    await asyncio.to_thread(
                        project_store.delete, project.id, user_id=user_id, require_no_main=True
                    )
            raise
        # Push the change to the owner's other connected clients so their
        # sidebars pick up the new folder without a reload.
        announce_projects_changed(user_id)
        return await response(project)

    @router.get("/projects")
    async def list_projects(
        request: Request,
        limit: int | None = Query(default=None, ge=1, le=_MAX_PROJECT_PAGE),
        after: str | None = None,
    ) -> dict[str, Any]:
        """List the caller's projects.

        Without ``limit`` or ``after`` this returns every project in the
        caller's display order, as the sidebar needs. With either, it returns
        one creation-ordered page.

        :param request: The incoming request, used to identify the user.
        :param limit: Page size, e.g. ``50``.
        :param after: The previous page's ``last_id``.
        :returns: ``{"object": "list", "data": [...]}``, plus ``first_id``,
            ``last_id`` and ``has_more`` for a page.
        :raises OmnigentError: 401 if unauthenticated in multi-user mode, 400
            for an unknown ``after``.
        """
        user_id = require_user(request, auth_provider)
        if limit is not None or after is not None:
            try:
                page, has_more = await asyncio.to_thread(
                    project_store.list_page,
                    user_id=user_id,
                    limit=limit or _DEFAULT_PROJECT_PAGE,
                    after_id=after,
                )
            except ValueError as exc:
                raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
            return {
                "object": "list",
                "data": [await response(p) for p in page],
                "first_id": page[0].id if page else None,
                "last_id": page[-1].id if page else None,
                "has_more": has_more,
            }

        def read_projects() -> list[Project]:
            return apply_project_order(
                project_store.list(user_id=user_id),
                project_store.get_order(user_id=user_id),
                project_id=lambda p: p.id,
                project_name=lambda p: p.name,
            )

        projects = await asyncio.to_thread(read_projects)
        return {"object": "list", "data": [await response(p) for p in projects]}

    @router.get(
        "/projects/order", response_model=None, responses={200: {"model": ProjectOrderResponse}}
    )
    async def get_project_order(request: Request) -> ProjectOrderPreference:
        """Read the caller's custom order."""
        user_id = require_user(request, auth_provider)
        return await asyncio.to_thread(project_store.get_order_preference, user_id=user_id)

    @router.put(
        "/projects/order", response_model=None, responses={200: {"model": ProjectOrderResponse}}
    )
    async def save_project_order(
        request: Request, body: ProjectOrderRequest
    ) -> ProjectOrderPreference:
        """Save the caller's order and notify their connected clients."""
        user_id = require_user(request, auth_provider)
        preference = await asyncio.to_thread(
            project_store.save_order, body.ordered_project_ids, user_id=user_id
        )
        announce_projects_changed(user_id)
        return preference

    @router.get("/projects/{project_id}")
    async def get_project(request: Request, project_id: str) -> dict[str, Any]:
        """Return one of the caller's projects.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project to fetch.
        :returns: The project as a serialized dict.
        :raises OmnigentError: 401 if unauthenticated, 404 if not found / not
            owned by the caller.
        """
        user_id = require_user(request, auth_provider)
        project = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return await response(project)

    @router.patch("/projects/{project_id}")
    async def update_project(
        request: Request,
        project_id: str,
        body: UpdateProjectRequest,
    ) -> dict[str, Any]:
        """Update one of the caller's projects (e.g. rename).

        :param request: The incoming request, used to identify the user.
        :param project_id: The project to update.
        :param body: Fields to change; ``None`` fields are left unchanged.
        :returns: The updated project as a serialized dict.
        :raises OmnigentError: 401 if unauthenticated, 404 if not found / not
            owned, 409 if the new name collides with another of the caller's
            projects.
        """
        user_id = require_user(request, auth_provider)
        await _require_private_source(request, user_id)
        _refuse_agent_mandate_write(request, body.mandate)
        session_id = None
        if body.main_session_id is not None:
            current = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
            if current is None or conversation_store is None:
                raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
            session_id = await validate_main_assignment(
                current, body.main_session_id, conversation_store, permission_store
            )
        project = await asyncio.to_thread(
            project_store.update,
            project_id,
            user_id=user_id,
            name=body.name,
            config=body.config,
            mandate=body.mandate,
            main_session_id=session_id,
            expected_main_session_id=expected_main_id(body.expected_main_session_id),
        )
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        # A rename/config change is only ever seen by other connected clients
        # (another tab, the mobile app) if it is pushed; nothing else refreshes
        # their projects cache until a full reload.
        announce_projects_changed(user_id)
        return await response(project)

    @router.post("/projects/{project_id}/mandate/proposals")
    async def propose_mandate_addition(
        request: Request, project_id: str, body: ProposeMandateRequest
    ) -> dict[str, Any]:
        """Queue a mandate addition for the owner; agents may propose but never apply one.

        :param request: The incoming request; an agent tool's names its session.
        :param project_id: The project whose mandate the addition extends.
        :param body: The proposed text.
        :returns: ``{"proposal": ..., "project": ...}``.
        :raises OmnigentError: 404 if not found / not owned, 400 for blank or
            oversized text, 409 when the pending list is full.
        """
        user_id = require_user(request, auth_provider)
        await _require_private_source(request, user_id)
        source = request.headers.get(ORCHESTRATION_SOURCE_HEADER)
        try:
            source_session = uuid_to_bytes(source).hex() if source else None
        except (InvalidUuidError, ValueError):
            source_session = None
        result = await asyncio.to_thread(
            project_store.propose_mandate_addition,
            project_id,
            body.text,
            user_id=user_id,
            session_id=source_session,
        )
        if result is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        project, proposal = result
        if source_session is not None:
            # The owner must accept or dismiss it, so the proposing session needs their eyes.
            session_live_state.raise_attention(source_session)
        announce_projects_changed(user_id)
        return {"proposal": _proposal_response(proposal), "project": await response(project)}

    async def resolve_proposal(
        request: Request, project_id: str, proposal_id: str, *, accept: bool
    ) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        if ORCHESTRATION_SOURCE_HEADER in request.headers:
            raise OmnigentError(
                "Only the project owner can accept or dismiss a mandate proposal",
                code=ErrorCode.FORBIDDEN,
            )
        project = await asyncio.to_thread(
            project_store.resolve_mandate_proposal,
            project_id,
            proposal_id,
            user_id=user_id,
            accept=accept,
        )
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        announce_projects_changed(user_id)
        return await response(project)

    @router.post("/projects/{project_id}/mandate/proposals/{proposal_id}/accept")
    async def accept_mandate_proposal(
        request: Request, project_id: str, proposal_id: str
    ) -> dict[str, Any]:
        """Append a pending proposal to the mandate. Owner only, never an agent tool.

        :returns: The updated project.
        :raises OmnigentError: 403 for an agent request, 404 for an unknown
            project or proposal, 400 when the mandate would exceed its limit.
        """
        return await resolve_proposal(request, project_id, proposal_id, accept=True)

    @router.delete("/projects/{project_id}/mandate/proposals/{proposal_id}")
    async def dismiss_mandate_proposal(
        request: Request, project_id: str, proposal_id: str
    ) -> dict[str, Any]:
        """Drop a pending proposal without changing the mandate. Owner only.

        :returns: The updated project.
        :raises OmnigentError: 403 for an agent request, 404 for an unknown
            project or proposal.
        """
        return await resolve_proposal(request, project_id, proposal_id, accept=False)

    # -- Identities: the built-in Orchestrator plus owner-defined roles --

    def _refuse_agent_identity_write(request: Request) -> None:
        """Identities shape agents' personas, so only the project owner edits them."""
        if ORCHESTRATION_SOURCE_HEADER in request.headers:
            raise OmnigentError(
                "Only the project owner can change a project's identities",
                code=ErrorCode.FORBIDDEN,
            )

    async def _owned_project(project_id: str, user_id: str | None) -> Project:
        project = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return project

    def _identity_id(identity_id: str) -> str:
        normalized = normalize_uuid(identity_id)
        if normalized is None:
            raise OmnigentError("Identity not found", code=ErrorCode.NOT_FOUND)
        return normalized

    async def _identity_payload(project: Project, identity: Any) -> dict[str, Any]:
        state = await identity_session_state(
            project, identity.active_session_id, conversation_store, permission_store
        )
        return identity_response(identity, state)

    @router.get("/projects/{project_id}/identities")
    async def list_identities(request: Request, project_id: str) -> dict[str, Any]:
        """List the project's identities, the built-in Orchestrator first."""
        user_id = require_user(request, auth_provider)
        project = await _owned_project(project_id, user_id)
        identities = await asyncio.to_thread(
            project_store.list_identities, project.id, user_id=user_id
        )
        orchestrator = orchestrator_identity_response(
            project, await main_session_state(project, conversation_store, permission_store)
        )
        data = [orchestrator] + [
            await _identity_payload(project, identity) for identity in identities
        ]
        return {"object": "list", "data": data}

    @router.post("/projects/{project_id}/identities")
    async def create_identity(
        request: Request, project_id: str, body: ProjectIdentityRequest
    ) -> dict[str, Any]:
        """Add a named role to the project, e.g. a PM with its own persona."""
        user_id = require_user(request, auth_provider)
        _refuse_agent_identity_write(request)
        if body.name is None:
            raise OmnigentError("name is required", code=ErrorCode.INVALID_INPUT)
        project = await _owned_project(project_id, user_id)
        key = creation_key(request) if "Idempotency-Key" in request.headers else None
        identity_id = (
            creation_id("project-identity", user_id, key, project.id) if key else uuid.uuid4().hex
        )
        identity = await asyncio.to_thread(
            project_store.create_identity,
            project.id,
            identity_id,
            user_id=user_id,
            name=body.name,
            instructions=body.instructions or "",
            agent_id=body.agent_id,
            model=body.model,
            effort=body.effort,
        )
        announce_projects_changed(user_id)
        return await _identity_payload(project, identity)

    @router.patch("/projects/{project_id}/identities/{identity_id}")
    async def update_identity(
        request: Request, project_id: str, identity_id: str, body: ProjectIdentityRequest
    ) -> dict[str, Any]:
        """Edit an identity's name, persona or session defaults; empty strings clear."""
        user_id = require_user(request, auth_provider)
        _refuse_agent_identity_write(request)
        project = await _owned_project(project_id, user_id)
        clear = frozenset(
            field
            for field in ("agent_id", "model", "effort")
            if getattr(body, field) == "" and field in body.model_fields_set
        )
        identity = await asyncio.to_thread(
            project_store.update_identity,
            project.id,
            _identity_id(identity_id),
            user_id=user_id,
            name=body.name,
            instructions=body.instructions,
            agent_id=body.agent_id or None,
            model=body.model or None,
            effort=body.effort or None,
            clear=clear,
        )
        if identity is None:
            raise OmnigentError("Identity not found", code=ErrorCode.NOT_FOUND)
        announce_projects_changed(user_id)
        return await _identity_payload(project, identity)

    @router.delete("/projects/{project_id}/identities/{identity_id}")
    async def delete_identity(
        request: Request, project_id: str, identity_id: str
    ) -> dict[str, Any]:
        """Remove an identity; its past sessions stay in the project."""
        user_id = require_user(request, auth_provider)
        _refuse_agent_identity_write(request)
        project = await _owned_project(project_id, user_id)
        normalized = _identity_id(identity_id)
        deleted = await asyncio.to_thread(
            project_store.delete_identity, project.id, normalized, user_id=user_id
        )
        if not deleted:
            raise OmnigentError("Identity not found", code=ErrorCode.NOT_FOUND)
        announce_projects_changed(user_id)
        return {"id": normalized, "object": "project.identity.deleted", "deleted": True}

    @router.post("/projects/{project_id}/identities/{identity_id}/session")
    async def open_identity_session(
        request: Request,
        project_id: str,
        identity_id: str,
        body: ProjectIdentitySessionRequest,
    ) -> dict[str, Any]:
        """Return the identity's active session, starting one only when it has none.

        Single-tracked: a live (idle or busy) session is reused, never doubled.
        A missing or archived one is replaced, guarded by ``expected_session_id``.
        """
        user_id = require_user(request, auth_provider)
        _refuse_agent_identity_write(request)
        project = await _owned_project(project_id, user_id)
        if identity_id == ORCHESTRATOR_IDENTITY_ID:
            raise OmnigentError(
                "Open the Orchestrator from the project header", code=ErrorCode.INVALID_INPUT
            )
        identity = await asyncio.to_thread(
            project_store.get_identity, project.id, _identity_id(identity_id), user_id=user_id
        )
        if identity is None:
            raise OmnigentError("Identity not found", code=ErrorCode.NOT_FOUND)
        state = await identity_session_state(
            project, identity.active_session_id, conversation_store, permission_store
        )
        if state in ("idle", "busy"):
            return {"session_id": identity.active_session_id, "created": False}
        if state == "unavailable":
            raise OmnigentError("The identity's session is unavailable", code=ErrorCode.CONFLICT)
        expected = expected_main_id(body.expected_session_id)
        if identity.active_session_id != expected:
            raise OmnigentError(
                "The identity's session changed; reload the project", code=ErrorCode.CONFLICT
            )
        if create_session is None:
            raise OmnigentError("Sessions are unavailable", code=ErrorCode.RUNNER_UNAVAILABLE)
        payload = await project_session_payload(
            request,
            project,
            {
                "project_id": project.id,
                "title": identity.name,
                "agent_id": identity.agent_id or project.config.get("agent_id"),
                "model_override": identity.model,
                "reasoning_effort": identity.effort,
                "labels": {PROJECT_IDENTITY_LABEL_KEY: identity.id},
            },
        )
        if not payload.get("agent_id"):
            payload["agent_id"] = await default_project_agent_id(
                request, getattr(request.app.state, "agent_store", None), payload.get("host_id")
            )
        created = await create_session(request, user_id, payload, None)
        session_id = getattr(created, "id", None) or getattr(created, "session_id", None)
        if not isinstance(session_id, str):
            raise OmnigentError("Session creation failed", code=ErrorCode.INTERNAL_ERROR)
        await asyncio.to_thread(
            project_store.set_identity_session,
            project.id,
            identity.id,
            session_id,
            user_id=user_id,
            expected_session_id=identity.active_session_id,
        )
        announce_projects_changed(user_id)
        return {"session_id": session_id, "created": True}

    @router.delete("/projects/{project_id}")
    async def delete_project(request: Request, project_id: str) -> dict[str, Any]:
        """Delete one of the caller's projects.

        Member sessions are not deleted; they are left for the caller to
        unfile (clearing their ``project_id``).

        :param request: The incoming request, used to identify the user.
        :param project_id: The project to delete.
        :returns: ``{"id": ..., "object": "project.deleted", "deleted": True}``.
        :raises OmnigentError: 401 if unauthenticated, 404 if not found / not
            owned by the caller.
        """
        user_id = require_user(request, auth_provider)
        await _require_private_source(request, user_id)
        deleted = await asyncio.to_thread(project_store.delete, project_id, user_id=user_id)
        if not deleted:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        # Drop the folder from the owner's other connected clients live.
        announce_projects_changed(user_id)
        return {"id": project_id, "object": "project.deleted", "deleted": True}

    return router
