"""Routes for the community agent registry (``/v1/registry``).

Provides browse/search, per-agent detail, publish, and star voting.
All writes (publish, star) require authentication when an auth provider
is configured. Reads are unauthenticated so the registry is publicly
browsable in open deployments.

The underlying :class:`~omnigent.stores.registry_store.RegistryStore`
is injected at app construction time; if ``registry_store`` is ``None``
when ``create_app`` is called, these routes are not mounted and the
``/v1/registry`` paths return 404.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy.exc import IntegrityError

from omnigent.db.utils import generate_registry_id, now_epoch
from omnigent.entities import PublishedAgent
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user as _require_user
from omnigent.server.schemas import (
    PaginatedList,
    PublishAgentRequest,
    PublishedAgentObject,
    StarResponse,
)
from omnigent.stores.registry_store import RegistryStore

_logger = logging.getLogger(__name__)


def _to_object(agent: PublishedAgent) -> PublishedAgentObject:
    return PublishedAgentObject(
        id=agent.id,
        name=agent.name,
        version=agent.version,
        harness=agent.harness,
        description=agent.description,
        author=agent.author,
        created_at=agent.created_at,
        category=agent.category,
        tags=agent.tags,
        prompt_excerpt=agent.prompt_excerpt,
        network_access=agent.network_access,
        write_access=agent.write_access,
        guardrails=agent.guardrails,
        source_url=agent.source_url,
        stars_count=agent.stars_count,
        bundle_location=agent.bundle_location,
        updated_at=agent.updated_at,
    )


def create_registry_router(
    registry_store: RegistryStore,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the router for ``/v1/registry``.

    Mounted with ``prefix="/v1"`` so all paths are under ``/v1/registry``.

    :param registry_store: Store backing the community registry.
    :param auth_provider: Optional auth provider; when set, mutating
        operations (publish, star) require an authenticated caller.
    :returns: A FastAPI router exposing the registry endpoints.
    """
    router = APIRouter()

    @router.get("/registry")
    async def browse_registry(
        category: str | None = Query(default=None),
        harness: str | None = Query(default=None),
        tag: str | None = Query(default=None),
        q: str | None = Query(default=None),
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
    ) -> PaginatedList:
        """Browse published agents with optional filters.

        :param category: Filter to a category slug, e.g. ``"coding"``.
        :param harness: Filter to a harness, e.g. ``"claude-sdk"``.
        :param tag: Filter to agents whose tags include this value.
        :param q: Keyword search over name and description.
        :param limit: Maximum number of results (1-1000).
        :param after: Cursor for pagination (publication id).
        :returns: A :class:`PaginatedList` of :class:`PublishedAgentObject`.
        """
        page = registry_store.browse(
            category=category,
            harness=harness,
            tag=tag,
            q=q,
            limit=limit,
            after=after,
        )
        return PaginatedList(
            data=[_to_object(a) for a in page.data],
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )

    @router.post("/registry", status_code=201)
    async def publish_agent(
        request: Request,
        body: PublishAgentRequest,
    ) -> PublishedAgentObject:
        """Publish a new agent to the community registry.

        :param body: Publication metadata.
        :returns: The created :class:`PublishedAgentObject`.
        :raises 409: If ``name@version`` is already published.
        """
        _require_user(request, auth_provider)
        publication_id = generate_registry_id()
        try:
            agent = registry_store.publish(
                publication_id=publication_id,
                name=body.name,
                version=body.version,
                harness=body.harness,
                description=body.description,
                author=body.author,
                created_at=now_epoch(),
                category=body.category,
                tags=body.tags,
                prompt_excerpt=body.prompt_excerpt,
                network_access=body.network_access,
                write_access=body.write_access,
                guardrails=body.guardrails,
                source_url=body.source_url,
                bundle_location=body.bundle_location,
            )
        except (IntegrityError, ValueError) as exc:
            raise HTTPException(
                status_code=409,
                detail=f"{body.name}@{body.version} is already published",
            ) from exc
        return _to_object(agent)

    @router.get("/registry/{name}")
    async def get_agent_latest(name: str) -> PublishedAgentObject:
        """Return the most recently published version of an agent.

        :param name: Agent name slug, e.g. ``"code-reviewer"``.
        :returns: The :class:`PublishedAgentObject` for the latest version.
        :raises 404: If no version has been published under this name.
        """
        agent = registry_store.get_latest(name)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent not found: {name!r}")
        return _to_object(agent)

    @router.get("/registry/{name}/{version}")
    async def get_agent_version(name: str, version: str) -> PublishedAgentObject:
        """Return a specific published version of an agent.

        :param name: Agent name slug, e.g. ``"code-reviewer"``.
        :param version: Semver string, e.g. ``"1.2.0"``.
        :returns: The :class:`PublishedAgentObject` for that version.
        :raises 404: If the exact ``name@version`` is not published.
        """
        agent = registry_store.get(name, version)
        if agent is None:
            raise HTTPException(
                status_code=404,
                detail=f"Agent not found: {name}@{version}",
            )
        return _to_object(agent)

    @router.post("/registry/{name}/star")
    async def star_agent(
        request: Request,
        name: str,
    ) -> StarResponse:
        """Increment the star count for an agent's latest version.

        :param name: Agent name slug, e.g. ``"code-reviewer"``.
        :returns: The new :class:`StarResponse` with updated ``stars_count``.
        :raises 404: If no version has been published under this name.
        """
        _require_user(request, auth_provider)
        agent = registry_store.get_latest(name)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent not found: {name!r}")
        new_count = registry_store.star(name, agent.version)
        return StarResponse(stars_count=new_count)

    return router
