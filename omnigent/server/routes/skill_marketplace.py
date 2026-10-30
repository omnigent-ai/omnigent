"""Browse, install and manage skills from git repositories (Settings → Skills).

Mounted only when the ``skills_marketplace`` feature is on and a skill store
is configured. Sources are admin-managed; installs are per user unless an
admin installs for the whole org. The operator's ``skills_marketplace:``
policy is checked on every fetch path, admins included. Fetching runs off the
event loop, one fetch per account at a time and two per server, and uses the
same hardened clone as ``omnigent agent add``.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import tarfile
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from omnigent.entities import InstalledSkill, SkillSource
from omnigent.entities.skill import SkillScope
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.skills.git_source import GitSourceError, repository_label
from omnigent.skills.limits import MAX_INSTALLED_PER_OWNER, MAX_NAMES_PER_INSTALL
from omnigent.skills.overlay import SkillTarballError, validate_skill_tarball
from omnigent.skills.policy import DEFAULT_POLICY, SkillPolicyError, SkillsMarketplacePolicy
from omnigent.skills.service import FetchedCatalog, PackagedSkill, SkillFetcher, resolve_source
from omnigent.stores import ArtifactStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.skill_store import (
    DEFAULT_PAGE,
    MAX_PAGE,
    SkillInstall,
    SkillStore,
    SkillValueError,
)

_logger = logging.getLogger(__name__)

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
    """One page; pass ``last_id``, an opaque cursor, as ``after`` for the next."""

    data: list[SkillSourceObject]
    has_more: bool = False
    last_id: str | None = None


class SkillSourceCreate(BaseModel):
    """``source`` is ``owner/repo`` or a GitHub URL; ``ref`` a branch or tag."""

    source: str = Field(min_length=1, max_length=512)
    ref: str | None = Field(default=None, max_length=255)
    name: str | None = Field(default=None, max_length=256)


class CatalogSkill(BaseModel):
    """One skill a repository offers, with the caller's installs of it if any."""

    name: str
    display_name: str | None
    description: str
    path: str
    plugin: str | None
    # The caller's own install, else the org's; and every scope it is installed in.
    installed_id: str | None
    installed_scopes: list[Literal["user", "org"]] = []


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
    bundle_bytes: int
    enabled: bool
    installed_by: str | None
    installed_at: int
    updated_at: int | None


class InstalledSkillList(BaseModel):
    """One page; pass ``last_id``, an opaque cursor, as ``after`` for the next."""

    data: list[InstalledSkillObject]
    has_more: bool = False
    last_id: str | None = None


class InstallSkillsRequest(BaseModel):
    """Installs are all-or-nothing: a refused name leaves nothing installed."""

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
        bundle_bytes=skill.bundle_bytes,
        enabled=skill.enabled,
        installed_by=skill.installed_by,
        installed_at=skill.installed_at,
        updated_at=skill.updated_at,
    )


async def _is_admin(user_id: str | None, permission_store: PermissionStore | None) -> bool:
    """Single-user mode (no permission store) is all-admin, like the other admin gates."""
    if permission_store is None:
        return True
    return user_id is not None and await asyncio.to_thread(permission_store.is_admin, user_id)


async def _require_admin(user_id: str | None, permission_store: PermissionStore | None) -> None:
    if not await _is_admin(user_id, permission_store):
        raise OmnigentError("Administrator access required", code=ErrorCode.FORBIDDEN)


async def _require_can_mutate(
    skill: InstalledSkill, user_id: str | None, permission_store: PermissionStore | None
) -> None:
    """Admins may change any row; a user only their own user-scoped installs."""
    if skill.scope == "user" and skill.owner_user_id == user_id:
        return
    if await _is_admin(user_id, permission_store):
        return
    raise OmnigentError("Not the owner of this skill", code=ErrorCode.FORBIDDEN)


