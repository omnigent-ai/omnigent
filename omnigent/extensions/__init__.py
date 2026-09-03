"""Public API for installed Omnigent extensions."""

from omnigent.extensions.api import (
    EXTENSION_API_VERSION,
    CommandContribution,
    ExtensionEntrypoints,
    ExtensionManifest,
    ExtensionPermission,
    ExtensionPluginState,
    PageContribution,
    PrimaryNavigationContribution,
)
from omnigent.extensions.registry import (
    ENTRY_POINT_GROUP,
    SUPPORTED_EXTENSION_API_VERSIONS,
    ExtensionValidationError,
    extension_manifest,
    extension_manifests,
    plugin_state,
    validate_manifest,
)

__all__ = [
    "ENTRY_POINT_GROUP",
    "EXTENSION_API_VERSION",
    "SUPPORTED_EXTENSION_API_VERSIONS",
    "CommandContribution",
    "ExtensionEntrypoints",
    "ExtensionManifest",
    "ExtensionPermission",
    "ExtensionPluginState",
    "ExtensionValidationError",
    "PageContribution",
    "PrimaryNavigationContribution",
    "extension_manifest",
    "extension_manifests",
    "plugin_state",
    "validate_manifest",
]
