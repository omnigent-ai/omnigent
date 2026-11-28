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
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Query, Request
from sqlalchemy.exc import IntegrityError

from omnigent.entities import Project
from omnigent.entities.mandate_sections import proposal_is_stale, proposal_target
from omnigent.entities.project import (
    PROJECT_IDENTITY_LABEL_KEY,
    MandateProposal,
    ProjectIdentity,
    ProjectShare,
    normalize_project_description,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import (
    ORCHESTRATION_SOURCE_HEADER,
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
from omnigent.server import session_live_state
from omnigent.server.auth import RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC, AuthProvider
from omnigent.server.board_readers import validate_board_readers
from omnigent.server.persona_mentions import Mention, PersonaMentionTargets
from omnigent.server.product_approval import OWNER_ONLY_CONFIG_KEYS
from omnigent.server.project_agents import create_role_agent
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
from omnigent.server.project_router import (
    LEAD_IDENTITY_ID,
    LEAD_NAME,
    choose_router,
    lead_present,
)
from omnigent.server.role_side_chat import refuse_role_side_chat
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._session_create_validation import agent_owner_id
from omnigent.server.routes.sessions import announce_projects_changed
from omnigent.server.schemas import (
    CreateProjectRequest,
    ProjectIdentityRequest,
    ProjectIdentitySessionRequest,
    ProjectOrderRequest,
    ProjectOrderResponse,
    ProjectRoutingRequest,
    ProposeMandateRequest,
    RetireIdentityRequest,
    ShareProjectRequest,
    UpdateProjectRequest,
    UpdateProjectShareRequest,
)
from omnigent.server.session_orchestration import require_named_source_private
from omnigent.server.stale_runner_refresh import TextChange, recycle_stale_sessions_soon
from omnigent.server.usage_events import record_usage_event
from omnigent.stores.agent_store import AgentStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.inbox_item_store import InboxItemStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.project_store import ProjectOrderPreference, ProjectStore, apply_project_order
from omnigent.util.uuid import InvalidUuidError, normalize_uuid, uuid_to_bytes

logger = logging.getLogger(__name__)

_DEFAULT_PROJECT_PAGE = 50
# Seconds before retrying a failed restore of an identity's session (e.g. no host online).
_HEAL_RETRY_SECONDS = 60.0
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
        "mandate_proposals": [
            _proposal_response(entry, project.mandate) for entry in project.mandate_proposals
        ],
        "description": project.description,
        "main_session_id": project.main_session_id,
        "owner_user_id": project.user_id,
        # Deprecated response alias; remove in 0.25.0.
        "orchestrator_session_id": project.main_session_id,
        "router_identity_id": project.router_identity_id,
        "lead_removed": project.lead_removed,
    }


def _proposal_response(proposal: MandateProposal, mandate: str | None) -> dict[str, Any]:
    """Serialize a pending proposal as a ``MandateProposalObject`` against the current mandate."""
    return {
        "id": proposal.id,
        "text": proposal.text,
        "created_at": proposal.created_at,
        "session_id": proposal.session_id,
        "mode": proposal.mode,
        "section": proposal.section,
        "current_text": proposal_target(mandate, proposal),
        "stale": proposal_is_stale(mandate, proposal),
    }


def _share_response(share: ProjectShare) -> dict[str, Any]:
    """Serialize a share as a ``ProjectShareObject``."""
    return {
        "object": "project.share",
        "user_id": share.user_id,
        "created_at": share.created_at,
        "created_by": share.created_by,
        "role": share.role,
    }


def _refuse_agent_share_write(request: Request) -> None:
    """Sharing is a person's decision: agent tools may never add, change or revoke a share.

    An agent's runner carries a source or a runner token header; either one refuses.
    """
    if (
        ORCHESTRATION_SOURCE_HEADER in request.headers
        or RUNNER_TUNNEL_TOKEN_HEADER in request.headers
    ):
        raise OmnigentError(
            "Agents can't change who has access to a project", code=ErrorCode.FORBIDDEN
        )


def _is_throwaway_project_name(name: str) -> bool:
    """Whether ``name`` marks a disposable verification project."""
    return name.startswith("zz-") or "throwaway" in name.lower()


def _refuse_owner_config_write(
    request: Request,
    requested: dict[str, Any] | None,
    current: dict[str, Any] | None,
    *,
    is_owner: bool,
) -> None:
    """The landing queue, product approvers and board readers are the owner's own settings.

    Agents and other members of a shared project are refused when they change them.
    """
    if requested is None:
        return
    changed = [
        key for key in OWNER_ONLY_CONFIG_KEYS if requested.get(key) != (current or {}).get(key)
    ]
    if changed and (is_agent_call(request) or not is_owner):
        raise OmnigentError(
            "Only the project owner can change " + ", ".join(f"`{key}`" for key in changed),
            code=ErrorCode.FORBIDDEN,
        )


# Session settings that decide what runs in the owner's own Lead and persona sessions.
OWNER_SESSION_CONFIG_KEYS = ("agent_id", "harness", "permission_mode", "terminal_launch_args")


def _refuse_member_session_config_write(
    requested: dict[str, Any] | None, current: dict[str, Any] | None, *, is_owner: bool
) -> None:
    """Only the owner picks the agent and launch permissions of sessions that run as them.

    The Lead and personas run as the owner (the Lead with full access), so a member
    choosing their agent would choose what runs unprompted as someone else. Members
    still edit model and effort, and may resend these keys unchanged.
    """
    if requested is None or is_owner:
        return
    changed = [
        key for key in OWNER_SESSION_CONFIG_KEYS if requested.get(key) != (current or {}).get(key)
    ]
    if changed:
        raise OmnigentError(
            "Only the project owner can change " + ", ".join(f"`{key}`" for key in changed),
            code=ErrorCode.FORBIDDEN,
        )


def _refuse_member_agent_choice(project: Project, user_id: str | None, changed: bool) -> None:
    """A member can't choose the agent of the Lead or a persona, which run as the owner."""
    if changed and project.user_id != user_id:
        raise OmnigentError(
            "Only the project owner can choose the agent of the Lead or a persona",
            code=ErrorCode.FORBIDDEN,
        )


def is_agent_call(request: Request) -> bool:
    """Whether an agent's runner made the call: it carries a source or a runner token header."""
    return (
        ORCHESTRATION_SOURCE_HEADER in request.headers
        or RUNNER_TUNNEL_TOKEN_HEADER in request.headers
    )


def _refuse_agent_mandate_write(request: Request, mandate: str | None) -> None:
    """Keep the mandate person-written: agents it bounds may only propose changes.

    :param request: The incoming request; agent tools mark theirs with
        :data:`ORCHESTRATION_SOURCE_HEADER`.
    :param mandate: The requested mandate, or ``None`` when unchanged.
    :raises OmnigentError: 403 for an agent-originated mandate write.
    """
    if mandate is not None and is_agent_call(request):
        raise OmnigentError(
            "Agents can't change a project's mandate; propose an addition "
            "with sys_project_mandate_propose instead",
            code=ErrorCode.FORBIDDEN,
        )


def _bound_lead_session(
    request: Request, project: Project, conversation_store: ConversationStore | None
) -> bool:
    """Whether the calling agent is proven to run in the project's current Lead session.

    The source header only names a session. The proof is the runner tunnel token,
    a per-runner secret the server issued: it must derive the runner bound to the
    named session, and that session (or the root it runs under) must be the
    project's current Lead, as with an agent rolling itself over.
    """
    source_id = normalize_uuid(request.headers.get(ORCHESTRATION_SOURCE_HEADER))
    token = (request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER) or "").strip()
    if not source_id or not token or conversation_store is None or not project.main_session_id:
        return False
    session = conversation_store.get_conversation(source_id)
    if session is None or session.archived:
        return False
    root = session
    if session.root_conversation_id and session.root_conversation_id != session.id:
        root = conversation_store.get_conversation(session.root_conversation_id)
    if root is None or root.id != project.main_session_id or root.archived:
        return False
    runner_id = token_bound_runner_id(token)
    return runner_id in {session.runner_id, root.runner_id}


