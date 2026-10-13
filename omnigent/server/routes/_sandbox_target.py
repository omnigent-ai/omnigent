"""Resolve a session create's ``sandbox_target`` into a host, workspace, and worktree.

Each target kind is one resolver behind :func:`resolve_sandbox_target`, so a
new kind never touches the others. A resolved create is an ordinary external
create: the rest of the create path (workspace validation, worktree creation,
rollback) runs unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any, TypeVar

import click
from fastapi import Request

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.managed_hosts import parse_repo_workspace
from omnigent.server.routes._session_create_validation import validate_session_agent
from omnigent.server.routes._workspace_validation import WorkspaceValidationError, ask_host_stat
from omnigent.server.schemas import SessionGitOptions, StaticSandboxTarget
from omnigent.server.vm_hosts import ensure_vm_host, static_entry, static_launcher

if TYPE_CHECKING:
    from omnigent.onboarding.sandboxes.base import StaticSandboxProvider
    from omnigent.stores import AgentStore, ConversationStore, PermissionStore
    from omnigent.stores.project_store import ProjectStore

_Body = TypeVar("_Body")
_logger = logging.getLogger(__name__)

DEFAULT_BRANCH_PREFIX = "omnigent/"


def default_session_branch() -> str:
    """A fresh branch name, so every session (even one on ``main``) gets its own worktree."""
    return f"{DEFAULT_BRANCH_PREFIX}{uuid.uuid4().hex[:8]}"


async def _existing_checkout(
    state: Any, host_id: str, launcher: StaticSandboxProvider, sandbox_id: str, repo_url: str
) -> str | None:
    """The clone's path when the connected host already has it, else ``None``.

    The provider names the path; only the host can say whether it exists.
    """
    host_registry = getattr(state, "host_registry", None)
    if host_registry is None:
        return None
    host_conn = host_registry.get(host_id)
    if host_conn is None:
        return None
    try:
        path = await asyncio.to_thread(launcher.static_checkout_path, sandbox_id, repo_url)
    except Exception:  # noqa: BLE001 - the clone call below answers authoritatively.
        _logger.warning("could not read %s's checkout path", sandbox_id, exc_info=True)
        return None
    try:
        stat = await ask_host_stat(
            host_registry=host_registry, host_conn=host_conn, path=f"{path}/.git"
        )
    except WorkspaceValidationError:
        return None
    return path if stat.get("exists") else None


async def _resolve_static(
    body: Any, target: StaticSandboxTarget, *, user_id: str | None, request: Request
) -> Any:
    if user_id is None:
        raise OmnigentError("static sandboxes need a signed-in user", code=ErrorCode.INVALID_INPUT)
    owner = user_id
    state = request.app.state
    host_store = getattr(state, "host_store", None)
    sandbox_config = getattr(state, "sandbox_config", None)
    if host_store is None:
        raise OmnigentError(
            "Static sandboxes need a host store on this server",
            code=ErrorCode.INVALID_INPUT,
        )
    sandbox_id = target.sandbox_id
    host_id = await ensure_vm_host(
        owner=owner,
        provider=target.provider,
        sandbox_id=sandbox_id,
        sandbox_config=sandbox_config,
        host_store=host_store,
    )
    git: SessionGitOptions | None = body.git
    workspace: str | None = body.workspace
    base_branch: str | None = None
    if workspace is None or not workspace.startswith("/"):
        launcher = static_launcher(sandbox_config, owner, target.provider).sandboxes
        repo_url = workspace or str(
            (await static_entry(launcher, sandbox_id)).get("default_repo_url") or ""
        )
        if not repo_url:
            raise OmnigentError(
                f"{sandbox_id} has no primary repository; choose one for the session",
                code=ErrorCode.INVALID_INPUT,
            )
        try:
            repo = parse_repo_workspace(repo_url)
        except ValueError as exc:
            raise OmnigentError(
                f"invalid repository {repo_url!r}: {exc}", code=ErrorCode.INVALID_INPUT
            ) from exc
        base_branch = repo.branch
        # Ask the connected host first: a clone that already exists needs no
        # shim exec, which costs ~15s even when it changes nothing.
        workspace = await _existing_checkout(state, host_id, launcher, sandbox_id, repo.url)
        if workspace is None:
            try:
                # The branch is the worktree's base, never checked out in the shared clone.
                workspace = await asyncio.to_thread(
                    launcher.materialize_workspace,
                    sandbox_id,
                    workspace="",
                    repo_url=repo.url,
                    repo_branch=None,
                    repo_name=repo.repo_name,
                )
            except click.ClickException as exc:
                raise OmnigentError(
                    f"Couldn't clone {repo.url}: {exc.message}",
                    code=ErrorCode.WORKSPACE_PREPARE_FAILED,
                ) from exc
    if git is None:
        git = SessionGitOptions(branch_name=default_session_branch(), base_branch=base_branch)
    elif (
        git.base_branch is None
        and base_branch
        and not (git.existing_branch or git.existing_worktree)
    ):
        git = git.model_copy(update={"base_branch": base_branch})
    # Resolved: the rest of the create is an ordinary external create, and the
    # body must still validate when a project re-checks it (target XOR host_id).
    return body.model_copy(
        update={"host_id": host_id, "workspace": workspace, "git": git, "sandbox_target": None}
    )


async def precheck_sandbox_target_create(
    body: Any,
    *,
    user_id: str | None,
    agent_store: AgentStore,
    permission_store: PermissionStore | None,
    conversation_store: ConversationStore,
    project_store: ProjectStore | None,
) -> None:
    """Fail a targeted create before resolving it when its agent or project is bad.

    Only database reads: the caller must be signed in, the project (if any)
    must be theirs, and the agent (explicit or the project's) must be bindable.
    The full create validation still runs after the target resolves.

    :raises OmnigentError: ``INVALID_INPUT`` without a signed-in caller, 404
        for an unknown or foreign project or agent.
    """
    if user_id is None:
        raise OmnigentError("static sandboxes need a signed-in user", code=ErrorCode.INVALID_INPUT)
    agent_id: str | None = getattr(body, "agent_id", None)
    project_id: str | None = getattr(body, "project_id", None)
    if project_id is not None:
        project = (
            await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
            if project_store is not None
            else None
        )
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        if agent_id is None:
            agent_id = project.config.get("agent_id")
    if agent_id:
        await validate_session_agent(
            user_id=user_id,
            agent_id=agent_id,
            agent_store=agent_store,
            permission_store=permission_store,
            conversation_store=conversation_store,
        )


async def resolve_sandbox_target(body: _Body, *, user_id: str | None, request: Request) -> _Body:
    """Fill in ``host_id`` / ``workspace`` / ``git`` from ``body.sandbox_target``.

    Returns *body* unchanged when it names no target.

    :raises OmnigentError: ``INVALID_INPUT`` without a signed-in caller, 404
        when the target is not the caller's, ``SANDBOX_GONE`` when its VM is
        gone, 503 when the sandbox service is unavailable,
        ``HOST_START_FAILED`` / ``WORKSPACE_PREPARE_FAILED`` when the host or
        the clone fails.
    """
    target = getattr(body, "sandbox_target", None)
    if target is None:
        return body
    if isinstance(target, StaticSandboxTarget):
        return await _resolve_static(body, target, user_id=user_id, request=request)
    raise OmnigentError(
        f"unsupported sandbox_target kind {target.kind!r}", code=ErrorCode.INVALID_INPUT
    )
