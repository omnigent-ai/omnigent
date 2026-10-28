"""Browse, install and manage skills from git repositories (Settings → Skills).

Mounted only when the ``skills_marketplace`` feature is on and a skill store
is configured. Sources are admin-managed; installs are per user unless an
admin installs for the whole org. The operator's ``skills_marketplace:``
policy is checked on every fetch path, admins included. Fetching runs off the
event loop and uses the same hardened clone as ``omnigent agent add``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from omnigent.entities import InstalledSkill, SkillSource
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.skills.git_source import GitSourceError, repository_label
from omnigent.skills.overlay import SkillTarballError, validate_skill_tarball
from omnigent.skills.policy import DEFAULT_POLICY, SkillPolicyError, SkillsMarketplacePolicy
from omnigent.skills.service import FetchedCatalog, SkillFetcher, resolve_source
from omnigent.stores import ArtifactStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.skill_store import DEFAULT_PAGE, MAX_PAGE, SkillStore, SkillValueError

_logger = logging.getLogger(__name__)

MAX_NAMES_PER_INSTALL = 20
# Clones and packaging are CPU, disk and network work; bound how many run at once.
_FETCH_CONCURRENCY = 2


# ── wire models ──────────────────────────────────────────────────────────


class SkillSourceObject(BaseModel):
    """A repository the marketplace browses."""

    id: str
    url: str
    ref: str | None
    name: str
    created_by: str | None
    created_at: int
    # Listed in the server config: seeded at startup and not removable here.
    managed: bool = False


class SkillSourceList(BaseModel):
    """One page; pass ``last_id`` as ``after`` for the next."""

    data: list[SkillSourceObject]
    has_more: bool = False
    last_id: str | None = None


class SkillSourceCreate(BaseModel):
    """``source`` is ``owner/repo`` or a github.com URL; ``ref`` a branch or tag."""

    source: str = Field(min_length=1, max_length=512)
    ref: str | None = Field(default=None, max_length=255)
    name: str | None = Field(default=None, max_length=256)


class CatalogSkill(BaseModel):
    """One skill a repository offers, with the caller's install of it if any."""

    name: str
    display_name: str | None
    description: str
    path: str
    plugin: str | None
    installed_id: str | None


class SkillCatalogResponse(BaseModel):
    url: str
    ref: str | None
    commit: str | None
    skills: list[CatalogSkill]
    skipped: list[str]


class SkillCatalogRequest(BaseModel):
    source: str = Field(min_length=1, max_length=512)
    ref: str | None = Field(default=None, max_length=255)


class InstalledSkillObject(BaseModel):
    """An installed skill as the UI lists it."""

    id: str
    name: str
    display_name: str | None
    description: str
    source_url: str
    source: str
    ref: str | None
    commit: str | None
    path: str
    scope: Literal["user", "org"]
    owner_user_id: str | None
    enabled: bool
    installed_by: str | None
    installed_at: int
    updated_at: int | None


class InstalledSkillList(BaseModel):
    """One page; pass ``last_id`` as ``after`` for the next."""

    data: list[InstalledSkillObject]
    has_more: bool = False
    last_id: str | None = None


class InstallSkillsRequest(BaseModel):
    source: str = Field(min_length=1, max_length=512)
    ref: str | None = Field(default=None, max_length=255)
    names: list[str] = Field(min_length=1, max_length=MAX_NAMES_PER_INSTALL)
    scope: Literal["user", "org"] = "user"


class InstalledSkillPatch(BaseModel):
    enabled: bool


class InstalledSkillContentResponse(BaseModel):
    markdown: str


# ── helpers ──────────────────────────────────────────────────────────────


def _source_object(source: SkillSource, policy: SkillsMarketplacePolicy) -> SkillSourceObject:
    return SkillSourceObject(
        **source.__dict__, managed=policy.is_configured(source.url, source.ref)
    )


