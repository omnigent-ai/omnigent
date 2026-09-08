"""Tests for deployment-wide release feature resolution."""

from __future__ import annotations

import dataclasses

import pytest

from omnigent.server.feature_flags import (
    CANVAS_ENV_VAR,
    FEATURE_DEFINITIONS,
    FEATURES_ENV_VAR,
    Feature,
    FeatureFlags,
    resolve_feature_flags,
)


def test_features_default_off() -> None:
    flags = resolve_feature_flags({})

    assert flags.enabled_features == frozenset()
    assert flags.frontend_dict() == {
        "usage_page": False,
        "harness_install": False,
        "canvas": False,
    }


def test_resolves_comma_separated_enabled_set() -> None:
    flags = resolve_feature_flags({FEATURES_ENV_VAR: " usage_page, harness_install,usage_page "})

    assert flags.enabled(Feature.USAGE_PAGE)
    assert flags.enabled(Feature.HARNESS_INSTALL)
    assert flags.enabled_names() == ("harness_install", "usage_page")


def test_empty_entries_are_ignored() -> None:
    assert resolve_feature_flags({FEATURES_ENV_VAR: " , , "}) == FeatureFlags()


def test_removed_harness_install_variable_fails_with_migration_hint() -> None:
    with pytest.raises(ValueError, match="OMNIGENT_FEATURES=harness_install"):
        resolve_feature_flags({"OMNIGENT_HARNESS_INSTALL_ENABLED": "1"})


def test_removed_harness_install_variable_allows_explicit_off() -> None:
    flags = resolve_feature_flags({"OMNIGENT_HARNESS_INSTALL_ENABLED": "0"})

    assert not flags.enabled(Feature.HARNESS_INSTALL)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " Yes "])
def test_canvas_switch_enables_the_canvas_feature(value: str) -> None:
    flags = resolve_feature_flags({CANVAS_ENV_VAR: value})

    assert flags.enabled(Feature.CANVAS)
    assert flags.frontend_dict()["canvas"] is True
    assert flags.enabled_names() == ("canvas",)


@pytest.mark.parametrize("value", ["", "0", "false", "off", "enabled"])
def test_canvas_switch_is_off_for_other_values(value: str) -> None:
    assert not resolve_feature_flags({CANVAS_ENV_VAR: value}).enabled(Feature.CANVAS)


def test_canvas_is_also_a_regular_feature_name() -> None:
    flags = resolve_feature_flags({FEATURES_ENV_VAR: "canvas", CANVAS_ENV_VAR: "true"})

    assert flags.enabled_names() == ("canvas",)


def test_unknown_feature_fails_with_known_names() -> None:
    with pytest.raises(ValueError) as exc_info:
        resolve_feature_flags({FEATURES_ENV_VAR: "usage-pgae"})

    message = str(exc_info.value)
    assert "usage-pgae" in message
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


def test_release_flags_have_lifecycle_metadata_and_default_off() -> None:
    assert {definition.feature for definition in FEATURE_DEFINITIONS} == set(Feature)
    for definition in FEATURE_DEFINITIONS:
        assert definition.owner
        assert definition.review_by_release
        assert not FeatureFlags().enabled(definition.feature)
