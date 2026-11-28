"""Tests for deployment-wide release feature resolution."""

from __future__ import annotations

import dataclasses
import logging

import pytest

from omnigent.server.feature_flags import (
    FEATURE_DEFINITIONS,
    FEATURES_ENV_VAR,
    Feature,
    FeatureDefinition,
    FeatureFlags,
    FeatureIntent,
    harness_install_enabled,
    resolve_effective_features,
    resolve_feature_flags,
)


@pytest.mark.parametrize("environ", [{}, {FEATURES_ENV_VAR: ""}, {FEATURES_ENV_VAR: " , , "}])
def test_unset_or_empty_uses_feature_defaults(environ: dict[str, str]) -> None:
    flags = resolve_feature_flags(environ)

    assert flags.enabled_features == frozenset(
        {
            Feature.AUTOMATIONS,
            Feature.AUTOMATIONS_V2,
            Feature.CLAUDE_OSC_STATUS,
            Feature.CODEX_PLAN_AT_LAUNCH,
            Feature.GRID_SESSIONS_DISPLAY_UI,
            Feature.PEER_SESSION_MESSAGING,
            Feature.PROJECT_KANBAN,
            Feature.PROJECT_REPOSITORY_CREATION,
            Feature.SCRIPTED_SESSIONS,
            Feature.SESSION_ORCHESTRATION,
            Feature.SIBLING_SESSIONS,
            Feature.SIDEBAR_GROUPING_UI,
            Feature.USER_INBOX,
            Feature.WORKSPACE_CONTENT_SEARCH,
        }
    )
    assert flags.frontend_dict() == {
        "sandbox_profiles": False,
        "usage_page": False,
        "harness_install": False,
        "canvas": False,
        "webfonts": False,
        "import_review": False,
        "sidebar_grouping_ui": True,
        "workspace_content_search": True,
        "grid_sessions_display_ui": True,
        "automations_v2": True,
        "splittable_terminal_view_ui": False,
        "sidebar_windowed_rows": False,
        "chat_composer_codemirror": False,
        "workflows": False,
        "session_overview": False,
        "project_kanban": True,
        "project_repository_creation": True,
        "quick_pick_palette": False,
        "workspace_sidebar_v2": False,
        "gadgets": False,
        "session_orchestration": True,
        "sibling_sessions": True,
        "codex_plan_at_launch": True,
        "workspace_direct_access": False,
        "workspace_api_server_access": False,
        "floating_composer": False,
        "sidebar_saved_views": False,
        "floating_panes": False,
        "shared_session_updates_socket": False,
        "pull_requests_v2": False,
    }


def test_resolves_comma_separated_enabled_set() -> None:
    flags = resolve_feature_flags({FEATURES_ENV_VAR: " usage_page, harness_install,usage_page "})

    assert flags.enabled(Feature.USAGE_PAGE)
    assert flags.enabled(Feature.HARNESS_INSTALL)
    assert flags.enabled_names() == (
        "automations",
        "automations_v2",
        "claude_osc_status",
        "codex_plan_at_launch",
        "grid_sessions_display_ui",
        "harness_install",
        "peer_session_messaging",
        "project_kanban",
        "project_repository_creation",
        "scripted_sessions",
        "session_orchestration",
        "sibling_sessions",
        "sidebar_grouping_ui",
        "usage_page",
        "user_inbox",
        "workspace_content_search",
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
        "automations",
        "automations_v2",
        "claude_osc_status",
        "grid_sessions_display_ui",
        "peer_session_messaging",
        "project_kanban",
        "project_repository_creation",
        "scripted_sessions",
        "session_orchestration",
        "sibling_sessions",
        "sidebar_grouping_ui",
        "user_inbox",
        "workspace_content_search",
    ],
)
def test_explicit_disable_wins_and_preserves_other_features(override: str, feature: str) -> None:
    features = override.format(feature=feature)
    flags = resolve_feature_flags({FEATURES_ENV_VAR: f"usage_page,{features}"})

    assert set(flags.enabled_names()) == {
        "automations",
        "automations_v2",
        "claude_osc_status",
        "codex_plan_at_launch",
        "grid_sessions_display_ui",
        "peer_session_messaging",
        "project_kanban",
        "project_repository_creation",
        "scripted_sessions",
        "session_orchestration",
        "sibling_sessions",
        "sidebar_grouping_ui",
        "usage_page",
        "user_inbox",
        "workspace_content_search",
    } - {feature}