def refuse_unless_owner_or_lead(
    request: Request,
    project: Project | None,
    refused: OmnigentError,
    *,
    user_id: str | None,
    conversation_store: ConversationStore | None,
) -> str | None:
    """Allow a write only from the project owner or the project's current Lead session.

    A call made by an agent's runner (it carries a source or runner token header)
    passes only when the server can bind it to the current Lead session; an
    agent that omits its source header is refused rather than treated as the owner.

    :param project: The stored project, or ``None`` on create (agents are refused then).
    :param refused: The error to raise when the caller may not write.
    :returns: The Lead session's ID for an agent call, else ``None``.
    :raises OmnigentError: *refused* for a shared member or an agent outside the Lead's session.
    """
    agent_call = (
        ORCHESTRATION_SOURCE_HEADER in request.headers
        or RUNNER_TUNNEL_TOKEN_HEADER in request.headers
    )
    if agent_call and (
        project is None or not _bound_lead_session(request, project, conversation_store)
    ):
        raise refused
    if project is not None and project.user_id != user_id:
        raise refused
    return normalize_uuid(request.headers.get(ORCHESTRATION_SOURCE_HEADER)) if agent_call else None


def _refuse_description_write(
    request: Request,
    project: Project | None,
    description: str | None,
    *,
    user_id: str | None,
    conversation_store: ConversationStore | None,
) -> None:
    """Keep the description the owner's or the Lead's: members and other agents only read it.

    :param project: The stored project, or ``None`` on create (agents may not set it then).
    :param description: The requested description, or ``None`` when unchanged.
    :raises OmnigentError: 403 for a shared member or an agent outside the Lead's session,
        400 for an oversized description.
    """
    if description is None:
        return
    normalize_project_description(description)
    refuse_unless_owner_or_lead(
        request,
        project,
        OmnigentError(
            "Only the project owner or its Lead can change the project description",
            code=ErrorCode.FORBIDDEN,
        ),
        user_id=user_id,
        conversation_store=conversation_store,
    )


async def _record_role_event(
    request: Request,
    project: Project,
    user_id: str | None,
    name: str,
    identity_id: str,
    body: Any,
) -> None:
    """Record a role edit or removal: who made it and which fields changed, never their values."""
    source = normalize_uuid(request.headers.get(ORCHESTRATION_SOURCE_HEADER))
    fields = sorted(body.model_fields_set) if body is not None else []
    await record_usage_event(
        request.app,
        name,
        project_id=project.id,
        user_id=None if source else (user_id or RESERVED_USER_LOCAL),
        session_id=source,
        subject_id=identity_id,
        props={"fields": fields} if fields else None,
    )


RETIRED_ROLES_CONFIG_KEY = "retired_roles"
# Roles the mandate requires; only the owner may retire them.
_OWNER_RETIRED_ROLE_NAMES = frozenset({"reviewer", "merger"})


def _retired_summary(retired: dict[str, Any]) -> dict[str, Any]:
    """What a listing shows of a retired role."""
    return {key: retired.get(key) for key in ("id", "name", "retired_at", "retired_by", "reason")}


def _retired_roles(project: Project) -> list[dict[str, Any]]:
    """The project's retired roles, most recently retired first."""
    stored = (project.config or {}).get(RETIRED_ROLES_CONFIG_KEY) or {}
    rows = [_retired_summary(row) for row in stored.values() if isinstance(row, dict)]
    return sorted(rows, key=lambda row: row.get("retired_at") or 0, reverse=True)


def _open_assigned_titles(tickets: Any, project: Project, name: str) -> list[str]:
    """Titles of tickets assigned to *name* that aren't in a completed stage."""
    stages = tickets.stages(project.id, user_id=project.user_id)["stages"]
    done = {stage["id"] for stage in stages if stage.get("is_complete")}
    wanted = name.strip().casefold()
    titles: list[str] = []
    after: str | None = None
    while True:
        page = tickets.list(project.id, user_id=project.user_id, after=after, limit=100)
        for ticket in page.get("data", []):
            assignee = next(
                (
                    label.get("value")
                    for label in ticket.get("labels") or []
                    if label.get("key") == "assignee"
                ),
                None,
            )
            if (
                isinstance(assignee, str)
                and assignee.strip().casefold() == wanted
                and ticket.get("stage_id") not in done
            ):
                titles.append(ticket.get("title") or ticket["id"])
        if not page.get("has_more"):
            return titles
        after = page.get("last_id")


