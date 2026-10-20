"""Public API for installed Omnigent extensions."""

from omnigent.extensions.api import (
    EXTENSION_API_VERSION,
    AgentContribution,
    CommandContribution,
    ExtensionDependency,
    ExtensionEntrypoints,
    ExtensionManifest,
    ExtensionPermission,
    ExtensionPluginState,
    PageContribution,
    PolicyContribution,
    PrimaryNavigationContribution,
)
from omnigent.extensions.conformance import check_extension_package
from omnigent.extensions.registry import (
    ENTRY_POINT_GROUP,
    SUPPORTED_EXTENSION_API_VERSIONS,
    ExtensionValidationError,
    extension_manifest,
    extension_manifests,
    plugin_state,
    validate_manifest,
)
from omnigent.extensions.runtime import (
    ExtensionContext,
    ExtensionHooks,
    SessionStatusNotification,
)

__all__ = [
    "ENTRY_POINT_GROUP",
    "EXTENSION_API_VERSION",
    "SUPPORTED_EXTENSION_API_VERSIONS",
    "AgentContribution",
    "CommandContribution",
    "ExtensionContext",
    "ExtensionDependency",
    "ExtensionEntrypoints",
    "ExtensionHooks",
    "ExtensionManifest",
    "ExtensionPermission",
    "ExtensionPluginState",
    "ExtensionValidationError",
    "PageContribution",
    "PolicyContribution",
    "PrimaryNavigationContribution",
    "SessionStatusNotification",
    "check_extension_package",
    "extension_manifest",
    "extension_manifests",
    "plugin_state",
    "validate_manifest",
]
