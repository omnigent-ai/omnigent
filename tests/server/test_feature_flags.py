"""Tests for deployment-wide release feature resolution."""

from __future__ import annotations

import dataclasses
import logging

import pytest

from omnigent.server.feature_flags import (
    FEATURE_DEFINITIONS,
    FEATURES_ENV_VAR,
    Feature,
    FeatureFlags,
    harness_install_enabled,
    resolve_feature_flags,
)


@pytest.mark.parametrize("environ", [{}, {FEATURES_ENV_VAR: ""}, {FEATURES_ENV_VAR: " , , "}])
def test_unset_or_empty_uses_feature_defaults(environ: dict[str, str]) -> None:
    flags = resolve_feature_flags(environ)

    assert flags.enabled_features == frozenset(
        {
            Feature.AUTOMATIONS_V2,
            Feature.CLAUDE_OSC_STATUS,
            Feature.CODEX_PLAN_AT_LAUNCH,
            Feature.PEER_SESSION_MESSAGING,
            Feature.PROJECT_REPOSITORY_CREATION,
            Feature.SESSION_ORCHESTRATION,
            Feature.SIBLING_SESSIONS,
            Feature.SIDEBAR_GROUPING_UI,
        }
    )
    assert flags.frontend_dict() == {
        "sandbox_profiles": False,
        "usage_page": False,
        "harness_install": False,
        "canvas": False,
        "webfonts": False,
        "arca_shutdown_warnings": False,
        "import_review": False,
        "sidebar_grouping_ui": True,
        "workspace_content_search": False,
        "grid_sessions_display_ui": False,
        "automations_v2": True,
        "splittable_terminal_view_ui": False,
        "sidebar_windowed_rows": False,
        "session_overview": False,
        "project_repository_creation": True,
        "session_orchestration": True,
        "sibling_sessions": True,
        "codex_plan_at_launch": True,
    }


def test_resolves_comma_separated_enabled_set() -> None:
    flags = resolve_feature_flags({FEATURES_ENV_VAR: " usage_page, harness_install,usage_page "})

    assert flags.enabled(Feature.USAGE_PAGE)
    assert flags.enabled(Feature.HARNESS_INSTALL)
    assert flags.enabled_names() == (
        "automations_v2",
        "claude_osc_status",
        "codex_plan_at_launch",
        "harness_install",
        "peer_session_messaging",
        "project_repository_creation",
        "session_orchestration",
        "sibling_sessions",
        "sidebar_grouping_ui",
        "usage_page",
    )


@pytest.mark.parametrize(
    "override",
    [
        "-{feature}",
        "{feature},-{feature}",
        "-{feature},{feature}",
    ],
)
@pytest.mark.parametrize(
    "feature",
    [
        "automations_v2",
        "claude_osc_status",
        "peer_session_messaging",
        "project_repository_creation",
        "session_orchestration",
        "sibling_sessions",
        "sidebar_grouping_ui",
    ],
)
def test_explicit_disable_wins_and_preserves_other_features(override: str, feature: str) -> None:
    features = override.format(feature=feature)
    flags = resolve_feature_flags({FEATURES_ENV_VAR: f"usage_page,{features}"})

    assert set(flags.enabled_names()) == {
        "automations_v2",
        "claude_osc_status",
        "codex_plan_at_launch",
        "peer_session_messaging",
        "project_repository_creation",
        "session_orchestration",
        "sibling_sessions",
        "sidebar_grouping_ui",
        "usage_page",
    } - {feature}


@pytest.mark.parametrize("feature", ["usage_page", "canvas"])
def test_default_off_features_can_also_be_explicitly_disabled(feature: str) -> None:
    flags = resolve_feature_flags({FEATURES_ENV_VAR: f"{feature},-{feature}"})

    assert flags.enabled_names() == (
        "automations_v2",
        "claude_osc_status",
        "codex_plan_at_launch",
        "peer_session_messaging",
        "project_repository_creation",
        "session_orchestration",
        "sibling_sessions",
        "sidebar_grouping_ui",
    )


