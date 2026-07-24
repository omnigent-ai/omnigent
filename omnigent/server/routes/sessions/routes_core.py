"""Core session routes: create, list, get, update, fork, switch-agent."""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
    status,
)
from fastapi.responses import Response
from pydantic import ValidationError
from starlette.datastructures import UploadFile as StarletteUploadFile

from omnigent.cost_plan import (
    reserved_cost_control_keys,
)
from omnigent.db.utils import generate_agent_id
from omnigent.entities import (
    CommentsFingerprint,
    Conversation,
    synthesize_conversation_title,
)
from omnigent.entities.permission import SessionPermission
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.model_override import validate_model_override
from omnigent.reasoning_effort import (
    EFFORT_CLEAR_VALUES,
    EFFORT_VALUES,
    validate_effort,
)
from omnigent.runner.identity import (
    RUNNER_TUNNEL_TOKEN_HEADER,
)
from omnigent.runner.routing import RunnerRouter
from omnigent.runtime import (
    pending_elicitations,
    user_session_stream,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.policies.approval import _ELICITATION_MODE
from omnigent.server._elicitation_registry import (
    _harness_elicitation_owners,
    _harness_elicitation_registry,
    _harness_parked_elicitations,
    _harness_pre_resolved_elicitations,
    _ParkedHarnessElicitation,
    _PreResolvedHarnessElicitation,
)
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_OWNER,
    LEVEL_READ,
    AuthProvider,
)
from omnigent.server.background_session_titles import (
    BackgroundSessionTitleCoordinator,
)
from omnigent.server.host_registry import HostRegistry, RunnerExitReports
from omnigent.server.permissions import check_session_access
from omnigent.server.routes._auth_helpers import (
    get_permission_level as _get_permission_level,
)
from omnigent.server.routes._auth_helpers import (
    get_user_id as _get_user_id,
)
from omnigent.server.routes._auth_helpers import (
    require_access as _require_access,
)
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._auth_helpers import (
    require_user as _require_user,
)
from omnigent.server.routes._content_type import (
    require_json_or_multipart_content_type,
)
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.server.routes._sessions.common import *
from omnigent.server.routes._sessions.common import (
    get_server_runner_router,
    set_server_runner_router,
)
from omnigent.server.routes._sessions.helpers import *
from omnigent.server.routes._sessions.orchestration import *
from omnigent.server.schemas import (
    AutomaticSessionRenameRequest,
    AutomaticSessionRenameResponse,
    CreatedSessionResponse,
    PaginatedList,
    ReadStatePutRequest,
    SessionAgentChangedEvent,
    SessionCreateRequest,
    SessionForkRequest,
    SessionLabelsResponse,
    SessionList,
    SessionListItem,
    SessionProjectSummary,
    SessionResponse,
    SessionSwitchAgentRequest,
    UpdateSessionRequest,
)
from omnigent.session_lifecycle import (
    labels_with_closed_status,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.comment_store import CommentStore
from omnigent.stores.conversation_store import (
    PINNED_LABEL_KEY,
    PROJECT_LABEL_KEY,
    ConversationNotFoundError,
    pinned_label_key,
)
from omnigent.stores.file_store import FileStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.project_store import ProjectStore


def register_core_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    file_store: FileStore | None = None,
    artifact_store: ArtifactStore | None = None,
    runner_router: RunnerRouter | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    agent_cache: AgentCache | None = None,
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None = None,
    comment_store: CommentStore | None = None,
    runner_tunnel_tokens: frozenset[str] | None = None,
    runner_exit_reports: RunnerExitReports | None = None,
    host_registry: HostRegistry | None = None,
    project_store: ProjectStore | None = None,
    background_title_coordinator: BackgroundSessionTitleCoordinator | None = None,
) -> None:
    """Register the core session routes on router."""

    @router.post(
        "/sessions",
        status_code=201,
        response_model=None,
        # CSRF hardening: this route dispatches on Content-Type (JSON vs
        # multipart bundled-create), so reject text/plain and other simple
        # types up front while still allowing both legitimate body shapes.
        # The multipart shape is CORS-safelisted, so the content-type guard
        # alone can't stop a cross-site bundle upload — require_trusted_origin
        # closes that gap (allows absent Origin for non-browser SDK/runner
        # clients; in local mode a present Origin must be loopback).
        dependencies=[
            Depends(require_json_or_multipart_content_type),
            Depends(require_trusted_origin),
        ],
    )
    async def create_session(
        request: Request,
    ) -> SessionResponse | CreatedSessionResponse:
        """
        Create a session.

        ``application/json`` preserves the existing contract: bind to
        an already-registered agent by ``agent_id`` and return the full
        session snapshot. ``multipart/form-data`` is the Alpha
        runner-state create path: the request carries a JSON
        ``metadata`` part and a ``bundle`` file part, then the server
        stores the bundle and creates the conversation row plus
        session-scoped agent row in one database transaction.

        :param request: FastAPI request containing either JSON or
            multipart form data.
        :returns: :class:`SessionResponse` for JSON create, or
            :class:`CreatedSessionResponse` for bundled create.
        :raises OmnigentError: If metadata, bundle, or agent lookup
            validation fails, artifact storage is unavailable, or
            database creation fails.
        """
        user_id = _require_user(request, auth_provider)
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type == "multipart/form-data":
            result = await _create_bundled_session_from_multipart(request, user_id)
            if permission_store is not None and user_id is not None:
                await asyncio.to_thread(permission_store.ensure_user, user_id)
                await asyncio.to_thread(
                    permission_store.grant, user_id, result.session_id, LEVEL_OWNER
                )
            # Push the new session to this user's other open tabs so it
            # enters the sidebar without a list poll (WS /sessions/updates).
            _announce_session_added(user_id, result.session_id)
            return result

        try:
            payload = await request.json()
            body = SessionCreateRequest.model_validate(payload)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail=[
                    {
                        "type": "json_invalid",
                        "loc": ["body"],
                        "msg": "Invalid JSON",
                        "input": None,
                    },
                ],
            ) from exc
        except ValidationError as exc:
            # include_context=False: pydantic v2 puts the RAW exception
            # object in ctx for validator-raised ValueErrors, which
            # JSONResponse cannot serialize — every model_validator 422
            # on this route 500'd as internal_error. The human-readable
            # message survives in each entry's `msg`.
            raise HTTPException(status_code=422, detail=exc.errors(include_context=False)) from exc

        resp = await _create_session_from_existing_agent(
            conversation_store,
            agent_store,
            runner_router,
            body,
            request,
            agent_cache=agent_cache,
            user_id=user_id,
            permission_store=permission_store,
            liveness_lookup=liveness_lookup,
            file_store=file_store,
            artifact_store=artifact_store,
            background_title_coordinator=background_title_coordinator,
        )
        # Notify the runner about the new session so it can resolve
        # the spec and cache sub_agent_name before the first turn.
        # Without this, the runner doesn't know this session exists
        # until the first forwarded event.
        conv = conversation_store.get_conversation(resp.id)
        # Mark the terminal spin-up flag at creation — the earliest
        # possible point — for a host-launched terminal-first session
        # (claude-native / codex-native). The runner's own pending emit
        # arrives much later (after host launch, runner boot, spec
        # resolve, and harness spawn — each a round-trip), so the spinner
        # would otherwise only flash for the sub-second window before the
        # already-spawned terminal resolves. Gated on host_id because the
        # runner only auto-creates (and thus only clears) a terminal for
        # host-launched sessions; a CLI-bound terminal-first session
        # manages its own terminal and would strand the flag. Clears come
        # from the runner's finally, the relay's resource.created
        # self-heal, or the host-launch-failure path below.
        _terminal_first_create = (
            conv is not None
            and body.host_id is not None
            and conv.labels.get(_CLAUDE_NATIVE_UI_LABEL_KEY) == _CLAUDE_NATIVE_UI_LABEL_VALUE
        )
        if _terminal_first_create:
            _publish_terminal_pending(resp.id, True)
        _rc = await _get_runner_client(resp.id, runner_router)
        if _rc is not None and conv is not None:
            try:
                await _rc.post(
                    "/v1/sessions",
                    json={
                        "session_id": resp.id,
                        "agent_id": conv.agent_id,
                        "sub_agent_name": conv.sub_agent_name,
                    },
                    timeout=10.0,
                )
            except (httpx.HTTPError, ConnectionError):
                _logger.warning(
                    "Failed to notify runner about session %s",
                    resp.id,
                    exc_info=True,
                )
        # Grant the creator ownership BEFORE any host launch so the
        # launch's session-ownership check (shared with
        # POST /v1/hosts/{host_id}/runners via resolve_host_launch)
        # sees the grant.
        if permission_store is not None and user_id is not None:
            await asyncio.to_thread(permission_store.ensure_user, user_id)
            await asyncio.to_thread(permission_store.grant, user_id, resp.id, LEVEL_OWNER)
            resp.permission_level = await _get_permission_level(user_id, resp.id, permission_store)
        # Push the new session to this user's other open tabs (see the
        # multipart path above for the rationale).
        _announce_session_added(user_id, resp.id)

        # Managed host: schedule a BACKGROUND sandbox provision bound
        # to this session and return immediately — provisioning takes
        # tens of seconds and must not block the create POST. The
        # background task binds host + workspace to the session row
        # and launches the runner once the sandbox host registers; a
        # message POST racing the provision rendezvouses on the
        # tracker entry registered here (see post_event). Config
        # problems and malformed repo workspaces still fail the POST
        # synchronously.
        launch_host_id = body.host_id
        if body.host_type == "managed" and resp.runner_id is None:
            sandbox_config = getattr(request.app.state, "sandbox_config", None)
            host_store_for_managed = getattr(request.app.state, "host_store", None)
            managed_launches = getattr(request.app.state, "managed_launches", None)
            if (
                sandbox_config is None
                or host_store_for_managed is None
                or managed_launches is None
            ):
                raise OmnigentError(
                    "managed hosts are not configured on this server — add a "
                    "'sandbox:' section to the server config",
                    code=ErrorCode.INVALID_INPUT,
                )
            from omnigent.server.auth import RESERVED_USER_LOCAL
            from omnigent.server.managed_hosts import (
                MANAGED_REPO_LABEL_KEY,
                parse_repo_workspace,
            )

            # A managed workspace is a repository URL (schema-
            # validated) the launch clones inside the sandbox; parse
            # it now so a malformed URL is a synchronous 4xx, not a
            # background failure.
            repo = parse_repo_workspace(body.workspace) if body.workspace is not None else None
            if body.workspace is not None:
                # The session row's workspace is overwritten with the
                # CLONED path at bind time; record the raw request
                # value so a sandbox relaunch can re-clone the same
                # repository into the new generation.
                await asyncio.to_thread(
                    conversation_store.set_labels,
                    resp.id,
                    {MANAGED_REPO_LABEL_KEY: body.workspace},
                )
            managed_launches.begin(resp.id)
            # Seed the launch-progress indicator before the background
            # task starts, so the first GET snapshot (the Web UI
            # navigates to the session page immediately after this
            # 201) already carries the "provisioning" stage.
            _publish_sandbox_status(resp.id, "provisioning")
            launch_task = asyncio.create_task(
                _run_managed_launch(
                    session_id=resp.id,
                    # On auth-disabled servers user_id is None; the
                    # sandbox host registers under the reserved local
                    # owner, same as a directly-connected host would.
                    owner=user_id if user_id is not None else RESERVED_USER_LOCAL,
                    sandbox_config=sandbox_config,
                    repo=repo,
                    tracker=managed_launches,
                    conversation_store=conversation_store,
                    host_store=host_store_for_managed,
                    host_registry=getattr(request.app.state, "host_registry", None),
                    tunnel_registry=getattr(request.app.state, "tunnel_registry", None),
                )
            )
            _managed_launch_tasks.add(launch_task)
            launch_task.add_done_callback(_managed_launch_tasks.discard)

        # Host launch: if a host is targeted (caller-supplied or
        # managed) and no runner is bound yet, authorize (caller must
        # own the host AND the session), atomically bind, then launch.
        # Same authorization path as POST /v1/hosts/{host_id}/runners.
        if launch_host_id is not None and resp.runner_id is None:
            host_registry = getattr(request.app.state, "host_registry", None)
            host_store_inst = getattr(request.app.state, "host_store", None)
            if host_registry is not None and host_store_inst is not None:
                from omnigent.host.frames import (
                    HostLaunchRunnerFrame,
                    encode_host_frame,
                )
                from omnigent.runner.identity import token_bound_runner_id
                from omnigent.server.routes._host_launch import resolve_host_launch

                target = await asyncio.to_thread(
                    resolve_host_launch,
                    user_id=user_id,
                    host_id=launch_host_id,
                    session_id=resp.id,
                    host_store=host_store_inst,
                    host_registry=host_registry,
                    conversation_store=conversation_store,
                    permission_store=permission_store,
                )
                conn = target.conn
                binding_token = secrets.token_urlsafe(32)
                runner_id = token_bound_runner_id(binding_token)
                # Atomic bind (WHERE runner_id IS NULL) closes the TOCTOU.
                bound = await asyncio.to_thread(
                    conversation_store.set_runner_id,
                    resp.id,
                    runner_id,
                )
                if not bound:
                    raise OmnigentError(
                        f"Session {resp.id!r} already has a runner bound",
                        code=ErrorCode.CONFLICT,
                    )
                # host_id and workspace were already written by
                # _create_session_from_existing_agent; we only need
                # to set runner_id atomically (above) and send the
                # launch frame.
                request_id = secrets.token_hex(8)
                future: asyncio.Future[dict[str, str | None]] = (
                    asyncio.get_running_loop().create_future()
                )
                conn.pending_launches[request_id] = future
                if resp.workspace is None:  # pragma: no cover — schema guards
                    raise OmnigentError(
                        "session has host_id but no workspace; "
                        "schema constraint should have prevented this",
                        code=ErrorCode.INTERNAL_ERROR,
                    )
                launch_frame = encode_host_frame(
                    HostLaunchRunnerFrame(
                        request_id=request_id,
                        binding_token=binding_token,
                        workspace=resp.workspace,
                        session_id=resp.id,
                        # Already canonical (see _resolve_harness); lets
                        # the host refuse an unconfigured harness before
                        # spawning. None (agent not resolvable) skips the
                        # host-side check.
                        harness=resp.harness,
                    )
                )
                host_registry.send_text(conn, launch_frame)
                try:
                    result = await asyncio.wait_for(future, timeout=30.0)
                except asyncio.TimeoutError:
                    conn.pending_launches.pop(request_id, None)
                    result = {"status": "failed", "error": "host launch timed out"}
                if result.get("status") == "failed":
                    # Lenient on every create-time launch failure, including
                    # an unconfigured harness: the picker's readiness data
                    # can be stale (the user may have run `omnigent setup`
                    # since the host last connected), so we never block the
                    # create. The session opens with the binding intact; the
                    # first message drives the real runner start, and if the
                    # host still refuses there, that path consults the daemon
                    # and persists a transcript error (see post_event's
                    # relaunch branch). No create-time harness gating.
                    _logger.warning(
                        "Host %s failed to launch runner for session %s: %s",
                        launch_host_id,
                        resp.id,
                        result.get("error"),
                    )
                    # The runner never booted, so its pending=False clear
                    # will never fire. Clear the spin-up flag here so a
                    # failed launch doesn't strand the Terminal-pill
                    # spinner. No-op when we never set it.
                    if _terminal_first_create:
                        _publish_terminal_pending(resp.id, False)
                resp.runner_id = runner_id
                resp.host_id = launch_host_id

        return resp

    async def _create_bundled_session_from_multipart(
        request: Request,
        user_id: str | None,
    ) -> CreatedSessionResponse:
        """
        Handle multipart ``POST /v1/sessions`` with inline agent upload.

        :param request: FastAPI request containing ``metadata`` and
            ``bundle`` form parts.
        :param user_id: Authenticated caller, e.g.
            ``"alice@example.com"``. Used to authorize
            ``metadata.parent_session_id`` and enforce
            runner ownership on parent inheritance.
        :returns: :class:`CreatedSessionResponse` with the new
            session id.
        :raises HTTPException: 422 when a required multipart part is
            absent.
        :raises OmnigentError: If metadata or bundle validation
            fails, or ``parent_session_id`` fails authorization.
        """
        if artifact_store is None:
            raise OmnigentError(
                "artifact store is not configured",
                code=ErrorCode.INTERNAL_ERROR,
            )
        form = await request.form()
        metadata = form.get("metadata")
        bundle = form.get("bundle")
        missing = [
            _multipart_missing_detail(field)
            for field, value in (("metadata", metadata), ("bundle", bundle))
            if value is None
        ]
        if missing:
            raise HTTPException(status_code=422, detail=missing)
        if not isinstance(metadata, str):
            raise HTTPException(status_code=422, detail=[_multipart_missing_detail("metadata")])
        if not isinstance(bundle, StarletteUploadFile):
            raise HTTPException(status_code=422, detail=[_multipart_missing_detail("bundle")])
        parsed_metadata = _parse_session_create_metadata(metadata)
        _reject_reserved_cost_control_label_seed(parsed_metadata.labels)
        _reject_server_reserved_label_seed(parsed_metadata.labels)

        inherited_runner_id: str | None = None
        if parsed_metadata.parent_session_id is not None:
            inherited_runner_id = await _authorize_bundled_parent_and_inherit_runner(
                parsed_metadata.parent_session_id,
                user_id=user_id,
                permission_store=permission_store,
                conversation_store=conversation_store,
                runner_router=runner_router,
            )

        bundle_bytes = await bundle.read()
        result = await asyncio.to_thread(
            _create_session_from_bundle,
            conversation_store,
            artifact_store,
            parsed_metadata,
            bundle_bytes,
            inherited_runner_id,
        )
        # Top-level creates (no inherited runner) skip the notify —
        # their runner registers itself later.
        if inherited_runner_id is not None:
            await _notify_runner_of_bundled_child(
                result.session_id,
                result.agent_id,
                runner_router,
            )
        return result

    # ── GET /sessions/projects ────────────────────────────────────
    #
    # MUST be registered before ``GET /sessions/{session_id}``: FastAPI
    # matches routes in registration order, so a literal ``/sessions/projects``
    # would otherwise be captured by the ``{session_id}`` path param and 404
    # as a missing conversation.

    @router.get("/sessions/projects")
    async def list_session_projects(
        request: Request,
    ) -> list[SessionProjectSummary]:
        """
        Return the caller's projects as ``{"id", "name"}`` pairs, ordered
        alphabetically by name.

        Dual-reads both project representations and unions them by name:
        - **First-class projects** (``project_store``) — carry an ``id`` and
          appear even when empty (the whole point of the first-class entity).
        - **Legacy label-projects** (implicit ``omni_project`` label) — exist
          while at least one owned session carries the label; ``id`` is
          ``None`` until such a project is promoted to first-class.

        A name present in both sources collapses to one entry that keeps the
        first-class ``id``. Filing is owner-only, so both halves are scoped to
        the caller (label-projects to their owned sessions, first-class to
        their owned rows) — a project shared to them but owned by another user
        does not surface as one of their own folders.

        :returns: List of :class:`SessionProjectSummary` ordered by name.
        """
        user_id = _require_user(request, auth_provider)

        def _list_union() -> list[SessionProjectSummary]:
            # First-class first so its id wins when a name exists in both.
            by_name: dict[str, SessionProjectSummary] = {}
            if project_store is not None:
                for proj in project_store.list(owner_user_id=user_id):
                    by_name[proj.name] = SessionProjectSummary(id=proj.id, name=proj.name)
            # Legacy path: label-derived projects (id=None unless already first-class).
            for name in conversation_store.list_projects(owned_by=user_id):
                by_name.setdefault(name, SessionProjectSummary(id=None, name=name))
            return [by_name[name] for name in sorted(by_name)]

        return await asyncio.to_thread(_list_union)

    # ── PUT /sessions/{session_id}/read-state ─────────────────────
    #
    # The per-user read-state *write* path. The *read* path is the
    # per-viewer ``viewer_last_seen`` / ``viewer_unread`` fields embedded in
    # the ``GET /v1/sessions`` list items — no separate read endpoint.

    @router.put(
        "/sessions/{session_id}/read-state",
        status_code=204,
    )
    async def put_read_state(
        request: Request,
        session_id: str,
        body: ReadStatePutRequest,
    ) -> Response:
        """
        Set the calling user's read-state for one session.

        Requires ``LEVEL_READ`` on the session in multi-user mode — you can
        only track read-state for sessions you can see. Stores the values
        verbatim (the client enforces the baseline's monotonicity and the
        unread semantics); the server does not interpret them against
        session status. Returns ``204`` — the client already has the
        optimistic state and re-reads the authoritative value on the next
        ``GET /v1/sessions`` poll.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier.
        :param body: The validated :class:`ReadStatePutRequest`.
        :returns: An empty ``204 No Content`` response.
        :raises OmnigentError: 403 if the caller lacks read access.
        """
        user_id = _require_user(request, auth_provider)
        await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        _set_read_state(user_id, session_id, body.last_seen, body.unread)
        return Response(status_code=204)

    # ── GET /sessions/{session_id} ───────────────────────────────

    @router.get(
        "/sessions/{session_id}",
        # See create_session for the response_model=None rationale. We keep
        # response_model=None (no response re-validation/serialization) but
        # still advertise the body schema for docs/SDK tooling via responses=.
        response_model=None,
        responses={200: {"model": SessionResponse}},
    )
    async def get_session(
        request: Request,
        response: Response,
        session_id: str,
        include_items: bool = Query(default=True),
        include_liveness: bool = Query(default=True),
        refresh_state: bool = Query(default=False),
    ) -> SessionResponse:
        """
        Return a session snapshot: identity, status, and committed
        items.

        :param request: The incoming FastAPI request (for auth).
        :param response: The FastAPI response (for cache headers).
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param include_items: When ``False``, skip the committed-items
            read and return ``items=[]``. The web chat surface passes
            ``False`` because it hydrates the transcript via the
            paginated ``GET /sessions/{id}/items`` endpoint in parallel
            and never reads the snapshot's copy; the items read is the
            single most expensive step of the snapshot build.
        :param include_liveness: When ``False``, skip the runner/host
            liveness lookup and return ``runner_online``/``host_online``
            as ``None``. The web chat surface passes ``False`` because
            it sources liveness from the ``/health`` poll and the WS
            stream, not the snapshot.
        :param refresh_state: When ``True``, refresh runner-derived
            snapshot overlays from the live session instead of serving
            stale AP-process caches. Browser reload/bind requests use
            this to recover from fixed bugs without restarting the AP
            server.
        :returns: The matching :class:`SessionResponse`.
        :raises OmnigentError: 404 if no session exists.
        """
        response.headers["Cache-Control"] = "no-store"
        user_id = _get_user_id(request, auth_provider)
        # Single permission pass: authorize + resolve the display level +
        # fetch the conversation once, then reuse the conversation in the
        # snapshot (the snapshot's read is skipped). Replaces the former
        # require_access + get_permission_level + snapshot-get_conversation
        # sequence, which made ~5-6 separate store round-trips.
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        return await _get_session_snapshot(
            conversation_store,
            session_id,
            access.level,
            agent_store,
            agent_cache,
            conversation=access.conversation,
            liveness_lookup=liveness_lookup if include_liveness else None,
            include_items=include_items,
            runner_exit_reports=runner_exit_reports,
            refresh_state=refresh_state,
            host_store=getattr(request.app.state, "host_store", None),
            sandbox_config=getattr(request.app.state, "sandbox_config", None),
            viewer_id=user_id,
        )

    @router.get(
        "/sessions/{session_id}/labels",
        response_model=SessionLabelsResponse,
    )
    async def get_session_labels(
        request: Request,
        response: Response,
        session_id: str,
    ) -> SessionLabelsResponse:
        """
        Return only the labels for a session.

        Native runner bridge setup needs labels during harness spawn,
        but the full session snapshot also loads history, skills,
        runner status, and agent metadata. This endpoint keeps that
        spawn-time dependency to one authorized conversation read.

        :param request: The incoming FastAPI request (for auth).
        :param response: The FastAPI response (for cache headers).
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The session id and labels.
        :raises OmnigentError: 404 if no session exists.
        """
        response.headers["Cache-Control"] = "no-store"
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        conv = access.conversation
        if conv is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise _session_not_found()
        return SessionLabelsResponse(
            id=conv.id,
            # Collapse per-user pin keys for this caller (never leak another
            # user's pin key to a native harness bridge).
            labels=labels_with_closed_status(_labels_for_viewer(conv.labels, user_id), conv.title),
        )

    # ── GET /sessions ───────────────────────────────────────────

    @router.get(
        "/sessions",
        response_model=None,
        responses={200: {"model": SessionList}},
    )
    async def list_sessions(
        request: Request,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        agent_id: str | None = Query(default=None),
        agent_name: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
        sort_by: str = Query(default="created_at", pattern="^(created_at|updated_at)$"),
        search_query: str | None = Query(default=None),
        include_archived: bool = Query(default=False),
        kind: str = Query(default="default", pattern="^(default|sub_agent|any)$"),
        project: str | None = Query(default=None),
        pinned: bool = Query(default=False),
    ) -> PaginatedList:
        """
        List sessions with cursor-based pagination.

        Sessions are conversations with a non-``None`` ``agent_id``
        — i.e. those created via ``POST /v1/sessions``.
        Conversations without an agent binding are excluded.

        :param limit: Maximum number of sessions to return
            (1-1000, default 20).
        :param after: Cursor — return sessions after this
            session ID in sort order, e.g. ``"conv_abc123"``.
        :param before: Cursor — return sessions before this
            session ID.
        :param agent_id: When set, only return sessions bound
            to this agent, e.g. ``"ag_abc123"``. ``None``
            returns sessions across all agents.
        :param agent_name: When set, only return sessions whose
            bound agent row has this name. This intentionally
            includes session-scoped agents that share a name but
            have distinct bundles. ``None`` disables the filter.
        :param order: Sort direction, ``"desc"`` (newest-first)
            or ``"asc"`` (oldest-first).
        :param sort_by: Column to sort on, ``"created_at"`` or
            ``"updated_at"``.
        :param search_query: Case-insensitive substring filter on
            the session title or conversation content. ``None``
            or empty string disables the filter. A session
            matches if its title contains the query or any of
            its conversation items' text does. Powers the
            sidebar's session search.
        :param include_archived: When ``False`` (default), archived
            sessions are omitted. When ``True``, archived sessions
            are returned alongside active ones (the sidebar groups
            them into an "Archived" section). Powers the sidebar's
            "Show archived" toggle.
        :param kind: Conversation kind to return. ``"default"``
            (the default) returns only top-level user-initiated
            sessions — the sidebar's view. ``"sub_agent"`` returns
            only sub-agent child sessions. ``"any"`` returns both;
            this lets the new-session agent picker discover agents
            that are only bound to sub-agent sessions (e.g. ones
            uploaded via ``sys_session_create``).
        :param pinned: When ``True``, return only sessions the user
            has pinned (the ``omnigent.pinned`` label). Lets the
            sidebar enumerate pinned sessions that fall outside the
            loaded pagination window. ``False`` (default) disables it.
        :returns: A :class:`PaginatedList` of
            :class:`SessionListItem`.
        """
        # Empty-string normalization — the UI sends
        # ``?search_query=`` when the search box is cleared and
        # that should behave identically to the param being
        # absent. Keeping the store's contract crisp: ``None``
        # means "no filter", anything else means "search".
        #
        # require_user, not get_user_id: ``accessible_by=None`` below
        # means "no ACL filter", so an unauthenticated request slipping
        # through as None would list EVERY user's sessions. Fail closed
        # with 401 instead (user_id stays None only when auth is
        # disabled entirely — no auth_provider).
        user_id = _require_user(request, auth_provider)
        normalized_query = search_query if search_query else None
        # A specific project folder ("My sessions"-only) must show only the
        # viewer's own sessions — a session shared with them but filed under a
        # like-named project belongs on "Shared with me", not in this folder.
        # Passing owned_by here also scopes the dual-read's first-class half:
        # the store resolves the project NAME to the caller's own project id.
        # The flat list (project=None) and Unfiled (project="") stay unscoped so
        # shared sessions still surface for the "Shared with me" tab.
        owned_by = user_id if project else None
        page = await asyncio.to_thread(
            conversation_store.list_conversations,
            limit=limit,
            after=after,
            before=before,
            agent_id=agent_id,
            agent_name=agent_name,
            accessible_by=user_id,
            owned_by=owned_by,
            has_agent_id=True,
            # The store treats ``None`` as "no kind filter"; the API
            # spells that ``kind=any`` to keep the param required-ish
            # and pattern-validated.
            kind=None if kind == "any" else kind,
            order=order,
            sort_by=sort_by,
            search_query=normalized_query,
            include_archived=include_archived,
            project=project,
            pinned=pinned,
            # Pins are per-user: filter to the caller's own pin key.
            pinned_owner=user_id,
        )
        # list_conversations may return rows with agent_id=None for
        # legacy conversations; skip them before building the batch IDs.
        conv_ids = [conv.id for conv in page.data if conv.agent_id is not None]
        if not conv_ids:
            return PaginatedList(
                data=[],
                first_id=page.first_id,
                last_id=page.last_id,
                has_more=page.has_more,
            )
        # Batch-fetch permissions and agent names concurrently.
        # The tasks table has been removed — status comes exclusively from
        # the relay-fed ``_session_status_cache``.
        unique_agent_ids = list({c.agent_id for c in page.data if c.agent_id is not None})
        if permission_store is not None:
            perms_by_conv, agent_names_by_id, child_ids_by_parent = await asyncio.gather(
                asyncio.to_thread(permission_store.list_for_sessions, conv_ids),
                asyncio.to_thread(agent_store.get_names, unique_agent_ids),
                asyncio.to_thread(
                    conversation_store.list_child_conversation_ids_by_parent,
                    conv_ids,
                ),
            )
            user_is_admin = (
                await asyncio.to_thread(permission_store.is_admin, user_id)
                if user_id is not None
                else False
            )
        else:
            agent_names_by_id, child_ids_by_parent = await asyncio.gather(
                asyncio.to_thread(agent_store.get_names, unique_agent_ids),
                asyncio.to_thread(
                    conversation_store.list_child_conversation_ids_by_parent,
                    conv_ids,
                ),
            )
            perms_by_conv: dict[str, list[SessionPermission]] = {}
            user_is_admin = False
        # In-memory lookup — no I/O, so batching avoids re-acquiring
        # the index's lock per row but otherwise has no DB cost.
        pending_counts = pending_elicitations.counts_for(conv_ids)
        comments_fingerprints = await _comments_fingerprints_for(conv_ids)
        items: list[SessionListItem] = [
            _build_session_list_item(
                conv,
                agent_names_by_id=agent_names_by_id,
                grants=perms_by_conv.get(conv.id, []),
                user_id=user_id,
                user_is_admin=user_is_admin,
                permissions_enabled=permission_store is not None,
                pending_count=pending_counts.get(conv.id, 0),
                child_session_ids=child_ids_by_parent[conv.id],
                comments_fingerprint=comments_fingerprints.get(conv.id),
            )
            for conv in page.data
            if conv.agent_id is not None
        ]
        # The list deliberately does NOT compute per-item liveness
        # (runner_online / host_online). No list consumer reads it: the
        # sidebar no longer surfaces connection state, and the only live
        # consumer — the open-session view — sources liveness from the
        # single-session snapshot, the WS stream, and the /health poll, not
        # from list rows. Skipping it here removes the session-connectivity
        # and hosts-table queries from every GET /v1/sessions.
        return PaginatedList(
            data=[item.model_dump(exclude_none=True) for item in items],
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )

    async def _comments_fingerprints_for(
        conv_ids: list[str],
    ) -> dict[str, CommentsFingerprint]:
        """
        Batch-fetch comment change fingerprints for the given sessions.

        Shared by the ``GET /v1/sessions`` page builder and
        ``WS /v1/sessions/updates`` so both emit the same
        ``comments_count`` / ``comments_updated_at`` values and the
        stream's diff fires when a comment is added, edited, addressed,
        or deleted.

        :param conv_ids: Session ids to summarize,
            e.g. ``["conv_abc123"]``.
        :returns: Map from session id to its
            :class:`CommentsFingerprint`; empty when no comment store
            is wired. Sessions without comments are absent.
        """
        if comment_store is None or not conv_ids:
            return {}
        return await asyncio.to_thread(comment_store.get_comments_fingerprints, conv_ids)

    # ── WS /sessions/updates ────────────────────────────────────

    async def _fetch_watched_items(
        watched: list[str],
        user_id: str | None,
    ) -> list[dict[str, Any]]:
        """
        Build current list-item payloads for the watched ids.

        Reads exactly the same sources as ``GET /v1/sessions`` (the
        relay-fed status cache plus the conversation store) and enforces
        per-session read access: ids the user cannot access, that don't
        exist, or that aren't sessions (no ``agent_id``) are silently
        omitted. This is the pull the session-updates stream diffs each
        interval — it is a drop-in for the client's former list poll, not
        a new event source, so it carries no new cross-replica semantics.

        When ``liveness_lookup`` is wired, each payload also carries
        ``runner_online`` and ``host_online`` (the same values
        ``GET /health`` and ``GET /v1/sessions`` return), so the client
        can drop its per-session ``/health`` poll for watched sessions.

        :param watched: Conversation ids the client is currently
            displaying, e.g. ``["conv_abc", "conv_def"]``. Already
            deduplicated and length-capped by the caller.
        :param user_id: The authenticated requesting user, or ``None``
            when permissions are disabled, e.g. ``"alice@example.com"``.
        :returns: One JSON-ready dict per accessible, existing watched
            session, in no particular order.
        """
        if not watched:
            return []
        if permission_store is not None:
            perms_by_conv = await asyncio.to_thread(permission_store.list_for_sessions, watched)
            user_is_admin = (
                await asyncio.to_thread(permission_store.is_admin, user_id)
                if user_id is not None
                else False
            )
            accessible = [
                cid
                for cid in watched
                if _permission_level_from_grants(
                    user_id, perms_by_conv.get(cid, []), user_is_admin
                )
                is not None
            ]
        else:
            perms_by_conv = {}
            user_is_admin = False
            accessible = list(watched)
        if not accessible:
            return []

        def _load_sessions(ids: list[str]) -> list[Conversation]:
            """Bulk-load the accessible conversations that are sessions
            (non-null ``agent_id``) in one batched store call, preserving
            the caller's id order for deterministic output."""
            by_id = conversation_store.get_conversations(ids)
            return [
                conv
                for cid in ids
                if (conv := by_id.get(cid)) is not None and conv.agent_id is not None
            ]

        convs = await asyncio.to_thread(_load_sessions, accessible)
        if not convs:
            return []
        unique_agent_ids = list({c.agent_id for c in convs if c.agent_id is not None})
        conv_ids = [c.id for c in convs]
        agent_names_by_id, child_ids_by_parent, comments_fingerprints = await asyncio.gather(
            asyncio.to_thread(agent_store.get_names, unique_agent_ids),
            asyncio.to_thread(
                conversation_store.list_child_conversation_ids_by_parent,
                conv_ids,
            ),
            _comments_fingerprints_for(conv_ids),
        )
        pending_counts = pending_elicitations.counts_for(conv_ids)
        items = [
            _build_session_list_item(
                conv,
                agent_names_by_id=agent_names_by_id,
                grants=perms_by_conv.get(conv.id, []),
                user_id=user_id,
                user_is_admin=user_is_admin,
                permissions_enabled=permission_store is not None,
                pending_count=pending_counts.get(conv.id, 0),
                child_session_ids=child_ids_by_parent[conv.id],
                comments_fingerprint=comments_fingerprints.get(conv.id),
            )
            for conv in convs
        ]
        await _apply_liveness_to_items(items, liveness_lookup)
        # Full-row dumps (every field, nulls included) — NOT exclude_none. The
        # stream is a diff source: the client overlays these onto its cached
        # rows, so a field that cleared to null must arrive as an explicit null
        # (an absent key would leave the stale value in the cache). The client
        # converts null → undefined on apply, so a cleared field lands in the
        # same shape GET /v1/sessions produces (absent), and the
        # ``permission_level === null`` full-access sentinel in the web sidebar
        # is never tripped by a streamed null. The GET list endpoint keeps
        # exclude_none — it replaces whole pages, so it has nothing to clear.
        #
        # search_snippet is excluded: it is search-only (populated just by
        # GET /v1/sessions?search_query=), so this no-query path always has it
        # None. Dumping it as an explicit null would overwrite a snippet the
        # search response put in the client cache, making the palette's match
        # preview flicker away on the next stream tick. Omitting the key leaves
        # the cached snippet untouched.
        return [item.model_dump(exclude={"search_snippet"}) for item in items]

    @router.websocket("/sessions/updates")
    async def session_updates(websocket: WebSocket) -> None:
        """
        Push session-list changes for a client-supplied watch-set.

        Replaces the web app's 4 s HTTP poll of ``GET /v1/sessions``
        with one persistent connection. Protocol (JSON text frames):

        - **client → server**:
          ``{"type": "watch", "session_ids": [...]}`` — the ids the
          client is currently displaying. Sent on connect and re-sent
          whenever the visible set changes (scroll / filter /
          pagination); it fully replaces the prior watch-set. Unknown
          message shapes are ignored for forward compatibility.
        - **server → client**:
          ``{"type": "snapshot", "items": [SessionListItem, ...]}`` once
          per ``watch`` (full state for the new set), then
          ``{"type": "changed", "items": [...]}`` /
          ``{"type": "removed", "ids": [...]}`` deltas as watched
          sessions change, and ``{"type": "heartbeat"}`` when idle.

        Watched-row freshness is pull-based — each interval the server
        re-reads the watched ids (the same read ``GET /v1/sessions`` does)
        and emits only what changed. *Discovery* of sessions the client
        isn't watching yet (created / forked / shared elsewhere) is instead
        push-based: a ``session_added`` event on this user's
        :mod:`user_session_stream` channel makes the server push the new
        session as a ``changed`` frame, which the client reconciles into the
        sidebar. Together these mean an idle list makes zero HTTP polls yet a
        new session still appears within a tick of being created.

        :param websocket: The incoming FastAPI :class:`WebSocket`.
        """
        user_id = auth_provider.get_user_id(websocket) if auth_provider is not None else None
        # When permissions are enabled, an unauthenticated socket can see
        # nothing useful and must not be allowed to probe ids; reject the
        # handshake (mirrors the terminal-attach authorization gate).
        if permission_store is not None and user_id is None:
            raise WebSocketException(
                code=status.WS_1008_POLICY_VIOLATION,
                reason="authentication required",
            )
        await websocket.accept()

        watched: list[str] = []
        # Last SessionListItem dump sent per id, used to diff. Keyed only
        # by currently-watched ids; pruned when the watch-set narrows.
        last_sent: dict[str, dict[str, Any]] = {}
        last_send_monotonic = time.monotonic()
        # Serializes the read-diff-send-update critical section between the
        # reader (snapshot on watch) and the ticker (interval deltas) so
        # they never interleave updates to ``last_sent``.
        emit_lock = asyncio.Lock()

        async def _send(frame: dict[str, Any]) -> None:
            """
            Serialize and send one frame, stamping the last-send time so
            the heartbeat timer measures idleness from the last real send.

            :param frame: The outgoing frame, e.g.
                ``{"type": "changed", "items": [...]}``. Sent as JSON text.
            """
            nonlocal last_send_monotonic
            # Stamp the active trace context into the frame so a client
            # with browser-side propagation can correlate sidebar updates
            # to the trace that produced them. No-op when no span is
            # active (idle heartbeats/snapshots), keeping the frame
            # wire-identical in the common case.
            from omnigent.runtime import telemetry

            telemetry.record_message_payload(frame)
            telemetry.inject_trace_context(frame)
            await websocket.send_text(json.dumps(frame))
            last_send_monotonic = time.monotonic()

        async def _emit_snapshot() -> None:
            """Send a full snapshot for the current watch-set and reset the
            diff baseline to it."""
            items = await _fetch_watched_items(watched, user_id)
            dumps = {item["id"]: item for item in items}
            last_sent.clear()
            last_sent.update(dumps)
            await _send({"type": "snapshot", "items": list(dumps.values())})

        async def _emit_deltas() -> None:
            """Diff the watched ids against the last frame and send only the
            changes; emit a heartbeat when nothing changed but the link has
            been idle."""
            nonlocal last_send_monotonic
            if watched:
                items = await _fetch_watched_items(watched, user_id)
                current = {item["id"]: item for item in items}
                changed = [dump for cid, dump in current.items() if last_sent.get(cid) != dump]
                # Removed = a still-watched id that no longer resolves (lost
                # access or deleted). De-watched ids are pruned silently
                # below, not reported as removed.
                removed = [cid for cid in watched if cid not in current and cid in last_sent]
                last_sent.clear()
                last_sent.update(current)
                if changed:
                    await _send({"type": "changed", "items": changed})
                if removed:
                    await _send({"type": "removed", "ids": removed})
            from omnigent.server.routes import sessions as _sf

            if time.monotonic() - last_send_monotonic >= _sf._SESSION_UPDATES_HEARTBEAT_INTERVAL_S:
                await _send({"type": "heartbeat"})

        async def _reader() -> None:
            """Apply incoming watch-set updates and snapshot each one."""
            nonlocal watched
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict) or msg.get("type") != "watch":
                    # Forward-compatible: ignore frames we don't understand.
                    continue
                ids = msg.get("session_ids")
                if not isinstance(ids, list):
                    continue
                # Dedupe preserving order, keep only strings. Dedupe fully
                # first, then cap — so the truncation count below is the real
                # number of distinct ids dropped, not skewed by duplicates that
                # happen to sit past the cap.
                deduped: list[str] = []
                unique: set[str] = set()
                for cid in ids:
                    if isinstance(cid, str) and cid not in unique:
                        unique.add(cid)
                        deduped.append(cid)
                from omnigent.server.routes import sessions as _sf

                if len(deduped) > _sf._SESSION_UPDATES_MAX_WATCHED:
                    # Ids past the cap get no push updates and are never reported
                    # "removed" (they aren't watched). The client's low-rate list
                    # reconciliation still covers them, but log the silent drop so
                    # an oversized watch-set is diagnosable rather than invisible.
                    _logger.warning(
                        "session-updates watch-set truncated to %d of %d distinct ids "
                        "for user %r; ids beyond the cap rely on list-poll reconciliation",
                        _sf._SESSION_UPDATES_MAX_WATCHED,
                        len(deduped),
                        user_id,
                    )
                    deduped = deduped[: _sf._SESSION_UPDATES_MAX_WATCHED]
                # The watched set after capping — used to prune baselines for ids
                # the client no longer watches (including any just truncated).
                watched_set = set(deduped)
                # Handle the watch under a span parented on any trace
                # context the browser stamped into the frame, so the
                # snapshot read (and its DB spans) nest under the
                # client-originated trace.
                from omnigent.runtime import telemetry

                with telemetry.consume_frame_span("session_updates.watch", msg):
                    async with emit_lock:
                        watched = deduped
                        # Drop baselines for ids no longer watched so they
                        # can't surface as spurious "removed" later.
                        for stale in [cid for cid in last_sent if cid not in watched_set]:
                            del last_sent[stale]
                        await _emit_snapshot()

        async def _ticker() -> None:
            """Emit deltas / heartbeats on a fixed interval."""
            while True:
                from omnigent.server.routes import sessions as _sf

                await asyncio.sleep(_sf._SESSION_UPDATES_RESCAN_INTERVAL_S)
                async with emit_lock:
                    try:
                        await _emit_deltas()
                    except WebSocketDisconnect:
                        # The client went away mid-send — the normal terminal
                        # condition. Propagate so the stream tears down and the
                        # reader/ticker pair is cancelled.
                        raise
                    except Exception:
                        # A transient store/DB read failure must not kill a live
                        # stream and force every watcher to reconnect +
                        # re-snapshot. Log it and try again next interval; the
                        # diff is recomputed from scratch each tick, so a skipped
                        # tick costs at most one delayed delta. (CancelledError
                        # is not an Exception subclass, so cancellation still
                        # propagates.)
                        _logger.warning(
                            "session-updates delta tick failed; retrying next interval",
                            exc_info=True,
                        )

        async def _discovery() -> None:
            """Push sessions newly made accessible to this user — created,
            forked, or shared from elsewhere — so they enter the sidebar
            without a list poll.

            Such ids are NOT in the client's watch-set (the client doesn't
            know about them yet), so the per-interval diff can't surface them.
            This reacts to the create/grant event instead: it fetches the one
            announced id (access-checked, same as the watch path) and pushes
            it. The client reconciles the unknown id into its cache, then
            re-sends its watch-set including it, after which it is tracked
            like any normal watched row. Idle users with no new sessions
            receive nothing — so the zero-traffic property holds."""
            async for evt in user_session_stream.subscribe(_discovery_key(user_id)):
                if not isinstance(evt, dict):
                    continue
                evt_type = evt.get("type")
                if evt_type == "session_added":
                    sid = evt.get("session_id")
                    if not isinstance(sid, str):
                        continue
                    async with emit_lock:
                        # Already watched ⇒ the normal diff already covers it.
                        if sid in watched:
                            continue
                        try:
                            items = await _fetch_watched_items([sid], user_id)
                            if items:
                                await _send({"type": "changed", "items": items})
                        except WebSocketDisconnect:
                            # Client gone mid-send — propagate to tear the stream down.
                            raise
                        except Exception:
                            # A transient read/send failure for one announcement
                            # must not drop the whole stream; the session is still
                            # discoverable on the client's next list reconcile.
                            _logger.warning(
                                "session-updates discovery push failed for %r; "
                                "falling back to list reconcile",
                                sid,
                                exc_info=True,
                            )
                elif evt_type == "hosts_changed":
                    async with emit_lock:
                        try:
                            await _send({"type": "hosts_changed"})
                        except WebSocketDisconnect:
                            raise
                        except Exception:
                            _logger.warning(
                                "hosts-changed push failed; client will rely on fallback poll",
                                exc_info=True,
                            )

        reader_task = asyncio.create_task(_reader(), name="session-updates-reader")
        ticker_task = asyncio.create_task(_ticker(), name="session-updates-ticker")
        discovery_task = asyncio.create_task(_discovery(), name="session-updates-discovery")
        try:
            done, pending = await asyncio.wait(
                {reader_task, ticker_task, discovery_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            for task in done:
                exc = task.exception()
                # A client disconnect is the normal terminal condition; any
                # other exception is a real bug worth surfacing in logs.
                if exc is not None and not isinstance(exc, WebSocketDisconnect):
                    _logger.warning("session-updates stream task crashed: %r", exc)
        finally:
            with contextlib.suppress(RuntimeError):
                await websocket.close()

    # ── Codex-native goal controls ───────────────────────────────

    from omnigent.server.routes.codex.sessions import register_codex_session_routes

    register_codex_session_routes(
        router,
        conversation_store=conversation_store,
        runner_router=runner_router,
        auth_provider=auth_provider,
        permission_store=permission_store,
        runner_exit_reports=runner_exit_reports,
    )

    # ── PATCH /sessions/{session_id} ────────────────────────────

    @router.post(
        "/sessions/{session_id}/auto-title",
        response_model=AutomaticSessionRenameResponse,
    )
    async def automatically_rename_session(
        request: Request,
        session_id: str,
        body: AutomaticSessionRenameRequest,
    ) -> AutomaticSessionRenameResponse:
        """Replace the deterministic first-message title when still current."""
        user_id = _get_user_id(request, auth_provider)
        await _require_access(
            user_id,
            session_id,
            LEVEL_EDIT,
            permission_store,
            conversation_store,
        )
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND)
        if conv.parent_conversation_id is not None:
            return AutomaticSessionRenameResponse(renamed=False, reason="not_top_level")

        title = " ".join(body.title.split())
        if "\n" in body.title or "\r" in body.title or len(title) < 2:
            raise OmnigentError(
                "title must be a single non-empty line",
                code=ErrorCode.INVALID_INPUT,
            )

        page = await asyncio.to_thread(
            conversation_store.list_items,
            session_id,
            100,
            None,
            None,
            "asc",
            None,
        )
        seed_title: str | None = None
        for item in page.data:
            seed_title = synthesize_conversation_title(_title_content_from_item(item))
            if seed_title is not None:
                break
        if seed_title is None:
            return AutomaticSessionRenameResponse(renamed=False, reason="no_seed")
        if conv.title != seed_title:
            return AutomaticSessionRenameResponse(renamed=False, reason="title_changed")
        updated = await asyncio.to_thread(
            conversation_store.rename_conversation_if_title_matches,
            session_id,
            seed_title,
            title,
        )
        if updated is None:
            return AutomaticSessionRenameResponse(renamed=False, reason="title_changed")
        return AutomaticSessionRenameResponse(renamed=True, title=updated.title)

    @router.patch(
        "/sessions/{session_id}",
        response_model=None,
        responses={200: {"model": SessionResponse}},
    )
    async def update_session(
        request: Request,
        session_id: str,
        body: UpdateSessionRequest,
    ) -> SessionResponse:
        """
        Update a session's mutable fields. When ``runner_id`` is
        provided, this is the mutable affinity primitive for the Alpha
        runner-state pivot: create-bind, resume-bind, and recover-bind
        all send the currently registered runner id, and the server
        atomically replaces ``conversations.runner_id`` with that
        value using last-write-wins semantics. Title, labels, and
        reasoning-effort updates remain supported for existing
        sessions clients.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param body: The validated :class:`UpdateSessionRequest`.
        :returns: The updated :class:`SessionResponse` snapshot.
        :raises OmnigentError: 400 if the runner is not
            registered; 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        # This PATCH gates at the least level the request actually needs, in
        # three tiers matching the if/elif/else below:
        #
        # * READ — a pin-only PATCH. Pinning is a personal, per-viewer
        #   preference (stored as the caller's own ``omnigent.pinned.<user>``
        #   label), not an edit to the session, so anyone who can SEE the
        #   session may pin it — including a read-only collaborator on a session
        #   shared with them. Only when the pinned label is the request's ONLY
        #   mutation.
        # * OWNER — archiving/unarchiving or filing into a project. Both are
        #   owner-only: projects are owner-private (an editor must not move a
        #   session between them), and archive pairs with a client-driven,
        #   owner-gated stop (an editor must not hide/stop a session they can't
        #   issue that stop for). Presence is the signal for project (``""``
        #   unfiles), so gate on model_fields_set, not a non-None value.
        # * EDIT — every other field.
        #
        # Owner implies edit, so a single check at the resolved level gates all
        # three with no redundant second permission-store read.
        set_project = "project_id" in body.model_fields_set
        pin_only = body.model_fields_set == {"labels"} and set(body.labels or {}) == {
            PINNED_LABEL_KEY
        }
        if pin_only:
            required_level = LEVEL_READ
        elif body.archived is not None or set_project:
            required_level = LEVEL_OWNER
        else:
            required_level = LEVEL_EDIT
        await _require_access(
            user_id, session_id, required_level, permission_store, conversation_store
        )
        if body.archived is True:
            await _best_effort_stop(session_id, conversation_store, runner_router)
        if body.runner_id is not None and permission_store is not None:
            if not check_session_access(
                user_id, session_id, LEVEL_OWNER, permission_store, conversation_store
            ):
                raise OmnigentError(
                    f"Only the session owner can attach a runner to session {session_id!r}. "
                    f"To fork this session instead, run: omnigent run --fork {session_id}",
                    code=ErrorCode.FORBIDDEN,
                )
        if body.labels:
            _reject_server_reserved_label_seed(body.labels)
            # Advisor-owned cost_control.* labels are written only by the
            # session's bound runner; gate them on runner proof BEFORE any
            # store mutation so a rejected request leaves the session untouched.
            _reserved_labels = reserved_cost_control_keys(body.labels)
            if _reserved_labels:
                _conv_for_reserved = await asyncio.to_thread(
                    conversation_store.get_conversation, session_id
                )
                _require_cost_control_label_authority(
                    reserved_keys=_reserved_labels,
                    tunnel_token=request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER),
                    bound_runner_id=(
                        _conv_for_reserved.runner_id if _conv_for_reserved is not None else None
                    ),
                    allowed_tunnel_tokens=runner_tunnel_tokens,
                    multi_user=permission_store is not None,
                )
        collaboration_mode_requested = "collaboration_mode" in body.model_fields_set
        requested_codex_collaboration_mode: str | None = None
        conv_for_collaboration_mode: Conversation | None = None
        if collaboration_mode_requested:
            if body.collaboration_mode is None:
                raise OmnigentError(
                    "collaboration_mode must be a non-empty string",
                    code=ErrorCode.INVALID_INPUT,
                )
            if body.collaboration_mode not in _CODEX_NATIVE_COLLABORATION_MODES:
                raise OmnigentError(
                    "collaboration_mode must be one of "
                    f"{sorted(_CODEX_NATIVE_COLLABORATION_MODES)}",
                    code=ErrorCode.INVALID_INPUT,
                )
            conv_for_collaboration_mode = await asyncio.to_thread(
                conversation_store.get_conversation,
                session_id,
            )
            if conv_for_collaboration_mode is None:
                raise _session_not_found()
            if (
                conv_for_collaboration_mode.labels.get(_CLAUDE_NATIVE_WRAPPER_LABEL_KEY)
                != _CODEX_NATIVE_WRAPPER_LABEL_VALUE
            ):
                raise OmnigentError(
                    "collaboration_mode is only supported for codex-native sessions",
                    code=ErrorCode.INVALID_INPUT,
                )
            requested_codex_collaboration_mode = body.collaboration_mode
        labels_to_set = dict(body.labels or {})
        # Pins are per-user. The client writes the canonical ``omnigent.pinned``
        # key; rewrite it to the caller's per-user key so one user's pin doesn't
        # pin the session for everyone with access. Empty value (unpin) carries
        # through to the delete-clear loop below under the per-user key.
        if PINNED_LABEL_KEY in labels_to_set:
            labels_to_set[pinned_label_key(user_id)] = labels_to_set.pop(PINNED_LABEL_KEY)
        if requested_codex_collaboration_mode is not None:
            labels_to_set[_CODEX_NATIVE_COLLABORATION_MODE_LABEL_KEY] = (
                requested_codex_collaboration_mode
            )
        effort = body.reasoning_effort
        clear_effort = effort in EFFORT_CLEAR_VALUES
        if effort is not None and not clear_effort:
            try:
                effort = validate_effort(
                    effort,
                    "session metadata",
                    EFFORT_VALUES,
                )
            except ValueError as exc:
                raise OmnigentError(
                    f"invalid reasoning_effort: {exc}",
                    code=ErrorCode.INVALID_INPUT,
                ) from exc

        # Empty / whitespace strings are rejected loud — the only
        # clear path is the explicit ``default | off | reset`` alias.
        model_override = body.model_override
        clear_model = (
            isinstance(model_override, str)
            and model_override.strip().lower() in EFFORT_CLEAR_VALUES
        )
        if model_override is not None and not clear_model:
            # Mirror the create path: the persisted value reaches a native
            # CLI as a ``--model`` argv element and the Codex provider
            # ``config.toml`` as a ``model="..."`` field, so it must pass the
            # conservative model-id charset before it is stored. A bare
            # strip()/non-empty check here let shell-/TOML-shaped values
            # through, enabling host RCE via the Codex ``auth.command``.
            if not isinstance(model_override, str):
                raise OmnigentError(
                    "invalid model_override: must be a non-empty string",
                    code=ErrorCode.INVALID_INPUT,
                )
            try:
                model_override = validate_model_override(model_override)
            except ValueError as exc:
                raise OmnigentError(
                    f"invalid model_override: {exc}",
                    code=ErrorCode.INVALID_INPUT,
                ) from exc

        # Cost-control switch: ``"off"`` is a real stored value here,
        # so the clear signal is an explicit JSON null (field present,
        # value None) rather than a clear alias; an omitted field
        # leaves the stored value unchanged.
        clear_cost_control = (
            "cost_control_mode_override" in body.model_fields_set
            and body.cost_control_mode_override is None
        )
        cost_control_mode_override = _validated_cost_control_mode_override(
            body.cost_control_mode_override
        )

        # Native-terminal pass-through args: ``None`` leaves them
        # unchanged; a provided list (including ``[]``) replaces the
        # stored value wholesale (resume is last-write-wins, never an
        # append). Bounds are validated here so a malformed list fails
        # loud at the route rather than at the DB.
        try:
            terminal_launch_args = _validate_terminal_launch_args(body.terminal_launch_args)
        except ValueError as exc:
            raise OmnigentError(
                f"invalid terminal_launch_args: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc

        if body.runner_id is not None:
            # Empty string is the clear sentinel (None = leave unchanged);
            # used by /clear and /switch to move the runner between sessions.
            if body.runner_id == "":
                try:
                    await asyncio.to_thread(conversation_store.clear_runner_id, session_id)
                except ConversationNotFoundError as exc:
                    raise _session_not_found() from exc
            else:
                from omnigent.server.routes import sessions as _sf

                runner_id = _sf._registered_runner_id(
                    runner_router, body.runner_id, user_id=user_id
                )
                try:
                    await asyncio.to_thread(
                        conversation_store.replace_runner_id, session_id, runner_id
                    )
                except ConversationNotFoundError as exc:
                    raise _session_not_found() from exc
                _runner_client = await _get_runner_client(
                    session_id,
                    runner_router,
                )
                # Notify the runner about the session so it can
                # resolve the spec and cache it before the first turn.
                # This is the design doc's "Server POST /v1/sessions
                # (to runner)" step from §7 Flow: session creation.
                conv = conversation_store.get_conversation(
                    session_id,
                )
                if _runner_client is not None and conv is not None:
                    try:
                        runner_init_resp = await _runner_client.post(
                            "/v1/sessions",
                            json={
                                "session_id": session_id,
                                "agent_id": conv.agent_id,
                                "sub_agent_name": conv.sub_agent_name,
                            },
                            timeout=10.0,
                        )
                        if runner_init_resp.status_code < 400:
                            await _publish_runner_recovered_status(session_id, conversation_store)
                    except (httpx.HTTPError, ConnectionError):
                        # ConnectionError covers a tunnel close mid-POST
                        # (same source as the relay's except clause).
                        _logger.warning(
                            "Failed to notify runner about session %s",
                            session_id,
                            exc_info=True,
                        )
                if _runner_client is None:
                    # Runner deregistered between validation and
                    # lookup; PATCH still returns 200 but no
                    # relay starts, so log the silent-skip case.
                    _logger.warning(
                        "PATCH rebind to %s on session %s: no runner "
                        "client resolved; relay not restarted.",
                        runner_id,
                        session_id,
                    )
                # Restart the relay for the new runner; replaces
                # any relay still pointing at the prior runner.
                await _ensure_runner_relay_ready(
                    session_id,
                    runner_id,
                    _runner_client,
                    conversation_store,
                )
        else:
            conv = conv_for_collaboration_mode
            if conv is None:
                conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
            if conv.agent_id is None:
                raise OmnigentError(
                    "Not a session (no agent binding)",
                    code=ErrorCode.NOT_FOUND,
                )

        updated = await asyncio.to_thread(
            conversation_store.update_conversation,
            session_id,
            title=body.title,
            reasoning_effort=None if clear_effort else effort,
            _unset_reasoning_effort=clear_effort,
            model_override=None if clear_model else model_override,
            _unset_model_override=clear_model,
            cost_control_mode_override=None if clear_cost_control else cost_control_mode_override,
            _unset_cost_control_mode_override=clear_cost_control,
            terminal_launch_args=terminal_launch_args,
            archived=body.archived,
        )
        if updated is None:
            raise _session_not_found()
        # Archiving hides the session from the default view (and its unread
        # dot), so drop its per-user read-state to bound in-memory growth.
        # Only on archive→true; unarchiving leaves it pruned (reads as seen).
        if body.archived is True:
            _prune_session_read_state(session_id)
        # Notify the runner of effort / model changes so harnesses
        # that can't re-read these from store at turn boundaries
        # (today: claude-native, whose ``claude`` binary has
        # ``--effort`` / ``--model`` baked in at spawn) get a chance
        # to propagate them live. Best-effort — persisted values
        # remain the authoritative fallback. Skip both when
        # ``silent`` so bind-time auto-apply doesn't inject visible
        # ``/model X`` items into a fresh pane.
        # Effort and model both go through the unified ``/events``
        # dispatch — Omnigent server stays harness-agnostic; the runner
        # dispatches by harness (claude-native injects the slash
        # command into tmux, other harnesses 204 no-op). See
        # ``_forward_session_change_to_runner`` for the shared
        # runner-client fallback + non-2xx logging.
        live_forward = not body.silent
        if live_forward and (effort is not None or clear_effort):
            await _forward_session_change_to_runner(
                session_id,
                runner_router,
                {"type": "effort_change", "effort": updated.reasoning_effort},
            )
        if live_forward and (model_override is not None or clear_model):
            await _forward_session_change_to_runner(
                session_id,
                runner_router,
                {"type": "model_change", "model": updated.model_override},
            )
            # Append a durable [System: model changed to X] note for sessions
            # whose history Omnigent writes. Gate on the wrapper label (NOT
            # omnigent.ui, which chat-first SDK terminal-view sessions like
            # polly/debby also carry) — see _persist_model_change_note for the
            # full rationale. live_forward (== not silent) already excludes
            # bind-time auto-applies, so only an explicit /model lands a note.
            if not _is_native_terminal_session(updated):
                await _persist_model_change_note(
                    session_id,
                    updated.model_override,
                    conversation_store,
                )
        if requested_codex_collaboration_mode is not None and live_forward:
            _codex_plan_enabled = _codex_plan_mode_enabled(requested_codex_collaboration_mode)
            _runner_result = await _forward_session_change_to_runner(
                session_id,
                runner_router,
                {
                    "type": "plan_mode_change",
                    "enabled": _codex_plan_enabled,
                },
            )
            _require_collaboration_mode_forward(
                session_id,
                _codex_plan_enabled,
                _runner_result,
            )
        # Some labels are cleared by DELETE, not by upserting an empty value:
        # the project membership (empty = "remove from project") and the pinned
        # flag (empty = "unpin"). Split any empty-valued clear keys out before
        # the bulk upsert so other labels are unaffected. Labels are upsert-only,
        # so without this an empty string would linger as a stored value.
        # The pinned key was rewritten to the caller's per-user key above, so
        # clear that one (not the canonical bare key) on an empty value.
        for _clear_key in (PROJECT_LABEL_KEY, pinned_label_key(user_id)):
            if labels_to_set.get(_clear_key) == "":
                labels_to_set = {k: v for k, v in labels_to_set.items() if k != _clear_key}
                await asyncio.to_thread(conversation_store.delete_label, session_id, _clear_key)
        if labels_to_set:
            await asyncio.to_thread(conversation_store.set_labels, session_id, labels_to_set)
        if requested_codex_collaboration_mode is not None:
            _publish_collaboration_mode(
                session_id,
                requested_codex_collaboration_mode,
            )
        if body.external_session_id is not None:
            try:
                await asyncio.to_thread(
                    conversation_store.set_external_session_id,
                    session_id,
                    body.external_session_id,
                )
            except ConversationNotFoundError as exc:
                # Race: row vanished between the update above and this
                # write. Reuse the NOT_FOUND code for consistency.
                raise _session_not_found() from exc
            except ValueError as exc:
                # Store raises ValueError on attempted overwrite of an
                # already-set external_session_id — surface as
                # invalid_input so the caller (a wrapper bridge) sees a
                # 400 with the conflict explained.
                raise OmnigentError(
                    str(exc),
                    code=ErrorCode.INVALID_INPUT,
                ) from exc
        # File into a first-class project (owner-only, gated above). ``""``
        # unfiles; a non-empty id must name a project the caller owns. Filing
        # into another owner's (or a missing) project is rejected as NOT_FOUND
        # — the same 404 the projects API returns, so we don't leak existence.
        if set_project:
            # ``""`` unfiles; a non-empty id files. Explicit JSON ``null`` is
            # not a valid value here (omitting the field is how you leave
            # membership unchanged), so reject it rather than treating it as a
            # destructive unfile.
            if body.project_id is None:
                raise OmnigentError(
                    'project_id must be a project id or "" to unfile; '
                    "omit the field to leave membership unchanged",
                    code=ErrorCode.INVALID_INPUT,
                )
            target_project_id = body.project_id
            if target_project_id == "":
                unfiled = await asyncio.to_thread(
                    conversation_store.set_conversation_project, session_id, None
                )
                if not unfiled:
                    raise _session_not_found()
            else:
                if project_store is None:
                    raise OmnigentError(
                        "Filing a session into a project is not supported by this server",
                        code=ErrorCode.INVALID_INPUT,
                    )
                owned = await asyncio.to_thread(
                    project_store.get, target_project_id, owner_user_id=user_id
                )
                if owned is None:
                    raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
                filed = await asyncio.to_thread(
                    conversation_store.set_conversation_project,
                    session_id,
                    target_project_id,
                )
                if not filed:
                    raise _session_not_found()
        level = await _get_permission_level(user_id, session_id, permission_store)
        return await _get_session_snapshot(
            conversation_store,
            session_id,
            level,
            agent_store,
            agent_cache,
            liveness_lookup=liveness_lookup,
            runner_exit_reports=runner_exit_reports,
            viewer_id=user_id,
        )

    # ── POST /sessions/{source_id}/fork ─────────────────────────

    @router.post(
        "/sessions/{source_id}/fork",
        status_code=201,
        # response_model=None keeps FastAPI from re-validating/serializing
        # the handler's SessionResponse; responses= still advertises the
        # body schema to docs/SDK tooling.
        response_model=None,
        responses={201: {"model": SessionResponse}},
    )
    async def fork_session(
        request: Request,
        source_id: str,
        body: SessionForkRequest,
    ) -> SessionResponse:
        """
        Fork an existing session into a new session.

        Deep-copies the source session's conversation items and
        clones the agent into a new session. When ``body.agent_id``
        is set, the fork binds that built-in agent instead of the
        source's — switching harness (e.g. Claude-SDK → Claude Code,
        or Claude → Codex). The source's model settings carry over
        only within the same provider family; a same-family native
        target also carries conversation history (the runner rebuilds
        its transcript). The REPL/CLI binds the fork to its runner via
        ``PATCH /v1/sessions/{id}`` after creation.

        When ``body.up_to_response_id`` is set, only history up to and
        including that response is copied into the fork (a "fork from
        this response"); a native target then rebuilds its transcript
        from the truncated items instead of resuming the source's full
        native transcript.

        :param request: The incoming FastAPI request (for auth).
        :param source_id: Session/conversation identifier of the
            source session to fork, e.g. ``"conv_abc123"``.
        :param body: The validated :class:`SessionForkRequest`.
        :returns: A :class:`SessionResponse` describing the newly
            created fork (status ``"idle"``).
        :raises OmnigentError: 404 if *source_id* does not exist
            or ``body.agent_id`` is not a bindable built-in agent;
            403 if the caller lacks read access; 400 if the source
            is a sub-agent session, has no agent binding, or
            ``body.up_to_response_id`` names no response in the
            source session.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, source_id, LEVEL_READ, permission_store, conversation_store
        )
        source = access.conversation
        if source is None:
            source = await asyncio.to_thread(conversation_store.get_conversation, source_id)
            if source is None:
                raise OmnigentError(
                    f"Session not found: {source_id!r}",
                    code=ErrorCode.NOT_FOUND,
                )
        if source.kind == "sub_agent":
            raise OmnigentError(
                "Cannot fork a sub-agent session — only top-level sessions can be forked.",
                code=ErrorCode.INVALID_INPUT,
            )
        if source.agent_id is None:
            raise OmnigentError(
                "Source session has no agent binding — cannot fork.",
                code=ErrorCode.INVALID_INPUT,
            )

        source_agent = await asyncio.to_thread(agent_store.get, source.agent_id)
        if source_agent is None:
            raise OmnigentError(
                f"Source agent not found: {source.agent_id!r}",
                code=ErrorCode.NOT_FOUND,
            )

        # By default the fork clones the source's agent (same harness). When
        # ``body.agent_id`` names a different agent, the fork SWITCHES to it
        # — e.g. fork a Claude-SDK session into Claude Code. Only built-in
        # agents (``session_id IS NULL``) are bindable: a session-scoped
        # agent belongs to one conversation (possibly another user's) and
        # must never be cloned across sessions.
        base_agent = source_agent
        switching_agent = body.agent_id is not None and body.agent_id != source.agent_id
        if switching_agent:
            target_agent = await asyncio.to_thread(agent_store.get, body.agent_id)
            if target_agent is None or target_agent.session_id is not None:
                raise OmnigentError(
                    f"Agent not found or not bindable: {body.agent_id!r}",
                    code=ErrorCode.NOT_FOUND,
                )
            base_agent = target_agent

        # Clone params for the fork's session-scoped agent. Created inside
        # fork_conversation's transaction (not agent_store.create): a
        # pre-created row would survive a fork failure as an orphaned
        # session_id=NULL built-in polluting the picker. Session-scoped rows
        # are exempt from the unique built-in-name index, so the clone reuses
        # the source's name verbatim — no "(fork …)" suffix needed.
        cloned_agent_id = generate_agent_id()
        cloned_agent_name = base_agent.name

        # A model id is provider-bound, so the source's model_override /
        # reasoning_effort only carry over when the switch stays in the same
        # provider family. A cross-family switch (or an undeterminable
        # family) resets them; same-agent forks always copy.
        copy_model_settings = True
        if switching_agent:
            copy_model_settings = await asyncio.to_thread(
                _same_provider_family, source_agent, base_agent
            )

        # When the fork binds a NATIVE target, the native CLI won't replay
        # the copied Omnigent transcript on its own — mark the fork so the
        # runner carries history into the native harness. Same-family: clone
        # the source's native transcript when present, else rebuild from the
        # copied Omnigent items. Cross-family: the source's native transcript
        # is the wrong format, so ALWAYS rebuild from the copied Omnigent
        # items (the converters consume Omnigent's normalized item shape, so
        # the source harness doesn't matter). SDK targets replay the
        # transcript as context regardless, so the marker is inert for them.
        # claude/codex/pi native rebuild the transcript (each rebuilds its
        # resumable session file from the copied items, so all three sit in
        # _FORK_HISTORY_NATIVE_HARNESSES); cursor native instead replays prior
        # turns as a text preamble (its conversation is server-backed, so a
        # local store can't be seeded — fork-only, see
        # _agent_carries_cursor_fork_history). The single FORK_CARRY_HISTORY
        # label drives both; the runner branches on harness.
        target_is_cursor = await asyncio.to_thread(_agent_carries_cursor_fork_history, base_agent)
        carry_history_into_native = target_is_cursor or await asyncio.to_thread(
            _agent_carries_native_fork_history, base_agent
        )
        # The source's native session id is only resumable by a target in the
        # SAME provider family — a Claude target can't clone a Codex rollout.
        # Cross-family, the store must skip the fork-source directive so the
        # runner takes the rebuild path instead of a doomed clone attempt
        # (a failed clone launches fresh, losing history). cursor never clones a
        # native session (server-backed; it carries history via the preamble),
        # so it always skips the source directive too.
        resume_source_native_session = (
            not switching_agent or copy_model_settings
        ) and not target_is_cursor

        # On an agent switch, recompute the Web UI presentation labels for
        # the TARGET harness so the clone isn't left in the source's UI mode
        # (e.g. a claude-native source's terminal-first labels would put an
        # SDK clone in terminal mode with a stale interactive terminal).
        # A same-agent fork leaves the copied labels untouched (None).
        presentation_labels = (
            await asyncio.to_thread(_presentation_labels_for_agent, base_agent)
            if switching_agent
            else None
        )

        try:
            new_conv = await asyncio.to_thread(
                conversation_store.fork_conversation,
                source_id,
                title=body.title,
                agent_id=cloned_agent_id,
                cloned_agent_name=cloned_agent_name,
                cloned_agent_bundle_location=base_agent.bundle_location,
                cloned_agent_description=base_agent.description,
                copy_model_settings=copy_model_settings,
                # Launch flags are CLI-specific. On an agent switch the fork may
                # bind a different CLI (e.g. claude-code → pi), whose flag set
                # differs — Claude Code's ``--permission-mode`` makes pi exit at
                # launch (unknown option → ``required_terminal_exited``). Only
                # carry the source's launch args on a same-agent fork.
                copy_terminal_launch_args=not switching_agent,
                carry_history_into_native=carry_history_into_native,
                resume_source_native_session=resume_source_native_session,
                presentation_labels=presentation_labels,
                up_to_response_id=body.up_to_response_id,
            )
        except LookupError as exc:
            raise OmnigentError(
                f"Session not found: {source_id!r}",
                code=ErrorCode.NOT_FOUND,
            ) from exc
        except ValueError as exc:
            # Store raises ValueError when up_to_response_id names no
            # response in the source conversation (stale client state).
            raise OmnigentError(
                str(exc),
                code=ErrorCode.INVALID_INPUT,
            ) from exc

        if permission_store is not None and user_id is not None:
            await asyncio.to_thread(permission_store.ensure_user, user_id)
            await asyncio.to_thread(permission_store.grant, user_id, new_conv.id, LEVEL_OWNER)
        # Push the forked session to this user's other open tabs.
        _announce_session_added(user_id, new_conv.id)

        fork_items = await asyncio.to_thread(
            conversation_store.list_items, new_conv.id, limit=10000
        )
        level = await _get_permission_level(user_id, new_conv.id, permission_store)
        return _build_session_response(
            new_conv,
            fork_items.data,
            "idle",
            permission_level=level,
            last_task_error=None,
            agent_name=base_agent.name,
        )

    # ── POST /sessions/{session_id}/switch-agent ─────────────────

    @router.post(
        "/sessions/{session_id}/switch-agent",
        # response_model=None keeps FastAPI from re-validating/serializing
        # the handler's SessionResponse; responses= still advertises the
        # body schema to docs/SDK tooling.
        response_model=None,
        responses={200: {"model": SessionResponse}},
    )
    async def switch_session_agent(
        request: Request,
        session_id: str,
        body: SessionSwitchAgentRequest,
        background_tasks: BackgroundTasks,
    ) -> SessionResponse:
        """
        Switch an existing session in place to a different agent/harness.

        Unlike fork, this keeps the SAME session — transcript, comments,
        files, host, and workspace are untouched; only the agent/harness
        changes. The current session-scoped agent is replaced by a clone
        of the target built-in, model settings carry over only within the
        same provider family (a model id is provider-bound), the native
        runtime session id is cleared, and the harness-presentation labels
        are recomputed for the target. The next turn cold-starts the new
        harness (rebuilding the native transcript from this session's own
        items for a same-family native target). Only built-in agents are
        bindable, and only while the session is idle.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier to switch,
            e.g. ``"conv_abc123"``.
        :param body: The validated :class:`SessionSwitchAgentRequest`.
        :returns: A :class:`SessionResponse` describing the session after
            the switch (status ``"idle"``).
        :raises OmnigentError: 404 if the session or target agent does
            not exist or the target is not a bindable built-in; 403 if the
            caller lacks edit access; 400 if the session is a sub-agent,
            has no agent binding, or the target bundle can't be loaded;
            409 if a turn is currently running.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        session = access.conversation
        if session is None:
            session = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if session is None:
                raise OmnigentError(
                    f"Session not found: {session_id!r}",
                    code=ErrorCode.NOT_FOUND,
                )
        if session.kind == "sub_agent":
            raise OmnigentError(
                "Cannot switch the agent of a sub-agent session — only top-level "
                "sessions can switch agent.",
                code=ErrorCode.INVALID_INPUT,
            )
        if session.agent_id is None:
            raise OmnigentError(
                "Session has no agent binding — cannot switch agent.",
                code=ErrorCode.INVALID_INPUT,
            )

        # Switching mid-turn would tear the running harness subprocess out
        # from under an active stream. Reject; the caller retries when idle.
        if _session_status_from_cache(session_id) == "running":
            raise OmnigentError(
                "Session is busy — wait for the current turn to finish before switching agent.",
                code=ErrorCode.CONFLICT,
            )

        current_agent = await asyncio.to_thread(agent_store.get, session.agent_id)
        if current_agent is None:
            raise OmnigentError(
                f"Current agent not found: {session.agent_id!r}",
                code=ErrorCode.NOT_FOUND,
            )

        # Only built-in agents (``session_id IS NULL``) are bindable: a
        # session-scoped agent belongs to one conversation (possibly another
        # user's) and must never be cloned across sessions.
        target_agent = await asyncio.to_thread(agent_store.get, body.agent_id)
        if target_agent is None or target_agent.session_id is not None:
            raise OmnigentError(
                f"Agent not found or not bindable: {body.agent_id!r}",
                code=ErrorCode.NOT_FOUND,
            )

        # Reject a no-op switch to the built-in the session is already running:
        # its session-scoped clone shares the built-in's ``bundle_location``, so
        # switching would delete + re-clone the same agent and tear the terminal
        # down for nothing. The contract is that the target differs from the
        # current agent; the picker already hides the current one, so this only
        # guards a direct API call.
        if target_agent.bundle_location == current_agent.bundle_location:
            raise OmnigentError(
                "Session is already running this agent — pick a different one.",
                code=ErrorCode.INVALID_INPUT,
            )

        # Load the target bundle BEFORE committing so an unloadable spec fails
        # the request with zero mutation — the irreversible part of the switch
        # (deleting the old agent) must not run for a target that can't start.
        try:
            from omnigent.server.routes import sessions as _sessions_facade

            await asyncio.to_thread(
                _sessions_facade.get_agent_cache().load,
                target_agent.id,
                target_agent.bundle_location,
            )
        except Exception as exc:
            # Surface any bundle-load failure as a 400 before mutating state.
            raise OmnigentError(
                f"Target agent bundle could not be loaded: {body.agent_id!r}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc

        # A model id is provider-bound, so model_override / reasoning_effort
        # carry over only within the same provider family. A native target
        # carries history regardless of family: the switch clears
        # external_session_id and drops the fork-source directive, so the
        # runner rebuilds the native transcript from this session's own
        # Omnigent items (a format-agnostic conversion). SDK targets replay
        # the AP transcript as context regardless.
        copy_model_settings = await asyncio.to_thread(
            _same_provider_family, current_agent, target_agent
        )
        # claude/codex/pi native can replay fork history (each rebuilds its
        # resumable session file from the copied items); cursor-native can't
        # (no resumable session file), so don't stamp a carry-history promise
        # it would silently break with a fresh launch.
        carry_history_into_native = await asyncio.to_thread(
            _agent_carries_native_fork_history, target_agent
        )
        presentation_labels = await asyncio.to_thread(_presentation_labels_for_agent, target_agent)

        # Resolve the built-in the session is leaving so the UI can offer a
        # one-click "Switch back". The current agent is a session-scoped clone
        # whose bundle_location was copied verbatim from its source built-in,
        # so match on that. Page through the full template-agent list (not a
        # single bounded scan) so the match isn't missed when there are many
        # built-ins. Best-effort: None when no built-in matches (e.g. its
        # source built-in was removed) → no switch-back offered.
        previous_builtin_id: str | None = None
        _after: str | None = None
        while True:
            _page = await asyncio.to_thread(agent_store.list, 100, _after)
            previous_builtin_id = next(
                (a.id for a in _page.data if a.bundle_location == current_agent.bundle_location),
                None,
            )
            if previous_builtin_id is not None or not _page.has_more or not _page.data:
                break
            _after = _page.last_id

        cloned_agent_id = generate_agent_id()
        cloned_agent_name = f"{target_agent.name} (switch {cloned_agent_id[:10]})"
        try:
            updated = await asyncio.to_thread(
                conversation_store.switch_conversation_agent,
                session_id,
                new_agent_id=cloned_agent_id,
                new_agent_name=cloned_agent_name,
                new_agent_bundle_location=target_agent.bundle_location,
                new_agent_description=target_agent.description,
                copy_model_settings=copy_model_settings,
                carry_history_into_native=carry_history_into_native,
                presentation_labels=presentation_labels,
                previous_builtin_id=previous_builtin_id,
            )
        except LookupError as exc:
            raise OmnigentError(
                f"Session not found: {session_id!r}",
                code=ErrorCode.NOT_FOUND,
            ) from exc

        # Tell every connected client the binding changed so they re-derive
        # session state (presentation labels, bound agent) from a fresh
        # snapshot. Without this, a client that bound before the switch keeps
        # treating the session as the OLD harness — e.g. its status handler
        # clears the optimistic first-message bubble that a native target
        # only reconciles later via session.input.consumed.
        switch_event = SessionAgentChangedEvent(
            type="session.agent_changed",
            conversation_id=session_id,
            agent_id=cloned_agent_id,
            # Clean target name, not the clone row's "<name> (switch ag_…)":
            # the suffix only disambiguates agent rows; clients render
            # agent_name verbatim (same choice as the session snapshot).
            agent_name=target_agent.name,
        )
        # Access session_stream through the facade so monkeypatches on
        # sessions.session_stream are honored (the facade's global dict is
        # the patch target; this closure's globals are routes_core's dict).
        from omnigent.server.routes import sessions as _sessions_facade

        _sessions_facade.session_stream.publish(session_id, switch_event.model_dump())

        # Reset the OLD harness's runner-side resources (async, after the
        # response): close the cached primary OSEnv so the new agent's
        # os_env/sandbox governs the web filesystem/shell endpoints, and tear
        # down the native terminal so it can't shadow the switch-back transcript
        # rebuild. Safe because the switch only runs while the session is idle
        # (doing it mid-turn would wedge the turn); the next access
        # re-materializes from the new agent's spec, preserving the workspace /
        # worktree (cwd comes from the runner workspace).
        background_tasks.add_task(_reset_runner_resources_after_switch, session_id)

        items = await asyncio.to_thread(conversation_store.list_items, session_id, limit=10000)
        level = await _get_permission_level(user_id, session_id, permission_store)
        return _build_session_response(
            updated,
            items.data,
            "idle",
            permission_level=level,
            last_task_error=None,
            agent_name=target_agent.name,
        )
