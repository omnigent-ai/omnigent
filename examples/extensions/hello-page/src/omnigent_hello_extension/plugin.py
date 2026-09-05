"""Lightweight manifest entry point for the hello-page extension."""

from omnigent.extensions import (
    EXTENSION_API_VERSION,
    ExtensionEntrypoints,
    ExtensionManifest,
    ExtensionPermission,
    PageContribution,
    PrimaryNavigationContribution,
)

_EXTENSION_ID = "omnigent.hello-page"
_PAGE_ID = f"{_EXTENSION_ID}.home"


def get_manifest() -> ExtensionManifest:
    """Return the reference extension's declarative contribution."""
    return ExtensionManifest(
        id=_EXTENSION_ID,
        display_name="Hello Extension",
        distribution="omnigent-hello-extension",
        version="0.1.0",
        requires_omnigent=">=0.11,<1",
        extension_api=EXTENSION_API_VERSION,
        entrypoints=ExtensionEntrypoints(
            browser="dist/extension.js",
            browser_css="dist/extension.css",
        ),
        permissions=frozenset({ExtensionPermission.NAVIGATION, ExtensionPermission.STORAGE_USER}),
        activation_events=(f"onPage:{_PAGE_ID}",),
        pages=(
            PageContribution(
                id=_PAGE_ID,
                title="Hello Extension",
                route="hello",
                view="hello",
            ),
        ),
        primary_navigation=(
            PrimaryNavigationContribution(
                id=f"{_EXTENSION_ID}.primary-nav",
                label="Hello Extension",
                page=_PAGE_ID,
                icon="puzzle",
            ),
        ),
    )
