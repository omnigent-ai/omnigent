"""REST API routes for hosts (``/v1/hosts``).

Provides endpoints for listing connected hosts and launching
runners on them. The Web UI uses these to let users pick a host
for a new session and trigger runner spawning.

Per ``designs/DAEMON_API.md``, host registration is persisted in
the ``hosts`` DB table, which is the cross-replica source of
truth for ``status``. The in-memory ``HostRegistry`` is
per-replica and is used here only when a route needs the live
``HostConnection`` on the current replica (e.g. proxying a
``host.list_dir`` frame). The list/get endpoints answer purely
from the DB so a host connected to replica B reads back as
``"online"`` from replica A.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from omnigent.db.utils import now_epoch
from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness
from omnigent.host.frames import (
    HARNESS_NOT_CONFIGURED_ERROR_CODE,
    HostCreateDirFrame,
    HostLaunchRunnerFrame,
    HostListDirFrame,
    encode_host_frame,
)
from omnigent.runner.identity import token_bound_runner_id
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import AuthProvider
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._host_launch import resolve_host_launch
from omnigent.server.schemas import SessionGitOptions
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.host_store import HostStore, host_is_live
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)

_LAUNCH_RESULT_TIMEOUT_S = 30.0
# Per-call timeout for host.list_dir round-trips. Listing is a single
# scandir + sort on the host side; 5s is generous for transient
# network slowness without making the picker feel hung.
_LIST_DIR_TIMEOUT_S = 5.0
_LIST_DIR_DEFAULT_LIMIT = 20
_LIST_DIR_MAX_LIMIT = 1000
# Per-call timeout for host.create_dir round-trips. mkdir is a single
# fast syscall on the host side; 5s matches list_dir and is generous
# for transient network slowness without making the picker feel hung.
_CREATE_DIR_TIMEOUT_S = 5.0


async def _proxy_list_dir(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    path: str,
    limit: int,
    after: str | None,
    before: str | None,
) -> dict[str, Any]:
    """
    Send a ``host.list_dir`` frame and await the result.

    Mirrors the structure of the workspace validator's
    ``_ask_host_stat``: enqueue the frame, register a future on
    the host connection's ``pending_list_dirs`` map, await with a
    timeout, and clean up in a finally block. The host's WS
    receive loop in ``host_tunnel.py`` resolves the future when
    the result frame arrives.

    :param host_registry: Server-side registry; used to enqueue
        the outbound frame on the host's send queue.
    :param host_conn: Live host connection.
    :param path: Absolute or tilde-prefixed path. The host
        expands ``~`` itself.
    :param limit: Max entries per page; clamped by the route.
    :param after: Optional forward-pagination cursor (entry path).
    :param before: Optional backward-pagination cursor.
    :returns: Dict with the result fields:
        ``status`` (``"ok"`` or ``"failed"``), ``entries`` (list
        of dicts), ``has_more`` (bool), ``error`` (string or
        ``None``).
    :raises HTTPException: 504 on timeout, 502 on connection drop
        or unexpected I/O failure on the host.
    """
    request_id = secrets.token_hex(8)
    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict[str, Any]] = loop.create_future()
    host_conn.pending_list_dirs[request_id] = future

    frame = encode_host_frame(
        HostListDirFrame(
            request_id=request_id,
            path=path,
            limit=limit,
            after=after,
            before=before,
        )
    )
    try:
        try:
            host_registry.send_text(host_conn, frame)
        except ConnectionError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"host '{host_conn.host_id}' connection lost",
            ) from exc
        try:
            return await asyncio.wait_for(future, timeout=_LIST_DIR_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail=(
                    f"host '{host_conn.host_id}' did not respond to list_dir "
                    f"within {_LIST_DIR_TIMEOUT_S:.0f}s"
                ),
            ) from exc
    finally:
        # Cleanup runs on every path so a cancelled caller doesn't
        # leave an orphan in the pending dict.
        host_conn.pending_list_dirs.pop(request_id, None)


async def _proxy_create_dir(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    path: str,
) -> dict[str, Any]:
    """
    Send a ``host.create_dir`` frame and await the result.

    Mirrors :func:`_proxy_list_dir`: enqueue the frame, register a
    future on the host connection's ``pending_create_dirs`` map, await
    with a timeout, and clean up in a finally block. The host's WS
    receive loop in ``host_tunnel.py`` resolves the future when the
    result frame arrives.

    :param host_registry: Server-side registry; used to enqueue the
        outbound frame on the host's send queue.
    :param host_conn: Live host connection.
    :param path: Absolute or tilde-prefixed directory to create. The
        host expands ``~`` itself.
    :returns: Dict with the result fields: ``status`` (``"ok"`` or
        ``"failed"``), ``path`` (created absolute path or ``None``),
        ``error`` (string or ``None``).
    :raises HTTPException: 504 on timeout, 502 on connection drop.
    """
    request_id = secrets.token_hex(8)
    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict[str, Any]] = loop.create_future()
    host_conn.pending_create_dirs[request_id] = future

    frame = encode_host_frame(
        HostCreateDirFrame(
            request_id=request_id,
            path=path,
        )
    )
    try:
        try:
            host_registry.send_text(host_conn, frame)
        except ConnectionError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"host '{host_conn.host_id}' connection lost",
            ) from exc
        try:
            return await asyncio.wait_for(future, timeout=_CREATE_DIR_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail=(
                    f"host '{host_conn.host_id}' did not respond to create_dir "
                    f"within {_CREATE_DIR_TIMEOUT_S:.0f}s"
                ),
            ) from exc
    finally:
        # Cleanup runs on every path so a cancelled caller doesn't
        # leave an orphan in the pending dict.
        host_conn.pending_create_dirs.pop(request_id, None)


class CreateDirectoryRequest(BaseModel):
    """Request body for ``POST /v1/hosts/{host_id}/directories``.

    :param path: Absolute path of the directory to create on the host
        machine, e.g. ``"/Users/corey/projects/new-app"``, or a
        tilde-prefixed path (``"~/scratch"``) the host expands against
        its own process owner. Missing parents are created.
    """

    path: str


class LaunchRunnerRequest(BaseModel):
    """Request body for ``POST /v1/hosts/{host_id}/runners``.

    :param session_id: Session to bind the new runner to, e.g.
        ``"conv_abc123"``.
    :param workspace: Absolute path on the host machine to use
        as the runner's working directory, e.g.
        ``"/Users/corey/projects/frontend"``. When ``git`` is set,
        this is interpreted as the source repository directory and
        the runner starts in the created worktree instead.
    :param git: Optional git worktree options. In create mode the
        server creates a worktree for a new branch off ``workspace`` on
        the host and binds the runner to it (the fork-resume path;
        mirrors ``POST /v1/sessions``). In bind mode
        (``existing_worktree=True``) ``workspace`` already IS a
        worktree — no worktree is created; ``branch_name`` is recorded
        as the session's ``git_branch`` for display and opt-in cleanup.
        ``None`` binds ``workspace`` directly. ``host_id`` is always
        present (it is in the path), so no host check is needed here.
    """

    session_id: str
    workspace: str
    git: SessionGitOptions | None = None


async def _resolve_agent_spec_cwd(
    conv: Conversation,
    agent_store: AgentStore,
    agent_cache: AgentCache,
) -> str | None:
    """
    Read the bound agent's ``os_env.cwd`` for workspace-boundary checks.

    :param conv: The session/conversation a runner is launching for.
    :param agent_store: Store to resolve ``conv.agent_id`` to an agent.
    :param agent_cache: Cache to load the agent's parsed spec.
    :returns: The agent's ``os_env.cwd`` (absolute or relative), or
        ``None`` when the session has no agent, no bundle, or no
        ``os_env`` block (headless / unconstrained boundary).
    """
    if conv.agent_id is None:
        return None
    agent = await asyncio.to_thread(agent_store.get, conv.agent_id)
    if agent is None or agent.bundle_location is None:
        return None
    loaded = await asyncio.to_thread(agent_cache.load, agent.id, agent.bundle_location)
    os_env = getattr(loaded.spec, "os_env", None)
    return getattr(os_env, "cwd", None) if os_env is not None else None


async def _resolve_agent_harness(
    conv: Conversation,
    agent_store: AgentStore,
    agent_cache: AgentCache,
) -> str | None:
    """
    Read the bound agent's canonical harness for the launch frame.

    Mirrors :func:`_resolve_agent_spec_cwd` — same resolution chain,
    different spec field. The harness rides on the
    ``host.launch_runner`` frame so the host can refuse an
    unconfigured harness before spawning.

    :param conv: The session/conversation a runner is launching for.
    :param agent_store: Store to resolve ``conv.agent_id`` to an agent.
    :param agent_cache: Cache to load the agent's parsed spec.
    :returns: The canonical harness id, e.g. ``"claude-sdk"``, or
        ``None`` when the session has no agent or no bundle (the host
        then skips the configuration check — fail open).
    """
    if conv.agent_id is None:
        return None
    agent = await asyncio.to_thread(agent_store.get, conv.agent_id)
    if agent is None or agent.bundle_location is None:
        return None
    loaded = await asyncio.to_thread(agent_cache.load, agent.id, agent.bundle_location)
    return canonicalize_harness(loaded.spec.executor.harness_kind)


def create_hosts_router(
    host_registry: HostRegistry,
    host_store: HostStore,
    conversation_store: ConversationStore,
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    agent_store: AgentStore | None = None,
    agent_cache: AgentCache | None = None,
) -> APIRouter:
    """Build the router for host REST endpoints.

    Mounted with ``prefix="/v1"`` so paths are ``/v1/hosts/...``.

    :param host_registry: In-memory registry of live host
        connections on this replica.
    :param host_store: Persistent store for host registrations.
    :param conversation_store: Conversation store for reading and
        updating session rows (runner_id, host_id).
    :param auth_provider: Optional auth provider for user identity.
    :param permission_store: Session permission store, used to verify
        the caller owns the session a runner is launched for. ``None``
        disables the session-owner check (single-user/local).
    :param agent_store: Agent store used to resolve a session's agent
        for workspace-boundary validation on runner launch (W6). When
        ``None`` (non-production wiring), the boundary check is skipped;
        :func:`omnigent.server.app.create_app` always supplies it.
    :param agent_cache: Agent-spec cache used to read the agent's
        ``os_env.cwd`` boundary. Paired with ``agent_store``.
    :returns: A FastAPI router with host endpoints.
    """
    router = APIRouter()

    @router.get("/hosts")
    async def list_hosts(request: Request) -> dict[str, list[dict[str, Any]]]:
        """List all hosts owned by the authenticated user.

        Returns both online and offline hosts, with live runner
        information for online hosts.

        :param request: The incoming request (for auth).
        :returns: ``{"hosts": [...]}`` with host details.
        """
        # require_user: unauthenticated callers 401. user_id is None
        # only when auth is disabled entirely — there the single-user
        # server's hosts are owned by the reserved "local" user.
        user_id = require_user(request, auth_provider)
        if user_id is None:
            hosts = await asyncio.to_thread(host_store.list_hosts, "local")
        else:
            hosts = await asyncio.to_thread(host_store.list_hosts, user_id)

        # One clock for the whole batch so every host is classified
        # against a consistent "now" (host_is_live's documented idiom).
        now = now_epoch()
        result: list[dict[str, Any]] = []
        for host in hosts:
            # Status comes from the DB, not host_registry. The registry
            # is per-replica; if a host is connected to replica B and
            # this request lands on replica A, A's registry won't know
            # about it. The hosts table is the cross-replica source of
            # truth — written by the tunnel endpoint on the replica
            # that owns the connection (upsert_on_connect / set_offline).
            # A stored "online" is only trusted if the host was seen
            # recently: a crashed host never runs set_offline and would
            # otherwise show as online forever in the picker.
            result.append(
                {
                    "host_id": host.host_id,
                    "name": host.name,
                    "owner": host.user_id,
                    "status": "online" if host_is_live(host, now=now) else "offline",
                    # Non-None marks a server-managed sandbox host (e.g.
                    # "modal"). Clients use it to hide sandbox-backed
                    # hosts from manual host pickers — they are launch
                    # targets the server creates on demand, not
                    # user-connectable machines.
                    "sandbox_provider": host.sandbox_provider,
                    "configured_harnesses": host.configured_harnesses,
                }
            )
        return {"hosts": result}

    @router.get("/hosts/{host_id}")
    async def get_host(request: Request, host_id: str) -> dict[str, Any]:
        """Get details for a single host.

        :param request: The incoming request (for auth).
        :param host_id: Host identifier, e.g.
            ``"host_a1b2c3d4..."``.
        :returns: Host details dict.
        :raises HTTPException: 404 if the host does not exist.
        """
        # require_user: with an auth provider configured, an
        # unauthenticated caller must get 401 here — get_user_id would
        # return None and the ownership check below would be skipped,
        # exposing another user's host. user_id is None only when auth
        # is disabled entirely (single-user server).
        user_id = require_user(request, auth_provider)
        host = await asyncio.to_thread(host_store.get_host, host_id)
        if host is None:
            raise HTTPException(status_code=404, detail="host not found")
        if user_id is not None and host.user_id != user_id:
            raise HTTPException(status_code=403, detail="not your host")

        # Status comes from the DB so the answer is consistent across
        # replicas, gated on the liveness freshness window — see
        # list_hosts above for the full rationale.
        return {
            "host_id": host.host_id,
            "name": host.name,
            "owner": host.user_id,
            "status": "online" if host_is_live(host) else "offline",
            # Same semantics as list_hosts: non-None marks a
            # server-managed sandbox host (e.g. "modal").
            "sandbox_provider": host.sandbox_provider,
            "configured_harnesses": host.configured_harnesses,
            "runners": [],
        }

    @router.post("/hosts/{host_id}/runners")
    async def launch_runner(
        request: Request,
        host_id: str,
        body: LaunchRunnerRequest,
    ) -> dict[str, Any]:
        """Launch a runner on a host for a session.

        Generates a binding token, writes the expected runner_id
        to the session row, sends the launch command to the host,
        and waits for the host's acknowledgement.

        :param request: The incoming request (for auth).
        :param host_id: Target host, e.g. ``"host_a1b2c3d4..."``.
        :param body: Launch request with ``session_id`` and
            ``workspace``.
        :returns: ``{"runner_id": ..., "status": "launching"}``.
        :raises HTTPException: 404 if host not found, 409 if host
            offline, 403 if caller doesn't own the host, 400 if
            session already has a runner.
        """
        # require_user: resolve_host_launch skips its ownership checks
        # for user_id=None (the auth-disabled single-user case), so an
        # unauthenticated caller slipping through as None could launch
        # a runner on another user's host. 401 instead.
        user_id = require_user(request, auth_provider)

        # Authorize against BOTH the host and the session before
        # spawning anything (see _host_launch for the threat model).
        target = await asyncio.to_thread(
            resolve_host_launch,
            user_id=user_id,
            host_id=host_id,
            session_id=body.session_id,
            host_store=host_store,
            host_registry=host_registry,
            conversation_store=conversation_store,
            permission_store=permission_store,
        )
        conn = target.conn

        # W6: validate the requested workspace against the agent's
        # os_env.cwd sandbox boundary BEFORE binding — the same check
        # POST /v1/sessions enforces. Without it, an owner could bind a
        # workspace outside the agent's declared boundary via this
        # shortcut and escape the sandbox. validate_workspace also
        # canonicalizes the path (realpath) for storage. Skipped only
        # when the router was wired without an agent cache (non-prod
        # test wiring); create_app always supplies one.
        workspace = body.workspace
        if agent_store is not None and agent_cache is not None:
            from omnigent.server.routes._workspace_validation import (
                WorkspaceValidationError,
                validate_workspace,
            )

            spec_cwd = await _resolve_agent_spec_cwd(target.conv, agent_store, agent_cache)
            try:
                workspace = await validate_workspace(
                    host_registry=host_registry,
                    host_id=host_id,
                    workspace=body.workspace,
                    spec_cwd=spec_cwd,
                    host_name_for_errors=target.host.name,
                )
            except WorkspaceValidationError as exc:
                raise HTTPException(status_code=400, detail=exc.message) from exc
        else:
            _logger.warning(
                "launch_runner: workspace boundary validation skipped for "
                "session %s (router built without an agent cache)",
                body.session_id,
            )

        # Optional git worktree: when the caller asks to branch, create a
        # worktree off the validated source repo and bind the runner to
        # the worktree path instead (the fork-resume path; mirrors
        # POST /v1/sessions). Created BEFORE the atomic runner bind so a
        # lost CAS or a failed launch can roll it back, leaving no orphan
        # worktree on the host.
        git_branch: str | None = None
        # CreatedWorktree | None — set ONLY when Omnigent creates a worktree
        # (create mode). Left None in bind mode so the rollback below never
        # force-removes the user's pre-existing worktree.
        worktree = None
        if body.git is not None:
            from omnigent.host.git_worktree import (
                WorktreeError,
                validate_branch_name,
            )

            # Shared by both modes — the host never runs git in bind mode, so
            # the server is the only gate on the name there.
            try:
                validate_branch_name(body.git.branch_name)
            except WorktreeError as exc:
                raise HTTPException(status_code=400, detail=exc.message) from exc

            if body.git.existing_worktree:
                # Binding to a pre-existing worktree: no worktree is created,
                # but record its branch so the sidebar shows it and the opt-in
                # delete flow can offer to remove it.
                git_branch = body.git.branch_name
            else:
                from omnigent.server.routes._host_worktree import (
                    WorktreeHostUnavailableError,
                    WorktreeProxyError,
                    create_worktree_on_host,
                )

                try:
                    worktree = await create_worktree_on_host(
                        host_registry=host_registry,
                        host_conn=conn,
                        repo_path=workspace,
                        branch_name=body.git.branch_name,
                        base_branch=body.git.base_branch,
                    )
                except WorktreeHostUnavailableError as exc:
                    # Host offline / unresponsive — infra, not user input.
                    raise HTTPException(status_code=409, detail=exc.message) from exc
                except WorktreeProxyError as exc:
                    # Host-reported git failure (dup branch, bad base, not a
                    # repo) — user-correctable input.
                    raise HTTPException(status_code=400, detail=exc.message) from exc
                workspace = worktree.worktree_path
                git_branch = worktree.branch

        async def _rollback_worktree() -> None:
            """
            Best-effort removal of the worktree created above.

            Called when the runner bind or launch fails after the
            worktree was created, so a failed request leaves no orphan
            worktree (and no orphan branch) on the host. Never raises —
            a cleanup failure is logged and the original error still
            propagates.
            """
            if worktree is None:
                return
            from omnigent.server.routes._host_worktree import (
                WorktreeProxyError,
                remove_worktree_on_host,
            )

            try:
                await remove_worktree_on_host(
                    host_registry=host_registry,
                    host_conn=conn,
                    worktree_path=worktree.worktree_path,
                    branch=worktree.branch,
                    delete_branch=True,
                )
            except WorktreeProxyError:
                _logger.warning(
                    "Best-effort worktree rollback failed for session %s (%s)",
                    body.session_id,
                    worktree.worktree_path,
                    exc_info=True,
                )

        async def _rollback_failed_launch() -> None:
            """
            Undo a failed launch *after* the runner was atomically bound.

            Fully unbinds the session — NULLs ``runner_id`` plus the
            ``host_id`` / ``workspace`` / ``git_branch`` persisted by the
            ``set_host_id`` call below — and rolls back any worktree
            created for this launch. Clearing the binding (not just
            ``runner_id``) keeps the DB consistent with the host's actual
            state: the worktree is gone, so the row must not keep pointing
            at it, and a retry that omits a worktree starts from a clean
            slate rather than inheriting a stale ``git_branch`` (which
            ``set_host_id`` cannot clear). ``POST /hosts/{id}/runners`` only
            binds a previously-unbound clone (the fork-resume picker), so a
            full unbind restores the true pre-call state. Used only on the
            post-bind failure paths; the lost-CAS path must NOT clear the
            binding because it belongs to the concurrent winner, not us.
            """
            await asyncio.to_thread(conversation_store.clear_host_binding, body.session_id)
            await _rollback_worktree()

        binding_token = secrets.token_urlsafe(32)
        runner_id = token_bound_runner_id(binding_token)

        # Atomic bind (UPDATE ... WHERE runner_id IS NULL): only one
        # concurrent launch can bind an unbound session; a second (or an
        # already-bound session) gets False. Closes the TOCTOU.
        bound = await asyncio.to_thread(
            conversation_store.set_runner_id,
            body.session_id,
            runner_id,
        )
        if not bound:
            await _rollback_worktree()
            raise HTTPException(
                status_code=400,
                detail="session already has a runner bound",
            )
        # Persist the validated, canonical workspace (the worktree path
        # when a worktree was created) alongside host_id, plus git_branch
        # when branching, so the conversation row satisfies
        # ck_conversations_workspace_required_for_host. ``workspace`` is the
        # realpath returned by validate_workspace (W6), or body.workspace
        # verbatim only in non-production wiring without an agent cache.
        await asyncio.to_thread(
            conversation_store.set_host_id,
            body.session_id,
            host_id,
            workspace,
            git_branch,
        )

        request_id = secrets.token_hex(8)
        future: asyncio.Future[dict[str, str | None]] = asyncio.get_running_loop().create_future()
        conn.pending_launches[request_id] = future

        # Resolve the agent's harness so the host can refuse an
        # unconfigured one before spawning (mirrors POST /v1/sessions).
        # None — no agent cache wired, or no resolvable agent — skips
        # the host-side check.
        harness: str | None = None
        if agent_store is not None and agent_cache is not None:
            harness = await _resolve_agent_harness(target.conv, agent_store, agent_cache)
        launch_frame = encode_host_frame(
            HostLaunchRunnerFrame(
                request_id=request_id,
                binding_token=binding_token,
                workspace=workspace,
                session_id=body.session_id,
                harness=harness,
            )
        )
        try:
            host_registry.send_text(conn, launch_frame)
        except ConnectionError:
            conn.pending_launches.pop(request_id, None)
            await _rollback_failed_launch()
            raise HTTPException(
                status_code=409,
                detail="host connection was replaced",
            ) from None

        try:
            result = await asyncio.wait_for(
                future,
                timeout=_LAUNCH_RESULT_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            conn.pending_launches.pop(request_id, None)
            await _rollback_failed_launch()
            raise HTTPException(
                status_code=504,
                detail="host did not respond to launch request",
            ) from None

        if result.get("status") == "failed":
            await _rollback_failed_launch()
            if result.get("error_code") == HARNESS_NOT_CONFIGURED_ERROR_CODE:
                # Categorical refusal: the harness isn't configured on
                # the host, so a retry can't succeed without user action
                # (`omnigent setup` on the host machine). Surface the
                # specific code (412) instead of the generic 502.
                raise OmnigentError(
                    f"host failed to launch runner: {result.get('error')}",
                    code=ErrorCode.HARNESS_NOT_CONFIGURED,
                )
            raise HTTPException(
                status_code=502,
                detail=f"host failed to launch runner: {result.get('error')}",
            )

        return {
            "runner_id": runner_id,
            "status": "launching",
        }

    @router.get("/hosts/{host_id}/filesystem")
    async def list_host_filesystem_root(
        request: Request,
        host_id: str,
        limit: int = Query(default=_LIST_DIR_DEFAULT_LIMIT, ge=1, le=_LIST_DIR_MAX_LIMIT),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """
        List the contents of the host daemon's home directory.

        Empty trailing path → forward ``~`` to the host (the host
        expands against its own process owner). Used by the
        Web UI's directory picker to show the "root" view.

        :param request: FastAPI request (for auth).
        :param host_id: Host identifier, e.g.
            ``"host_a1b2c3d4..."``.
        :param limit: Max entries per page.
        :param after: Optional forward pagination cursor (entry
            path), e.g. ``"/Users/corey/projects/m"``.
        :param before: Optional backward pagination cursor.
        :returns: ``{"object": "list", "data": [...], "has_more": bool}``
            mirroring the existing session-scoped filesystem
            endpoint shape.
        :raises HTTPException: 404 if host not found, 403 if not
            owned by caller, 409 if host is offline, 504 on host
            timeout, 502 on host I/O failure.
        """
        return await _list_host_filesystem(
            request=request,
            host_id=host_id,
            path="~",
            limit=limit,
            after=after,
            before=before,
        )

    @router.get("/hosts/{host_id}/filesystem/{path:path}")
    async def list_host_filesystem(
        request: Request,
        host_id: str,
        path: str,
        limit: int = Query(default=_LIST_DIR_DEFAULT_LIMIT, ge=1, le=_LIST_DIR_MAX_LIMIT),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """
        List the contents of a directory on a host.

        Used by the Web UI's directory picker (and stat-style
        existence checks) to render the host's filesystem before
        any runner exists. Owner-scoped: only the host owner can
        browse. NOT scoped to a session — this endpoint exposes
        the entire host filesystem to the authenticated host owner
        per ``designs/SESSION_WORKSPACE_SELECTION.md`` "Security
        surface".

        :param request: FastAPI request (for auth).
        :param host_id: Host identifier.
        :param path: Absolute path on the host (e.g.
            ``"/Users/corey/universe"``) OR a tilde-prefixed
            path (``"~/foo"``). The host expands ``~`` itself.
            FastAPI's ``:path`` converter strips the leading
            ``/`` from the URL, so we re-add it for absolute paths.
        :param limit: Max entries per page.
        :param after: Optional forward pagination cursor.
        :param before: Optional backward pagination cursor.
        :returns: ``{"object": "list", "data": [...], "has_more": bool}``.
        :raises HTTPException: 404 (host or path missing), 403
            (not owner), 409 (offline), 400 (path validation),
            504 (timeout), 502 (host I/O).
        """
        # FastAPI's :path converter strips the leading slash from
        # the URL match. Re-add it unless the path is tilde-prefixed
        # (~/foo stays tilde-prefixed; /Users/x becomes Users/x → /Users/x).
        if not path.startswith("~"):
            path = "/" + path
        return await _list_host_filesystem(
            request=request,
            host_id=host_id,
            path=path,
            limit=limit,
            after=after,
            before=before,
        )

    async def _list_host_filesystem(
        *,
        request: Request,
        host_id: str,
        path: str,
        limit: int,
        after: str | None,
        before: str | None,
    ) -> dict[str, Any]:
        """
        Shared implementation for the filesystem endpoints.

        Authorizes (owner check), looks up the live host, validates
        the path shape, sends ``host.list_dir``, and returns the
        result in the runner-compatible response shape.

        :param request: FastAPI request (for auth).
        :param host_id: Host identifier.
        :param path: Already-normalized path (absolute or tilde).
        :param limit: Max entries.
        :param after: Forward cursor.
        :param before: Backward cursor.
        :returns: Listing dict with ``object``, ``data``, ``has_more``.
        :raises HTTPException: See per-route docstrings for codes.
        """
        # require_user: unauthenticated callers 401 instead of slipping
        # past the owner check below as None (see get_host above).
        user_id = require_user(request, auth_provider)

        # Owner check: load the host record, fail with 404 if it
        # doesn't exist (don't leak existence to non-owners), fail
        # with 403 only when an authenticated caller doesn't own it.
        host = await asyncio.to_thread(host_store.get_host, host_id)
        if host is None:
            raise HTTPException(status_code=404, detail="host not found")
        if user_id is not None and host.user_id != user_id:
            raise HTTPException(status_code=403, detail="not your host")

        if "\x00" in path:
            raise HTTPException(
                status_code=400,
                detail="path must not contain NUL bytes",
            )

        conn = host_registry.get(host.host_id)
        if conn is None:
            raise HTTPException(status_code=409, detail="host is offline")

        result = await _proxy_list_dir(
            host_registry=host_registry,
            host_conn=conn,
            path=path,
            limit=limit,
            after=after,
            before=before,
        )

        if result.get("status") == "failed":
            # Unexpected I/O failure on the host.
            raise HTTPException(
                status_code=502,
                detail=f"host list_dir failed: {result.get('error') or 'unknown error'}",
            )

        # Missing path (host returned ok with an error message) maps
        # to 404 so the Web UI can distinguish "browse a path that
        # doesn't exist" from "host is broken".
        if result.get("error") and not result.get("entries"):
            raise HTTPException(
                status_code=404,
                detail=str(result.get("error")),
            )

        # Shape mirrors GET /v1/sessions/{id}/resources/environments/default/filesystem
        # so the Web UI can reuse fetchWorkspaceDirectory etc.
        return {
            "object": "list",
            "data": result.get("entries", []),
            "has_more": bool(result.get("has_more", False)),
        }

    @router.post("/hosts/{host_id}/directories")
    async def create_host_directory(
        request: Request,
        host_id: str,
        body: CreateDirectoryRequest,
    ) -> dict[str, Any]:
        """
        Create a new directory on a host.

        Backs the Web UI workspace picker's "New folder" action so a
        user can make a fresh directory to start a session in without
        dropping to a terminal. Owner-scoped exactly like the
        filesystem browse endpoints (``GET /v1/hosts/{id}/filesystem``):
        only the host owner can create directories, and — like browse —
        this is NOT scoped to a session. The workspace-boundary check
        still runs at session-create time, so creating a directory here
        does not by itself grant an agent access to it.

        :param request: FastAPI request (for auth).
        :param host_id: Host identifier, e.g. ``"host_a1b2c3d4..."``.
        :param body: Request body carrying the absolute (or
            tilde-prefixed) ``path`` to create.
        :returns: ``{"object": "directory", "path": "<created abs path>"}``.
        :raises HTTPException: 404 if host not found, 403 if not owned
            by caller, 409 if host is offline or the directory could not
            be created (already exists / permission denied), 400 on path
            validation, 504 on host timeout, 502 on host I/O failure.
        """
        # require_user: unauthenticated callers 401 instead of slipping
        # past the owner check below as None.
        user_id = require_user(request, auth_provider)

        host = await asyncio.to_thread(host_store.get_host, host_id)
        if host is None:
            raise HTTPException(status_code=404, detail="host not found")
        if user_id is not None and host.user_id != user_id:
            raise HTTPException(status_code=403, detail="not your host")

        path = body.path
        if not path.strip():
            raise HTTPException(status_code=400, detail="path must not be empty")
        if "\x00" in path:
            raise HTTPException(
                status_code=400,
                detail="path must not contain NUL bytes",
            )
        # Absolute or tilde-prefixed only — the host needs a path it can
        # resolve on its own; a relative path has no stable meaning here.
        if not path.startswith(("/", "~")):
            raise HTTPException(
                status_code=400,
                detail="path must be absolute or tilde-prefixed",
            )

        conn = host_registry.get(host.host_id)
        if conn is None:
            raise HTTPException(status_code=409, detail="host is offline")

        result = await _proxy_create_dir(
            host_registry=host_registry,
            host_conn=conn,
            path=path,
        )

        if result.get("status") == "failed":
            # Unexpected I/O failure on the host.
            raise HTTPException(
                status_code=502,
                detail=f"host create_dir failed: {result.get('error') or 'unknown error'}",
            )
        # Expected filesystem error (already exists / permission denied /
        # parent is a file) → 409 Conflict with the host's message, so
        # the picker can show "directory already exists" inline.
        if result.get("error"):
            raise HTTPException(
                status_code=409,
                detail=str(result.get("error")),
            )

        return {
            "object": "directory",
            "path": result.get("path"),
        }

    @router.get("/hosts/{host_id}/worktrees")
    async def list_host_worktrees(
        request: Request,
        host_id: str,
        path: str = Query(...),
    ) -> dict[str, Any]:
        """
        List the git worktrees of a repository on a host.

        Used by the Web UI's new-session worktree picker to show the
        worktrees a session can start in directly. Owner-scoped exactly
        like the filesystem browse endpoints; NOT scoped to a session.
        A path that is not a git repository is reported as 400 so the
        picker can quietly fall back to "no worktrees".

        :param request: FastAPI request (for auth).
        :param host_id: Host identifier, e.g. ``"host_a1b2c3d4..."``.
        :param path: Absolute path inside the repo on the host to list
            worktrees for, e.g. ``"/Users/alice/myrepo"``.
        :returns: ``{"object": "list", "data": [{path, branch,
            is_main, detached}, ...]}`` (main first).
        :raises HTTPException: 404 if host not found, 403 if not owned
            by caller, 409 if host is offline/unresponsive, 400 on path
            validation or a non-git path.
        """
        from omnigent.server.routes._host_worktree import (
            WorktreeHostUnavailableError,
            WorktreeProxyError,
            list_worktrees_on_host,
        )

        # require_user: unauthenticated callers 401 instead of slipping
        # past the owner check below as None.
        user_id = require_user(request, auth_provider)

        host = await asyncio.to_thread(host_store.get_host, host_id)
        if host is None:
            raise HTTPException(status_code=404, detail="host not found")
        if user_id is not None and host.user_id != user_id:
            raise HTTPException(status_code=403, detail="not your host")

        if not path.strip():
            raise HTTPException(status_code=400, detail="path must not be empty")
        if "\x00" in path:
            raise HTTPException(status_code=400, detail="path must not contain NUL bytes")

        conn = host_registry.get(host.host_id)
        if conn is None:
            raise HTTPException(status_code=409, detail="host is offline")

        try:
            worktrees = await list_worktrees_on_host(
                host_registry=host_registry,
                host_conn=conn,
                repo_path=path,
            )
        except WorktreeHostUnavailableError as exc:
            raise HTTPException(status_code=409, detail=exc.message) from exc
        except WorktreeProxyError as exc:
            # Not a git repo / git failure — user-correctable; the picker
            # treats this as "no worktrees here".
            raise HTTPException(status_code=400, detail=exc.message) from exc

        return {"object": "list", "data": worktrees}

    return router