@pytest.mark.parametrize("feature", ["usage_page", "canvas"])
def test_default_off_features_can_also_be_explicitly_disabled(feature: str) -> None:
    flags = resolve_feature_flags({FEATURES_ENV_VAR: f"{feature},-{feature}"})

    assert flags.enabled_names() == (
        "automations",
        "automations_v2",
        "claude_osc_status",
        "codex_plan_at_launch",
        "grid_sessions_display_ui",
        "peer_session_messaging",
        "project_kanban",
        "project_repository_creation",
        "scripted_sessions",
        "session_orchestration",
        "sibling_sessions",
        "sidebar_grouping_ui",
        "user_inbox",
        "workspace_content_search",
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
        "automations",
        "automations_v2",
        "canvas",
        "claude_osc_status",
        "codex_plan_at_launch",
        "grid_sessions_display_ui",
        "peer_session_messaging",
        "project_kanban",
        "project_repository_creation",
        "scripted_sessions",
        "session_orchestration",
        "sibling_sessions",
        "sidebar_grouping_ui",
        "user_inbox",
        "workspace_content_search",
    )


def test_webfonts_is_a_frontend_visible_feature() -> None:
    flags = resolve_feature_flags({FEATURES_ENV_VAR: "webfonts"})

    assert flags.enabled(Feature.WEBFONTS)
    assert flags.frontend_dict()["webfonts"] is True
    defaults = resolve_feature_flags({}).enabled_features
    assert flags.enabled_features == defaults | {Feature.WEBFONTS}


def test_floating_composer_is_a_frontend_visible_feature() -> None:
    flags = resolve_feature_flags({FEATURES_ENV_VAR: "floating_composer"})
    assert flags.enabled(Feature.FLOATING_COMPOSER)
    assert flags.frontend_dict()["floating_composer"] is True


def test_chat_composer_codemirror_is_an_opt_in_frontend_feature() -> None:
    assert not resolve_feature_flags({}).enabled(Feature.CHAT_COMPOSER_CODEMIRROR)

    flags = resolve_feature_flags({FEATURES_ENV_VAR: "chat_composer_codemirror"})

    assert flags.enabled(Feature.CHAT_COMPOSER_CODEMIRROR)
    assert flags.frontend_dict()["chat_composer_codemirror"] is True


def test_workspace_sidebar_v2_is_an_opt_in_frontend_feature() -> None:
    assert not resolve_feature_flags({}).enabled(Feature.WORKSPACE_SIDEBAR_V2)
    assert resolve_feature_flags({}).frontend_dict()["workspace_sidebar_v2"] is False

    flags = resolve_feature_flags({FEATURES_ENV_VAR: "workspace_sidebar_v2"})

    assert flags.enabled(Feature.WORKSPACE_SIDEBAR_V2)
    assert flags.frontend_dict()["workspace_sidebar_v2"] is True


def test_pull_requests_v2_is_opt_in_and_does_not_follow_workspace_sidebar_v2() -> None:
    # The new sidebar rolls out on its own; the new pull request page waits for its own flag.
    assert resolve_feature_flags({}).frontend_dict()["pull_requests_v2"] is False
    sidebar_only = resolve_feature_flags({FEATURES_ENV_VAR: "workspace_sidebar_v2"})
    assert sidebar_only.frontend_dict()["pull_requests_v2"] is False

    flags = resolve_feature_flags({FEATURES_ENV_VAR: "workspace_sidebar_v2,pull_requests_v2"})

    assert flags.enabled(Feature.PULL_REQUESTS_V2)
    assert flags.frontend_dict()["pull_requests_v2"] is True


def test_shared_session_updates_socket_is_an_opt_in_frontend_feature() -> None:
    assert not resolve_feature_flags({}).enabled(Feature.SHARED_SESSION_UPDATES_SOCKET)

    flags = resolve_feature_flags({FEATURES_ENV_VAR: "shared_session_updates_socket"})

    assert flags.enabled(Feature.SHARED_SESSION_UPDATES_SOCKET)
    assert flags.frontend_dict()["shared_session_updates_socket"] is True


