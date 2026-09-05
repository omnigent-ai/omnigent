"""Public manifest types for installed Omnigent extensions.

The extension API is versioned independently from the Omnigent package. Entry
points should return these lightweight, declarative values without importing
extension runtime implementations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

EXTENSION_API_VERSION = 1


class ExtensionPermission(StrEnum):
    """Host capabilities an extension may request."""

    NAVIGATION = "navigation"
    PROJECTS_READ = "projects.read"
    PROJECTS_WRITE = "projects.write"
    SESSIONS_READ = "sessions.read"
    STORAGE_USER = "storage.user"


@dataclass(frozen=True)
class ExtensionEntrypoints:
    """Lazy runtime assets declared by an extension."""

    browser: str | None = None
    browser_css: str | None = None


@dataclass(frozen=True)
class PageContribution:
    """A page rendered below the extension's namespaced route."""

    id: str
    title: str
    route: str
    view: str


@dataclass(frozen=True)
class PrimaryNavigationContribution:
    """A link contributed to the application's primary sidebar navigation."""

    id: str
    label: str
    page: str
    icon: str | None = None
    order: int = 500
    when: str | None = None


@dataclass(frozen=True)
class CommandContribution:
    """Reserved command metadata; V1 does not execute contributed commands."""

    id: str
    title: str


@dataclass(frozen=True)
class ExtensionManifest:
    """One installed package's declarative extension contribution."""

    id: str
    display_name: str
    distribution: str
    version: str
    requires_omnigent: str
    extension_api: int
    entrypoints: ExtensionEntrypoints = field(default_factory=ExtensionEntrypoints)
    permissions: frozenset[ExtensionPermission] = frozenset()
    activation_events: tuple[str, ...] = ()
    pages: tuple[PageContribution, ...] = ()
    primary_navigation: tuple[PrimaryNavigationContribution, ...] = ()
    commands: tuple[CommandContribution, ...] = ()


@dataclass(frozen=True)
class ExtensionPluginState:
    """Validated manifests plus non-fatal discovery errors."""

    manifests: tuple[ExtensionManifest, ...]
    load_errors: dict[str, str] = field(default_factory=dict)
    asset_packages: dict[str, str] = field(default_factory=dict)

    def get(self, extension_id: str) -> ExtensionManifest | None:
        """Return one accepted manifest by ID."""
        return next((item for item in self.manifests if item.id == extension_id), None)

    def asset_package(self, extension_id: str) -> str | None:
        """Return the verified package holding an extension's browser assets."""
        return self.asset_packages.get(extension_id)
