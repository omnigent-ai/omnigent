"""Agent-facing worktree routes (``/v1/sessions/{id}/worktrees``).

Back the ``sys_worktree_create`` / ``sys_worktree_remove`` tools, so an agent
that needs a second working tree (a fan-out worker per task, a clean tree to
build in) goes through Omnigent instead of shelling out to ``git worktree
add``. Routing it here is what makes the project's worktree settings apply to
an agent-made worktree too: it lands under the project's ``worktree_root``
rather than wherever the agent invented, and the project's setup / teardown
scripts run around it.

Authority comes from the SESSION, not from parameters: the repository is
always the one the calling session already works in (its ``workspace``), and a
removal target must be a linked worktree of that same repository. So these
routes grant an agent no reach it did not already have — only a consistent,
observable way to use it.

Both scripts are fail-open, as everywhere else: their outcome is reported in
the response (which is the agent's tool result, so the agent finally SEES it)
but never fails the create or blocks the removal.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request

from omnigent.entities.conversation import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_EDIT, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id as _get_user_id
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._host_worktree import (
    WorktreeHookOutcome,
    WorktreeHostUnavailableError,
    WorktreeProxyError,
    create_worktree_on_host,
    list_worktrees_on_host,
    remove_worktree_on_host,
    run_worktree_hook_on_host,
)
from omnigent.server.schemas import (
    CreateSessionWorktreeRequest,
    DeleteSessionWorktreeRequest,
)
from omnigent.server.worktree_hooks import (
    POST_CREATE_HOOK,
    PRE_DELETE_HOOK,
    WorktreeHookConfig,
    hook_config_for_conversation,
    worktree_root_for_conversation,
)
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore


def _normalized(path: str) -> str:
    """Normalize a host path for comparison against ``git``'s own output.

    Collapses ``..``, doubled separators, and a trailing slash textually.
    Deliberately not :func:`os.path.realpath` — the server cannot see the
    host's filesystem, and both sides of every comparison here are paths
    git reported.

    :param path: A host path, e.g. ``"/repo/.worktrees/feature/"``.
    :returns: The normalized form, e.g. ``"/repo/.worktrees/feature"``.
    """
    return os.path.normpath(path)


def _hook_payload(
    outcome: WorktreeHookOutcome | None,
    error: str | None,
) -> dict[str, Any]:
    """Project a lifecycle script's result into the tool-visible shape.

    :param outcome: What the host reported, or ``None`` when the script
        could not be run at all.
    :param error: Why it could not run, e.g. ``"host went offline"``.
        ``None`` when it ran.
    :returns: ``{"ran", "ok", "exit_code", "timed_out", "output_tail",
        "error"}``.
    """
    if outcome is None:
        return {
            "ran": False,
            "ok": False,
            "exit_code": None,
            "timed_out": False,
            "output_tail": "",
            "error": error,
        }
    return {
        "ran": True,
        "ok": outcome.ok,
        "exit_code": outcome.exit_code,
        "timed_out": outcome.timed_out,
        "output_tail": outcome.output_tail,
        "error": None,
    }


@dataclass(frozen=True)
class _WorktreeOpTarget:
    """The authorized session a worktree operation acts for.

    Carries the host and repository already narrowed to non-``None``, so
    the handlers never re-check what
    :func:`_session_for_worktree_op` has guaranteed.

    :param conv: The session row (for the project lookup).
    :param user_id: The requesting user, for the owner-scoped project read.
    :param host_id: The host that owns the repository.
    :param repo_path: The session's workspace — the ONLY repository these
        routes will touch.
    """

    conv: Conversation
    user_id: str | None
    host_id: str
    repo_path: str


async def _session_for_worktree_op(
    *,
    request: Request,
    session_id: str,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> _WorktreeOpTarget:
    """Resolve and authorize the session a worktree operation runs for.

    :param request: The inbound request, used for identity extraction.
    :param session_id: Session/conversation identifier.
    :param conversation_store: Store the session is read from.
    :param auth_provider: Auth provider, or ``None`` in single-user mode.
    :param permission_store: Store backing the access check, or ``None``.
    :returns: The session, its user, and its host + repository.
    :raises OmnigentError: 404 when no such session; ``conflict`` when the
        session has no host + workspace to branch from (a sandbox or
        not-yet-bound session).
    """
    user_id = _get_user_id(request, auth_provider)
    await _require_access_and_level(
        user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
    )
    conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
    if conv is None:
        raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND)
    if conv.host_id is None or not conv.workspace:
        raise OmnigentError(
            "this session is not bound to a host workspace, so it has no repository "
            "to create a worktree in",
            code=ErrorCode.CONFLICT,
        )
    return _WorktreeOpTarget(
        conv=conv,
        user_id=user_id,
        host_id=conv.host_id,
        repo_path=conv.workspace,
    )


async def _run_hook_for_worktree(
    *,
    command: str,
    hook: str,
    worktree_path: str,
    branch: str | None,
    repo_path: str,
    timeout_seconds: float,
    host_conn: Any,
    host_registry: Any,
) -> dict[str, Any]:
    """Run one lifecycle script in a worktree, fail-open.

    :param command: The project's configured script.
    :param hook: ``"post_create"`` or ``"pre_delete"``.
    :param worktree_path: Directory to run it in.
    :param branch: Branch checked out there, exported to the script.
    :param repo_path: The session's repository, exported to the script.
    :param timeout_seconds: The project's clamped hook timeout.
    :param host_conn: Live host connection.
    :param host_registry: Server-side host registry.
    :returns: The tool-visible hook payload; a host failure becomes an
        ``error`` in it rather than an exception.
    """
    try:
        outcome = await run_worktree_hook_on_host(
            host_registry=host_registry,
            host_conn=host_conn,
            command=command,
            worktree_path=worktree_path,
            hook=hook,
            repo_path=repo_path,
            branch=branch,
            timeout_seconds=timeout_seconds,
        )
    except (WorktreeProxyError, WorktreeHostUnavailableError) as exc:
        return _hook_payload(None, exc.message)
    return _hook_payload(outcome, None)


def register_worktree_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    """Register the session worktree routes on ``router``.

    :param router: The sessions router to attach to.
    :param conversation_store: Store sessions are read from.
    :param auth_provider: Auth provider, or ``None`` in single-user mode.
    :param permission_store: Store backing the access check, or ``None``.
    """

    async def _hook_config(target: _WorktreeOpTarget, request: Request) -> WorktreeHookConfig:
        """Resolve the project's lifecycle scripts for a session.

        :param target: The authorized session the operation acts for.
        :param request: FastAPI request carrying ``app.state``.
        :returns: The project's hook config.
        """
        return await asyncio.to_thread(
            hook_config_for_conversation,
            conv=target.conv,
            user_id=target.user_id,
            project_store=getattr(request.app.state, "project_store", None),
        )

    @router.post("/sessions/{session_id}/worktrees", response_model=None)
    async def create_session_worktree(
        request: Request,
        session_id: str,
        body: CreateSessionWorktreeRequest,
    ) -> dict[str, Any]:
        """
        Create a worktree off the calling session's repository.

        Places it under the project's ``worktree_root``, forks it from the
        CALLING session's own branch by default, and runs the project's
        setup script in it, reporting the script's outcome in the response
        rather than only on the session's event stream — the caller here
        is an agent, and this is its tool result.

        :param request: The inbound request, used for identity extraction.
        :param session_id: The calling session, which supplies the
            repository, the default base branch, and the authority.
        :param body: ``branch_name`` plus an optional ``base_branch``.
        :returns: ``{"object", "worktree_path", "branch", "base_branch",
            "setup"}``, where ``setup`` is ``None`` when the project
            configures no setup script.
        :raises OmnigentError: 404 unknown session; ``invalid_input`` for
            a bad branch name or a host-reported git failure (duplicate
            branch, bad base ref); ``conflict`` when the session has no
            workspace or its host is unreachable.
        """
        from omnigent.host.git_worktree import WorktreeError, validate_branch_name
        from omnigent.server.routes._sessions.helpers import _require_host_conn_for_worktree

        target = await _session_for_worktree_op(
            request=request,
            session_id=session_id,
            conversation_store=conversation_store,
            auth_provider=auth_provider,
            permission_store=permission_store,
        )
        try:
            validate_branch_name(body.branch_name)
        except WorktreeError as exc:
            raise OmnigentError(exc.message, code=ErrorCode.INVALID_INPUT) from exc

        host_conn = _require_host_conn_for_worktree(target.host_id, request)
        host_registry = request.app.state.host_registry
        worktree_root = await asyncio.to_thread(
            worktree_root_for_conversation,
            conv=target.conv,
            user_id=target.user_id,
            project_store=getattr(request.app.state, "project_store", None),
        )
        # Anchor the fan-out on the CALLING session's tree. The host resolves
        # every worktree off the MAIN work tree, so a bare `git worktree add -b`
        # would fork from the main checkout's HEAD and silently discard whatever
        # the orchestrator's own session worktree is sitting on. Its branch is
        # the relevant base; an explicit base_branch still wins.
        base_branch = body.base_branch or target.conv.git_branch
        try:
            created = await create_worktree_on_host(
                host_registry=host_registry,
                host_conn=host_conn,
                repo_path=target.repo_path,
                branch_name=body.branch_name,
                base_branch=base_branch,
                worktree_root=worktree_root,
            )
        except WorktreeHostUnavailableError as exc:
            raise OmnigentError(exc.message, code=ErrorCode.CONFLICT) from exc
        except WorktreeProxyError as exc:
            raise OmnigentError(exc.message, code=ErrorCode.INVALID_INPUT) from exc

        config = await _hook_config(target, request)
        setup: dict[str, Any] | None = None
        if config.post_create_command is not None:
            setup = await _run_hook_for_worktree(
                command=config.post_create_command,
                hook=POST_CREATE_HOOK,
                worktree_path=created.worktree_path,
                branch=created.branch,
                repo_path=target.repo_path,
                timeout_seconds=config.timeout_seconds,
                host_conn=host_conn,
                host_registry=host_registry,
            )
        return {
            "object": "worktree",
            "worktree_path": created.worktree_path,
            "branch": created.branch,
            # Echoed so the caller can see what it actually forked from
            # without having to know the defaulting rule.
            "base_branch": base_branch,
            "setup": setup,
        }

    @router.delete("/sessions/{session_id}/worktrees", response_model=None)
    async def delete_session_worktree(
        request: Request,
        session_id: str,
        body: DeleteSessionWorktreeRequest,
    ) -> dict[str, Any]:
        """
        Remove a worktree of the calling session's repository.

        Runs the project's teardown script first, then removes the
        directory. Refuses anything that is not a linked worktree of this
        session's repository, and refuses the session's own workspace
        (which would pull the tree out from under the running agent).

        :param request: The inbound request, used for identity extraction.
        :param session_id: The calling session, which supplies both the
            repository and the authority.
        :param body: ``worktree_path`` plus optional ``delete_branch``.
        :returns: ``{"object", "worktree_path", "deleted", "teardown"}``,
            where ``teardown`` is ``None`` when the project configures no
            teardown script.
        :raises OmnigentError: 404 unknown session; ``invalid_input`` when
            the path is not a removable worktree of this repository;
            ``conflict`` when the host is unreachable.
        """
        from omnigent.server.routes._sessions.helpers import _require_host_conn_for_worktree

        target = await _session_for_worktree_op(
            request=request,
            session_id=session_id,
            conversation_store=conversation_store,
            auth_provider=auth_provider,
            permission_store=permission_store,
        )
        wanted = _normalized(body.worktree_path)
        if not os.path.isabs(wanted):
            # A relative path is resolved against the session's own workspace,
            # which is how an agent naturally refers to a sibling worktree.
            wanted = _normalized(os.path.join(target.repo_path, body.worktree_path))
        if wanted == _normalized(target.repo_path):
            raise OmnigentError(
                "refusing to remove the worktree this session is running in",
                code=ErrorCode.INVALID_INPUT,
            )

        host_conn = _require_host_conn_for_worktree(target.host_id, request)
        host_registry = request.app.state.host_registry
        try:
            entries = await list_worktrees_on_host(
                host_registry=host_registry,
                host_conn=host_conn,
                repo_path=target.repo_path,
            )
        except WorktreeHostUnavailableError as exc:
            raise OmnigentError(exc.message, code=ErrorCode.CONFLICT) from exc
        except WorktreeProxyError as exc:
            raise OmnigentError(exc.message, code=ErrorCode.INVALID_INPUT) from exc

        match = next(
            (
                entry
                for entry in entries
                if isinstance(entry.get("path"), str)
                and _normalized(str(entry["path"])) == wanted
                and not entry.get("is_main")
            ),
            None,
        )
        if match is None:
            # Covers both "not a worktree at all" and "the main work tree",
            # deliberately: an agent has no business learning which.
            raise OmnigentError(
                f"{body.worktree_path!r} is not a removable worktree of this session's repository",
                code=ErrorCode.INVALID_INPUT,
            )
        # Act on the path GIT reported, not the caller's spelling of it — the
        # match only proves the two normalize to the same directory.
        resolved = str(match["path"])
        branch = match.get("branch")
        branch_name = branch if isinstance(branch, str) else None

        config = await _hook_config(target, request)
        teardown: dict[str, Any] | None = None
        if config.pre_delete_command is not None:
            teardown = await _run_hook_for_worktree(
                command=config.pre_delete_command,
                hook=PRE_DELETE_HOOK,
                worktree_path=resolved,
                branch=branch_name,
                repo_path=target.repo_path,
                timeout_seconds=config.timeout_seconds,
                host_conn=host_conn,
                host_registry=host_registry,
            )
        try:
            await remove_worktree_on_host(
                host_registry=host_registry,
                host_conn=host_conn,
                worktree_path=resolved,
                branch=branch_name,
                delete_branch=body.delete_branch,
                # An agent may only delete a branch whose work already landed
                # on its own branch. Deleting a fanned-out task branch before
                # integrating it would destroy the very work the fan-out
                # produced; a human deleting a session's worktree from the UI
                # is a deliberate act and stays unconditional.
                require_merged_into=target.conv.git_branch,
            )
        except WorktreeHostUnavailableError as exc:
            raise OmnigentError(exc.message, code=ErrorCode.CONFLICT) from exc
        except WorktreeProxyError as exc:
            raise OmnigentError(exc.message, code=ErrorCode.INVALID_INPUT) from exc
        return {
            "object": "worktree.deleted",
            "worktree_path": resolved,
            "deleted": True,
            "teardown": teardown,
        }
