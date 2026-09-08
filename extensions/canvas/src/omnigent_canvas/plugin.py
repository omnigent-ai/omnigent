"""Lightweight manifest entry point for Canvas."""

from omnigent.extensions import (
    EXTENSION_API_VERSION,
    ExtensionEntrypoints,
    ExtensionManifest,
    ExtensionPermission,
    PageContribution,
    PrimaryNavigationContribution,
)

_EXTENSION_ID = "omnigent.canvas"
_PAGE_ID = f"{_EXTENSION_ID}.home"


def get_manifest() -> ExtensionManifest:
    """Return the Canvas declarative contribution."""
    return ExtensionManifest(
        id=_EXTENSION_ID,
        display_name="Canvas",
        distribution="omnigent-canvas",
        version="0.1.0",
        requires_omnigent=">=0.13.0.dev0,<1",
        extension_api=EXTENSION_API_VERSION,
        entrypoints=ExtensionEntrypoints(
            browser="dist/extension.js",
            browser_css="dist/extension.css",
        ),
        permissions=frozenset(
            {
                ExtensionPermission.NAVIGATION,
                ExtensionPermission.PROJECTS_READ,
                ExtensionPermission.PROJECTS_WRITE,
                ExtensionPermission.SESSIONS_READ,
                ExtensionPermission.STORAGE_USER,
            }
        ),
        activation_events=(f"onPage:{_PAGE_ID}",),
        pages=(
            PageContribution(
                id=_PAGE_ID,
                title="Canvas",
                route="canvas",
                view="canvas",
            ),
        ),
        primary_navigation=(
            PrimaryNavigationContribution(
                id=f"{_EXTENSION_ID}.primary-nav",
                label="Canvas",
                page=_PAGE_ID,
                icon="panels-top-left",
                order=350,
            ),
        ),
    )
