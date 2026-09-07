"""Deployment-wide release feature management.

Release features are enabled as a comma-separated set in
``OMNIGENT_FEATURES``. The set is resolved once when an application or route
factory is built, so every request handled by that process sees one immutable
snapshot.
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


@dataclass(frozen=True)
class FeatureDefinition:
    """Lifecycle metadata for one temporary release feature."""

    feature: Feature
    description: str
    owner: str
    review_by_release: str
    frontend_visible: bool = True


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
)


@dataclass(frozen=True)
class FeatureFlags:
    """Immutable feature values for one server process."""

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

    The variable is a comma-separated enabled set, for example
    ``usage_page,harness_install``. Unset or empty means every release feature
    is off. Unknown names fail startup instead of silently applying a typo.

    :param environ: Environment mapping; defaults to :data:`os.environ`.
    :returns: Resolved immutable feature values.
    :raises ValueError: If the enabled set contains an unknown feature name.
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

    enabled: set[Feature] = set()
    unknown: list[str] = []
    for name in names:
        try:
            enabled.add(Feature(name))
        except ValueError:
            unknown.append(name)

    if unknown:
        known = ", ".join(feature.value for feature in Feature)
        invalid = ", ".join(sorted(set(unknown)))
        raise ValueError(
            f"unknown feature(s) in {FEATURES_ENV_VAR}: {invalid}; known features: {known}"
        )

    return FeatureFlags(frozenset(enabled))