def create_projects_router(
    project_store: ProjectStore,
    auth_provider: AuthProvider | None = None,
    conversation_store: ConversationStore | None = None,
    permission_store: PermissionStore | None = None,
    create_session: ProjectSessionCreator | None = None,
    register_mention_targets: Callable[[PersonaMentionTargets], None] | None = None,
    agent_store: AgentStore | None = None,
    artifact_store: ArtifactStore | None = None,
    inbox_item_store: InboxItemStore | None = None,
) -> APIRouter:
    """Build the projects router (``/v1/projects``).

    :param project_store: The store backing project persistence.
    :param auth_provider: Auth provider used to identify the requesting user.
        ``None`` in single-user mode (owner scope is ``None``).
    :param conversation_store: Resolves the calling session an agent tool names.
    :param permission_store: Checks whether that session is shared with edit access.
    :param agent_store: Stores each role's project agent; ``None`` keeps roles on their base.
    :param artifact_store: Holds the project agents' bundles.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    async def response(project: Project, viewer: str | None) -> dict[str, Any]:
        identities = await asyncio.to_thread(
            project_store.list_identities, project.id, user_id=project.user_id
        )
        router_choice = choose_router(project, identities)
        return {
            **_to_response(project),
            "router": {"identity_id": router_choice.identity_id, "name": router_choice.name},
            "shared": project.user_id != viewer,
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

    async def _refuse_role_side_chat(request: Request, user_id: str | None, action: str) -> None:
        """Refuse a coordination write from a side chat of the Lead or an identity."""
        if conversation_store is not None:
            await asyncio.to_thread(
                refuse_role_side_chat,
                request,
                action=action,
                user_id=user_id,
                conversations=conversation_store,
                projects=project_store,
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
        _refuse_description_write(
            request, None, body.description, user_id=user_id, conversation_store=None
        )
        _refuse_owner_config_write(request, body.config, None, is_owner=True)
        validate_board_readers(body.config, None)
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
                    description=body.description,
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
        return await response(project, user_id)

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
                "data": [await response(p, user_id) for p in page],
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
        return {"object": "list", "data": [await response(p, user_id) for p in projects]}

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
        return await response(project, user_id)

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
        await _refuse_role_side_chat(request, user_id, "Editing the project")
        _refuse_agent_mandate_write(request, body.mandate)
        if body.description is not None:
            current = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
            if current is None:
                raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
            await asyncio.to_thread(
                _refuse_description_write,
                request,
                current,
                body.description,
                user_id=user_id,
                conversation_store=conversation_store,
            )
        if body.config is not None:
            stored = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
            is_owner = stored is None or stored.user_id == user_id
            # Retired roles are written only by the retire and restore routes.
            body.config.pop(RETIRED_ROLES_CONFIG_KEY, None)
            if stored is not None and stored.config.get(RETIRED_ROLES_CONFIG_KEY):
                body.config[RETIRED_ROLES_CONFIG_KEY] = stored.config[RETIRED_ROLES_CONFIG_KEY]
            _refuse_owner_config_write(
                request, body.config, stored.config if stored else None, is_owner=is_owner
            )
            _refuse_member_session_config_write(
                body.config, stored.config if stored else None, is_owner=is_owner
            )
            validate_board_readers(body.config, project_id)
        session_id = None
        if body.main_session_id is not None:
            current = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
            if current is None or conversation_store is None:
                raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
            session_id = await validate_main_assignment(
                current, body.main_session_id, conversation_store, permission_store
            )
        # A Mandate edit reaches sessions on older runners only by relaunching them.
        mandate_before = (
            await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
            if body.mandate is not None
            else None
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
        if body.description is not None and body.description.strip() != (
            project.description or ""
        ):
            project = await asyncio.to_thread(
                project_store.set_description,
                project_id,
                user_id=user_id,
                description=body.description,
            )
        if mandate_before is not None and (mandate_before.mandate or "") != (
            project.mandate or ""
        ):
            refresh_stale_sessions(request, project, TextChange())
        # A rename/config change is only ever seen by other connected clients
        # (another tab, the mobile app) if it is pushed; nothing else refreshes
        # their projects cache until a full reload.
        announce_projects_changed(user_id)
        return await response(project, user_id)

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
        await _refuse_role_side_chat(request, user_id, "Proposing mandate changes")
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
            mode=body.mode,
            section=body.section,
        )
        if result is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        project, proposal = result
        if source_session is not None:
            # The owner must accept or dismiss it, so the proposing session needs their eyes.
            session_live_state.raise_attention(source_session)
        announce_projects_changed(user_id)
        return {
            "proposal": _proposal_response(proposal, project.mandate),
            "project": await response(project, user_id),
        }

    def refresh_stale_sessions(request: Request, project: Project, change: TextChange) -> None:
        """Relaunch, once idle, sessions whose runner can't bring them this edit."""
        if conversation_store is not None:
            recycle_stale_sessions_soon(
                request.app, project, change, conversations=conversation_store, template=request
            )

    async def resolve_proposal(
        request: Request, project_id: str, proposal_id: str, *, accept: bool
    ) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        if is_agent_call(request):
            raise OmnigentError(
                "Agents can't accept or dismiss a mandate proposal; a person must",
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
        if accept:
            refresh_stale_sessions(request, project, TextChange())
        announce_projects_changed(user_id)
        return await response(project, user_id)

    @router.post("/projects/{project_id}/mandate/proposals/{proposal_id}/accept")
    async def accept_mandate_proposal(
        request: Request, project_id: str, proposal_id: str
    ) -> dict[str, Any]:
        """Apply a pending proposal to the mandate. Owner only, never an agent tool.

        :returns: The updated project.
        :raises OmnigentError: 403 for an agent request, 404 for an unknown
            project or proposal, 400 when the mandate would exceed its limit,
            409 when a replacement is stale.
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

    # -- Identities: the built-in Lead plus owner-defined roles --

    def _refuse_agent_identity_delete(request: Request) -> None:
        """Agents may create and edit identities, but only the project owner removes them."""
        if ORCHESTRATION_SOURCE_HEADER in request.headers:
            raise OmnigentError(
                "Only the project owner can remove a project's identities",
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
        """List the project's identities, the built-in Lead first unless it was removed.

        Each entry's ``router`` marks the one that receives routed events.
        """
        user_id = require_user(request, auth_provider)
        project = await _owned_project(project_id, user_id)
        identities = await asyncio.to_thread(
            project_store.list_identities, project.id, user_id=user_id
        )
        # Every identity keeps one live session; this listing is what the board polls.
        project = await heal_main_session(request, project)
        identities = await heal_identity_sessions(request, project, identities)
        data = [await _identity_payload(project, identity) for identity in identities]
        if lead_present(project, identities):
            lead = orchestrator_identity_response(
                project, await main_session_state(project, conversation_store, permission_store)
            )
            data.insert(0, lead)
        router_id = choose_router(project, identities).identity_id
        for entry in data:
            entry["router"] = entry["id"] == router_id
        return {"object": "list", "data": data, "retired": _retired_roles(project)}

    @router.put("/projects/{project_id}/routing")
    async def set_project_routing(
        request: Request, project_id: str, body: ProjectRoutingRequest
    ) -> dict[str, Any]:
        """Choose the router persona and remove or restore the Lead. Owner-only, never agents.

        The router receives ticket events, digests and escalations. Without a
        choice it is the Lead, or the first persona once the Lead is removed.
        """
        user_id = require_user(request, auth_provider)
        if ORCHESTRATION_SOURCE_HEADER in request.headers:
            raise OmnigentError(
                "Only the project owner can change who receives its routed events",
                code=ErrorCode.FORBIDDEN,
            )
        router_id = body.router_identity_id
        if router_id is not None and router_id != LEAD_IDENTITY_ID:
            router_id = _identity_id(router_id)
        if router_id == LEAD_IDENTITY_ID and body.lead_removed:
            raise OmnigentError(
                "Choose another router before removing the Lead", code=ErrorCode.INVALID_INPUT
            )
        project = await asyncio.to_thread(
            project_store.set_routing,
            project_id,
            user_id=user_id,
            router_identity_id=router_id,
            lead_removed=body.lead_removed,
        )
        announce_projects_changed(user_id)
        return await response(project, user_id)

    def _source_session(request: Request) -> str | None:
        source = request.headers.get(ORCHESTRATION_SOURCE_HEADER)
        try:
            return uuid_to_bytes(source).hex() if source else None
        except (InvalidUuidError, ValueError):
            return None

    @router.post("/projects/{project_id}/identities")
    async def create_identity(
        request: Request, project_id: str, body: ProjectIdentityRequest
    ) -> dict[str, Any]:
        """Add a named role to the project, e.g. a PM with its own persona.

        Agents may create one directly (``sys_project_identity_create``); it is
        recorded as agent-created, so agents may later edit it too.
        """
        user_id = require_user(request, auth_provider)
        await _require_private_source(request, user_id)
        await _refuse_role_side_chat(request, user_id, "Creating project identities")
        source_session = _source_session(request)
        if ORCHESTRATION_SOURCE_HEADER in request.headers and source_session is None:
            raise OmnigentError("Invalid orchestration source", code=ErrorCode.INVALID_INPUT)
        if body.name is None:
            raise OmnigentError("name is required", code=ErrorCode.INVALID_INPUT)
        project = await _owned_project(project_id, user_id)
        _refuse_member_agent_choice(project, user_id, bool(body.agent_id))
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
            created_by_session_id=source_session,
            scope=body.scope.model_dump() if body.scope is not None else None,
        )
        await record_usage_event(
            request.app,
            "role.created",
            project_id=project.id,
            user_id=None if source_session else (user_id or RESERVED_USER_LOCAL),
            session_id=source_session,
            subject_id=identity.id,
        )
        announce_projects_changed(user_id)
        # A new identity starts its one session right away; a failure retries on the next list.
        [identity] = await heal_identity_sessions(request, project, [identity])
        return await _identity_payload(project, identity)

    async def _update_lead(
        request: Request, project: Project, user_id: str | None, body: ProjectIdentityRequest
    ) -> dict[str, Any]:
        """Edit the built-in Lead: its charter, agent, model and effort, never its name.

        The Lead is owner-made, so as with other owner-made identities agents can't
        edit it. People with access edit its agent, model and effort (the project's
        session defaults); only the owner edits its charter, like the mandate.
        """
        if ORCHESTRATION_SOURCE_HEADER in request.headers:
            raise OmnigentError(
                "Only the project's people can edit the Lead; agents can't",
                code=ErrorCode.FORBIDDEN,
            )
        if body.name is not None and body.name.strip() != LEAD_NAME:
            raise OmnigentError("The Lead can't be renamed", code=ErrorCode.INVALID_INPUT)
        defaults = {
            field: getattr(body, field) or None
            for field in ("agent_id", "model", "effort")
            if field in body.model_fields_set
        }
        _refuse_member_agent_choice(
            project,
            user_id,
            "agent_id" in defaults and defaults["agent_id"] != project.config.get("agent_id"),
        )
        # A member's save resends the charter unchanged; only a real change is owner-only.
        charter_changed = body.instructions is not None and body.instructions.strip() != (
            project.lead_charter or ""
        )
        if charter_changed:
            project = await asyncio.to_thread(
                project_store.set_lead_charter,
                project.id,
                user_id=user_id,
                charter=body.instructions or "",
            )
            refresh_stale_sessions(request, project, TextChange(lead=True))
        if defaults:
            updated = await asyncio.to_thread(
                project_store.set_lead_session_defaults,
                project.id,
                user_id=user_id,
                values=defaults,
            )
            if updated is None:
                raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
            project = updated
        if charter_changed or defaults:
            announce_projects_changed(user_id)
        return orchestrator_identity_response(
            project, await main_session_state(project, conversation_store, permission_store)
        )

    @router.patch("/projects/{project_id}/identities/{identity_id}")
    async def update_identity(
        request: Request, project_id: str, identity_id: str, body: ProjectIdentityRequest
    ) -> dict[str, Any]:
        """Edit an identity's name, persona or session defaults; empty strings clear.

        Agents may edit only identities an agent created; owner-made ones stay owner-only.
        """
        user_id = require_user(request, auth_provider)
        await _require_private_source(request, user_id)
        await _refuse_role_side_chat(request, user_id, "Editing project identities")
        project = await _owned_project(project_id, user_id)
        if identity_id == LEAD_IDENTITY_ID:
            return await _update_lead(request, project, user_id, body)
        clear = frozenset(
            field
            for field in ("agent_id", "model", "effort")
            if getattr(body, field) == "" and field in body.model_fields_set
        )
        current = await asyncio.to_thread(
            project_store.get_identity, project.id, _identity_id(identity_id), user_id=user_id
        )
        base_before = current.agent_id if current is not None else None
        # Renaming would slip the Reviewer or Merger past the owner-only retire guard.
        if (
            is_agent_call(request)
            and current is not None
            and current.name.casefold() in _OWNER_RETIRED_ROLE_NAMES
            and body.name is not None
            and body.name.strip().casefold() != current.name.casefold()
        ):
            raise OmnigentError(
                f"Only the project owner can rename the {current.name}",
                code=ErrorCode.FORBIDDEN,
            )
        if "agent_id" in body.model_fields_set and project.user_id != user_id:
            _refuse_member_agent_choice(
                project,
                user_id,
                current is not None and (body.agent_id or None) != current.agent_id,
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
            agent_editor=ORCHESTRATION_SOURCE_HEADER in request.headers,
            scope=body.scope.model_dump() if body.scope is not None else None,
        )
        if identity is None:
            raise OmnigentError("Identity not found", code=ErrorCode.NOT_FOUND)
        if body.instructions is not None and (
            current is None or (current.instructions or "") != (identity.instructions or "")
        ):
            refresh_stale_sessions(request, project, TextChange(identity_id=identity.id))
        if identity.project_agent_id and identity.agent_id != base_before:
            # A new base means a new copy, which the role's next session makes.
            stale = identity.project_agent_id
            identity = await asyncio.to_thread(
                project_store.set_identity_project_agent,
                project.id,
                identity.id,
                None,
                user_id=project.user_id,
                expected=stale,
            )
            await asyncio.to_thread(_discard_role_agent, stale)
        await _record_role_event(request, project, user_id, "role.updated", identity.id, body)
        announce_projects_changed(user_id)
        return await _identity_payload(project, identity)

    @router.delete("/projects/{project_id}/identities/{identity_id}")
    async def delete_identity(
        request: Request, project_id: str, identity_id: str
    ) -> dict[str, Any]:
        """Remove an identity; its past sessions stay in the project. Owner only."""
        user_id = require_user(request, auth_provider)
        _refuse_agent_identity_delete(request)
        project = await _owned_project(project_id, user_id)
        if identity_id == LEAD_IDENTITY_ID:
            raise OmnigentError(
                "The Lead can't be deleted; choose another router to remove it",
                code=ErrorCode.INVALID_INPUT,
            )
        if project.user_id != user_id:
            raise OmnigentError(
                "Only the project owner can remove a project's identities",
                code=ErrorCode.FORBIDDEN,
            )
        normalized = _identity_id(identity_id)
        identity = await asyncio.to_thread(
            project_store.get_identity, project.id, normalized, user_id=user_id
        )
        deleted = await asyncio.to_thread(
            project_store.delete_identity, project.id, normalized, user_id=user_id
        )
        if not deleted:
            raise OmnigentError("Identity not found", code=ErrorCode.NOT_FOUND)
        if identity is not None and identity.project_agent_id:
            await asyncio.to_thread(_discard_role_agent, identity.project_agent_id)
        await _record_role_event(request, project, user_id, "role.deleted", normalized, None)
        announce_projects_changed(user_id)
        return {"id": normalized, "object": "project.identity.deleted", "deleted": True}

    @router.post("/projects/{project_id}/identities/{identity_id}/retire")
    async def retire_identity(
        request: Request, project_id: str, identity_id: str, body: RetireIdentityRequest
    ) -> dict[str, Any]:
        """Park a role: archive it and close its session, keeping its history. Reversible.

        The owner, or the project's Lead session, may retire a role. The Lead can't retire
        itself, the router, the Reviewer or the Merger, and nobody can retire a role that
        still has open tickets or a running session.
        """
        user_id = require_user(request, auth_provider)
        await _require_private_source(request, user_id)
        await _refuse_role_side_chat(request, user_id, "Retiring project roles")
        project = await _owned_project(project_id, user_id)
        source = normalize_uuid(request.headers.get(ORCHESTRATION_SOURCE_HEADER))
        if source is not None and source != project.main_session_id:
            raise OmnigentError(
                "Only the project's Lead can retire a role", code=ErrorCode.FORBIDDEN
            )
        if source is None and (is_agent_call(request) or project.user_id != user_id):
            raise OmnigentError(
                "Only the project owner or its Lead can retire a role", code=ErrorCode.FORBIDDEN
            )
        if identity_id == LEAD_IDENTITY_ID:
            raise OmnigentError("The Lead can't be retired", code=ErrorCode.INVALID_INPUT)
        normalized = _identity_id(identity_id)
        identities = await asyncio.to_thread(
            project_store.list_identities, project.id, user_id=project.user_id
        )
        identity = next((item for item in identities if item.id == normalized), None)
        if identity is None:
            raise OmnigentError("Identity not found", code=ErrorCode.NOT_FOUND)
        if choose_router(project, identities).identity_id == identity.id:
            raise OmnigentError(
                "This role routes the project's events; choose another router first",
                code=ErrorCode.INVALID_INPUT,
            )
        if source is not None and identity.name.casefold() in _OWNER_RETIRED_ROLE_NAMES:
            raise OmnigentError(
                f"Only the project owner can retire the {identity.name}",
                code=ErrorCode.FORBIDDEN,
            )
        if source is not None and identity.active_session_id == source:
            raise OmnigentError("A role can't retire itself", code=ErrorCode.FORBIDDEN)
        blockers = await _retire_blockers(request, project, identity)
        if blockers:
            raise OmnigentError(
                f"{identity.name} can't be retired yet: " + "; ".join(blockers),
                code=ErrorCode.CONFLICT,
            )
        retired = {
            "id": identity.id,
            "name": identity.name,
            "instructions": identity.instructions,
            "agent_id": identity.agent_id,
            "model": identity.model,
            "effort": identity.effort,
            "scope": identity.scope,
            "project_agent_id": identity.project_agent_id,
            "created_by_session_id": identity.created_by_session_id,
            "created_at": identity.created_at,
            "last_session_id": identity.active_session_id,
            "retired_at": int(time.time()),
            "retired_by": "lead" if source is not None else "owner",
            "reason": body.reason.strip(),
        }
        config = dict(project.config or {})
        config[RETIRED_ROLES_CONFIG_KEY] = {
            **(config.get(RETIRED_ROLES_CONFIG_KEY) or {}),
            identity.id: retired,
        }
        await asyncio.to_thread(
            project_store.update, project.id, user_id=project.user_id, config=config
        )
        await asyncio.to_thread(
            project_store.delete_identity, project.id, identity.id, user_id=project.user_id
        )
        if identity.active_session_id and conversation_store is not None:
            await asyncio.to_thread(
                conversation_store.update_conversation, identity.active_session_id, archived=True
            )
        await _record_role_event(request, project, user_id, "role.retired", identity.id, None)
        await _tell_owner_retired(project, retired, source)
        announce_projects_changed(project.user_id)
        return {"object": "project.identity.retired", **_retired_summary(retired)}

    @router.post("/projects/{project_id}/identities/{identity_id}/restore")
    async def restore_identity(
        request: Request, project_id: str, identity_id: str
    ) -> dict[str, Any]:
        """Bring a retired role back with its persona, defaults and agent. Owner only."""
        user_id = require_user(request, auth_provider)
        if is_agent_call(request):
            raise OmnigentError(
                "Only the project owner can restore a role", code=ErrorCode.FORBIDDEN
            )
        project = await _owned_project(project_id, user_id)
        if project.user_id != user_id:
            raise OmnigentError(
                "Only the project owner can restore a role", code=ErrorCode.FORBIDDEN
            )
        normalized = _identity_id(identity_id)
        config = dict(project.config or {})
        retired_roles = dict(config.get(RETIRED_ROLES_CONFIG_KEY) or {})
        retired = retired_roles.get(normalized)
        if not isinstance(retired, dict):
            raise OmnigentError("No retired role with that ID", code=ErrorCode.NOT_FOUND)
        identity = await asyncio.to_thread(
            project_store.create_identity,
            project.id,
            normalized,
            user_id=user_id,
            name=retired["name"],
            instructions=retired.get("instructions") or "",
            agent_id=retired.get("agent_id"),
            model=retired.get("model"),
            effort=retired.get("effort"),
            created_by_session_id=retired.get("created_by_session_id"),
            scope=retired.get("scope"),
        )
        if retired.get("project_agent_id"):
            identity = await asyncio.to_thread(
                project_store.set_identity_project_agent,
                project.id,
                identity.id,
                retired["project_agent_id"],
                user_id=user_id,
                expected=None,
            )
        retired_roles.pop(normalized, None)
        config[RETIRED_ROLES_CONFIG_KEY] = retired_roles
        await asyncio.to_thread(project_store.update, project.id, user_id=user_id, config=config)
        await _record_role_event(request, project, user_id, "role.restored", identity.id, None)
        announce_projects_changed(user_id)
        return await _identity_payload(project, identity)

    async def _retire_blockers(
        request: Request, project: Project, identity: ProjectIdentity
    ) -> list[str]:
        """What keeps a role from retiring: open tickets assigned to it, or a running session."""
        blockers: list[str] = []
        notifications = getattr(request.app.state, "project_ticket_notifications", None)
        tickets = getattr(notifications, "tickets", None)
        if tickets is not None:
            open_titles = await asyncio.to_thread(
                _open_assigned_titles, tickets, project, identity.name
            )
            if open_titles:
                shown = ", ".join(f'"{title}"' for title in open_titles[:3])
                more = f" and {len(open_titles) - 3} more" if len(open_titles) > 3 else ""
                blockers.append(
                    f"{len(open_titles)} open ticket(s) are assigned to it ({shown}{more}); "
                    "reassign them first"
                )
        if identity.active_session_id and conversation_store is not None:
            session = await asyncio.to_thread(
                conversation_store.get_conversation, identity.active_session_id
            )
            if (
                session is not None
                and not session.archived
                and session.live_status not in (None, "idle", "failed")
            ):
                blockers.append("its session is still working; wait for it to finish")
        return blockers

    async def _tell_owner_retired(
        project: Project, retired: dict[str, Any], source: str | None
    ) -> None:
        """Put a notice of a Lead's retirement in the owner's Inbox; best effort."""
        if source is None or inbox_item_store is None or agent_store is None:
            return
        session = (
            await asyncio.to_thread(conversation_store.get_conversation, source)
            if conversation_store is not None
            else None
        )
        if session is None or session.agent_id is None:
            return
        from omnigent.server.inbox_dedupe import DedupeInputs, dedupe_key, normalize_topic
        from omnigent.stores.inbox_item_store import DuplicateInboxItemError, NewInboxItem

        inputs = DedupeInputs(
            kind="info",
            primary_link=None,
            topic=normalize_topic(f"retired role {retired['name']} {retired['retired_at']}"),
        )
        item = NewInboxItem(
            user_id=project.user_id or RESERVED_USER_LOCAL,
            source_session_id=source,
            agent_id=session.agent_id,
            kind="info",
            title=f"The Lead retired {retired['name']} in {project.name}",
            body_md=(
                f"**Reason:** {retired['reason']}\n\n"
                "Its history, tickets and spend stay in the project. You can restore it "
                "from the Team dialog's retired roles."
            ),
            links=[],
            actions=[],
            media=[],
            primary_link=None,
            topic=inputs.topic,
            dedupe_key=dedupe_key(inputs),
        )
        try:
            await asyncio.to_thread(inbox_item_store.create, item)
        except (DuplicateInboxItemError, OmnigentError):
            logger.info("Retired-role notice for %s not posted", retired["id"], exc_info=True)

    def _discard_role_agent(agent_id: str) -> None:
        """Delete a role's project agent once no session uses it; in-use ones stay hidden."""
        if agent_store is None or conversation_store is None:
            return
        if conversation_store.count_sessions_for_agent(agent_id, cap=1) == 0:
            agent_store.delete(agent_id)

    async def _role_agent_id(
        project: Project, identity: ProjectIdentity, base_agent_id: str
    ) -> tuple[ProjectIdentity, str | None]:
        """The identity's project agent, copied from *base_agent_id* on first use.

        ``None`` keeps the role on its base: no store, an ineligible base, or a
        failed copy, which retries on the role's next session.
        """
        if agent_store is None or artifact_store is None:
            return identity, None
        if identity.project_agent_id:
            current = await asyncio.to_thread(agent_store.get, identity.project_agent_id)
            if current is not None and current.project_id == project.id:
                return identity, current.id
        base = await asyncio.to_thread(agent_store.get, base_agent_id)
        if base is None:
            return identity, None
        try:
            role_agent = await asyncio.to_thread(
                create_role_agent,
                agent_store,
                artifact_store,
                owner=agent_owner_id(project.user_id),
                project_id=project.id,
                base=base,
                role_name=identity.name,
                instructions="",
            )
            identity = await asyncio.to_thread(
                project_store.set_identity_project_agent,
                project.id,
                identity.id,
                role_agent.id,
                user_id=project.user_id,
                expected=identity.project_agent_id,
            )
        except OmnigentError:
            logger.warning("Kept identity %s on its base agent", identity.id, exc_info=True)
            return identity, None
        return identity, identity.project_agent_id

    # Single-flight per identity, so concurrent opens and heals never start two sessions.
    identity_locks: dict[str, asyncio.Lock] = {}
    # Earliest monotonic time a failed background heal may retry, per identity.
    heal_not_before: dict[str, float] = {}

    async def ensure_identity_session(
        request: Request,
        project: Project,
        identity_id: str,
    ) -> tuple[ProjectIdentity, bool]:
        """Return the identity with a live session, starting one only when it has none.

        A missing (deleted) or archived session is replaced, keeping the persona.
        Replacements are single-flight per identity under a lock, so a stale caller
        view can never start a second session; it simply gets the live one.

        :returns: The (possibly updated) identity and whether a session was started.
        """
        user_id = project.user_id
        lock = identity_locks.setdefault(identity_id, asyncio.Lock())
        async with lock:
            identity = await asyncio.to_thread(
                project_store.get_identity, project.id, identity_id, user_id=user_id
            )
            if identity is None:
                raise OmnigentError("Identity not found", code=ErrorCode.NOT_FOUND)
            state = await identity_session_state(
                project, identity.active_session_id, conversation_store, permission_store
            )
            if state in ("idle", "busy"):
                return identity, False
            if state == "unavailable":
                raise OmnigentError(
                    "The identity's session is unavailable", code=ErrorCode.CONFLICT
                )
            if create_session is None or conversation_store is None:
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
                },
            )
            if not payload.get("agent_id"):
                payload["agent_id"] = await default_project_agent_id(
                    request,
                    getattr(request.app.state, "agent_store", None),
                    payload.get("host_id"),
                )
            # The role runs its own copy of the base, which carries the tools the owner set.
            identity, role_agent_id = await _role_agent_id(project, identity, payload["agent_id"])
            if role_agent_id:
                payload["agent_id"] = role_agent_id
            store = conversation_store

            async def label_identity_session(session_id: str) -> None:
                # Server-owned label, so it is written here rather than seeded by the
                # client-validated payload; the runner reads it at launch for the persona.
                await asyncio.to_thread(
                    store.set_labels, session_id, {PROJECT_IDENTITY_LABEL_KEY: identity.id}
                )

            created = await create_session(request, user_id, payload, label_identity_session)
            session_id = getattr(created, "id", None) or getattr(created, "session_id", None)
            if not isinstance(session_id, str):
                raise OmnigentError("Session creation failed", code=ErrorCode.INTERNAL_ERROR)
            updated = await asyncio.to_thread(
                project_store.set_identity_session,
                project.id,
                identity.id,
                session_id,
                user_id=user_id,
                expected_session_id=identity.active_session_id,
            )
            heal_not_before.pop(identity.id, None)
        announce_projects_changed(user_id)
        return updated, True

    async def heal_identity_sessions(
        request: Request, project: Project, identities: list[ProjectIdentity]
    ) -> list[ProjectIdentity]:
        """Give every identity without a live session a new one; failures retry later."""
        healed = []
        for identity in identities:
            state = await identity_session_state(
                project, identity.active_session_id, conversation_store, permission_store
            )
            # An in-flight heal for the same identity is awaited inside
            # ensure_identity_session, so this caller reports the new session too.
            if state not in ("absent", "missing", "archived") or time.monotonic() < (
                heal_not_before.get(identity.id, 0.0)
            ):
                healed.append(identity)
                continue
            try:
                identity, _ = await ensure_identity_session(request, project, identity.id)
            except Exception:
                # Never fail the listing (the roster polls it); retry after a pause.
                heal_not_before[identity.id] = time.monotonic() + _HEAL_RETRY_SECONDS
                logger.warning(
                    "Could not restore identity %s's session", identity.id, exc_info=True
                )
            healed.append(identity)
        return healed

    retitled_leads: set[str] = set()

    async def _retitle_legacy_lead(project: Project) -> None:
        """Give a Lead session still titled with the old default name the new one."""
        session_id = project.main_session_id
        if session_id is None or session_id in retitled_leads or conversation_store is None:
            return
        retitled_leads.add(session_id)
        renamed = await asyncio.to_thread(
            conversation_store.rename_conversation_if_title_matches,
            session_id,
            "Orchestrator",
            LEAD_NAME,
        )
        if renamed is not None:
            announce_projects_changed(project.user_id)

    main_heal_locks: dict[str, asyncio.Lock] = {}
    main_heal_not_before: dict[str, float] = {}

    async def heal_main_session(request: Request, project: Project) -> Project:
        """Start a fresh Lead when the project's one is deleted or was never made.

        An archived main is left alone (a rollover keeps the pointer on its archived
        predecessor until the switch), and so is any project mid-rollover.
        """
        from omnigent.server.project_orchestrator_rollover import (
            rollover_in_progress,
            start_fresh_main_session,
        )

        lock = main_heal_locks.setdefault(project.id, asyncio.Lock())
        async with lock:
            current = await asyncio.to_thread(
                project_store.get, project.id, user_id=project.user_id
            )
            if current is None:
                return project
            if rollover_in_progress(current.id) or time.monotonic() < (
                main_heal_not_before.get(current.id, 0.0)
            ):
                return current
            if current.lead_removed:
                identities = await asyncio.to_thread(
                    project_store.list_identities, current.id, user_id=current.user_id
                )
                # A removed Lead stays removed while another persona can route.
                if not lead_present(current, identities):
                    return current
            state = await main_session_state(current, conversation_store, permission_store)
            if state not in ("absent", "missing"):
                await _retitle_legacy_lead(current)
                return current
            try:
                await start_fresh_main_session(
                    request.app,
                    current,
                    user_id=current.user_id,
                    expected_main_session_id=current.main_session_id,
                    reason="heal",
                    template_request=request,
                )
            except Exception:
                # A CONFLICT means someone else replaced it; anything else retries later.
                main_heal_not_before[current.id] = time.monotonic() + _HEAL_RETRY_SECONDS
                logger.warning(
                    "Could not restore project %s's orchestrator", current.id, exc_info=True
                )
            else:
                main_heal_not_before.pop(current.id, None)
                announce_projects_changed(current.user_id)
            healed = await asyncio.to_thread(
                project_store.get, current.id, user_id=current.user_id
            )
            return healed or current

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
        Agents may call it (``sys_project_identity_session``) to delegate to an
        existing identity.
        """
        user_id = require_user(request, auth_provider)
        await _require_private_source(request, user_id)
        await _refuse_role_side_chat(request, user_id, "Opening identity sessions")
        project = await _owned_project(project_id, user_id)
        if identity_id == ORCHESTRATOR_IDENTITY_ID:
            raise OmnigentError(
                "Open the Lead from the project header", code=ErrorCode.INVALID_INPUT
            )
        # ``body.expected_session_id`` is accepted for older clients; the lock makes it moot.
        del body
        identity, created = await ensure_identity_session(
            request, project, _identity_id(identity_id)
        )
        return {"session_id": identity.active_session_id, "created": created}

    # -- Shares: the owner (or a Manager, for ordinary members) gives other users access --

    @router.get("/projects/{project_id}/shares")
    async def list_project_shares(request: Request, project_id: str) -> dict[str, Any]:
        """List the users a project is shared with; any member may read it.

        ``viewer_role`` is the caller's own role: ``owner``, ``manager`` or ``member``.
        """
        user_id = require_user(request, auth_provider)
        project = await _owned_project(project_id, user_id)
        shares = await asyncio.to_thread(project_store.list_shares, project.id, user_id=user_id)
        viewer_role = "owner" if project.user_id == user_id else None
        if viewer_role is None:
            viewer_role = next(
                (share.role for share in shares if share.user_id == user_id), "member"
            )
        return {
            "object": "list",
            "owner_user_id": project.user_id,
            "viewer_role": viewer_role,
            "data": [_share_response(share) for share in shares],
        }

    @router.post("/projects/{project_id}/shares")
    async def share_project(
        request: Request, project_id: str, body: ShareProjectRequest
    ) -> dict[str, Any]:
        """Share a project with another user. Never from an agent tool.

        The owner may add a member or a Manager (re-adding sets the role). A
        Manager may add ordinary members only. The member can then open the
        board, tickets, files and every session in the project. Their actions
        in owner-created sessions run on the owner's agents and hosts.
        """
        user_id = require_user(request, auth_provider)
        _refuse_agent_share_write(request)
        if user_id is None:
            raise OmnigentError(
                "Sharing needs a server with user sign-in", code=ErrorCode.INVALID_INPUT
            )
        member_id = body.user_id.strip()
        if not member_id or member_id in (RESERVED_USER_PUBLIC, RESERVED_USER_LOCAL):
            raise OmnigentError("Share with a signed-in user's ID", code=ErrorCode.INVALID_INPUT)
        share = await asyncio.to_thread(
            project_store.add_share, project_id, member_id, user_id=user_id, role=body.role
        )
        if permission_store is not None:
            permission_store.invalidate_access_cache()
        announce_projects_changed(user_id)
        announce_projects_changed(member_id)
        return _share_response(share)

    @router.patch("/projects/{project_id}/shares/{member_id}")
    async def update_project_share(
        request: Request, project_id: str, member_id: str, body: UpdateProjectShareRequest
    ) -> dict[str, Any]:
        """Make a member a Manager or back. Owner-only and never from an agent tool."""
        user_id = require_user(request, auth_provider)
        _refuse_agent_share_write(request)
        share = await asyncio.to_thread(
            project_store.set_share_role, project_id, member_id, body.role, user_id=user_id
        )
        announce_projects_changed(user_id)
        announce_projects_changed(member_id)
        return _share_response(share)

    @router.delete("/projects/{project_id}/shares/{member_id}")
    async def revoke_project_share(
        request: Request, project_id: str, member_id: str
    ) -> dict[str, Any]:
        """Revoke a user's share. Never from an agent tool.

        The owner may remove anyone; a Manager may remove ordinary members only.
        """
        user_id = require_user(request, auth_provider)
        _refuse_agent_share_write(request)
        removed = await asyncio.to_thread(
            project_store.remove_share, project_id, member_id, user_id=user_id
        )
        if not removed:
            raise OmnigentError("Share not found", code=ErrorCode.NOT_FOUND)
        if permission_store is not None:
            permission_store.invalidate_access_cache()
        announce_projects_changed(user_id)
        announce_projects_changed(member_id)
        return {"user_id": member_id, "object": "project.share.deleted", "deleted": True}

    @router.delete("/projects/{project_id}")
    async def delete_project(
        request: Request,
        project_id: str,
        delete_tickets: bool = Query(default=False),
    ) -> dict[str, Any]:
        """Delete one of the caller's projects.

        Member sessions are not deleted; they are left for the caller to
        unfile (clearing their ``project_id``). A project with tickets is
        refused unless ``delete_tickets`` is set, which removes its tickets and
        their history too. That is allowed only for a throwaway project (name
        starting with ``zz-`` or containing "throwaway") and only for the owner,
        never an agent tool.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project to delete.
        :param delete_tickets: Also delete the project's tickets.
        :returns: ``{"id": ..., "object": "project.deleted", "deleted": True}``.
        :raises OmnigentError: 401 if unauthenticated, 403 for an agent request
            with ``delete_tickets``, 404 if not found / not owned by the caller,
            409 if the project has tickets and ``delete_tickets`` is unset or
            the project is not a throwaway project.
        """
        user_id = require_user(request, auth_provider)
        await _require_private_source(request, user_id)
        existing = await _owned_project(project_id, user_id)
        if existing.user_id != user_id:
            raise OmnigentError(
                "Only the project owner can delete a project", code=ErrorCode.FORBIDDEN
            )
        if delete_tickets:
            if ORCHESTRATION_SOURCE_HEADER in request.headers:
                raise OmnigentError(
                    "Only the project owner can delete a project's tickets",
                    code=ErrorCode.FORBIDDEN,
                )
            project = await _owned_project(project_id, user_id)
            if not _is_throwaway_project_name(project.name):
                raise OmnigentError(
                    "Tickets can only be deleted with throwaway projects (a name starting "
                    "with 'zz-' or containing 'throwaway'); ticket history is retained.",
                    code=ErrorCode.CONFLICT,
                )
        deleted = await asyncio.to_thread(
            project_store.delete, project_id, user_id=user_id, delete_tickets=delete_tickets
        )
        if not deleted:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        # Drop the folder from the owner's other connected clients live.
        announce_projects_changed(user_id)
        return {"id": project_id, "object": "project.deleted", "deleted": True}

    async def mention_personas(project: Project) -> list[Mention]:
        identities = await asyncio.to_thread(
            project_store.list_identities, project.id, user_id=project.user_id
        )
        personas = [Mention(identity.id, identity.name) for identity in identities]
        if lead_present(project, identities):
            personas.insert(0, Mention(LEAD_IDENTITY_ID, LEAD_NAME))
        return personas

    async def open_mention_session(
        request: Request, project: Project, mention: Mention
    ) -> str | None:
        """The persona's current session, restored first if it was lost."""
        if mention.id == ORCHESTRATOR_IDENTITY_ID:
            healed = await heal_main_session(request, project)
            state = await main_session_state(healed, conversation_store, permission_store)
            return healed.main_session_id if state == "active" else None
        identity, _ = await ensure_identity_session(request, project, mention.id)
        return identity.active_session_id

    async def mention_people(project: Project) -> list[str]:
        """The owner and shared members, who can be cc'd; none on a single-user server."""
        if project.user_id is None or project.user_id == RESERVED_USER_LOCAL:
            return []
        shares = await asyncio.to_thread(
            project_store.list_shares, project.id, user_id=project.user_id
        )
        return [project.user_id, *(share.user_id for share in shares)]

    if register_mention_targets is not None:
        register_mention_targets(
            PersonaMentionTargets(
                project=_owned_project,
                personas=mention_personas,
                open=open_mention_session,
                people=mention_people,
            )
        )

    return router
