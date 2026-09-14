"""Session-free, read-only workspace resources served by a connected host."""

from __future__ import annotations

import asyncio
import ntpath
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from omnigent.entities.session_resources import (
    DEFAULT_ENVIRONMENT_ID,
    default_environment_resource,
    session_resource_view_to_dict,
)
from omnigent.server.auth import AuthProvider
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._host_filesystem import (
    HostFsError,
    HostFsUnavailableError,
    read_workspace_from_host,
)
from omnigent.server.routes._host_launch import host_absent_error, resolve_host_owner
from omnigent.server.routes._workspace_validation import (
    WorkspaceValidationError,
    _ask_host_stat,
)
from omnigent.stores.host_store import HostStore


def register_host_workspace_resource_routes(
    router: APIRouter,
    *,
    host_registry: HostRegistry,
    host_store: HostStore,
    auth_provider: AuthProvider | None,
) -> None:
    """Register owner-only workspace reads that do not require a session."""

    def _validate_workspace(workspace: str) -> str:
        if "\x00" in workspace:
            raise HTTPException(status_code=400, detail="workspace must not contain NUL bytes")
        if workspace.startswith("~"):
            return workspace
        if workspace.startswith("/") or ntpath.isabs(workspace):
            return workspace
        raise HTTPException(
            status_code=400,
            detail="workspace must be an absolute or tilde-prefixed path",
        )

    async def _authorize_host(
        request: Request,
        host_id: str,
        workspace: str,
    ) -> tuple[HostConnection, str]:
        user_id = require_user(request, auth_provider)
        host = await asyncio.to_thread(
            resolve_host_owner, user_id=user_id, host_id=host_id, host_store=host_store
        )
        workspace = _validate_workspace(workspace)
        conn = host_registry.get(host.host_id)
        if conn is None:
            raise host_absent_error(host)
        return conn, workspace

    async def _read(
        request: Request,
        host_id: str,
        workspace: str,
        *,
        op: str,
        params: dict[str, Any],
        environment_id: str | None = None,
    ) -> dict[str, Any]:
        conn, workspace = await _authorize_host(request, host_id, workspace)
        if environment_id is not None:
            _require_default_environment(environment_id)
        try:
            return await read_workspace_from_host(
                host_registry=host_registry,
                host_conn=conn,
                op=op,
                workspace=workspace,
                # An empty id is intentional: no session exists before launch.
                # Git workspaces reconstruct changes from disk; non-git
                # workspaces have no session edit registry and report none.
                session_id="",
                params=params,
            )
        except HostFsError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc
        except HostFsUnavailableError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    async def _resolve_workspace(
        conn: HostConnection,
        workspace: str,
    ) -> str:
        """Verify a workspace directory and return the host's canonical path."""
        try:
            result = await _ask_host_stat(
                host_registry=host_registry,
                host_conn=conn,
                path=workspace,
            )
        except WorkspaceValidationError as exc:
            raise HTTPException(status_code=502, detail=exc.message) from exc
        if not result.get("exists"):
            raise HTTPException(
                status_code=404, detail="workspace directory does not exist on host"
            )
        if result.get("type") != "directory":
            raise HTTPException(status_code=400, detail="workspace must be a directory")
        canonical = result.get("canonical_path")
        if not isinstance(canonical, str) or not canonical:
            raise HTTPException(
                status_code=502, detail="host returned no canonical workspace path"
            )
        return canonical

    def _require_default_environment(environment_id: str) -> None:
        if environment_id != DEFAULT_ENVIRONMENT_ID:
            raise HTTPException(
                status_code=404,
                detail=f"Environment {environment_id!r} not found",
            )

    def _environment(workspace: str) -> dict[str, object]:
        resource = default_environment_resource("")
        payload = session_resource_view_to_dict(resource)
        metadata = dict(resource.metadata)
        metadata.update(
            {
                "root": workspace,
                "reachable": {
                    "unconfined": False,
                    "roots": [{"path": workspace, "access": "read", "origin": "cwd"}],
                },
            }
        )
        payload["metadata"] = metadata
        return payload

    def _resource_page(workspace: str) -> dict[str, object]:
        environment = _environment(workspace)
        return {
            "object": "list",
            "data": [environment],
            "first_id": DEFAULT_ENVIRONMENT_ID,
            "last_id": DEFAULT_ENVIRONMENT_ID,
            "has_more": False,
        }

    @router.get("/hosts/{host_id}/workspace/resources", response_model=None)
    async def list_workspace_resources(
        request: Request,
        host_id: str,
        workspace: str = Query(..., min_length=1),
    ) -> dict[str, object]:
        """Return the selected workspace's single logical environment."""
        conn, workspace = await _authorize_host(request, host_id, workspace)
        workspace = await _resolve_workspace(conn, workspace)
        return _resource_page(workspace)

    @router.get("/hosts/{host_id}/workspace/resources/environments", response_model=None)
    async def list_workspace_environments(
        request: Request,
        host_id: str,
        workspace: str = Query(..., min_length=1),
    ) -> dict[str, object]:
        """Return the selected workspace's default environment."""
        conn, workspace = await _authorize_host(request, host_id, workspace)
        workspace = await _resolve_workspace(conn, workspace)
        return _resource_page(workspace)

    @router.get(
        "/hosts/{host_id}/workspace/resources/environments/{environment_id}",
        response_model=None,
    )
    async def get_workspace_environment(
        request: Request,
        host_id: str,
        environment_id: str,
        workspace: str = Query(..., min_length=1),
    ) -> dict[str, object]:
        """Describe the root and reach of the selected workspace."""
        conn, workspace = await _authorize_host(request, host_id, workspace)
        _require_default_environment(environment_id)
        workspace = await _resolve_workspace(conn, workspace)
        return _environment(workspace)

    @router.get(
        "/hosts/{host_id}/workspace/resources/environments/{environment_id}/filesystem",
        response_model=None,
    )
    async def list_workspace_root(
        request: Request,
        host_id: str,
        environment_id: str,
        workspace: str = Query(..., min_length=1),
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> dict[str, Any]:
        """List the selected workspace root."""
        return await _read(
            request,
            host_id,
            workspace,
            op="list_or_read",
            environment_id=environment_id,
            params={
                "path": "",
                "limit": limit,
                "after": after,
                "before": before,
                "order": order,
            },
        )

    @router.get(
        "/hosts/{host_id}/workspace/resources/environments/{environment_id}"
        "/filesystem/{relative_path:path}",
        response_model=None,
    )
    async def read_or_list_workspace_path(
        request: Request,
        host_id: str,
        environment_id: str,
        relative_path: str,
        workspace: str = Query(..., min_length=1),
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> dict[str, Any]:
        """Read a file or list a directory below the selected workspace."""
        return await _read(
            request,
            host_id,
            workspace,
            op="list_or_read",
            environment_id=environment_id,
            params={
                "path": relative_path,
                "limit": limit,
                "after": after,
                "before": before,
                "order": order,
            },
        )

    async def _search(
        request: Request,
        host_id: str,
        environment_id: str,
        workspace: str,
        path: str,
        *,
        q: str,
        include: str | None,
        exclude: str | None,
        limit: int,
    ) -> dict[str, Any]:
        return await _read(
            request,
            host_id,
            workspace,
            op="search",
            environment_id=environment_id,
            params={
                "path": path,
                "q": q,
                "include": include,
                "exclude": exclude,
                "limit": limit,
            },
        )

    @router.get(
        "/hosts/{host_id}/workspace/resources/environments/{environment_id}/search",
        response_model=None,
    )
    async def search_workspace(
        request: Request,
        host_id: str,
        environment_id: str,
        workspace: str = Query(..., min_length=1),
        q: str = Query(min_length=1, pattern=r".*\S.*"),
        include: str | None = Query(default=None),
        exclude: str | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=500),
    ) -> dict[str, Any]:
        """Search the selected workspace recursively."""
        return await _search(
            request,
            host_id,
            environment_id,
            workspace,
            "",
            q=q,
            include=include,
            exclude=exclude,
            limit=limit,
        )

    @router.get(
        "/hosts/{host_id}/workspace/resources/environments/{environment_id}/search/{path:path}",
        response_model=None,
    )
    async def search_workspace_under_path(
        request: Request,
        host_id: str,
        environment_id: str,
        path: str,
        workspace: str = Query(..., min_length=1),
        q: str = Query(min_length=1, pattern=r".*\S.*"),
        include: str | None = Query(default=None),
        exclude: str | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=500),
    ) -> dict[str, Any]:
        """Search recursively under one selected-workspace directory."""
        return await _search(
            request,
            host_id,
            environment_id,
            workspace,
            path,
            q=q,
            include=include,
            exclude=exclude,
            limit=limit,
        )

    @router.get(
        "/hosts/{host_id}/workspace/resources/environments/{environment_id}/changes",
        response_model=None,
    )
    async def list_workspace_changes(
        request: Request,
        host_id: str,
        environment_id: str,
        workspace: str = Query(..., min_length=1),
    ) -> dict[str, Any]:
        """List git working-tree changes for the selected workspace."""
        return await _read(
            request,
            host_id,
            workspace,
            op="changes",
            params={},
            environment_id=environment_id,
        )

    @router.get(
        "/hosts/{host_id}/workspace/resources/environments/{environment_id}"
        "/diff/{relative_path:path}",
        response_model=None,
    )
    async def read_workspace_diff(
        request: Request,
        host_id: str,
        environment_id: str,
        relative_path: str,
        workspace: str = Query(..., min_length=1),
    ) -> dict[str, Any]:
        """Return before/after content for a working-tree change."""
        return await _read(
            request,
            host_id,
            workspace,
            op="diff",
            params={"path": relative_path},
            environment_id=environment_id,
        )

    @router.get("/hosts/{host_id}/workspace/resources/github", response_model=None)
    async def get_workspace_github(
        request: Request,
        host_id: str,
        workspace: str = Query(..., min_length=1),
        pr_url: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """Return repository, branch, and pull-request context."""
        return await _read(
            request,
            host_id,
            workspace,
            op="github_info",
            params={"pr_url": pr_url} if pr_url else {},
        )

    @router.get("/hosts/{host_id}/workspace/resources/github/changes", response_model=None)
    async def list_workspace_github_changes(
        request: Request,
        host_id: str,
        workspace: str = Query(..., min_length=1),
        pr_url: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """List files changed by the selected pull request."""
        return await _read(
            request,
            host_id,
            workspace,
            op="github_changes",
            params={"pr_url": pr_url} if pr_url else {},
        )

    @router.get("/hosts/{host_id}/workspace/resources/github/diff", response_model=None)
    async def read_workspace_github_diff(
        request: Request,
        host_id: str,
        workspace: str = Query(..., min_length=1),
        pr_url: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """Return the selected pull request's unified patch."""
        return await _read(
            request,
            host_id,
            workspace,
            op="github_pr_diff",
            params={"pr_url": pr_url} if pr_url else {},
        )

    @router.get(
        "/hosts/{host_id}/workspace/resources/github/diff/{relative_path:path}",
        response_model=None,
    )
    async def read_workspace_github_file_diff(
        request: Request,
        host_id: str,
        relative_path: str,
        workspace: str = Query(..., min_length=1),
        base: str | None = Query(default=None),
        pr_url: str | None = Query(default=None),
        previous_path: str | None = Query(default=None),
        head_sha: str | None = Query(default=None),
        base_sha: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """Return before/after content for one pull-request file."""
        return await _read(
            request,
            host_id,
            workspace,
            op="github_diff",
            params={
                key: value
                for key, value in {
                    "path": relative_path,
                    "base": base,
                    "pr_url": pr_url,
                    "previous_path": previous_path,
                    "head_sha": head_sha,
                    "base_sha": base_sha,
                }.items()
                if value is not None
            },
        )