@pytest.mark.parametrize("override", ["custom_agents_settings_ui", "-custom_agents_settings_ui"])
def test_retired_feature_is_ignored_with_warning(
    override: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        flags = resolve_feature_flags({FEATURES_ENV_VAR: f"usage_page,{override}"})

    assert flags == resolve_feature_flags({FEATURES_ENV_VAR: "usage_page"})
    assert f"{override} has been retired" in caplog.text


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
    assert flags.enabled_names() == (
        "automations_v2",
        "canvas",
        "claude_osc_status",
        "codex_plan_at_launch",
        "peer_session_messaging",
        "project_repository_creation",
        "session_orchestration",
        "sibling_sessions",
        "sidebar_grouping_ui",
    )


def test_webfonts_is_a_frontend_visible_feature() -> None:
    flags = resolve_feature_flags({FEATURES_ENV_VAR: "webfonts"})

    assert flags.enabled(Feature.WEBFONTS)
    assert flags.frontend_dict()["webfonts"] is True
    assert flags.enabled_names() == ("webfonts",)


def test_arca_shutdown_warnings_are_frontend_visible() -> None:
    flags = resolve_feature_flags({FEATURES_ENV_VAR: "arca_shutdown_warnings"})

    assert flags.enabled(Feature.ARCA_SHUTDOWN_WARNINGS)
    assert flags.frontend_dict()["arca_shutdown_warnings"] is True


def test_sibling_sessions_explicit_enable_preserves_default_features() -> None:
    flags = resolve_feature_flags({FEATURES_ENV_VAR: "sibling_sessions"})
    assert flags.enabled_features == resolve_feature_flags({}).enabled_features
    assert flags.frontend_dict()["sibling_sessions"] is True


@pytest.mark.parametrize("feature", [Feature.CLAUDE_OSC_STATUS, Feature.PEER_SESSION_MESSAGING])
def test_backend_features_are_not_frontend_visible(feature: Feature) -> None:
    flags = resolve_feature_flags({FEATURES_ENV_VAR: feature.value})

    assert flags.enabled(feature)
    assert feature.value in flags.enabled_names()
    assert feature.value not in flags.frontend_dict()


def test_session_orchestration_is_frontend_visible_so_trigger_templates_can_gate_on_it() -> None:
    assert resolve_feature_flags({}).frontend_dict()["session_orchestration"] is True
    disabled = resolve_feature_flags({FEATURES_ENV_VAR: "-session_orchestration"})
    assert disabled.frontend_dict()["session_orchestration"] is False


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


def test_harness_install_callable_failure_fails_closed(caplog: pytest.LogCaptureFixture) -> None:
    """A broken deployment gate cannot expose setup routes or break requests."""

    def broken_gate() -> bool:
        raise RuntimeError("gate unavailable")

    with caplog.at_level(logging.WARNING):
        assert harness_install_enabled(broken_gate) is False

    assert "harness_install gate failed" in caplog.text


def test_release_flags_have_lifecycle_metadata_and_expected_defaults() -> None:
    assert {definition.feature for definition in FEATURE_DEFINITIONS} == set(Feature)
    defaults = resolve_feature_flags({})
    for definition in FEATURE_DEFINITIONS:
        assert definition.owner
        assert definition.review_by_release
        assert defaults.enabled(definition.feature) == definition.default_enabled
    assert FeatureFlags().enabled_features == frozenset()


@pytest.mark.parametrize("master", [False, True, lambda: False, lambda: True])
@pytest.mark.parametrize(
    "claude,codex", [(False, False), (True, False), (False, True), (True, True)]
)
def test_mcp_active_harnesses(master, claude, codex):
    from omnigent.server.feature_flags import FeatureFlags, active_harnesses

    features = FeatureFlags(
        frozenset(
            feature
            for feature, enabled in [
                (Feature.MCP_DISABLED_SERVERS_CLAUDE, claude),
                (Feature.MCP_DISABLED_SERVERS_CODEX, codex),
            ]
            if enabled
        )
    )
    expected = [name for name, enabled in [("claude", claude), ("codex", codex)] if enabled]
    assert active_harnesses(master, features) == (
        expected if (master() if callable(master) else master) else []
    )


def test_mcp_gate_fails_closed():
    from omnigent.server.feature_flags import FeatureFlags, active_harnesses

    def broken():
        raise RuntimeError("SAFE unavailable")

    assert active_harnesses(broken, FeatureFlags(frozenset(Feature))) == []


def test_mcp_harness_feature_mapping_is_complete():
    from omnigent.mcp_disabled_servers import HARNESS_EXECUTORS
    from omnigent.server.feature_flags import MCP_DISABLED_SERVERS_HARNESS_FEATURES

    assert MCP_DISABLED_SERVERS_HARNESS_FEATURES.keys() == HARNESS_EXECUTORS.keys()