def _skill_object(skill: InstalledSkill) -> InstalledSkillObject:
    return InstalledSkillObject(
        id=skill.id,
        name=skill.name,
        display_name=skill.display_name,
        description=skill.description,
        source_url=skill.source_url,
        source=repository_label(skill.source_url),
        ref=skill.ref,
        commit=skill.commit,
        path=skill.path,
        scope=skill.scope,
        owner_user_id=skill.owner_user_id,
        enabled=skill.enabled,
        installed_by=skill.installed_by,
        installed_at=skill.installed_at,
        updated_at=skill.updated_at,
    )


def _is_admin(user_id: str | None, permission_store: PermissionStore | None) -> bool:
    """Single-user mode (no permission store) is all-admin, like the other admin gates."""
    if permission_store is None:
        return True
    return user_id is not None and permission_store.is_admin(user_id)


def _require_admin(user_id: str | None, permission_store: PermissionStore | None) -> None:
    if not _is_admin(user_id, permission_store):
        raise OmnigentError("Administrator access required", code=ErrorCode.FORBIDDEN)


def _require_can_mutate(
    skill: InstalledSkill, user_id: str | None, permission_store: PermissionStore | None
) -> None:
    """Admins may change any row; a user only their own user-scoped installs."""
    if _is_admin(user_id, permission_store):
        return
    if skill.scope == "user" and skill.owner_user_id == user_id:
        return
    raise OmnigentError("Not the owner of this skill", code=ErrorCode.FORBIDDEN)


def _skill_location(skill_name: str, scope: str, owner: str | None, tarball: bytes) -> str:
    digest = hashlib.sha256(tarball).hexdigest()[:16]
    bucket = "org" if scope == "org" else hashlib.sha256((owner or "").encode()).hexdigest()[:16]
    return f"skills/{bucket}/{skill_name}/{digest}.tar.gz"


def _catalog_response(
    fetched: FetchedCatalog, installed: list[InstalledSkill]
) -> SkillCatalogResponse:
    mine = {(s.source_url, s.name): s.id for s in installed}
    return SkillCatalogResponse(
        url=fetched.url,
        ref=fetched.ref,
        commit=fetched.commit,
        skills=[
            CatalogSkill(
                name=entry.name,
                display_name=entry.display_name,
                description=entry.description,
                path=entry.path,
                plugin=entry.plugin,
                installed_id=mine.get((fetched.url, entry.name)),
            )
            for entry in fetched.entries
        ],
        skipped=list(fetched.skipped),
    )


def _invalid(exc: Exception) -> OmnigentError:
    """A policy refusal is forbidden; anything else about the request is invalid input."""
    code = ErrorCode.FORBIDDEN if isinstance(exc, SkillPolicyError) else ErrorCode.INVALID_INPUT
    return OmnigentError(str(exc), code=code)


async def _listed_source(skill_store: SkillStore, url: str, ref: str | None) -> SkillSource | None:
    """The browsable source for this exact repository and ref, if there is one."""
    after: str | None = None
    while True:
        page = await asyncio.to_thread(skill_store.list_sources, after=after)
        for source in page.data:
            if source.url == url and source.ref == ref:
                return source
        if not page.has_more or page.last_id is None:
            return None
        after = page.last_id


def seed_configured_sources(skill_store: SkillStore, policy: SkillsMarketplacePolicy) -> None:
    """Add the config's ``sources`` to the browse list; an existing row is left as is."""
    for source in policy.sources:
        try:
            skill_store.add_source(
                url=source.url,
                ref=source.ref,
                name=source.name or repository_label(source.url),
                created_by=None,
            )
        except Exception:
            _logger.warning("could not seed skill source %s", source.url, exc_info=True)


async def _all_installed(skill_store: SkillStore, user_id: str | None) -> list[InstalledSkill]:
    """Every installed skill the user can see, read page by page."""
    out: list[InstalledSkill] = []
    after: str | None = None
    while True:
        page = await asyncio.to_thread(skill_store.list_for_user, user_id, after=after)
        out.extend(page.data)
        if not page.has_more or page.last_id is None:
            return out
        after = page.last_id


# ── router ───────────────────────────────────────────────────────────────


