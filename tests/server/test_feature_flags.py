"""Tests for deployment-wide release feature resolution."""

from __future__ import annotations

import dataclasses

import pytest

from omnigent.server.feature_flags import (
    FEATURE_DEFINITIONS,
    FEATURES_ENV_VAR,
    Feature,
    FeatureFlags,
    resolve_feature_flags,
)


@pytest.mark.parametrize("environ", [{}, {FEATURES_ENV_VAR: ""}, {FEATURES_ENV_VAR: " , , "}])
def test_unset_or_empty_uses_feature_defaults(environ: dict[str, str]) -> None:
    flags = resolve_feature_flags(environ)

    assert flags.enabled_features == frozenset({Feature.CLAUDE_OSC_STATUS})
    assert flags.frontend_dict() == {
        "usage_page": False,
        "harness_install": False,
        "canvas": False,
        "arca_shutdown_warnings": False,
        "import_review": False,
        "custom_agents_settings_ui": False,
        "sidebar_grouping_ui": False,
        "workspace_content_search": False,
    }


def test_resolves_comma_separated_enabled_set() -> None:
    flags = resolve_feature_flags({FEATURES_ENV_VAR: " usage_page, harness_install,usage_page "})

    assert flags.enabled(Feature.USAGE_PAGE)
    assert flags.enabled(Feature.HARNESS_INSTALL)
    assert flags.enabled_names() == ("claude_osc_status", "harness_install", "usage_page")


@pytest.mark.parametrize(
    "features",
    [
        "-claude_osc_status",
        "claude_osc_status,-claude_osc_status",
        "-claude_osc_status,claude_osc_status",
    ],
)
def test_explicit_disable_wins_and_preserves_other_features(features: str) -> None:
    flags = resolve_feature_flags({FEATURES_ENV_VAR: f"usage_page,{features}"})

    assert flags.enabled_names() == ("usage_page",)


def test_default_off_features_can_also_be_explicitly_disabled() -> None:
    flags = resolve_feature_flags({FEATURES_ENV_VAR: "usage_page,-usage_page"})

    assert flags.enabled_names() == ("claude_osc_status",)


def test_removed_harness_install_variable_fails_with_migration_hint() -> None:
    with pytest.raises(ValueError, match="OMNIGENT_FEATURES=harness_install"):
        resolve_feature_flags({"OMNIGENT_HARNESS_INSTALL_ENABLED": "1"})


def test_removed_harness_install_variable_allows_explicit_off() -> None:
    flags = resolve_feature_flags({"OMNIGENT_HARNESS_INSTALL_ENABLED": "0"})

    assert not flags.enabled(Feature.HARNESS_INSTALL)


def test_canvas_is_a_frontend_visible_feature() -> None:
    flags = resolve_feature_flags({FEATURES_ENV_VAR: "canvas"})

    assert flags.enabled(Feature.CANVAS)
    assert flags.frontend_dict()["canvas"] is True
    assert flags.enabled_names() == ("canvas", "claude_osc_status")


def test_arca_shutdown_warnings_are_frontend_visible() -> None:
    flags = resolve_feature_flags({FEATURES_ENV_VAR: "arca_shutdown_warnings"})

    assert flags.enabled(Feature.ARCA_SHUTDOWN_WARNINGS)
    assert flags.frontend_dict()["arca_shutdown_warnings"] is True


def test_claude_osc_status_is_runner_only() -> None:
    flags = resolve_feature_flags({FEATURES_ENV_VAR: "claude_osc_status"})

    assert flags.enabled(Feature.CLAUDE_OSC_STATUS)
    assert "claude_osc_status" in flags.enabled_names()
    assert "claude_osc_status" not in flags.frontend_dict()


@pytest.mark.parametrize("name", ["usage-pgae", "-usage-pgae", "-", "--claude_osc_status"])
def test_unknown_feature_fails_with_known_names(name: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        resolve_feature_flags({FEATURES_ENV_VAR: name})

    message = str(exc_info.value)
    assert name in message
    assert "usage_page" in message
    assert "harness_install" in message


def test_snapshot_is_immutable_and_does_not_follow_environment_mutation() -> None:
    environ = {FEATURES_ENV_VAR: "usage_page"}
    flags = resolve_feature_flags(environ)
    environ[FEATURES_ENV_VAR] = "harness_install"

    assert flags.enabled(Feature.USAGE_PAGE)
    assert not flags.enabled(Feature.HARNESS_INSTALL)
    with pytest.raises(dataclasses.FrozenInstanceError):
        flags.enabled_features = frozenset()  # type: ignore[misc]


def test_release_flags_have_lifecycle_metadata_and_expected_defaults() -> None:
    assert {definition.feature for definition in FEATURE_DEFINITIONS} == set(Feature)
    defaults = resolve_feature_flags({})
    for definition in FEATURE_DEFINITIONS:
        assert definition.owner
        assert definition.review_by_release
        assert defaults.enabled(definition.feature) == definition.default_enabled
    assert FeatureFlags().enabled_features == frozenset()