def test_sidebar_saved_views_is_an_opt_in_frontend_feature() -> None:
    assert not resolve_feature_flags({}).enabled(Feature.SIDEBAR_SAVED_VIEWS)

    flags = resolve_feature_flags({FEATURES_ENV_VAR: "sidebar_saved_views"})

    assert flags.enabled(Feature.SIDEBAR_SAVED_VIEWS)
    assert flags.frontend_dict()["sidebar_saved_views"] is True


@pytest.mark.parametrize(
    ("override", "enabled", "intent"),
    [
        ("", False, FeatureIntent.AUTOMATIC),
        ("workspace_sidebar_v2", True, FeatureIntent.AUTOMATIC),
        ("workspace_sidebar_v2,-floating_panes", False, FeatureIntent.OFF),
        ("-floating_panes,workspace_sidebar_v2", False, FeatureIntent.OFF),
        ("floating_panes", True, FeatureIntent.ON),
        ("floating_panes,-floating_panes", False, FeatureIntent.OFF),
    ],
)
def test_floating_panes_follows_workspace_sidebar_v2_unless_named(
    override: str, enabled: bool, intent: FeatureIntent
) -> None:
    flags = resolve_feature_flags({FEATURES_ENV_VAR: override})

    assert flags.enabled(Feature.FLOATING_PANES) is enabled
    assert flags.frontend_dict()["floating_panes"] is enabled
    assert flags.intent(Feature.FLOATING_PANES) is intent


def test_a_following_feature_on_automatic_takes_its_policys_new_value() -> None:
    # The followed feature turning on by default carries Automatic along, but not an explicit off.
    ramped = tuple(
        dataclasses.replace(definition, default_enabled=True)
        if definition.feature is Feature.WORKSPACE_SIDEBAR_V2
        else definition
        for definition in FEATURE_DEFINITIONS
    )

    assert Feature.FLOATING_PANES in resolve_effective_features(ramped, {})
    assert Feature.FLOATING_PANES not in resolve_effective_features(
        ramped, {Feature.FLOATING_PANES: FeatureIntent.OFF}
    )
    assert Feature.FLOATING_PANES not in resolve_effective_features(FEATURE_DEFINITIONS, {})


def test_a_followed_feature_needs_a_definition_and_no_cycle() -> None:
    def definition(feature: Feature, follows: Feature) -> FeatureDefinition:
        return FeatureDefinition(
            feature=feature,
            description="",
            owner="web",
            review_by_release="0.0.0",
            follows=follows,
        )

    with pytest.raises(ValueError, match="has no definition"):
        resolve_effective_features((definition(Feature.FLOATING_PANES, Feature.CANVAS),), {})
    cycle = (
        definition(Feature.FLOATING_PANES, Feature.WORKSPACE_SIDEBAR_V2),
        definition(Feature.WORKSPACE_SIDEBAR_V2, Feature.FLOATING_PANES),
    )
    with pytest.raises(ValueError, match="follows itself"):
        resolve_effective_features(cycle, {})
    # An explicit intent doesn't read what the feature follows, so it settles either case.
    on = {Feature.FLOATING_PANES: FeatureIntent.ON}
    missing = (definition(Feature.FLOATING_PANES, Feature.CANVAS),)
    assert resolve_effective_features(missing, on) == {Feature.FLOATING_PANES}
    off = {Feature.WORKSPACE_SIDEBAR_V2: FeatureIntent.OFF}
    assert resolve_effective_features(cycle, off) == frozenset()


def test_sibling_sessions_explicit_enable_preserves_default_features() -> None:
    flags = resolve_feature_flags({FEATURES_ENV_VAR: "sibling_sessions"})
    assert flags.enabled_features == resolve_feature_flags({}).enabled_features
    assert flags.frontend_dict()["sibling_sessions"] is True


@pytest.mark.parametrize(
    "feature", [Feature.AUTOMATIONS, Feature.CLAUDE_OSC_STATUS, Feature.PEER_SESSION_MESSAGING]
)
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


def test_memory_is_opt_in_and_backend_only() -> None:
    assert not resolve_feature_flags({}).enabled(Feature.MEMORY)
    assert resolve_feature_flags({FEATURES_ENV_VAR: "memory"}).enabled(Feature.MEMORY)
    assert "memory" not in resolve_feature_flags({FEATURES_ENV_VAR: "memory"}).frontend_dict()
