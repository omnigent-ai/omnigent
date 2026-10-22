"""Opt-in, controller-owned working directories for reusable sandbox sessions."""

from __future__ import annotations

import asyncio

from fastapi import HTTPException

from omnigent.onboarding.sandboxes.base import SandboxHostLauncher
from omnigent.onboarding.sandboxes.types import RepoWorkspace, SandboxError
from omnigent.server.managed_hosts import (
    ManagedSandboxDeployment,
    _apply_git_clone_options,
    build_owner_launcher,
    parse_repo_workspace,
)
from omnigent.server.routes._workspace_validation import _is_unc_path, _is_windows_absolute_path
from omnigent.server.schemas import SandboxWorkspaceRequest
from omnigent.stores.host_store import Host


def session_workspace_launcher(
    config: ManagedSandboxDeployment | None,
    provider: str | None,
    owner: str,
    selection: SandboxWorkspaceRequest,
) -> tuple[SandboxHostLauncher, list[RepoWorkspace]]:
    """Validate capability and repo limits before creating a session or sandbox."""
    entry = config.for_provider(provider) if config is not None else None
    if entry is None or config is None:
        raise HTTPException(400, "sandbox provider is not configured")
    launcher = build_owner_launcher(entry, config, owner)
    if not launcher.capabilities.reusable_hosts or not launcher.capabilities.session_workspaces:
        raise HTTPException(400, "sandbox provider does not support session workspaces")
    if len(selection.repositories) > 1 and not launcher.capabilities.multi_repo:
        raise HTTPException(400, "sandbox provider supports only one repository")
    repos = [parse_repo_workspace(value) for value in selection.repositories]
    return launcher, list(_apply_git_clone_options(launcher, repos, entry.git_clone))


async def prepare_sandbox_session_workspace(
    *,
    host: Host,
    config: ManagedSandboxDeployment | None,
    session_id: str,
    selection: SandboxWorkspaceRequest,
) -> str:
    """Prepare a directory after caller ownership has been checked by the route."""
    if not host.sandbox_attached or host.sandbox_id is None or host.sandbox_provider is None:
        raise HTTPException(400, "session workspaces require a saved static sandbox")
    launcher, repos = session_workspace_launcher(
        config, host.sandbox_provider, host.user_id, selection
    )
    try:
        workspace = await asyncio.to_thread(
            launcher.prepare_session_workspace,
            host.sandbox_id,
            session_id=session_id,
            repos=repos,
            create_worktrees=selection.create_worktrees,
        )
    except PermissionError as exc:
        raise HTTPException(403, "sandbox workspace preparation not authorized") from exc
    except SandboxError as exc:
        raise HTTPException(502, "sandbox workspace preparation failed") from exc
    if (
        not isinstance(workspace, str)
        or not workspace
        or "\x00" in workspace
        or not (
            workspace.startswith("/")
            or _is_windows_absolute_path(workspace)
            or _is_unc_path(workspace)
        )
    ):
        raise HTTPException(502, "sandbox controller returned an invalid working directory")
    return workspace
