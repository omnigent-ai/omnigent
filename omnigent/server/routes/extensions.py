"""Read-only catalog and diagnostics for installed extensions."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.extensions import ExtensionManifest, ExtensionPluginState
from omnigent.extensions.assets import ASSET_SCRIPT, ASSET_STYLES, ResolvedBundle
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores.permission_store import PermissionStore


class ExtensionPageResponse(BaseModel):
    """One namespaced page contribution."""

    id: str
    title: str
    route: str
    view: str


class ExtensionPrimaryNavigationResponse(BaseModel):
    """One primary-sidebar navigation contribution."""

    id: str
    label: str
    page: str
    icon: str | None
    order: int
    when: str | None


class ExtensionBrowserBundleResponse(BaseModel):
    """Opaque browser-bundle availability and content-addressed asset URLs."""

    declared: bool
    has_styles: bool
    digest: str | None = None
    script_url: str | None = None
    style_url: str | None = None


class ExtensionResponse(BaseModel):
    """Public metadata for one accepted extension."""

    object: Literal["extension"] = "extension"
    id: str
    display_name: str
    distribution: str
    version: str
    extension_api: int
    status: Literal["enabled", "unavailable"]
    permissions: list[str]
    pages: list[ExtensionPageResponse]
    primary_navigation: list[ExtensionPrimaryNavigationResponse]
    browser: ExtensionBrowserBundleResponse


class ExtensionListResponse(BaseModel):
    """Public catalog of accepted extensions."""

    object: Literal["list"] = "list"
    data: list[ExtensionResponse]


class ExtensionLoadErrorResponse(BaseModel):
    """Concise diagnostic for one rejected entry point."""

    entry_point: str
    status: Literal["rejected"] = "rejected"
    error: str


class ExtensionAssetErrorResponse(BaseModel):
    """Concise diagnostic for one unresolved browser bundle."""

    extension_id: str
    status: Literal["unresolved"] = "unresolved"
    error: str


class ExtensionDiagnosticsResponse(BaseModel):
    """Administrator view of accepted and rejected extensions."""

    object: Literal["extension_diagnostics"] = "extension_diagnostics"
    extensions: list[ExtensionResponse]
    load_errors: list[ExtensionLoadErrorResponse]
    asset_errors: list[ExtensionAssetErrorResponse]


def _serialize_manifest(
    manifest: ExtensionManifest,
    bundle: ResolvedBundle | None = None,
) -> ExtensionResponse:
    """Convert a validated manifest into stable JSON-safe catalog metadata."""
    unavailable = manifest.entrypoints.browser is not None and bundle is None
    pages = (
        []
        if unavailable
        else [
            ExtensionPageResponse(id=page.id, title=page.title, route=page.route, view=page.view)
            for page in sorted(manifest.pages, key=lambda item: item.id)
        ]
    )
    navigation = (
        []
        if unavailable
        else [
            ExtensionPrimaryNavigationResponse(
                id=item.id,
                label=item.label,
                page=item.page,
                icon=item.icon,
                order=item.order,
                when=item.when,
            )
            for item in sorted(manifest.primary_navigation, key=lambda item: (item.order, item.id))
        ]
    )
    return ExtensionResponse(
        id=manifest.id,
        display_name=manifest.display_name,
        distribution=manifest.distribution,
        version=manifest.version,
        extension_api=manifest.extension_api,
        status="unavailable" if unavailable else "enabled",
        permissions=sorted(permission.value for permission in manifest.permissions),
        pages=pages,
        primary_navigation=navigation,
        browser=ExtensionBrowserBundleResponse(
            declared=manifest.entrypoints.browser is not None,
            has_styles=manifest.entrypoints.browser_css is not None,
            digest=bundle.digest if bundle is not None else None,
            script_url=bundle.url(ASSET_SCRIPT) if bundle is not None else None,
            style_url=(
                bundle.url(ASSET_STYLES)
                if bundle is not None and ASSET_STYLES in bundle.assets
                else None
            ),
        ),
    )


async def _require_admin(
    request: Request,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> None:
    """Require an administrator in multi-user mode."""
    user_id = require_user(request, auth_provider)
    if permission_store is None:
        return
    if user_id is None:
        raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
    is_admin = await asyncio.to_thread(permission_store.is_admin, user_id)
    if not is_admin:
        raise OmnigentError(
            "Admin privileges required to inspect extension diagnostics",
            code=ErrorCode.FORBIDDEN,
        )


def _diagnostic_message(error: str) -> str:
    """Bound and flatten plugin errors before returning them through the API."""
    printable = "".join(character if character.isprintable() else " " for character in error)
    return " ".join(printable.split())[:512]


def create_extensions_router(
    state: ExtensionPluginState,
    *,
    bundles: Mapping[str, ResolvedBundle] | None = None,
    asset_errors: Mapping[str, str] | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the installed-extension catalog router, mounted under ``/v1``."""
    router = APIRouter()
    resolved_bundles = bundles or {}
    catalog = tuple(
        _serialize_manifest(manifest, resolved_bundles.get(manifest.id))
        for manifest in sorted(state.manifests, key=lambda item: item.id)
    )
    by_id = {item.id: item for item in catalog}
    diagnostics = tuple(
        ExtensionLoadErrorResponse(entry_point=entry_point, error=_diagnostic_message(error))
        for entry_point, error in sorted(state.load_errors.items())
    )
    asset_diagnostics = tuple(
        ExtensionAssetErrorResponse(extension_id=extension_id, error=_diagnostic_message(error))
        for extension_id, error in sorted((asset_errors or {}).items())
    )

    @router.get("/extensions", response_model=ExtensionListResponse)
    async def list_extensions(request: Request) -> ExtensionListResponse:
        """List extensions installed and enabled when the server started.

        V1 treats operator installation as enablement. Installing, removing, or
        upgrading a package requires a server restart to refresh this snapshot.
        """
        require_user(request, auth_provider)
        return ExtensionListResponse(data=list(catalog))

    # Keep this static path above ``/{extension_id}`` so it cannot be consumed
    # as an extension identifier.
    @router.get("/extensions/diagnostics", response_model=ExtensionDiagnosticsResponse)
    async def extension_diagnostics(request: Request) -> ExtensionDiagnosticsResponse:
        """Show accepted extensions and concise discovery failures to administrators."""
        await _require_admin(request, auth_provider, permission_store)
        return ExtensionDiagnosticsResponse(
            extensions=list(catalog),
            load_errors=list(diagnostics),
            asset_errors=list(asset_diagnostics),
        )

    @router.get("/extensions/{extension_id}", response_model=ExtensionResponse)
    async def get_extension(request: Request, extension_id: str) -> ExtensionResponse:
        """Return one installed extension's public contribution metadata."""
        require_user(request, auth_provider)
        extension = by_id.get(extension_id)
        if extension is None:
            raise OmnigentError("Extension not found", code=ErrorCode.NOT_FOUND)
        return extension

    return router