def create_skill_marketplace_router(
    *,
    skill_store: SkillStore,
    artifact_store: ArtifactStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    policy: SkillsMarketplacePolicy = DEFAULT_POLICY,
) -> APIRouter:
    """Build the marketplace routes; the fetcher is read from ``app.state.skill_fetcher``."""
    router = APIRouter()
    mutating = [Depends(require_trusted_origin), Depends(require_json_content_type)]
    fetch_slots = asyncio.Semaphore(_FETCH_CONCURRENCY)

    def fetcher(request: Request) -> SkillFetcher:
        return request.app.state.skill_fetcher

    async def fetch(fn, *args, **kwargs):  # type: ignore[no-untyped-def]  # thin thread hop
        async with fetch_slots:
            return await asyncio.to_thread(fn, *args, **kwargs)

    def resolve_allowed(source: str, ref: str | None) -> tuple[str, str | None]:
        """``(clone_url, ref)`` for a source within ``allowed_sources``, before any fetch."""
        url, ref = resolve_source(source, ref)
        policy.require_allowed(url)
        return url, ref

    async def require_browsable(url: str, ref: str | None) -> None:
        """With URL installs off, a repository must be a listed source to fetch from."""
        if policy.allow_url_install:
            return
        if await _listed_source(skill_store, url, ref) is None:
            raise OmnigentError(
                "Installing from a URL is disabled on this server; "
                "choose a repository from the sources list.",
                code=ErrorCode.FORBIDDEN,
            )

    # ── sources ──

    @router.get("/skill-sources")
    async def list_skill_sources(
        request: Request,
        limit: int = Query(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE),
        after: str | None = Query(default=None),
    ) -> SkillSourceList:
        """A page of the repositories the marketplace browses."""
        require_user(request, auth_provider)
        try:
            page = await asyncio.to_thread(skill_store.list_sources, limit=limit, after=after)
        except SkillValueError as exc:
            raise _invalid(exc) from None
        return SkillSourceList(
            data=[_source_object(s, policy) for s in page.data],
            has_more=page.has_more,
            last_id=page.last_id,
        )

    @router.post("/skill-sources", status_code=201, dependencies=mutating)
    async def add_skill_source(request: Request, body: SkillSourceCreate) -> SkillSourceObject:
        """Add a repository to browse (administrators only, within ``allowed_sources``)."""
        user_id = require_user(request, auth_provider)
        _require_admin(user_id, permission_store)
        try:
            url, ref = resolve_allowed(body.source, body.ref)
        except GitSourceError as exc:
            raise _invalid(exc) from None
        try:
            source = await asyncio.to_thread(
                skill_store.add_source,
                url=url,
                ref=ref,
                name=body.name or repository_label(url),
                created_by=user_id,
            )
        except SkillValueError as exc:
            raise _invalid(exc) from None
        return _source_object(source, policy)

    @router.delete("/skill-sources/{source_id}", status_code=204, dependencies=mutating[:1])
    async def delete_skill_source(request: Request, source_id: str) -> Response:
        """Remove a repository from the browse list; installed skills stay."""
        user_id = require_user(request, auth_provider)
        _require_admin(user_id, permission_store)
        source = await asyncio.to_thread(skill_store.get_source, source_id)
        if source is None:
            raise OmnigentError("Skill source not found", code=ErrorCode.NOT_FOUND)
        if policy.is_configured(source.url, source.ref):
            raise OmnigentError(
                "This source is set in the server config and cannot be removed here.",
                code=ErrorCode.FORBIDDEN,
            )
        if not await asyncio.to_thread(skill_store.delete_source, source_id):
            raise OmnigentError("Skill source not found", code=ErrorCode.NOT_FOUND)
        return Response(status_code=204)

    @router.get("/skill-sources/{source_id}/catalog")
    async def skill_source_catalog(
        request: Request, source_id: str, refresh: bool = False
    ) -> SkillCatalogResponse:
        """The skills a configured source offers, marking the caller's installs."""
        user_id = require_user(request, auth_provider)
        source = await asyncio.to_thread(skill_store.get_source, source_id)
        if source is None:
            raise OmnigentError("Skill source not found", code=ErrorCode.NOT_FOUND)
        try:
            # A source added under a looser policy is refused once it is outside it.
            policy.require_allowed(source.url)
            fetched = await fetch(
                fetcher(request).catalog, source.url, source.ref, refresh=refresh
            )
        except GitSourceError as exc:
            raise _invalid(exc) from None
        return _catalog_response(fetched, await _all_installed(skill_store, user_id))

    @router.post("/skill-catalog", dependencies=mutating)
    async def preview_skill_catalog(
        request: Request, body: SkillCatalogRequest
    ) -> SkillCatalogResponse:
        """The skills an arbitrary GitHub repository offers (the Add from URL path)."""
        user_id = require_user(request, auth_provider)
        try:
            url, ref = resolve_allowed(body.source, body.ref)
            await require_browsable(url, ref)
            fetched = await fetch(fetcher(request).catalog, url, ref)
        except GitSourceError as exc:
            raise _invalid(exc) from None
        return _catalog_response(fetched, await _all_installed(skill_store, user_id))

    # ── installed skills ──

    @router.get("/installed-skills")
    async def list_installed_skills(
        request: Request,
        limit: int = Query(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE),
        after: str | None = Query(default=None),
    ) -> InstalledSkillList:
        """A page of the org's installed skills plus the caller's own."""
        user_id = require_user(request, auth_provider)
        try:
            page = await asyncio.to_thread(
                skill_store.list_for_user, user_id, limit=limit, after=after
            )
        except SkillValueError as exc:
            raise _invalid(exc) from None
        return InstalledSkillList(
            data=[_skill_object(s) for s in page.data],
            has_more=page.has_more,
            last_id=page.last_id,
        )

    @router.post("/installed-skills", status_code=201, dependencies=mutating)
    async def install_skills(request: Request, body: InstallSkillsRequest) -> InstalledSkillList:
        """Fetch the named skills from a repository and install them."""
        user_id = require_user(request, auth_provider)
        if body.scope == "org":
            _require_admin(user_id, permission_store)
        names = list(dict.fromkeys(body.names))
        try:
            url, ref = resolve_allowed(body.source, body.ref)
            await require_browsable(url, ref)
            commit, packaged = await fetch(fetcher(request).package, url, ref, names)
        except GitSourceError as exc:
            raise _invalid(exc) from None
        # Validate every tarball and every stored value before the first write,
        # so a bad request leaves no artifact and no row behind.
        for skill in packaged:
            try:
                validate_skill_tarball(skill.tarball)
                _check_metadata(
                    skill.entry.name, skill.entry.display_name, skill.entry.description
                )
            except (SkillTarballError, SkillValueError) as exc:
                raise _invalid(exc) from None
        installed: list[InstalledSkill] = []
        for skill in packaged:
            location = _skill_location(skill.entry.name, body.scope, user_id, skill.tarball)
            await asyncio.to_thread(artifact_store.put, location, skill.tarball)
            try:
                row = await asyncio.to_thread(
                    skill_store.install,
                    name=skill.entry.name,
                    display_name=skill.entry.display_name,
                    description=skill.entry.description,
                    source_url=url,
                    ref=ref,
                    commit=commit,
                    path=skill.entry.path,
                    scope=body.scope,
                    owner_user_id=user_id,
                    bundle_location=location,
                    installed_by=user_id,
                )
            except SkillValueError as exc:
                _drop_artifact(artifact_store, location)
                raise _invalid(exc) from None
            installed.append(row)
        return InstalledSkillList(data=[_skill_object(s) for s in installed])

    async def _load(skill_id: str, user_id: str | None) -> InstalledSkill:
        skill = await asyncio.to_thread(skill_store.get, skill_id)
        visible = skill is not None and (
            skill.scope == "org"
            or skill.owner_user_id == user_id
            or _is_admin(user_id, permission_store)
        )
        if skill is None or not visible:
            raise OmnigentError("Installed skill not found", code=ErrorCode.NOT_FOUND)
        return skill

    @router.get("/installed-skills/{skill_id}/content")
    async def installed_skill_content(
        request: Request, skill_id: str
    ) -> InstalledSkillContentResponse:
        """The stored ``SKILL.md`` of an installed skill."""
        user_id = require_user(request, auth_provider)
        skill = await _load(skill_id, user_id)
        tarball = await asyncio.to_thread(artifact_store.get, skill.bundle_location)
        return InstalledSkillContentResponse(markdown=_read_skill_markdown(tarball))

    @router.post("/installed-skills/{skill_id}/update", dependencies=mutating[:1])
    async def update_installed_skill(request: Request, skill_id: str) -> InstalledSkillObject:
        """Refetch the skill from its repository at the same ref."""
        user_id = require_user(request, auth_provider)
        skill = await _load(skill_id, user_id)
        _require_can_mutate(skill, user_id, permission_store)
        try:
            policy.require_allowed(skill.source_url)
            await require_browsable(skill.source_url, skill.ref)
            commit, packaged = await fetch(
                fetcher(request).package, skill.source_url, skill.ref, [skill.name]
            )
            validate_skill_tarball(packaged[0].tarball)
            fresh = packaged[0]
            _check_metadata(fresh.entry.name, fresh.entry.display_name, fresh.entry.description)
        except (GitSourceError, SkillTarballError, SkillValueError) as exc:
            raise _invalid(exc) from None
        location = _skill_location(skill.name, skill.scope, skill.owner_user_id, fresh.tarball)
        await asyncio.to_thread(artifact_store.put, location, fresh.tarball)
        try:
            updated = await asyncio.to_thread(
                skill_store.update,
                skill.id,
                commit=commit,
                bundle_location=location,
                description=fresh.entry.description,
                display_name=fresh.entry.display_name,
            )
        except SkillValueError as exc:
            _drop_artifact(artifact_store, location)
            raise _invalid(exc) from None
        if updated is None:
            raise OmnigentError("Installed skill not found", code=ErrorCode.NOT_FOUND)
        if location != skill.bundle_location:
            _drop_artifact(artifact_store, skill.bundle_location)
        return _skill_object(updated)

    @router.patch("/installed-skills/{skill_id}", dependencies=mutating)
    async def patch_installed_skill(
        request: Request, skill_id: str, body: InstalledSkillPatch
    ) -> InstalledSkillObject:
        """Enable or disable a skill without removing it."""
        user_id = require_user(request, auth_provider)
        skill = await _load(skill_id, user_id)
        _require_can_mutate(skill, user_id, permission_store)
        updated = await asyncio.to_thread(skill_store.set_enabled, skill.id, body.enabled)
        if updated is None:
            raise OmnigentError("Installed skill not found", code=ErrorCode.NOT_FOUND)
        return _skill_object(updated)

    @router.delete("/installed-skills/{skill_id}", status_code=204, dependencies=mutating[:1])
    async def delete_installed_skill(request: Request, skill_id: str) -> Response:
        """Remove an installed skill and its stored tarball."""
        user_id = require_user(request, auth_provider)
        skill = await _load(skill_id, user_id)
        _require_can_mutate(skill, user_id, permission_store)
        removed = await asyncio.to_thread(skill_store.delete, skill.id)
        if removed is not None:
            _drop_artifact(artifact_store, removed.bundle_location)
        return Response(status_code=204)

    return router


def _check_metadata(name: str, display_name: str | None, description: str) -> None:
    """Reject repository metadata that would not fit its column, before any write."""
    from omnigent.stores.skill_store import (
        MAX_DESCRIPTION_CHARS,
        MAX_DISPLAY_NAME_CHARS,
        MAX_NAME_CHARS,
        check_bound,
    )

    check_bound("name", name, MAX_NAME_CHARS)
    check_bound("display_name", display_name, MAX_DISPLAY_NAME_CHARS)
    check_bound("description", description, MAX_DESCRIPTION_CHARS)


def _drop_artifact(artifact_store: ArtifactStore, location: str) -> None:
    try:
        artifact_store.delete(location)
    except Exception:
        _logger.warning("could not delete skill tarball %s", location, exc_info=True)


def _read_skill_markdown(tarball: bytes) -> str:
    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:*") as tf:
        for member in tf:
            if member.isfile() and member.name.lstrip("./") == "SKILL.md":
                handle = tf.extractfile(member)
                return handle.read().decode("utf-8", errors="replace") if handle else ""
    return ""