def _skill_location(skill_name: str, scope: str, owner: str | None) -> str:
    """A key unique to this write, so each row owns its object outright.

    A shared (content-addressed) key would let one request's failure cleanup
    delete an object that a concurrent request's committed row points at.
    """
    bucket = "org" if scope == "org" else hashlib.sha256((owner or "").encode()).hexdigest()[:16]
    return f"skills/{bucket}/{skill_name}/{uuid.uuid4().hex}.tar.gz"


def _catalog_response(
    fetched: FetchedCatalog, installed: list[InstalledSkill]
) -> SkillCatalogResponse:
    """Mark each entry with the caller's installs of it, scope by scope."""
    by_name: dict[str, dict[str, str]] = {}
    for skill in installed:
        if skill.source_url == fetched.url:
            by_name.setdefault(skill.name, {})[skill.scope] = skill.id
    skills = []
    for entry in fetched.entries:
        scopes = by_name.get(entry.name, {})
        skills.append(
            CatalogSkill(
                name=entry.name,
                display_name=entry.display_name,
                description=entry.description,
                path=entry.path,
                plugin=entry.plugin,
                installed_id=scopes.get("user") or scopes.get("org"),
                installed_scopes=[s for s in ("user", "org") if s in scopes],  # type: ignore[misc]
            )
        )
    return SkillCatalogResponse(
        url=fetched.url,
        ref=fetched.ref,
        commit=fetched.commit,
        skills=skills,
        skipped=list(fetched.skipped),
    )


def _invalid(exc: Exception) -> OmnigentError:
    """A policy refusal is forbidden; anything else about the request is invalid input."""
    code = ErrorCode.FORBIDDEN if isinstance(exc, SkillPolicyError) else ErrorCode.INVALID_INPUT
    return OmnigentError(str(exc), code=code)


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


