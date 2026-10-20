"""Deployment-wide release feature management.

Release feature defaults can be overridden by comma-separated names in
``OMNIGENT_FEATURES``; prefix a name with ``-`` to disable it. Values are
resolved once when an application, route factory or runner resource registry
is built, giving each one an immutable snapshot.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

FEATURES_ENV_VAR = "OMNIGENT_FEATURES"
_REMOVED_HARNESS_INSTALL_ENV_VAR = "OMNIGENT_HARNESS_INSTALL_ENABLED"


class Feature(StrEnum):
    """Canonical release-feature keys accepted by ``OMNIGENT_FEATURES``."""

    USAGE_PAGE = "usage_page"
    HARNESS_INSTALL = "harness_install"
    CANVAS = "canvas"
    ARCA_SHUTDOWN_WARNINGS = "arca_shutdown_warnings"
    IMPORT_REVIEW = "import_review"
    CUSTOM_AGENTS_SETTINGS_UI = "custom_agents_settings_ui"
    SIDEBAR_GROUPING_UI = "sidebar_grouping_ui"
    CLAUDE_OSC_STATUS = "claude_osc_status"


@dataclass(frozen=True)
class FeatureDefinition:
    """Lifecycle metadata for one temporary release feature."""

    feature: Feature
    description: str
    owner: str
    review_by_release: str
    frontend_visible: bool = True
    default_enabled: bool = False


FEATURE_DEFINITIONS: tuple[FeatureDefinition, ...] = (
    FeatureDefinition(
        feature=Feature.USAGE_PAGE,
        description="Web Usage page with cost timeline and breakdowns",
        owner="web",
        review_by_release="0.11.0",
    ),
    FeatureDefinition(
        feature=Feature.HARNESS_INSTALL,
        description="Install and configure missing harnesses from the web UI",
        owner="onboarding",
        review_by_release="0.11.0",
    ),
    FeatureDefinition(
        feature=Feature.CANVAS,
        description="Web Canvas page: sessions as draggable cards grouped by project",
        owner="web",
        review_by_release="0.15.0",
    ),
    FeatureDefinition(
        feature=Feature.ARCA_SHUTDOWN_WARNINGS,
        description="Weekday Arca shutdown warning in the web UI",
        owner="web",
        review_by_release="0.20.0",
        frontend_visible=True,
    ),
    FeatureDefinition(
        feature=Feature.IMPORT_REVIEW,
        description="Auto-opening import review modal for a newly connected host's context",
        owner="onboarding",
        review_by_release="0.18.0",
    ),
    FeatureDefinition(
        feature=Feature.CUSTOM_AGENTS_SETTINGS_UI,
        description="Manage reusable custom agents from Settings",
        owner="web",
        review_by_release="0.18.0",
    ),
    FeatureDefinition(
        feature=Feature.SIDEBAR_GROUPING_UI,
        description="Sidebar grouping, ordering and row metadata options, Shared section",
        owner="web",
        review_by_release="0.19.0",
    ),
    FeatureDefinition(
        feature=Feature.CLAUDE_OSC_STATUS,
        description="Claude-native working status from OSC 7501 terminal reports",
        owner="runner",
        review_by_release="0.20.0",
        frontend_visible=False,
        default_enabled=True,
    ),
)


@dataclass(frozen=True)
class FeatureFlags:
    """Immutable feature values for one server or runner process."""

    enabled_features: frozenset[Feature] = frozenset()

    def enabled(self, feature: Feature) -> bool:
        """Return whether *feature* is enabled in this snapshot."""
        return feature in self.enabled_features

    def frontend_dict(self) -> dict[str, bool]:
        """Return every frontend-visible feature and its resolved value."""
        return {
            definition.feature.value: self.enabled(definition.feature)
            for definition in FEATURE_DEFINITIONS
            if definition.frontend_visible
        }

    def enabled_names(self) -> tuple[str, ...]:
        """Return enabled canonical names in stable order."""
        return tuple(sorted(feature.value for feature in self.enabled_features))


def resolve_feature_flags(environ: Mapping[str, str] | None = None) -> FeatureFlags:
    """Resolve ``OMNIGENT_FEATURES`` into an immutable feature snapshot.

    Comma-separated names enable features; ``-name`` disables them, for example
    ``usage_page,-claude_osc_status``. Unset or empty uses feature defaults.
    Explicit disables win regardless of order. Unknown names fail startup.

    :param environ: Environment mapping; defaults to :data:`os.environ`.
    :returns: Resolved immutable feature values.
    :raises ValueError: If an override contains an unknown feature name.
    """
    source = os.environ if environ is None else environ
    legacy_harness_install = source.get(_REMOVED_HARNESS_INSTALL_ENV_VAR, "").strip().lower()
    if legacy_harness_install not in {"", "0", "false", "no", "off"}:
        raise ValueError(
            f"{_REMOVED_HARNESS_INSTALL_ENV_VAR} is no longer supported; "
            f"enable harness installation with {FEATURES_ENV_VAR}=harness_install"
        )

    raw = source.get(FEATURES_ENV_VAR, "")
    names = [name.strip() for name in raw.split(",") if name.strip()]

    enabled = {
        definition.feature for definition in FEATURE_DEFINITIONS if definition.default_enabled
    }
    disabled: set[Feature] = set()
    unknown: list[str] = []
    for name in names:
        try:
            if name.startswith("-"):
                disabled.add(Feature(name[1:]))
            else:
                enabled.add(Feature(name))
        except ValueError:
            unknown.append(name)

    if unknown:
        known = ", ".join(feature.value for feature in Feature)
        invalid = ", ".join(sorted(set(unknown)))
        raise ValueError(
            f"unknown feature(s) in {FEATURES_ENV_VAR}: {invalid}; known features: {known}"
        )

    return FeatureFlags(frozenset(enabled - disabled))
