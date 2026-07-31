"""Routes for git-imported agents: import and refresh.

POST /v1/agents/import-git    — clone a repo on a host, bundle, validate, register.
POST /v1/agents/{id}/refresh  — re-clone on the same host that imported it.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from pydantic import BaseModel

from omnigent.db.utils import generate_agent_id
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import AuthProvider, local_single_user_enabled
from omnigent.server.bundles import bundle_location, validate_agent_bundle
from omnigent.server.host_registry import HostConnection
from omnigent.server.routes._auth_helpers import require_user as _require_user
from omnigent.server.routes._host_git_import import (
    ClonedBundle,
    GitImportHostUnavailableError,
    GitImportProxyError,
    clone_and_bundle_on_host,
)
from omnigent.server.routes.builtin_agents import _to_agent_object
from omnigent.server.schemas import AgentObject
from omnigent.stores import AgentStore
from omnigent.stores.artifact_store import ArtifactStore


def _require_host_conn(host_id: str | None, request: Request) -> HostConnection:
    """Resolve a live host connection for a git-import operation.

    :param host_id: Target host id from the request body, e.g.
        ``"host_a1b2c3d4..."``. ``None`` is rejected — git import always
        requires a host.
    :param request: FastAPI request carrying ``app.state.host_registry``.
    :returns: The live :class:`HostConnection` for ``host_id``.
    :raises OmnigentError: ``invalid_input`` when ``host_id`` is
        ``None``; ``internal_error`` when no host registry is
        configured; ``conflict`` when the host is offline.
    """
    if host_id is None:
        raise OmnigentError(
            "git import requires host_id; select an online host",
            code=ErrorCode.INVALID_INPUT,
        )
    host_registry = getattr(request.app.state, "host_registry", None)
    if host_registry is None:
        raise OmnigentError(
            "host registry is not configured; cannot perform git import",
            code=ErrorCode.INTERNAL_ERROR,
        )
    host_conn = host_registry.get(host_id)
    if host_conn is None:
        raise OmnigentError(
            f"host {host_id!r} is offline; reconnect the host and try again",
            code=ErrorCode.CONFLICT,
        )
    return host_conn


class ImportGitBody(BaseModel):
    git_url: str
    git_ref: str | None = None
    git_subpath: str | None = None
    host_id: str


def create_agents_router(
    agent_store: AgentStore,
    agent_cache: AgentCache,
    artifact_store: ArtifactStore,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the router for git agent import/refresh routes.

    Mounted with ``prefix="/v1"`` so the final path is
    ``/v1/agents/import-git``.

    :param agent_store: Store for persisting registered agents.
    :param agent_cache: Cache for loading agent specs.
    :param artifact_store: Store for persisting agent bundles.
    :param auth_provider: Optional auth provider; when set, the caller
        must be authenticated.
    :returns: An :class:`APIRouter` with the git-import routes.
    """
    router = APIRouter()

    @router.post("/agents/import-git")
    async def import_git(request: Request, body: ImportGitBody) -> AgentObject:
        """Clone a git repo on the given host, bundle, validate, and register.

        The clone is always performed on a host (never server-side).  The
        ``host_id`` in the request body must refer to a host that is currently
        online; if it is offline the route returns 409 CONFLICT.

        :param request: Incoming FastAPI request.
        :param body: Import spec with ``git_url``, optional ``git_ref``,
            optional ``git_subpath``, and required ``host_id``.
        :returns: The newly registered :class:`AgentObject`.
        :raises OmnigentError: 400 INVALID_INPUT for bad URL, duplicate name,
            or bundle validation failure; 409 CONFLICT when the host is offline;
            401 UNAUTHORIZED when unauthenticated in multi-user mode.
        """
        _require_user(request, auth_provider)
        host_conn = _require_host_conn(body.host_id, request)

        try:
            cloned: ClonedBundle = await clone_and_bundle_on_host(
                host_registry=request.app.state.host_registry,
                host_conn=host_conn,
                git_url=body.git_url,
                git_ref=body.git_ref,
                git_subpath=body.git_subpath,
            )
        except GitImportHostUnavailableError as exc:
            raise OmnigentError(exc.message, code=ErrorCode.CONFLICT) from exc
        except GitImportProxyError as exc:
            raise OmnigentError(exc.message, code=ErrorCode.INVALID_INPUT) from exc

        bundle_bytes = cloned.bundle_bytes
        sha = cloned.commit_sha
        resolved_ref = cloned.resolved_ref

        spec = await asyncio.to_thread(
            validate_agent_bundle,
            bundle_bytes,
            enforce_handler_allowlist=not local_single_user_enabled(),
        )

        if spec.name is None:
            raise OmnigentError("Bundle has no agent name.", code=ErrorCode.INVALID_INPUT)

        existing = await asyncio.to_thread(agent_store.get_by_name, spec.name)
        if existing is not None:
            raise OmnigentError(
                f"An agent named {spec.name!r} already exists.",
                code=ErrorCode.INVALID_INPUT,
            )

        agent_id = generate_agent_id()
        loc = bundle_location(agent_id, bundle_bytes)
        await asyncio.to_thread(artifact_store.put, loc, bundle_bytes)
        agent = await asyncio.to_thread(
            agent_store.create,
            agent_id,
            spec.name,
            loc,
            spec.description,
            git_url=body.git_url,
            git_ref=resolved_ref,
            git_subpath=body.git_subpath,
            git_commit=sha,
            git_host_id=body.host_id,
        )
        return _to_agent_object(agent, agent_cache)

    @router.post("/agents/{agent_id}/refresh")
    async def refresh_git(request: Request, agent_id: str) -> AgentObject:
        """Re-clone the tracked branch HEAD on the same host, re-bundle, and update.

        The refresh always uses the ``git_host_id`` stored on the agent at
        import time. If that host is now offline the route returns 409 CONFLICT.

        :param request: Incoming FastAPI request.
        :param agent_id: ID of the agent to refresh.
        :returns: The updated :class:`AgentObject` (or unchanged if no new commits).
        :raises OmnigentError: 404 NOT_FOUND if the agent does not exist;
            400 INVALID_INPUT if the agent was not imported from git or has no
            host binding; 409 CONFLICT if the bound host is offline;
            400 INVALID_INPUT if the repo name has changed.
        """
        _require_user(request, auth_provider)
        agent = await asyncio.to_thread(agent_store.get, agent_id)
        if agent is None:
            raise OmnigentError(f"Agent not found: {agent_id!r}", code=ErrorCode.NOT_FOUND)
        if agent.git_url is None:
            raise OmnigentError(
                "This agent was not imported from git.",
                code=ErrorCode.INVALID_INPUT,
            )
        if agent.git_host_id is None:
            raise OmnigentError(
                "This agent has no host binding to refresh from.",
                code=ErrorCode.INVALID_INPUT,
            )

        host_conn = _require_host_conn(agent.git_host_id, request)

        try:
            cloned = await clone_and_bundle_on_host(
                host_registry=request.app.state.host_registry,
                host_conn=host_conn,
                git_url=agent.git_url,
                git_ref=agent.git_ref,
                git_subpath=agent.git_subpath,
            )
        except GitImportHostUnavailableError as exc:
            raise OmnigentError(exc.message, code=ErrorCode.CONFLICT) from exc
        except GitImportProxyError as exc:
            raise OmnigentError(exc.message, code=ErrorCode.INVALID_INPUT) from exc

        bundle_bytes = cloned.bundle_bytes
        sha = cloned.commit_sha

        spec = await asyncio.to_thread(
            validate_agent_bundle,
            bundle_bytes,
            enforce_handler_allowlist=not local_single_user_enabled(),
        )
        if spec.name != agent.name:
            raise OmnigentError(
                f"Repo now defines {spec.name!r}; agent name {agent.name!r} is immutable.",
                code=ErrorCode.INVALID_INPUT,
            )
        # Idempotency: if the git HEAD hasn't changed, nothing to do.
        if sha == agent.git_commit:
            return _to_agent_object(agent, agent_cache)  # idempotent no-op
        new_loc = bundle_location(agent.id, bundle_bytes)
        # Forward-compatible guard: no-op if content hash matches.
        # Currently dead code because bundle_directory is non-deterministic
        # (real mtimes + unsorted rglob); activates automatically once it
        # produces stable bytes for identical inputs.
        if new_loc == agent.bundle_location:
            return _to_agent_object(agent, agent_cache)  # same content, no-op
        await asyncio.to_thread(artifact_store.put, new_loc, bundle_bytes)
        updated = await asyncio.to_thread(
            agent_store.update,
            agent.id,
            new_loc,
            git_commit=sha,
        )
        if updated is None:
            raise OmnigentError(f"Agent not found: {agent.id!r}", code=ErrorCode.NOT_FOUND)
        agent_cache.replace(
            agent.id,
            new_loc,
            bundle_bytes,
            expand_env=agent.session_id is None,
        )
        return _to_agent_object(updated, agent_cache)

    return router