async def _installed_among(
    skill_store: SkillStore, user_id: str | None, fetched: FetchedCatalog
) -> list[InstalledSkill]:
    """The caller's visible installs of the catalog's names: one bounded lookup per name."""
    names = [entry.name for entry in fetched.entries]
    return await asyncio.to_thread(skill_store.find_for_user, user_id, names)


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
    # Accounts with a clone in flight: one at a time each, so one signed-in
    # user cannot hold both server slots or queue up repository fetches.
    fetching: set[str] = set()

    def fetcher(request: Request) -> SkillFetcher:
        return request.app.state.skill_fetcher

    async def fetch(user_id: str | None, fn, *args, **kwargs):  # type: ignore[no-untyped-def]  # thread hop
        account = user_id or ""
        if account in fetching:
            raise OmnigentError(
                "Another repository fetch for your account is still running; "
                "try again in a moment.",
                code=ErrorCode.CONFLICT,
            )
        fetching.add(account)
        try:
            async with fetch_slots:
                return await asyncio.to_thread(fn, *args, **kwargs)
        finally:
            fetching.discard(account)

    def resolve_allowed(source: str, ref: str | None) -> tuple[str, str | None]:
        """``(clone_url, ref)`` for a source within ``allowed_sources``, before any fetch."""
        url, ref = resolve_source(source, ref)
        policy.require_allowed(url)
        return url, ref

    async def refuse_when_full(user_id: str | None, scope: SkillScope, names: list[str]) -> None:
        """Save a clone when the principal is plainly full; the store's check is the authority."""
        present = await asyncio.to_thread(skill_store.find_for_user, user_id, names)
        replacing = {skill.name for skill in present if skill.scope == scope}
        adding = len([name for name in names if name not in replacing])
        count = await asyncio.to_thread(skill_store.count_installed, scope, user_id)
        if count + adding > MAX_INSTALLED_PER_OWNER:
            who = "everyone" if scope == "org" else "a user"
            raise OmnigentError(
                f"At most {MAX_INSTALLED_PER_OWNER} skills can be installed for {who}; "
                "remove some first.",
                code=ErrorCode.INVALID_INPUT,
            )

    async def require_browsable(url: str, ref: str | None) -> None:
        """With URL installs off, a repository must be a listed source to fetch from."""
        if policy.allow_url_install:
            return
        if await asyncio.to_thread(skill_store.find_source, url, ref) is None:
            raise OmnigentError(
                "Installing from a URL is disabled on this server; "
                "choose a repository from the sources list.",
                code=ErrorCode.FORBIDDEN,
            )

    def checked(skill: PackagedSkill) -> None:
        """Validate a tarball and its stored values before anything is written."""
        try:
            validate_skill_tarball(skill.tarball)
            _check_metadata(skill.entry.name, skill.entry.display_name, skill.entry.description)
        except (SkillTarballError, SkillValueError) as exc:
            raise _invalid(exc) from None

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
        await _require_admin(user_id, permission_store)
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
        await _require_admin(user_id, permission_store)
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
                user_id, fetcher(request).catalog, source.url, source.ref, refresh=refresh
            )
        except GitSourceError as exc:
            raise _invalid(exc) from None
        return _catalog_response(fetched, await _installed_among(skill_store, user_id, fetched))

    @router.post("/skill-catalog", dependencies=mutating)
    async def preview_skill_catalog(
        request: Request, body: SkillCatalogRequest
    ) -> SkillCatalogResponse:
        """The skills an arbitrary GitHub repository offers (the Add from URL path)."""
        user_id = require_user(request, auth_provider)
        try:
            url, ref = resolve_allowed(body.source, body.ref)
            await require_browsable(url, ref)
            fetched = await fetch(user_id, fetcher(request).catalog, url, ref)
        except GitSourceError as exc:
            raise _invalid(exc) from None
        return _catalog_response(fetched, await _installed_among(skill_store, user_id, fetched))

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
        """Fetch the named skills from a repository and install them, all or none."""
        user_id = require_user(request, auth_provider)
        if body.scope == "org":
            await _require_admin(user_id, permission_store)
        names = list(dict.fromkeys(body.names))
        try:
            url, ref = resolve_allowed(body.source, body.ref)
            await require_browsable(url, ref)
            await refuse_when_full(user_id, body.scope, names)
            commit, packaged = await fetch(user_id, fetcher(request).package, url, ref, names)
        except GitSourceError as exc:
            raise _invalid(exc) from None
        for skill in packaged:
            checked(skill)
        # Every tarball is stored first; the rows then commit in one transaction
        # under the principal's lock, so a refusal leaves no row and no new object.
        items: list[SkillInstall] = []
        # Every location is unique to this request, so no other row can point at one.
        created: list[str] = []
        try:
            for skill in packaged:
                location = _skill_location(skill.entry.name, body.scope, user_id)
                created.append(location)
                await asyncio.to_thread(artifact_store.put, location, skill.tarball)
                items.append(
                    SkillInstall(
                        name=skill.entry.name,
                        display_name=skill.entry.display_name,
                        description=skill.entry.description,
                        source_url=url,
                        ref=ref,
                        commit=commit,
                        path=skill.entry.path,
                        bundle_location=location,
                        bundle_bytes=len(skill.tarball),
                        user_invocable=skill.entry.user_invocable,
                    )
                )
        except Exception:
            # A later store failed: no row exists yet, so every object this
            # request created is certainly unreferenced.
            for location in created:
                _drop_artifact(artifact_store, location)
            raise
        try:
            results = await asyncio.to_thread(
                skill_store.install_many,
                items,
                scope=body.scope,
                owner_user_id=user_id,
                installed_by=user_id,
            )
        except SkillValueError as exc:
            # A refusal is raised before the transaction commits. Anything else
            # from here leaves the commit ambiguous, so its objects stay: a row
            # that did commit must not lose the bytes it points at.
            for location in created:
                _drop_artifact(artifact_store, location)
            raise _invalid(exc) from None
        # A reinstall leaves the row's previous object unreferenced.
        for result in results:
            if result.replaced_location is not None:
                _drop_artifact(artifact_store, result.replaced_location)
        return InstalledSkillList(data=[_skill_object(r.skill) for r in results])

    async def _load(skill_id: str, user_id: str | None) -> InstalledSkill:
        skill = await asyncio.to_thread(skill_store.get, skill_id)
        visible = skill is not None and (
            skill.scope == "org"
            or skill.owner_user_id == user_id
            or await _is_admin(user_id, permission_store)
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
        try:
            tarball = await asyncio.to_thread(artifact_store.get, skill.bundle_location)
        except KeyError:
            # Removed, or its object dropped, since the row was read.
            raise OmnigentError(
                "The stored copy of this skill is gone; update or remove it.",
                code=ErrorCode.NOT_FOUND,
            ) from None
        try:
            markdown = _read_skill_markdown(tarball)
        except (tarfile.TarError, OSError, ValueError):
            raise OmnigentError(
                "The stored copy of this skill cannot be read; update or remove it.",
                code=ErrorCode.CONFLICT,
            ) from None
        return InstalledSkillContentResponse(markdown=markdown)

    @router.post("/installed-skills/{skill_id}/update", dependencies=mutating[:1])
    async def update_installed_skill(request: Request, skill_id: str) -> InstalledSkillObject:
        """Refetch the skill from its repository at the same ref."""
        user_id = require_user(request, auth_provider)
        skill = await _load(skill_id, user_id)
        await _require_can_mutate(skill, user_id, permission_store)
        try:
            policy.require_allowed(skill.source_url)
            await require_browsable(skill.source_url, skill.ref)
            commit, packaged = await fetch(
                user_id, fetcher(request).package, skill.source_url, skill.ref, [skill.name]
            )
        except GitSourceError as exc:
            raise _invalid(exc) from None
        fresh = packaged[0]
        checked(fresh)
        location = _skill_location(skill.name, skill.scope, skill.owner_user_id)
        await asyncio.to_thread(artifact_store.put, location, fresh.tarball)
        try:
            updated = await asyncio.to_thread(
                skill_store.update,
                skill.id,
                commit=commit,
                bundle_location=location,
                bundle_bytes=len(fresh.tarball),
                description=fresh.entry.description,
                display_name=fresh.entry.display_name,
                path=fresh.entry.path,
                user_invocable=fresh.entry.user_invocable,
            )
        except SkillValueError as exc:
            _drop_artifact(artifact_store, location)
            raise _invalid(exc) from None
        if updated is None:
            # Removed while the fetch ran: the object just stored belongs to no row.
            _drop_artifact(artifact_store, location)
            raise OmnigentError("Installed skill not found", code=ErrorCode.NOT_FOUND)
        # Drop what the row pointed at under the lock, not what this request read
        # before fetching: a concurrent update may have moved it since.
        if updated.replaced_location is not None:
            _drop_artifact(artifact_store, updated.replaced_location)
        return _skill_object(updated.skill)

    @router.patch("/installed-skills/{skill_id}", dependencies=mutating)
    async def patch_installed_skill(
        request: Request, skill_id: str, body: InstalledSkillPatch
    ) -> InstalledSkillObject:
        """Enable or disable a skill without removing it."""
        user_id = require_user(request, auth_provider)
        skill = await _load(skill_id, user_id)
        await _require_can_mutate(skill, user_id, permission_store)
        try:
            updated = await asyncio.to_thread(skill_store.set_enabled, skill.id, body.enabled)
        except SkillValueError as exc:
            raise _invalid(exc) from None
        if updated is None:
            raise OmnigentError("Installed skill not found", code=ErrorCode.NOT_FOUND)
        return _skill_object(updated)

    @router.delete("/installed-skills/{skill_id}", status_code=204, dependencies=mutating[:1])
    async def delete_installed_skill(request: Request, skill_id: str) -> Response:
        """Remove an installed skill and its stored tarball."""
        user_id = require_user(request, auth_provider)
        skill = await _load(skill_id, user_id)
        await _require_can_mutate(skill, user_id, permission_store)
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
    """The archive's ``SKILL.md``, or ``""``; raises like :mod:`tarfile` on a bad archive."""
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:*") as tf:
        for member in tf:
            if member.isfile() and member.name.lstrip("./") == "SKILL.md":
                handle = tf.extractfile(member)
                return handle.read().decode("utf-8", errors="replace") if handle else ""
    return ""
